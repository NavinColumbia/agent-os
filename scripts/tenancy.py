#!/usr/bin/env python3
"""tenancy.py — multi-tenant isolation (SaaS). Each customer is a tenant with their own API token;
products belong to tenants; a tenant can only see/operate their own. The isolation boundary for a
hosted offering where many users bring their own agents/keys. (At cloud scale, add row-level
security per table keyed on tenant_id.)

    tenancy.py create <name>     # -> tenant_id + api_token
    from tenancy import tenant_for_token, register_product, owns, products_of
Run with the agent-os venv python.
"""
import secrets
import sys
from pathlib import Path

import psycopg

ENV = Path.home() / "projects" / "agent-os" / ".env.local"
DB = next((l.split("=", 1)[1].strip() for l in ENV.read_text().splitlines()
           if l.strip().startswith("DATABASE_URL=")), None)


def create_tenant(name):
    tid = "t-" + secrets.token_hex(4)
    tok = "aos_" + secrets.token_hex(20)
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("INSERT INTO tenants (tenant_id, name, api_token) VALUES (%s,%s,%s)", (tid, name, tok))
        c.commit()
    return {"tenant_id": tid, "name": name, "api_token": tok}


def tenant_for_token(token):
    # C2: the per-request auth lookup (every authenticated console call) — the hottest read, pooled (fail-open).
    import dbpool
    with dbpool.connection(autocommit=True) as c, c.cursor() as cur:
        cur.execute("SELECT tenant_id FROM tenants WHERE api_token=%s", (token,))
        r = cur.fetchone()
        return r[0] if r else None


def register_product(product, tenant_id):
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("INSERT INTO tenant_products (product, tenant_id) VALUES (%s,%s) ON CONFLICT DO NOTHING", (product, tenant_id))
        c.commit()


def owns(tenant_id, product):
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT 1 FROM tenant_products WHERE product=%s AND tenant_id=%s", (product, tenant_id))
        return cur.fetchone() is not None


def products_of(tenant_id):
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT product FROM tenant_products WHERE tenant_id=%s ORDER BY product", (tenant_id,))
        return [r[0] for r in cur.fetchall()]


def _test():
    a = create_tenant("acme"); b = create_tenant("globex")
    register_product("acme-app", a["tenant_id"]); register_product("globex-app", b["tenant_id"])
    # token resolves to the right tenant; each sees only their own; cross-access denied
    ta = tenant_for_token(a["api_token"]) == a["tenant_id"]
    iso = products_of(a["tenant_id"]) == ["acme-app"] and products_of(b["tenant_id"]) == ["globex-app"]
    cross_denied = (not owns(a["tenant_id"], "globex-app")) and owns(b["tenant_id"], "globex-app")
    bad_token = tenant_for_token("aos_wrong") is None
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("DELETE FROM tenant_products WHERE tenant_id IN (%s,%s)", (a["tenant_id"], b["tenant_id"]))
        cur.execute("DELETE FROM tenants WHERE tenant_id IN (%s,%s)", (a["tenant_id"], b["tenant_id"])); c.commit()
    ok = ta and iso and cross_denied and bad_token
    print(f"token→tenant={ta}, isolation={iso}, cross-access-denied={cross_denied}, bad-token-rejected={bad_token}")
    print("PASS: multi-tenant isolation — each tenant sees only their own ✅" if ok else "FAIL")
    sys.exit(0 if ok else 1)


def _main(a):
    if a and a[0] == "create":
        print(create_tenant(a[1]))
    elif a and a[0] == "test":
        _test()
    else:
        sys.exit("usage: tenancy.py create <name> | test")


if __name__ == "__main__":
    _main(sys.argv[1:])
