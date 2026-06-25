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
import sys
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit   # noqa: E402
import vault   # noqa: E402

ENV = Path.home() / "projects" / "agent-os" / ".env.local"
DB = next((l.split("=", 1)[1].strip() for l in ENV.read_text().splitlines()
           if l.strip().startswith("DATABASE_URL=")), None)

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


def connect(tid, provider, mode="api_key", key=None):
    """Connect a provider either by BYO API key (mode='api_key', metered) or by SUBSCRIPTION login
    (mode='subscription' — runs on the host CLI's logged-in Claude/ChatGPT account, no per-token billing).
    Subscription mode needs no key. Returns connected status."""
    if provider not in _BY_SLUG:
        return {"error": f"unknown provider '{provider}'"}
    if mode not in ("api_key", "subscription"):
        return {"error": "mode must be 'api_key' or 'subscription'"}
    if mode == "api_key" and not key:
        return {"error": "an API key is required for api_key mode"}
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
            v = vault.get_secret(_secret_name(provider), f"tenant:{tid}", "prod", "builder")
            key = v if isinstance(v, str) else (v.get("value") if isinstance(v, dict) else None)
        except Exception:
            key = None
    return {"engine": meta["engine"], "provider": provider, "key": key, "auth_mode": auth_mode}


def build_kwargs(tid):
    """factory.build_product(**build_kwargs(tid)) routing for this tenant."""
    r = resolve(tid)
    if r["engine"] == "codex":
        return {"engine": "codex", "provider_key": r["key"], "api_key": None}
    return {"engine": "claude", "provider_key": None, "api_key": r["key"]}


def _selftest():
    import billing
    tid = billing.signup("tprov-selftest", "free")["tenant_id"]
    try:
        d0 = resolve(tid); default_ok = d0["engine"] == "claude" and d0["provider"] is None
        add_key(tid, "openai", "sk-codexkey-xyz")                      # "no claude, only codex" case (api key)
        r1 = resolve(tid); codex_ok = r1["engine"] == "codex" and r1["provider"] == "openai" and r1["key"] and r1["auth_mode"] == "api_key"
        bk = build_kwargs(tid); bk_ok = bk["engine"] == "codex" and bk["provider_key"]
        # SUBSCRIPTION mode: connect Claude via the logged-in account (no key) and prefer it
        connect(tid, "anthropic", "subscription"); set_priority(tid, ["anthropic", "openai"])
        r2 = resolve(tid); sub_ok = r2["engine"] == "claude" and r2["auth_mode"] == "subscription" and r2["key"] is None
        bk2 = build_kwargs(tid); bk2_ok = bk2["engine"] == "claude" and not bk2["api_key"]   # runs on CLI login
        lst = list_providers(tid); both = sum(1 for p in lst if p["connected"]) == 2
        ok = default_ok and codex_ok and bk_ok and sub_ok and bk2_ok and both
        print(f"default={d0['engine']} codex-key->{r1['engine']} sub-claude->{r2['auth_mode']}(key={r2['key']}) "
              f"bk-sub={bk2['engine']}/{bk2['api_key']} connected={sum(1 for p in lst if p['connected'])}")
        print("PASS: multi-provider (codex key / claude SUBSCRIPTION login / split) ✅" if ok else "FAIL")
    finally:
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
    elif a[0] == "remove" and len(a) > 2:
        print(json.dumps(remove_key(a[1], a[2])))
    elif a[0] == "list" and len(a) > 1:
        print(json.dumps(list_providers(a[1]), indent=2))
    elif a[0] == "resolve" and len(a) > 1:
        r = resolve(a[1]); r["key"] = "***" if r["key"] else None
        print(json.dumps(r, indent=2))
    else:
        sys.exit("usage: tenantproviders.py add <tid> <provider> <key> | remove | list <tid> | resolve <tid> | selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
