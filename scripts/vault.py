#!/usr/bin/env python3
"""vault.py — scoped secrets vault (the secure way agents get credentials).

Secrets are encrypted at rest (Fernet) and scoped by (product, environment, allowed roles). An agent
NEVER reads a secret from a file — it requests it from the vault, which checks the agent's role AND
environment before decrypting, and audits every grant/deny. This is how a QA/test agent gets TEST
credentials (e.g., an iOS app's test API key for simulator E2E) while PROD secrets never reach test.

    vault.py put <name> <product> <env> <role[,role]> <value> [ttl]
    vault.py get <name> <product> <env> <role> [requester_tenant]
    from vault import put_secret, get_secret

Per-tenant secrets are namespaced as product='tenant:<tid>'. get_secret BINDS the requester's
tenant: a tenant-owned secret is only released when the caller passes tenant_id=<tid> matching the
owner (role alone — e.g. 'builder' — is shared across tenants and is NOT sufficient). See #52.
Run with the agent-os venv python.
"""
import sys
from pathlib import Path

import psycopg
from cryptography.fernet import Fernet

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit  # noqa: E402

ENV = Path.home() / "projects" / "agent-os" / ".env.local"
_cfg = {l.split("=", 1)[0].strip(): l.split("=", 1)[1].strip()
        for l in ENV.read_text().splitlines() if l.strip() and not l.startswith("#") and "=" in l}
DB = _cfg["DATABASE_URL"]
_F = Fernet(_cfg["VAULT_KEY"].encode())


class AccessDenied(Exception):
    pass


# Sentinel stored for non-tenant (global/infra) secrets. tenant_id is part of the PK and
# Postgres PK columns cannot be NULL, so global secrets carry the empty string.
_GLOBAL = ""


def _tenant_of(product):
    """The tenant that OWNS a secret, derived from its product namespace.

    Frontdoor/integrations namespace per-tenant secrets as product='tenant:<tid>'. Anything else
    (e.g. 'iosapp') is a shared/infra secret owned by no single tenant -> _GLOBAL."""
    if isinstance(product, str) and product.startswith("tenant:"):
        return product.split(":", 1)[1]
    return _GLOBAL


_SCHEMA_READY = False


def _ensure_schema():
    """Idempotently make the live `secrets` table tenant-scoped (#52).

    The original PK was (name, product, environment) with no tenant column, so a secret was
    addressable by anyone who could name its product — and `get_secret` never bound the *requester's*
    tenant. We add `tenant_id` and fold it into the PK so tenant isolation is enforced in storage,
    not just by convention. ADD COLUMN IF NOT EXISTS is a no-op once applied; the PK is only
    rebuilt when tenant_id is not yet part of it."""
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("ALTER TABLE secrets ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT ''")
        # Backfill tenant_id for any rows written before this column existed.
        cur.execute("UPDATE secrets SET tenant_id=split_part(product,':',2) "
                    "WHERE tenant_id='' AND product LIKE 'tenant:%'")
        cur.execute(
            """SELECT a.attname FROM pg_index i
                 JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum = ANY(i.indkey)
                WHERE i.indrelid='secrets'::regclass AND i.indisprimary"""
        )
        pk = {r[0] for r in cur.fetchall()}
        if "tenant_id" not in pk:
            cur.execute("ALTER TABLE secrets DROP CONSTRAINT IF EXISTS secrets_pkey")
            cur.execute("ALTER TABLE secrets ADD PRIMARY KEY (name, product, environment, tenant_id)")
        c.commit()
    _SCHEMA_READY = True


def put_secret(name, product, environment, allowed_roles, value, ttl_seconds=None, tenant_id=None):
    """Store an encrypted secret. The owning tenant is derived from the product namespace
    (product='tenant:<tid>' -> owner <tid>); pass tenant_id only to override that explicitly."""
    _ensure_schema()
    owner = tenant_id if tenant_id is not None else _tenant_of(product)
    enc = _F.encrypt(value.encode())
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute(
            """INSERT INTO secrets (name, product, environment, tenant_id, allowed_roles, value_enc, expires_at)
               VALUES (%s,%s,%s,%s,%s,%s, CASE WHEN %s::int IS NULL THEN NULL ELSE now()+(%s::int||' seconds')::interval END)
               ON CONFLICT (name, product, environment, tenant_id)
               DO UPDATE SET allowed_roles=EXCLUDED.allowed_roles, value_enc=EXCLUDED.value_enc, expires_at=EXCLUDED.expires_at""",
            (name, product, environment, owner, list(allowed_roles), enc, ttl_seconds, ttl_seconds),
        )
        c.commit()
    return True


def get_secret(name, product, environment, role, tenant_id=None):
    """Return the plaintext secret IFF (a) the secret's owning tenant matches the requester's
    tenant AND (b) `role` is allowed in that product+environment. Else raise + audit.

    #52 fix — tenant binding: a per-tenant secret (product='tenant:<tid>') is only released to a
    caller that proves it is acting for that same tenant by passing tenant_id=<tid>. Roles such as
    'builder' are shared across every tenant, so role alone is NOT sufficient — without this bind,
    tenant A's build agent could read tenant B's BYO model key just by naming product='tenant:B'.
    The check is FAIL-CLOSED: a tenant-owned secret requested with a missing/mismatched tenant is
    denied. Global/infra secrets (no tenant namespace) are unaffected."""
    _ensure_schema()
    owner = _tenant_of(product)
    resource = f"{product}/{environment}/{name}"
    # Bind the requester's tenant BEFORE touching the row. For a tenant-owned secret the caller must
    # present the matching tenant_id; we never fall back to trusting the product string alone.
    if owner != _GLOBAL and (tenant_id is None or str(tenant_id) != owner):
        audit.append(actor=role, action="GetSecret", resource=resource, decision="deny",
                     payload={"reason": f"tenant bind failed: requester={tenant_id!r} owner={owner!r}"})
        raise AccessDenied(f"tenant mismatch for {resource}")
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute(
            "SELECT allowed_roles, value_enc FROM secrets WHERE name=%s AND product=%s AND environment=%s "
            "AND tenant_id=%s AND (expires_at IS NULL OR expires_at > now())",
            (name, product, environment, owner),
        )
        r = cur.fetchone()
    if not r:
        audit.append(actor=role, action="GetSecret", resource=resource, decision="deny", payload={"reason": "not found/expired"})
        raise AccessDenied(f"no secret {resource}")
    allowed, enc = r
    if role not in allowed:
        audit.append(actor=role, action="GetSecret", resource=resource, decision="deny", payload={"reason": f"role {role} not in {allowed}"})
        raise AccessDenied(f"role '{role}' not permitted for {resource}")
    audit.append(actor=role, action="GetSecret", resource=resource, decision="allow")
    return _F.decrypt(bytes(enc)).decode()


def delete_secrets_for_products(products):
    """Purge every secret scoped to any of `products` (GDPR right-to-erasure). Returns count deleted.

    Secrets ARE addressed by `product` (DELETE ... WHERE product = ANY). A tenant's own credentials
    (BYO LLM key, integration keys) live under the synthetic product='tenant:<tid>', so a complete
    erasure must include that namespace in `products` (see account.delete), not only the tenant's
    built-product slugs. (The table also carries a tenant_id column for read-time isolation — #52 —
    but it is derived from this same namespace.) Empty/falsy entries are dropped; empty list -> 0.
    """
    products = [p for p in products if p]
    if not products:
        return 0
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("DELETE FROM secrets WHERE product = ANY(%s)", (products,))
        n = cur.rowcount
        c.commit()
    return n


def _main(a):
    if a and a[0] == "put":
        ttl = int(a[6]) if len(a) > 6 else None
        put_secret(a[1], a[2], a[3], a[4].split(","), a[5], ttl); print(f"stored {a[2]}/{a[3]}/{a[1]} for roles {a[4]}")
    elif a and a[0] == "get":
        try:
            tid = a[5] if len(a) > 5 else None  # optional: requester tenant for tenant-scoped secrets
            print(get_secret(a[1], a[2], a[3], a[4], tenant_id=tid))
        except AccessDenied as e:
            print(f"DENIED: {e}"); sys.exit(1)
    elif a and a[0] in ("test", "selftest"):
        # prove scoping: test secret reachable in test by qa/builder; prod secret NOT reachable from test
        put_secret("API_KEY", "iosapp", "test", ["builder", "qa-security"], "test-sk-123")
        put_secret("DB_PASSWORD", "iosapp", "prod", ["platform-infra"], "prod-pw-xyz")
        ok = get_secret("API_KEY", "iosapp", "test", "qa-security") == "test-sk-123"
        denied_prod = False
        try:
            get_secret("DB_PASSWORD", "iosapp", "test", "builder")  # wrong env+role -> deny
        except AccessDenied:
            denied_prod = True
        denied_role = False
        try:
            get_secret("API_KEY", "iosapp", "test", "research-growth")  # role not allowed
        except AccessDenied:
            denied_role = True
        # #52: per-tenant BYO key must be tenant-bound. Same role ('builder') across two tenants.
        put_secret("byo_llm_key", "tenant:A", "prod", ["builder", "factory"], "sk-tenantA")
        put_secret("byo_llm_key", "tenant:B", "prod", ["builder", "factory"], "sk-tenantB")
        own = get_secret("byo_llm_key", "tenant:A", "prod", "builder", tenant_id="A") == "sk-tenantA"
        cross = False
        try:  # tenant A's builder tries to read tenant B's key -> deny
            get_secret("byo_llm_key", "tenant:B", "prod", "builder", tenant_id="A")
        except AccessDenied:
            cross = True
        nobind = False
        try:  # tenant-owned secret requested without proving tenant -> fail-closed deny
            get_secret("byo_llm_key", "tenant:A", "prod", "builder")
        except AccessDenied:
            nobind = True
        tenant_ok = own and cross and nobind
        passed = ok and denied_prod and denied_role and tenant_ok
        print("PASS: scoped vault — test→QA ✅, prod denied to test ✅, role denied ✅, "
              "tenant-bound BYO key ✅ (cross-tenant + no-bind denied)"
              if (passed and audit.verify()[0]) else "FAIL")
        sys.exit(0 if passed else 1)
    else:
        sys.exit("usage: vault.py put|get|test ...")


if __name__ == "__main__":
    _main(sys.argv[1:])
