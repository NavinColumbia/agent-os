#!/usr/bin/env python3
"""auth.py — real email + password accounts so a person NEVER has to copy/paste an access token.

The token-only flow was a UX failure: signup flashed "save this token" and vanished. This adds proper
accounts: a person signs up with name + email + password (password stored as a salted PBKDF2 hash, never
plaintext) and can sign back in from any device with email + password — the tenant token is managed for
them behind the scenes. A person is a PERSON (one account, one login) who can then create MANY orgs
(companies) — orgs are separate from the account.

    auth.py signup <email> <password> [name]
    auth.py login <email> <password>
    auth.py selftest
Run with the agent-os venv python.
"""
import hashlib
import hmac
import os
import sys
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit    # noqa: E402
import billing  # noqa: E402

ENV = Path.home() / "projects" / "agent-os" / ".env.local"
DB = next((l.split("=", 1)[1].strip() for l in ENV.read_text().splitlines()
           if l.strip().startswith("DATABASE_URL=")), None)
_ROUNDS = 200_000


def _ensure():
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS accounts (
            email TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, name TEXT,
            pw_salt TEXT NOT NULL, pw_hash TEXT NOT NULL, created_at TIMESTAMPTZ DEFAULT now())""")
        c.commit()


def _hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), _ROUNDS).hex()


def _norm(email):
    return (email or "").strip().lower()


def signup(email, password, name=None, plan="free"):
    email = _norm(email)
    if "@" not in email or "." not in email.split("@")[-1]:
        return {"error": "enter a valid email"}
    if not password or len(password) < 8:
        return {"error": "password must be at least 8 characters"}
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT 1 FROM accounts WHERE email=%s", (email,))
        if cur.fetchone():
            return {"error": "an account with that email already exists — sign in instead"}
    reg = billing.signup(name or email.split("@")[0], plan)          # creates the tenant + token
    tid, token = reg["tenant_id"], reg["api_token"]
    salt = os.urandom(16).hex()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO accounts (email, tenant_id, name, pw_salt, pw_hash)
                       VALUES (%s,%s,%s,%s,%s)""", (email, tid, name, salt, _hash(password, salt)))
        c.commit()
    audit.append(actor="auth", action="SignUp", resource=tid, decision="created", payload={"email": email})
    return {"tenant_id": tid, "api_token": token, "email": email, "plan": reg.get("plan", plan)}


def login(email, password):
    email = _norm(email)
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT tenant_id, pw_salt, pw_hash FROM accounts WHERE email=%s", (email,))
        row = cur.fetchone()
    if not row:
        return {"error": "no account with that email — sign up first"}
    tid, salt, want = row
    if not hmac.compare_digest(_hash(password or "", salt), want):
        return {"error": "wrong password"}
    # fetch the tenant's current api token
    try:
        import tenancy
        token = tenancy.token_for_tenant(tid) if hasattr(tenancy, "token_for_tenant") else None
    except Exception:
        token = None
    if not token:                                                    # fall back to reading the token store
        with psycopg.connect(DB) as c, c.cursor() as cur:
            try:
                cur.execute("SELECT api_token FROM tenants WHERE tenant_id=%s", (tid,))
                r = cur.fetchone(); token = r[0] if r else None
            except Exception:
                token = None
    audit.append(actor="auth", action="LogIn", resource=tid, decision="ok", payload={"email": email})
    return {"tenant_id": tid, "api_token": token, "email": email}


def _selftest():
    email = f"qa-{os.urandom(3).hex()}@example.com"
    try:
        bad = signup(email, "short")                                 # too-short password rejected
        ok1 = signup(email, "correct-horse-battery")                 # real signup
        dup = signup(email, "correct-horse-battery")                 # dup rejected
        good = login(email, "correct-horse-battery")                 # login returns the same tenant + a token
        wrong = login(email, "nope")                                 # wrong password rejected
        nouser = login("ghost@example.com", "whatever")
        passed = (bad.get("error") and ok1.get("api_token") and dup.get("error")
                  and good.get("tenant_id") == ok1["tenant_id"] and good.get("api_token")
                  and wrong.get("error") == "wrong password" and nouser.get("error"))
        print(f"reject-short={bool(bad.get('error'))} signup={bool(ok1.get('api_token'))} "
              f"dup-blocked={bool(dup.get('error'))} login-token={bool(good.get('api_token'))} "
              f"wrong-pw={wrong.get('error')=='wrong password'}")
        print("PASS: email+password accounts (signup/login, hashed, no token-pasting) ✅" if passed else "FAIL")
    finally:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("SELECT tenant_id FROM accounts WHERE email=%s", (email,))
            r = cur.fetchone()
            if r:
                cur.execute("DELETE FROM accounts WHERE email=%s", (email,))
                cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (r[0],))
            c.commit()
    sys.exit(0 if passed else 1)


def _main(a):
    import json
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "signup" and len(a) >= 3:
        print(json.dumps(signup(a[1], a[2], a[3] if len(a) > 3 else None)))
    elif a[0] == "login" and len(a) >= 3:
        r = login(a[1], a[2]); r["api_token"] = "***" if r.get("api_token") else None
        print(json.dumps(r))
    else:
        sys.exit("usage: auth.py signup <email> <password> [name] | login <email> <password> | selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
