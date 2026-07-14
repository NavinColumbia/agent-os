#!/usr/bin/env python3
"""tenantproviders.py — multi-provider BYO keys PER TENANT: work with whatever model accounts a user has.

(Distinct from providers.py, which is the single global engine config.) A user shouldn't be forced to own
a Claude key — they might have only Codex/OpenAI, or both and want to split. This is the per-tenant
registry: which providers they've connected (keys stored encrypted in the vault, never here), an ordered
preference, and resolve() — which the build flow calls to pick the engine + key. No keys -> platform default.

Engines we drive headless: Anthropic Claude (`claude -p`) and OpenAI/Codex (`codex exec`). factory.agent
routes a tenant to Codex-as-primary when that's their connected provider.

    tenantproviders.py add <tid> <provider> <key>    # connect a provider (key -> vault)
    tenantproviders.py remove <tid> <provider>
    tenantproviders.py list <tid>
    tenantproviders.py resolve <tid>
    tenantproviders.py selftest
Run with the agent-os venv python.
"""
import json
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit   # noqa: E402
import vault   # noqa: E402

from aoscfg import ENV, DB

CATALOG = [
    {"slug": "anthropic", "name": "Anthropic Claude", "engine": "claude", "env_key": "ANTHROPIC_API_KEY",
     "blurb": "Claude models via claude -p. Default engine.", "key_hint": "sk-ant-…"},
    {"slug": "openai", "name": "OpenAI / Codex", "engine": "codex", "env_key": "OPENAI_API_KEY",
     "blurb": "GPT/Codex via the Codex CLI. Use this if you don't have a Claude key.", "key_hint": "sk-…"},
]
_BY_SLUG = {p["slug"]: p for p in CATALOG}


def _ensure():
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS tenant_providers (
            tenant_id TEXT NOT NULL, provider TEXT NOT NULL, has_key BOOLEAN NOT NULL DEFAULT false,
            priority INT NOT NULL DEFAULT 5, added_at TIMESTAMPTZ DEFAULT now(),
            PRIMARY KEY (tenant_id, provider))""")
        # auth_mode: 'api_key' (BYO metered key) or 'subscription' (run on the host CLI's logged-in
        # Claude/ChatGPT account — no per-token billing). Added idempotently for existing tables.
        cur.execute("ALTER TABLE tenant_providers ADD COLUMN IF NOT EXISTS auth_mode TEXT DEFAULT 'api_key'")
        c.commit()


def _secret_name(provider):
    return f"byo_key_{provider}"


_HTTP_TIMEOUT = 8     # seconds — a cheap real call to the provider, kept short
_CLI_TIMEOUT = 20     # seconds — local auth-status probe of the host CLI


def _validate_api_key(provider, key):
    """Make a cheap REAL call to the provider to prove the key works BEFORE we ever mark connected.
    Anthropic -> GET https://api.anthropic.com/v1/models ; OpenAI -> GET https://api.openai.com/v1/models.
    Returns {"ok": True} only on a verified key. Returns {"error": ...} when the key is rejected
    (401/403) OR when we couldn't reach the provider to verify it — the two are worded differently so
    the user knows whether to fix the key or their connection. Stdlib urllib only; selftest stubs this."""
    name = _BY_SLUG[provider]["name"]
    if provider == "anthropic":
        url = "https://api.anthropic.com/v1/models?limit=1"
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    elif provider == "openai":
        url = "https://api.openai.com/v1/models"
        headers = {"Authorization": f"Bearer {key}"}
    else:
        return {"error": f"don't know how to validate a key for '{provider}'"}
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
            r.read(1)                       # 2xx -> the key authenticated
        return {"ok": True}
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return {"error": f"that API key was rejected by {name} — check it and retry"}
        if e.code == 429:
            return {"ok": True}             # authenticated, just rate-limited right now -> key is real
        return {"error": f"{name} couldn't verify that key right now (HTTP {e.code}) — try again in a moment"}
    except (urllib.error.URLError, TimeoutError, OSError):
        return {"error": f"couldn't reach {name} to verify the key — check your connection and retry"}


def _cli_logged_in(provider):
    """Verify the host CLI for this provider is ACTUALLY signed in BEFORE we mark a subscription
    connection — a 'subscription' connection just runs builds on this machine's logged-in CLI account,
    so if the CLI isn't logged in there is nothing to connect to. Probes the local auth status (no
    tokens spent): `claude auth status --json` / `codex login status`. Selftest stubs this."""
    engine = _BY_SLUG[provider]["engine"]
    name = _BY_SLUG[provider]["name"]
    hint = "claude login" if engine == "claude" else "codex login"
    cmd = ["claude", "auth", "status", "--json"] if engine == "claude" else ["codex", "login", "status"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=_CLI_TIMEOUT)
    except FileNotFoundError:
        return {"error": f"this machine's {name} CLI isn't installed — install it and run `{hint}` on the host, then retry"}
    except (subprocess.TimeoutExpired, OSError):
        return {"error": f"couldn't check this machine's {name} CLI sign-in — make sure `{hint}` works on the host, then retry"}
    signed_in = False
    if engine == "claude":
        try:
            signed_in = bool(json.loads(p.stdout or "{}").get("loggedIn"))
        except (ValueError, TypeError):
            signed_in = False
    else:
        signed_in = p.returncode == 0 and "logged in" in ((p.stdout or "") + (p.stderr or "")).lower()
    if signed_in:
        return {"ok": True}
    return {"error": f"this machine's {name} CLI isn't signed in — run `{hint}` on the host, then retry"}


def subscription_status(provider):
    """Is the host CLI for this provider genuinely signed in right now? (UI 'Check connection')."""
    if provider not in _BY_SLUG:
        return {"error": f"unknown provider '{provider}'"}
    return _cli_logged_in(provider)


def start_subscription_login(provider):
    """TRIGGER a REAL OAuth sign-in for the SELF-HOSTED case (console + browser on the same machine):
    if the host CLI isn't already signed in, launch the provider's actual login — `claude auth login
    --claudeai` / `codex login` — which opens the browser to the Claude/ChatGPT OAuth page and runs a
    local callback. We launch it detached (fire-and-forget) and hand the UI an instruction to finish in
    the browser, then 'Check connection' (which re-verifies via subscription_status). Honest about the
    constraint: providers' subscription OAuth is FIRST-PARTY-CLI-ONLY and signs in the whole HOST to one
    account — there is no per-tenant subscription token. So in a multi-tenant deploy this binds the host
    to a single Claude/ChatGPT account; it's a genuine login only for the self-hosted single-operator box.
    Returns {"already": True} if nothing to do, {"started": True, "instruction": ...} once launched, or
    {"error": ...} if the CLI is missing / couldn't launch."""
    if provider not in _BY_SLUG:
        return {"error": f"unknown provider '{provider}'"}
    name = _BY_SLUG[provider]["name"]
    engine = _BY_SLUG[provider]["engine"]
    pre = _cli_logged_in(provider)
    if pre.get("ok"):
        return {"already": True, "message": f"{name} is already signed in on this machine."}
    cmd = ["claude", "auth", "login", "--claudeai"] if engine == "claude" else ["codex", "login"]
    shown = " ".join(cmd)
    try:
        logf = open(Path(tempfile.gettempdir()) / f"provider-login-{provider}.log", "ab")
        # detached so the interactive browser-OAuth flow outlives this request; we don't block on it.
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, start_new_session=True)
    except FileNotFoundError:
        return {"error": f"this machine's {name} CLI isn't installed — install it and run `{shown}` on the host, then click Check connection"}
    except OSError:
        return {"error": f"couldn't start {name} sign-in on the host — run `{shown}` yourself, then click Check connection"}
    return {"started": True, "command": shown,
            "instruction": f"Opening {name} sign-in in a browser on THIS machine — complete it, then click Check connection. "
                           f"(If no window opens, run `{shown}` on the host.)"}


def connect(tid, provider, mode="api_key", key=None):
    """Connect a provider either by BYO API key (mode='api_key', metered) or by SUBSCRIPTION login
    (mode='subscription' — runs on the host CLI's logged-in Claude/ChatGPT account, no per-token billing).
    Subscription mode needs no key. VERIFIES for real before claiming connected — a key is validated with
    a cheap call to the provider, and a subscription is checked against the host CLI's actual sign-in —
    so 'connected' never lies (a fake connection would also pass the provider gate and break builds later).
    Returns connected status, or {"error": ...} if verification fails (key not stored, nothing marked)."""
    if provider not in _BY_SLUG:
        return {"error": f"unknown provider '{provider}'"}
    if mode not in ("api_key", "subscription"):
        return {"error": "mode must be 'api_key' or 'subscription'"}
    if mode == "api_key" and not key:
        return {"error": "an API key is required for api_key mode"}
    # Prove it works BEFORE storing anything or flipping has_key.
    check = _validate_api_key(provider, key) if mode == "api_key" else _cli_logged_in(provider)
    if not check.get("ok"):
        return {"error": check.get("error", "could not verify this provider")}
    _ensure()
    if mode == "api_key":
        vault.put_secret(_secret_name(provider), f"tenant:{tid}", "prod", ["builder", "factory"], key)
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT COALESCE(min(priority),5) FROM tenant_providers WHERE tenant_id=%s AND has_key", (tid,))
        top = cur.fetchone()[0]
        cur.execute("""INSERT INTO tenant_providers (tenant_id, provider, has_key, priority, auth_mode)
                       VALUES (%s,%s,true,%s,%s)
                       ON CONFLICT (tenant_id, provider) DO UPDATE SET has_key=true, auth_mode=EXCLUDED.auth_mode""",
                    (tid, provider, max(1, top), mode))
        c.commit()
    audit.append(actor="tenantproviders", action="ProviderConnected", resource=tid, decision="connected",
                 payload={"provider": provider, "mode": mode})
    return {"ok": True, "provider": provider, "engine": _BY_SLUG[provider]["engine"], "mode": mode}


def add_key(tid, provider, key):
    """Back-compat: connect via BYO API key."""
    return connect(tid, provider, "api_key", key)


def remove_key(tid, provider):
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("UPDATE tenant_providers SET has_key=false WHERE tenant_id=%s AND provider=%s", (tid, provider))
        c.commit()
    audit.append(actor="tenantproviders", action="ProviderDisconnected", resource=tid, decision="removed",
                 payload={"provider": provider})
    return {"ok": True, "provider": provider}


def set_priority(tid, ordered):
    """ordered = provider slugs best-first; lower number = higher preference (how to 'split' between them)."""
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        for i, p in enumerate(ordered):
            cur.execute("""INSERT INTO tenant_providers (tenant_id, provider, priority) VALUES (%s,%s,%s)
                           ON CONFLICT (tenant_id, provider) DO UPDATE SET priority=EXCLUDED.priority""",
                        (tid, p, i + 1))
        c.commit()
    return {"ok": True, "order": ordered}


def list_providers(tid):
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT provider, has_key, priority, auth_mode FROM tenant_providers WHERE tenant_id=%s", (tid,))
        state = {p: (hk, pr, am) for p, hk, pr, am in cur.fetchall()}
    out = []
    for p in CATALOG:
        hk, pr, am = state.get(p["slug"], (False, 5, None))
        out.append({**{k: p[k] for k in ("slug", "name", "engine", "blurb", "key_hint")},
                    "connected": bool(hk), "priority": pr, "auth_mode": am,
                    "modes": ["subscription", "api_key"]})   # both ways to connect
    out.sort(key=lambda x: (not x["connected"], x["priority"]))
    return out


def resolve(tid):
    """The engine + key/auth the build flow should use: highest-preference connected provider, else default.
    Subscription-mode providers carry no key (the CLI runs on its own logged-in account)."""
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT provider, auth_mode FROM tenant_providers WHERE tenant_id=%s AND has_key
                       ORDER BY priority, added_at LIMIT 1""", (tid,))
        row = cur.fetchone()
    if not row:
        return {"engine": "claude", "provider": None, "key": None, "auth_mode": None}
    provider, auth_mode = row[0], row[1] or "api_key"
    meta = _BY_SLUG.get(provider, _BY_SLUG["anthropic"])
    key = None
    if auth_mode == "api_key":                            # subscription -> no key, run on the CLI login
        try:
            v = vault.get_secret(_secret_name(provider), f"tenant:{tid}", "prod", "builder", tenant_id=tid)
            key = v if isinstance(v, str) else (v.get("value") if isinstance(v, dict) else None)
        except Exception:
            key = None
    return {"engine": meta["engine"], "provider": provider, "key": key, "auth_mode": auth_mode}


def resolve_provider(tid, provider):
    """Resolve one specific provider for fallback/secondary use, ignoring priority order.

    resolve() returns the tenant's primary provider. Factory failover needs a different question:
    "does this tenant also have OpenAI/Codex connected so Claude can fall over to THEIR Codex account?"
    This returns connected=False when absent or when an api_key row lost its vault secret; subscription
    rows are connected with key=None because the host CLI login is the credential.
    """
    if provider not in _BY_SLUG:
        return {"connected": False, "provider": provider, "engine": None, "key": None, "auth_mode": None}
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT auth_mode FROM tenant_providers
                       WHERE tenant_id=%s AND provider=%s AND has_key""", (tid, provider))
        row = cur.fetchone()
    meta = _BY_SLUG[provider]
    if not row:
        return {"connected": False, "provider": provider, "engine": meta["engine"], "key": None, "auth_mode": None}
    auth_mode = row[0] or "api_key"
    key = None
    if auth_mode == "api_key":
        try:
            v = vault.get_secret(_secret_name(provider), f"tenant:{tid}", "prod", "builder", tenant_id=tid)
            key = v if isinstance(v, str) else (v.get("value") if isinstance(v, dict) else None)
        except Exception:
            key = None
        if not key:
            return {"connected": False, "provider": provider, "engine": meta["engine"], "key": None,
                    "auth_mode": auth_mode}
    return {"connected": True, "provider": provider, "engine": meta["engine"], "key": key,
            "auth_mode": auth_mode}


def build_kwargs(tid):
    """factory.build_product(**build_kwargs(tid)) routing for this tenant."""
    r = resolve(tid)
    if r["engine"] == "codex":
        return {"engine": "codex", "provider_key": r["key"], "api_key": None}
    return {"engine": "claude", "provider_key": None, "api_key": r["key"]}


def _selftest():
    import billing
    global _validate_api_key, _cli_logged_in
    _real_vak, _real_cli = _validate_api_key, _cli_logged_in
    # Don't touch the network or the host CLI in the selftest — stub the real-verify hooks. We toggle
    # them between "rejects" and "accepts" to prove BOTH that bad creds are blocked and good ones connect.
    _validate_api_key = lambda provider, key: {"error": "that API key was rejected — check it and retry"}
    _cli_logged_in = lambda provider: {"error": "this machine's CLI isn't signed in — run `claude login`"}
    tid = billing.signup("tprov-selftest", "free")["tenant_id"]
    try:
        d0 = resolve(tid); default_ok = d0["engine"] == "claude" and d0["provider"] is None
        # HONESTY GATE 1: an api_key that the provider REJECTS must NOT connect (nothing stored/flipped).
        bad_key = connect(tid, "openai", "api_key", "sk-obviously-bad")
        bad_key_blocked = "error" in bad_key and not any(p["connected"] for p in list_providers(tid))
        # HONESTY GATE 2: subscription mode with the host CLI NOT signed in must NOT connect.
        no_cli = connect(tid, "anthropic", "subscription")
        no_cli_blocked = "error" in no_cli and not any(p["connected"] for p in list_providers(tid))
        # Now creds verify successfully -> the original happy path holds.
        _validate_api_key = lambda provider, key: {"ok": True}
        _cli_logged_in = lambda provider: {"ok": True}
        add_key(tid, "openai", "sk-codexkey-xyz")                      # "no claude, only codex" case (api key)
        r1 = resolve(tid); codex_ok = r1["engine"] == "codex" and r1["provider"] == "openai" and r1["key"] and r1["auth_mode"] == "api_key"
        bk = build_kwargs(tid); bk_ok = bk["engine"] == "codex" and bk["provider_key"]
        # SUBSCRIPTION mode: connect Claude via the logged-in account (no key) and prefer it
        connect(tid, "anthropic", "subscription"); set_priority(tid, ["anthropic", "openai"])
        r2 = resolve(tid); sub_ok = r2["engine"] == "claude" and r2["auth_mode"] == "subscription" and r2["key"] is None
        bk2 = build_kwargs(tid); bk2_ok = bk2["engine"] == "claude" and not bk2["api_key"]   # runs on CLI login
        lst = list_providers(tid); both = sum(1 for p in lst if p["connected"]) == 2
        # When the host CLI is already signed in, the real-login trigger must NO-OP (never relaunch OAuth).
        start_noop = start_subscription_login("anthropic").get("already") is True
        ok = default_ok and bad_key_blocked and no_cli_blocked and codex_ok and bk_ok and sub_ok and bk2_ok and both and start_noop
        print(f"default={d0['engine']} bad-key-blocked={bad_key_blocked} no-cli-blocked={no_cli_blocked} start-noop={start_noop} "
              f"codex-key->{r1['engine']} sub-claude->{r2['auth_mode']}(key={r2['key']}) "
              f"bk-sub={bk2['engine']}/{bk2['api_key']} connected={sum(1 for p in lst if p['connected'])}")
        print("PASS: multi-provider + verify-before-connect (reject bad key / require host CLI sign-in / split) ✅" if ok else "FAIL")
    finally:
        _validate_api_key, _cli_logged_in = _real_vak, _real_cli
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM tenant_providers WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,))
            c.commit()
    sys.exit(0 if ok else 1)


def _main(a):
    import json
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "add" and len(a) > 3:
        print(json.dumps(add_key(a[1], a[2], a[3])))
    elif a[0] == "connect" and len(a) > 3:
        print(json.dumps(connect(a[1], a[2], a[3], a[4] if len(a) > 4 else None)))
    elif a[0] == "login" and len(a) > 1:           # trigger the real host OAuth for a subscription connect
        print(json.dumps(start_subscription_login(a[1])))
    elif a[0] == "substatus" and len(a) > 1:        # is the host CLI genuinely signed in?
        print(json.dumps(subscription_status(a[1])))
    elif a[0] == "remove" and len(a) > 2:
        print(json.dumps(remove_key(a[1], a[2])))
    elif a[0] == "list" and len(a) > 1:
        print(json.dumps(list_providers(a[1]), indent=2))
    elif a[0] == "resolve" and len(a) > 1:
        r = resolve(a[1]); r["key"] = "***" if r["key"] else None
        print(json.dumps(r, indent=2))
    else:
        sys.exit("usage: tenantproviders.py add <tid> <provider> <key> | remove | list <tid> | resolve <tid> | login <provider> | substatus <provider> | selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
