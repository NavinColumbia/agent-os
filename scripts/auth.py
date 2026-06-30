#!/usr/bin/env python3
"""auth.py — real email + password accounts so a person NEVER has to copy/paste an access token.

The token-only flow was a UX failure: signup flashed "save this token" and vanished. This adds proper
accounts: a person signs up with name + email + password (password stored as a salted PBKDF2 hash, never
plaintext) and can sign back in from any device with email + password — the tenant token is managed for
them behind the scenes. A person is a PERSON (one account, one login) who can then create MANY orgs
(companies) — orgs are separate from the account.

    auth.py signup <email> <password> [name]
    auth.py login <email> <password>
    auth.py verify <email> <code>
    auth.py resend <email>
    auth.py selftest
Run with the agent-os venv python.

EMAIL VERIFICATION — honest self-host reality: signup no longer hands back a tenant token immediately.
The account is created UNVERIFIED, a 6-digit code is issued (stored hashed, ~15 min expiry), and the token
is withheld until verify_email() succeeds. Real email delivery is GATED like Stripe/OAuth: it only happens
when an email provider is configured (AOS_SMTP_URL or SENDGRID_API_KEY). On a single-CEO self-host with no
provider we DON'T pretend to send — we return the code as `dev_code` (and log it to stderr) so the UI can
show it, while the account still requires that code. Wire up an email service to flip on real delivery.
"""
import hashlib
import hmac
import os
import secrets
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
        # email verification: gate app access until the emailed code is confirmed (idempotent like the rest)
        cur.execute("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS verified BOOLEAN NOT NULL DEFAULT false")
        cur.execute("""CREATE TABLE IF NOT EXISTS email_codes (
            email TEXT PRIMARY KEY, code_hash TEXT NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL, created_at TIMESTAMPTZ DEFAULT now())""")
        c.commit()


def _hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), _ROUNDS).hex()


def _norm(email):
    return (email or "").strip().lower()


def _gen_code():
    return f"{secrets.randbelow(1_000_000):06d}"


def _code_hash(code):
    # one-way: a leaked DB row should not reveal the live code (mirrors the password-hash stance above)
    return hashlib.sha256(("aos-emailcode:" + (code or "")).encode()).hexdigest()


def _email_configured():
    """Is a real email provider wired up? Gated like Stripe/OAuth — off by default on a self-host."""
    return bool(os.environ.get("AOS_SMTP_URL") or os.environ.get("SENDGRID_API_KEY"))


def _send_code(email, code):
    """Best-effort REAL delivery. Returns True only if a configured provider accepted the message; False
    (the self-host default, no provider) means the caller must surface dev_code instead. Never raises."""
    body = f"Your agent-os verification code is {code}. It expires in 15 minutes."
    frm = os.environ.get("AOS_SMTP_FROM", "no-reply@agent-os.local")
    try:
        url = os.environ.get("AOS_SMTP_URL")
        if url:
            import smtplib
            import ssl
            from email.message import EmailMessage
            from urllib.parse import urlparse
            u = urlparse(url)
            msg = EmailMessage()
            msg["Subject"], msg["From"], msg["To"] = "Your agent-os verification code", frm, email
            msg.set_content(body)
            port = u.port or (465 if u.scheme == "smtps" else 587)
            if u.scheme == "smtps":
                s = smtplib.SMTP_SSL(u.hostname, port, context=ssl.create_default_context())
            else:
                s = smtplib.SMTP(u.hostname, port)
                s.starttls(context=ssl.create_default_context())
            if u.username:
                s.login(u.username, u.password or "")
            s.send_message(msg)
            s.quit()
            return True
        key = os.environ.get("SENDGRID_API_KEY")
        if key:
            import requests
            r = requests.post(
                "https://api.sendgrid.com/v3/mail/send",
                headers={"Authorization": f"Bearer {key}"},
                json={"personalizations": [{"to": [{"email": email}]}],
                      "from": {"email": frm},
                      "subject": "Your agent-os verification code",
                      "content": [{"type": "text/plain", "value": body}]},
                timeout=10)
            return r.status_code < 400
    except Exception:
        return False
    return False


def _issue_code(email):
    """Generate + store a fresh code (15-min expiry, upsert so a resend supersedes the old one), then try
    to email it. Returns dev_code (the plaintext code) when delivery is NOT configured, else None."""
    code = _gen_code()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO email_codes (email, code_hash, expires_at)
                       VALUES (%s, %s, now() + interval '15 minutes')
                       ON CONFLICT (email) DO UPDATE
                         SET code_hash=EXCLUDED.code_hash, expires_at=EXCLUDED.expires_at, created_at=now()""",
                    (email, _code_hash(code)))
        c.commit()
    if _send_code(email, code):
        return None
    print(f"[auth] email delivery not configured — verification code for {email}: {code}", file=sys.stderr)
    return code


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
    tid = reg["tenant_id"]
    salt = os.urandom(16).hex()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        # account is created UNVERIFIED — the tenant token is withheld until verify_email() succeeds
        cur.execute("""INSERT INTO accounts (email, tenant_id, name, pw_salt, pw_hash, verified)
                       VALUES (%s,%s,%s,%s,%s, false)""", (email, tid, name, salt, _hash(password, salt)))
        c.commit()
    dev_code = _issue_code(email)
    audit.append(actor="auth", action="SignUp", resource=tid, decision="pending_verification",
                 payload={"email": email})
    return {"pending_verification": True, "email": email, "dev_code": dev_code}


def verify_email(email, code):
    """Confirm the emailed code. On success: mark the account verified and return the normal token dict
    (same shape signup used to return) so the UI can log them straight in. On a bad/expired code: error."""
    email = _norm(email)
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT code_hash FROM email_codes WHERE email=%s AND expires_at > now()", (email,))
        row = cur.fetchone()
        if not row or not hmac.compare_digest(row[0], _code_hash(code)):
            return {"error": "that code is wrong or expired — resend a new one"}
        cur.execute("UPDATE accounts SET verified=true WHERE email=%s", (email,))
        cur.execute("DELETE FROM email_codes WHERE email=%s", (email,))     # single-use
        cur.execute("SELECT tenant_id FROM accounts WHERE email=%s", (email,))
        arow = cur.fetchone()
        c.commit()
    if not arow:
        return {"error": "that code is wrong or expired — resend a new one"}
    tid = arow[0]
    token, plan = None, "free"
    with psycopg.connect(DB) as c, c.cursor() as cur:
        try:
            cur.execute("SELECT api_token, plan FROM tenants WHERE tenant_id=%s", (tid,))
            r = cur.fetchone()
            if r:
                token, plan = r[0], (r[1] or "free")
        except Exception:
            token = None
    audit.append(actor="auth", action="VerifyEmail", resource=tid, decision="verified",
                 payload={"email": email})
    return {"tenant_id": tid, "api_token": token, "email": email, "plan": plan}


def resend_code(email):
    """(Re)issue + (re)send the verification code. No-op (still ok) for an unknown/already-verified email
    so we don't leak which addresses have pending accounts."""
    email = _norm(email)
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT verified FROM accounts WHERE email=%s", (email,))
        row = cur.fetchone()
    if not row or row[0]:
        return {"ok": True, "dev_code": None}
    return {"ok": True, "dev_code": _issue_code(email)}


def login(email, password):
    email = _norm(email)
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT tenant_id, pw_salt, pw_hash, verified FROM accounts WHERE email=%s", (email,))
        row = cur.fetchone()
    if not row:
        return {"error": "no account with that email — sign up first"}
    tid, salt, want, verified = row
    if not hmac.compare_digest(_hash(password or "", salt), want):
        return {"error": "wrong password"}
    if not verified:                                                 # never issue a token to an unverified account
        return {"error": "please verify your email first — check for the code"}
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


def provider_resolved(tid):
    """Provider-resolution gate: does this tenant have a USABLE model provider on file, so the fleet runs
    on THEIR account instead of silently spending the platform's own credentials? Resolved iff a connected
    provider exists AND it is either a SUBSCRIPTION login (runs on the host CLI's logged-in account, no key)
    OR carries a usable BYO API key. Nothing connected, or an api_key connection whose vault secret is
    missing, -> NOT resolved (would fall back to platform credit -> refuse upstream; never silent-fallback).

    No tenant in context (platform/internal build, offline selftest) -> resolved=True (not a tenant action,
    not gated — symmetric to factory's consent backstop, which only fires when _ctx.tenant is set). Fails
    OPEN on resolver-infra error: a DB/vault hiccup must not wedge the fleet (consent + budget caps still
    bound spend), so a clean 'nothing resolved' fails closed but a broken resolver does not."""
    if not tid:
        return True                                                  # no tenant => not a gated tenant action
    try:
        import tenantproviders
        r = tenantproviders.resolve(tid)
    except Exception:
        return True                                                  # resolver infra error => fail OPEN
    if not r or not r.get("provider"):
        return False                                                 # nothing connected => platform default
    return r.get("auth_mode") == "subscription" or bool(r.get("key"))


def _selftest():
    email = f"qa-{os.urandom(3).hex()}@example.com"
    passed = False
    try:
        bad = signup(email, "short")                                 # too-short password rejected
        pend = signup(email, "correct-horse-battery")                # signup -> PENDING, no token yet
        dup = signup(email, "correct-horse-battery")                 # dup rejected
        blocked = login(email, "correct-horse-battery")              # unverified account: login blocked, no token
        code = pend.get("dev_code")                                  # self-host fallback exposes the code
        wrong_code = "000000" if code != "000000" else "111111"
        badcode = verify_email(email, wrong_code)                    # wrong code rejected
        ver = verify_email(email, code)                              # right code -> normal token dict
        good = login(email, "correct-horse-battery")                 # verified account can now log in + get a token
        wrong = login(email, "nope")                                 # wrong password rejected
        nouser = login("ghost@example.com", "whatever")
        no_prov = provider_resolved(ver["tenant_id"])                # fresh account, nothing connected -> NOT resolved
        no_tenant = provider_resolved("")                            # no tenant in context -> not gated (resolved)
        passed = (bad.get("error")
                  and pend.get("pending_verification") is True and not pend.get("api_token")
                  and dup.get("error")
                  and "verify" in (blocked.get("error") or "") and not blocked.get("api_token")
                  and badcode.get("error") and not badcode.get("api_token")
                  and ver.get("api_token") and ver.get("tenant_id")
                  and good.get("tenant_id") == ver["tenant_id"] and good.get("api_token")
                  and wrong.get("error") == "wrong password" and nouser.get("error")
                  and no_prov is False and no_tenant is True)
        print(f"reject-short={bool(bad.get('error'))} signup-pending={pend.get('pending_verification') is True} "
              f"no-token-yet={not pend.get('api_token')} dup-blocked={bool(dup.get('error'))} "
              f"unverified-login-blocked={'verify' in (blocked.get('error') or '')} "
              f"wrong-code-rejected={bool(badcode.get('error'))} verify-token={bool(ver.get('api_token'))} "
              f"verified-login-token={bool(good.get('api_token'))} wrong-pw={wrong.get('error')=='wrong password'} "
              f"provider-gate(none={no_prov},no-tenant={no_tenant})")
        print("PASS: email-verified accounts (pending signup, gated login, verify -> token) ✅" if passed else "FAIL")
    finally:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("SELECT tenant_id FROM accounts WHERE email=%s", (email,))
            r = cur.fetchone()
            cur.execute("DELETE FROM email_codes WHERE email=%s", (email,))
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
    elif a[0] == "verify" and len(a) >= 3:
        r = verify_email(a[1], a[2])
        if r.get("api_token"):
            r["api_token"] = "***"
        print(json.dumps(r))
    elif a[0] == "resend" and len(a) >= 2:
        print(json.dumps(resend_code(a[1])))
    else:
        sys.exit("usage: auth.py signup <email> <password> [name] | login <email> <password> | "
                 "verify <email> <code> | resend <email> | selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
