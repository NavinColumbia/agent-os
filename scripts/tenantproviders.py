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
        c.commit()


def _secret_name(provider):
    return f"byo_key_{provider}"


def add_key(tid, provider, key):
    if provider not in _BY_SLUG:
        return {"error": f"unknown provider '{provider}'"}
    _ensure()
    if key:
        vault.put_secret(_secret_name(provider), f"tenant:{tid}", "prod", ["builder", "factory"], key)
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT COALESCE(min(priority),5) FROM tenant_providers WHERE tenant_id=%s AND has_key", (tid,))
        top = cur.fetchone()[0]
        cur.execute("""INSERT INTO tenant_providers (tenant_id, provider, has_key, priority)
                       VALUES (%s,%s,true,%s)
                       ON CONFLICT (tenant_id, provider) DO UPDATE SET has_key=true""",
                    (tid, provider, max(1, top)))
        c.commit()
    audit.append(actor="tenantproviders", action="ProviderConnected", resource=tid, decision="connected",
                 payload={"provider": provider})
    return {"ok": True, "provider": provider, "engine": _BY_SLUG[provider]["engine"]}


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
        cur.execute("SELECT provider, has_key, priority FROM tenant_providers WHERE tenant_id=%s", (tid,))
        state = {p: (hk, pr) for p, hk, pr in cur.fetchall()}
    out = []
    for p in CATALOG:
        hk, pr = state.get(p["slug"], (False, 5))
        out.append({**{k: p[k] for k in ("slug", "name", "engine", "blurb", "key_hint")},
                    "connected": bool(hk), "priority": pr})
    out.sort(key=lambda x: (not x["connected"], x["priority"]))
    return out


def resolve(tid):
    """The engine + BYO key the build flow should use: highest-preference connected provider, else default."""
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT provider FROM tenant_providers WHERE tenant_id=%s AND has_key
                       ORDER BY priority, added_at LIMIT 1""", (tid,))
        row = cur.fetchone()
    if not row:
        return {"engine": "claude", "provider": None, "key": None}
    provider = row[0]; meta = _BY_SLUG.get(provider, _BY_SLUG["anthropic"])
    key = None
    try:
        v = vault.get_secret(_secret_name(provider), f"tenant:{tid}", "prod", "builder")
        key = v if isinstance(v, str) else (v.get("value") if isinstance(v, dict) else None)
    except Exception:
        key = None
    return {"engine": meta["engine"], "provider": provider, "key": key}


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
        add_key(tid, "openai", "sk-codexkey-xyz")                      # "no claude, only codex" case
        r1 = resolve(tid); codex_ok = r1["engine"] == "codex" and r1["provider"] == "openai" and r1["key"]
        bk = build_kwargs(tid); bk_ok = bk["engine"] == "codex" and bk["provider_key"]
        add_key(tid, "anthropic", "sk-ant-abc"); set_priority(tid, ["anthropic", "openai"])
        r2 = resolve(tid); claude_ok = r2["engine"] == "claude" and r2["provider"] == "anthropic"
        lst = list_providers(tid); both = sum(1 for p in lst if p["connected"]) == 2
        ok = default_ok and codex_ok and bk_ok and claude_ok and both
        print(f"default={d0['engine']} codex-only->{r1['engine']} bk={bk['engine']} "
              f"prefer-claude->{r2['engine']} connected={sum(1 for p in lst if p['connected'])}")
        print("PASS: multi-provider resolve (codex-only / claude / split preference) ✅" if ok else "FAIL")
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
