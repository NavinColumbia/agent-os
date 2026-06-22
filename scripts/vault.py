#!/usr/bin/env python3
"""vault.py — scoped secrets vault (the secure way agents get credentials).

Secrets are encrypted at rest (Fernet) and scoped by (product, environment, allowed roles). An agent
NEVER reads a secret from a file — it requests it from the vault, which checks the agent's role AND
environment before decrypting, and audits every grant/deny. This is how a QA/test agent gets TEST
credentials (e.g., an iOS app's test API key for simulator E2E) while PROD secrets never reach test.

    vault.py put <name> <product> <env> <role[,role]> <value> [ttl]
    vault.py get <name> <product> <env> <role>
    from vault import put_secret, get_secret
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


def put_secret(name, product, environment, allowed_roles, value, ttl_seconds=None):
    enc = _F.encrypt(value.encode())
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute(
            """INSERT INTO secrets (name, product, environment, allowed_roles, value_enc, expires_at)
               VALUES (%s,%s,%s,%s,%s, CASE WHEN %s::int IS NULL THEN NULL ELSE now()+(%s::int||' seconds')::interval END)
               ON CONFLICT (name, product, environment)
               DO UPDATE SET allowed_roles=EXCLUDED.allowed_roles, value_enc=EXCLUDED.value_enc, expires_at=EXCLUDED.expires_at""",
            (name, product, environment, list(allowed_roles), enc, ttl_seconds, ttl_seconds),
        )
        c.commit()
    return True


def get_secret(name, product, environment, role):
    """Return the plaintext secret IFF role is allowed in that product+environment. Else raise + audit."""
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute(
            "SELECT allowed_roles, value_enc FROM secrets WHERE name=%s AND product=%s AND environment=%s "
            "AND (expires_at IS NULL OR expires_at > now())",
            (name, product, environment),
        )
        r = cur.fetchone()
    resource = f"{product}/{environment}/{name}"
    if not r:
        audit.append(actor=role, action="GetSecret", resource=resource, decision="deny", payload={"reason": "not found/expired"})
        raise AccessDenied(f"no secret {resource}")
    allowed, enc = r
    if role not in allowed:
        audit.append(actor=role, action="GetSecret", resource=resource, decision="deny", payload={"reason": f"role {role} not in {allowed}"})
        raise AccessDenied(f"role '{role}' not permitted for {resource}")
    audit.append(actor=role, action="GetSecret", resource=resource, decision="allow")
    return _F.decrypt(bytes(enc)).decode()


def _main(a):
    if a and a[0] == "put":
        ttl = int(a[6]) if len(a) > 6 else None
        put_secret(a[1], a[2], a[3], a[4].split(","), a[5], ttl); print(f"stored {a[2]}/{a[3]}/{a[1]} for roles {a[4]}")
    elif a and a[0] == "get":
        try:
            print(get_secret(a[1], a[2], a[3], a[4]))
        except AccessDenied as e:
            print(f"DENIED: {e}"); sys.exit(1)
    elif a and a[0] == "test":
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
        print("PASS: scoped vault — test secret to QA ✅, prod secret denied to test ✅, role denied ✅"
              if (ok and denied_prod and denied_role and audit.verify()[0]) else "FAIL")
        sys.exit(0 if (ok and denied_prod and denied_role) else 1)
    else:
        sys.exit("usage: vault.py put|get|test ...")


if __name__ == "__main__":
    _main(sys.argv[1:])
