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
    auth.py reset <email>
    auth.py reset-verify <email> <code> <new_password>
    auth.py notify-status
    auth.py selftest
Run with the agent-os venv python.

EMAIL VERIFICATION — honest self-host reality: signup no longer hands back a tenant token immediately.
The account is created UNVERIFIED, a 6-digit code is issued (stored hashed, ~15 min expiry), and the token
is withheld until verify_email() succeeds. Real delivery is GATED like Stripe/OAuth: it only happens when a
channel is configured — SMTP (AOS_SMTP_URL), SendGrid (SENDGRID_API_KEY), or self-hosted ntfy (NTFY_TOPIC).
On a bare single-CEO self-host with NO channel we DON'T pretend to send — we return the code as `dev_code`
(and log it to stderr) so the UI can show it, while the account still requires that code. Wire up any one
channel to flip on real delivery; signup/verify surface `notify_configured` so onboarding can nudge setup.

PASSWORD RESET — same honest code mechanism. issue_reset() mints a hashed one-time 'reset' code (~15 min
expiry) and delivers it over the configured channel, or exposes `dev_code` on-screen on a bare self-host.
verify_reset() checks the code, sets the new password, and (since holding the code proves email ownership)
also marks the account verified, so a stuck-unverified person can recover in one flow. It returns the normal
token dict so the UI logs them straight in.

NOTIFICATIONS — the same senders back notify(title, message): the "I'll ping you when it's done" milestone
channel so a person who left the tab still hears about it. Best-effort; returns whether it was delivered.
"""
import hashlib
import hmac
import os
import secrets
import sys
import threading
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit    # noqa: E402
import billing  # noqa: E402

from aoscfg import ENV, DB, get
from dbpool import connection, tenant_connection

MAX_CODE_ATTEMPTS = int(os.environ.get("AOS_MAX_CODE_ATTEMPTS", "6"))   # wrong-code tries before a code locks
SESSION_TTL_DAYS = int(os.environ.get("AOS_AUTH_SESSION_TTL_DAYS", "30"))
# Public endpoint limits are deliberately separate by IP and account. The DB-backed buckets work across
# threads/process restarts and across multiple console workers; no attacker can reset them by reconnecting.
PUBLIC_RATE_POLICIES = {
    "signup": (3600, 8, 3),
    "login": (900, 20, 10),
    "verify-email": (900, 30, 8),
    "resend-code": (3600, 12, 4),
    "reset-request": (3600, 12, 4),
    "reset-password": (900, 20, 8),
    "assurance-intake": (3600, 8, 3),
}
_BAD_LOGIN = "that email or password is incorrect"   # non-enumerating: same for no-account and wrong-password
_ROUNDS = 200_000
_ensured = False
_ensure_lock = threading.Lock()


def _conn():
    return connection()


def _ensure():
    global _ensured
    if _ensured:
        return
    with _ensure_lock:
        if _ensured:
            return
        with _conn() as c, c.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS accounts (
                email TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, name TEXT,
                pw_salt TEXT NOT NULL, pw_hash TEXT NOT NULL, created_at TIMESTAMPTZ DEFAULT now())""")
            # email verification: gate app access until the emailed code is confirmed (idempotent like the rest)
            cur.execute("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS verified BOOLEAN NOT NULL DEFAULT false")
            cur.execute("""CREATE TABLE IF NOT EXISTS email_codes (
                email TEXT NOT NULL, code_hash TEXT NOT NULL,
                purpose TEXT NOT NULL DEFAULT 'verify',
                expires_at TIMESTAMPTZ NOT NULL, created_at TIMESTAMPTZ DEFAULT now(),
                PRIMARY KEY (email, purpose))""")
            # migrate a pre-existing single-column (email) PK -> composite (email, purpose) so a live 'verify'
            # code and a 'reset' code for the same person can coexist instead of clobbering each other.
            cur.execute("ALTER TABLE email_codes ADD COLUMN IF NOT EXISTS purpose TEXT NOT NULL DEFAULT 'verify'")
            cur.execute("ALTER TABLE email_codes DROP CONSTRAINT IF EXISTS email_codes_pkey")
            cur.execute("ALTER TABLE email_codes ADD CONSTRAINT email_codes_pkey PRIMARY KEY (email, purpose)")
            # brute-force guard: a 6-digit code (1M space) with no attempt cap is guessable in the 15-min window.
            cur.execute("ALTER TABLE email_codes ADD COLUMN IF NOT EXISTS attempts INT NOT NULL DEFAULT 0")
            cur.execute("""CREATE TABLE IF NOT EXISTS auth_sessions (
                token_hash TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, email TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                last_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
                expires_at TIMESTAMPTZ NOT NULL)""")
            cur.execute("CREATE INDEX IF NOT EXISTS auth_sessions_tenant_idx ON auth_sessions(tenant_id)")
            cur.execute("""CREATE TABLE IF NOT EXISTS auth_rate_limits (
                action TEXT NOT NULL, key_hash TEXT NOT NULL, bucket BIGINT NOT NULL,
                attempts INT NOT NULL DEFAULT 1, updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (action, key_hash, bucket))""")
            c.commit()
        _ensured = True


def _hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), _ROUNDS).hex()


def _norm(email):
    return (email or "").strip().lower()


def _gen_code():
    return f"{secrets.randbelow(1_000_000):06d}"


def _code_hash(code, purpose="verify"):
    # one-way: a leaked DB row should not reveal the live code (mirrors the password-hash stance above).
    # purpose is bound into the hash so a 'verify' code can never be replayed as a 'reset' code.
    return hashlib.sha256((f"aos-emailcode:{purpose}:" + (code or "")).encode()).hexdigest()


def _opaque_hash(value):
    return hashlib.sha256((value or "").encode()).hexdigest()


def _rate_key(action, dimension, value):
    # Never persist raw addresses or client IPs in the abuse-control ledger. Public preflight requires a
    # strong AUDIT_HMAC_KEY; the fallback keeps local development functional without pretending it is secret.
    secret = (get("AUDIT_HMAC_KEY") or "agent-os-development-rate-limit-key").encode()
    payload = f"{action}:{dimension}:{value}".encode()
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def public_rate_limit(action, email="", client_id=""):
    """Atomically admit one unauthenticated auth request or return a retry delay.

    Both the client and normalized account dimensions must remain under their own policy. This prevents one
    IP spraying many accounts and a distributed set of IPs hammering one account. The result is deliberately
    non-enumerating and contains no email/IP.
    """
    policy = PUBLIC_RATE_POLICIES.get(str(action))
    if not policy:
        raise ValueError(f"unknown public auth action: {action}")
    window_s, client_limit, account_limit = policy
    now = int(time.time())
    bucket = now // window_s
    dimensions = [("client", str(client_id or "unknown"), int(client_limit))]
    norm_email = _norm(email)
    if norm_email:
        dimensions.append(("account", norm_email, int(account_limit)))
    counts = []
    _ensure()
    with _conn() as c, c.cursor() as cur:
        for dimension, value, limit in dimensions:
            key_hash = _rate_key(action, dimension, value)
            cur.execute("""INSERT INTO auth_rate_limits(action,key_hash,bucket,attempts)
                           VALUES (%s,%s,%s,1)
                           ON CONFLICT(action,key_hash,bucket) DO UPDATE
                             SET attempts=auth_rate_limits.attempts+1, updated_at=now()
                           RETURNING attempts""", (action, key_hash, bucket))
            counts.append((int(cur.fetchone()[0]), limit))
        # Bounded housekeeping; this table never becomes an unbounded history of authentication metadata.
        cur.execute("DELETE FROM auth_rate_limits WHERE updated_at < now() - interval '2 days'")
        c.commit()
    allowed = all(count <= limit for count, limit in counts)
    return {"allowed": allowed, "retry_after_s": max(1, ((bucket + 1) * window_s) - now)}


def clear_public_rate_limit(action, email="", client_id=""):
    """Clear the current successful login/code-completion buckets without exposing their raw keys."""
    policy = PUBLIC_RATE_POLICIES.get(str(action))
    if not policy:
        return
    window_s = policy[0]
    values = [("client", str(client_id or "unknown"))]
    if _norm(email):
        values.append(("account", _norm(email)))
    with _conn() as c, c.cursor() as cur:
        for dimension, value in values:
            cur.execute("DELETE FROM auth_rate_limits WHERE action=%s AND key_hash=%s AND bucket=%s",
                        (action, _rate_key(action, dimension, value), int(time.time()) // window_s))
        c.commit()


def create_session(tenant_id, email):
    """Mint a revocable opaque browser session. Only its SHA-256 digest reaches Postgres."""
    _ensure()
    token = secrets.token_urlsafe(32)
    with _conn() as c, c.cursor() as cur:
        cur.execute("DELETE FROM auth_sessions WHERE expires_at <= now()")
        cur.execute("""INSERT INTO auth_sessions(token_hash,tenant_id,email,expires_at)
                       VALUES (%s,%s,%s,now() + (%s * interval '1 day'))""",
                    (_opaque_hash(token), tenant_id, _norm(email), max(1, SESSION_TTL_DAYS)))
        c.commit()
    return token


def tenant_for_session(token):
    """Resolve a live verified-account session, touching last_seen at most twice per day."""
    if not token:
        return None
    _ensure()
    with _conn() as c, c.cursor() as cur:
        cur.execute("""SELECT s.tenant_id, s.last_seen < now() - interval '12 hours'
                         FROM auth_sessions s JOIN accounts a
                           ON a.tenant_id=s.tenant_id AND a.email=s.email
                        WHERE s.token_hash=%s AND s.expires_at > now() AND a.verified=true
                        LIMIT 1""", (_opaque_hash(token),))
        row = cur.fetchone()
        if row and row[1]:
            cur.execute("UPDATE auth_sessions SET last_seen=now() WHERE token_hash=%s", (_opaque_hash(token),))
            c.commit()
    return row[0] if row else None


def revoke_session(token):
    if not token:
        return
    _ensure()
    with _conn() as c, c.cursor() as cur:
        cur.execute("DELETE FROM auth_sessions WHERE token_hash=%s", (_opaque_hash(token),))
        c.commit()


def revoke_tenant_sessions(tenant_id):
    _ensure()
    with _conn() as c, c.cursor() as cur:
        cur.execute("DELETE FROM auth_sessions WHERE tenant_id=%s", (tenant_id,))
        c.commit()


def _ntfy_configured():
    """Is the self-hosted ntfy 'ping my phone' channel wired (a real topic, not the placeholder)?"""
    try:
        import notify
        topic = notify.load_env().get("NTFY_TOPIC", "")
        return bool(topic) and "CHANGE-ME" not in topic
    except Exception:
        return False


def _email_configured():
    """Is a real email provider wired up? Gated like Stripe/OAuth — off by default on a self-host."""
    return bool(get("AOS_SMTP_URL") or get("SENDGRID_API_KEY"))


def _notify_configured():
    """Is ANY delivery channel wired (SMTP, SendGrid, or ntfy)? Off by default on a bare self-host, where
    we fall back to the honest on-screen dev code. Drives the onboarding 'set up notifications' nudge."""
    return _email_configured() or _ntfy_configured()


def _channels():
    """Human-readable list of the delivery channels currently wired (for status/nudge copy)."""
    ch = []
    if get("AOS_SMTP_URL"):
        ch.append("email (SMTP)")
    if get("SENDGRID_API_KEY"):
        ch.append("email (SendGrid)")
    if _ntfy_configured():
        ch.append("ntfy push")
    return ch


def notify_status():
    """Onboarding/setup surface: are notifications wired, over which channels, and how to turn them on if
    not. Honest + graceful — never a dead-end: the on-screen code fallback keeps everything working."""
    configured = _notify_configured()
    return {
        "configured": configured,
        "channels": _channels(),
        "hint": ("Notifications are on." if configured else
                 "No notification channel is set up yet, so codes and 'done' pings show on-screen only. "
                 "To also reach you when you've left the tab, set NTFY_TOPIC (self-hosted ntfy push) or "
                 "AOS_SMTP_URL / SENDGRID_API_KEY (email) in .env.local."),
    }


def _send_over_channels(email, subject, body):
    """Best-effort REAL delivery over whatever is wired (email first, then ntfy push). Returns True if ANY
    channel accepted the message; False (the bare self-host default) tells the caller to surface dev_code
    instead. Never raises."""
    frm = get("AOS_SMTP_FROM", "no-reply@agent-os.local")
    sent = False
    try:
        url = get("AOS_SMTP_URL")
        if url:
            import smtplib
            import ssl
            from email.message import EmailMessage
            from urllib.parse import urlparse
            u = urlparse(url)
            msg = EmailMessage()
            msg["Subject"], msg["From"], msg["To"] = subject, frm, email
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
            sent = True
    except Exception:
        pass
    try:
        key = get("SENDGRID_API_KEY")
        if not sent and key:
            import requests
            r = requests.post(
                "https://api.sendgrid.com/v3/mail/send",
                headers={"Authorization": f"Bearer {key}"},
                json={"personalizations": [{"to": [{"email": email}]}],
                      "from": {"email": frm},
                      "subject": subject,
                      "content": [{"type": "text/plain", "value": body}]},
                timeout=10)
            sent = r.status_code < 400
    except Exception:
        pass
    try:
        if not sent and _ntfy_configured():        # self-host push: reaches the CEO's phone even off-tab
            import notify
            sent = notify.send(body, title=subject)
    except Exception:
        pass
    return sent


def _send_code(email, code, purpose="verify"):
    """Deliver a verification/reset code over any wired channel. Returns True on delivery, else False."""
    what = "password reset" if purpose == "reset" else "verification"
    subject = f"Your agent-os {what} code"
    body = f"Your agent-os {what} code is {code}. It expires in 15 minutes."
    return _send_over_channels(email, subject, body)


def notify(title, message, email=None):
    """Public milestone notifier — "I'll ping you when it's done". Sends over any wired channel (email if an
    address is given + email is configured, otherwise ntfy push). Best-effort: returns True if delivered,
    False if nothing is wired (the caller already shows in-app state, so this is an extra, not a crutch)."""
    if email and _email_configured():
        return _send_over_channels(_norm(email), title, message)
    try:
        if _ntfy_configured():
            import notify as _n
            return _n.send(message, title=title)
    except Exception:
        return False
    return False


def _issue_code(email, purpose="verify"):
    """Generate + store a fresh code (15-min expiry, upsert so a resend supersedes the old one for this
    purpose), then try to deliver it. Returns dev_code (the plaintext code) when NO channel is configured,
    else None."""
    code = _gen_code()
    with _conn() as c, c.cursor() as cur:
        cur.execute("""INSERT INTO email_codes (email, code_hash, purpose, expires_at)
                       VALUES (%s, %s, %s, now() + interval '15 minutes')
                       ON CONFLICT (email, purpose) DO UPDATE
                         SET code_hash=EXCLUDED.code_hash, expires_at=EXCLUDED.expires_at,
                             created_at=now(), attempts=0""",   # a fresh/resent code gets a fresh attempt budget
                    (email, _code_hash(code, purpose), purpose))
        c.commit()
    _send_code(email, code, purpose)                # best-effort deliver over every wired channel (email + ntfy ping)
    # dev_code (on-screen fallback) is withheld ONLY when a real per-address EMAIL went out — the recipient
    # will read it there. An ntfy topic push is a nice extra ping to the operator's phone, NOT proof this
    # specific person received it, so on a self-host we KEEP showing the code on-screen as the source of truth.
    if _email_configured():
        return None
    what = "password-reset" if purpose == "reset" else "verification"
    print(f"[auth] email delivery not configured — {what} code for {email}: {code}", file=sys.stderr)
    return code


def signup(email, password, name=None, plan="free"):
    email = _norm(email)
    if "@" not in email or "." not in email.split("@")[-1]:
        return {"error": "enter a valid email"}
    if not password or len(password) < 8:
        return {"error": "password must be at least 8 characters"}
    _ensure()
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT 1 FROM accounts WHERE email=%s", (email,))
        if cur.fetchone():
            return {"error": "an account with that email already exists — sign in instead"}
    reg = billing.signup(name or email.split("@")[0], plan)          # creates the tenant + token
    tid = reg["tenant_id"]
    salt = os.urandom(16).hex()
    with tenant_connection(tid) as c, c.cursor() as cur:
        # account is created UNVERIFIED — the tenant token is withheld until verify_email() succeeds
        cur.execute("""INSERT INTO accounts (email, tenant_id, name, pw_salt, pw_hash, verified)
                       VALUES (%s,%s,%s,%s,%s, false)""", (email, tid, name, salt, _hash(password, salt)))
    dev_code = _issue_code(email)
    audit.append(actor="auth", action="SignUp", resource=tid, decision="pending_verification",
                 payload={"email": email}, tenant_id=tid)
    return {"pending_verification": True, "email": email, "dev_code": dev_code,
            "notify_configured": _notify_configured()}


def _tenant_token(tid):
    """Read a tenant's current api token + plan. Best-effort — a missing tenants row/col yields (None,'free')
    rather than raising, so verify/reset still return a usable dict."""
    token, plan = None, "free"
    with tenant_connection(tid) as c, c.cursor() as cur:
        try:
            cur.execute("SELECT api_token, plan FROM tenants WHERE tenant_id=%s", (tid,))
            r = cur.fetchone()
            if r:
                token, plan = r[0], (r[1] or "free")
        except Exception:
            token = None
    return token, plan


def verify_email(email, code):
    """Confirm the emailed code. On success: mark the account verified and return the normal token dict
    (same shape signup used to return) so the UI can log them straight in. On a bad/expired code: error."""
    email = _norm(email)
    _ensure()
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT code_hash, attempts FROM email_codes WHERE email=%s AND purpose='verify' AND expires_at > now()",
                    (email,))
        row = cur.fetchone()
        if not row:
            return {"error": "that code is wrong or expired — resend a new one"}
        if row[1] >= MAX_CODE_ATTEMPTS:                  # BRUTE-FORCE GUARD: burn the code, force a resend
            cur.execute("DELETE FROM email_codes WHERE email=%s AND purpose='verify'", (email,)); c.commit()
            return {"error": "too many attempts — resend a new code"}
        if not hmac.compare_digest(row[0], _code_hash(code, "verify")):
            cur.execute("UPDATE email_codes SET attempts=attempts+1 WHERE email=%s AND purpose='verify'", (email,))
            c.commit()
            return {"error": "that code is wrong or expired — resend a new one"}
        cur.execute("UPDATE accounts SET verified=true WHERE email=%s", (email,))
        cur.execute("DELETE FROM email_codes WHERE email=%s AND purpose='verify'", (email,))     # single-use
        cur.execute("SELECT tenant_id FROM accounts WHERE email=%s", (email,))
        arow = cur.fetchone()
        c.commit()
    if not arow:
        return {"error": "that code is wrong or expired — resend a new one"}
    tid = arow[0]
    token, plan = _tenant_token(tid)
    audit.append(actor="auth", action="VerifyEmail", resource=tid, decision="verified",
                 payload={"email": email}, tenant_id=tid)
    return {"tenant_id": tid, "api_token": token, "email": email, "plan": plan}


def resend_code(email):
    """(Re)issue + (re)send the verification code. No-op (still ok) for an unknown/already-verified email
    so we don't leak which addresses have pending accounts."""
    email = _norm(email)
    _ensure()
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT verified FROM accounts WHERE email=%s", (email,))
        row = cur.fetchone()
    if not row or row[0]:
        return {"ok": True, "dev_code": None}
    return {"ok": True, "dev_code": _issue_code(email)}


def issue_reset(email):
    """Start a 'Forgot password?' flow. Always returns ok (never reveal whether an email has an account) —
    but only mint + deliver a hashed one-time 'reset' code (~15 min) when the account actually exists. On a
    bare self-host with no delivery channel, `dev_code` carries the code to the on-screen UI exactly like
    signup verification does; with a channel wired, dev_code is None and the code goes to email/ntfy."""
    email = _norm(email)
    _ensure()
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT 1 FROM accounts WHERE email=%s", (email,))
        exists = cur.fetchone()
    if not exists:
        return {"ok": True, "dev_code": None}                # don't leak account existence over the wire
    audit.append(actor="auth", action="ResetRequest", resource=email, decision="issued",
                 payload={"email": email})
    return {"ok": True, "email": email, "dev_code": _issue_code(email, "reset")}


def verify_reset(email, code, new_password):
    """Complete the reset: check the one-time 'reset' code, set the new password, and — since holding the
    code proves control of the mailbox — also mark the account verified (so a never-verified person can
    still recover in one shot). Returns the normal token dict so the UI logs them straight in."""
    email = _norm(email)
    if not new_password or len(new_password) < 8:
        return {"error": "password must be at least 8 characters"}
    _ensure()
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT code_hash, attempts FROM email_codes WHERE email=%s AND purpose='reset' AND expires_at > now()",
                    (email,))
        row = cur.fetchone()
        if not row:
            return {"error": "that code is wrong or expired — resend a new one"}
        if row[1] >= MAX_CODE_ATTEMPTS:                  # BRUTE-FORCE GUARD (reset codes reset the password!)
            cur.execute("DELETE FROM email_codes WHERE email=%s AND purpose='reset'", (email,)); c.commit()
            return {"error": "too many attempts — resend a new code"}
        if not hmac.compare_digest(row[0], _code_hash(code, "reset")):
            cur.execute("UPDATE email_codes SET attempts=attempts+1 WHERE email=%s AND purpose='reset'", (email,))
            c.commit()
            return {"error": "that code is wrong or expired — resend a new one"}
        salt = os.urandom(16).hex()
        cur.execute("UPDATE accounts SET pw_salt=%s, pw_hash=%s, verified=true WHERE email=%s",
                    (salt, _hash(new_password, salt), email))
        cur.execute("DELETE FROM email_codes WHERE email=%s AND purpose='reset'", (email,))   # single-use
        cur.execute("SELECT tenant_id FROM accounts WHERE email=%s", (email,))
        arow = cur.fetchone()
        if arow:
            # A password reset is also a credential-compromise boundary: revoke every existing browser
            # session before issuing the caller a fresh one at the HTTP layer.
            cur.execute("DELETE FROM auth_sessions WHERE tenant_id=%s", (arow[0],))
        c.commit()
    if not arow:
        return {"error": "that code is wrong or expired — resend a new one"}
    tid = arow[0]
    token, plan = _tenant_token(tid)
    audit.append(actor="auth", action="ResetPassword", resource=tid, decision="reset",
                 payload={"email": email}, tenant_id=tid)
    return {"tenant_id": tid, "api_token": token, "email": email, "plan": plan}


def login(email, password):
    email = _norm(email)
    _ensure()
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT tenant_id, pw_salt, pw_hash, verified FROM accounts WHERE email=%s", (email,))
        row = cur.fetchone()
    if not row:
        _hash(password or "", "00" * 16)   # equalize timing with the wrong-password branch (valid hex salt)
        return {"error": _BAD_LOGIN}                          # do NOT reveal whether the account exists
    tid, salt, want, verified = row
    if not hmac.compare_digest(_hash(password or "", salt), want):
        return {"error": _BAD_LOGIN}
    if not verified:                                                 # never issue a token to an unverified account
        return {"error": "please verify your email first — check for the code"}
    # fetch the tenant's current api token
    try:
        import tenancy
        token = tenancy.token_for_tenant(tid) if hasattr(tenancy, "token_for_tenant") else None
    except Exception:
        token = None
    if not token:                                                    # fall back to reading the token store
        with tenant_connection(tid) as c, c.cursor() as cur:
            try:
                cur.execute("SELECT api_token FROM tenants WHERE tenant_id=%s", (tid,))
                r = cur.fetchone(); token = r[0] if r else None
            except Exception:
                token = None
    audit.append(actor="auth", action="LogIn", resource=tid, decision="ok", payload={"email": email}, tenant_id=tid)
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
        return False
    return r.get("auth_mode") in {"subscription", "default_cli"} or bool(r.get("key"))


def _selftest():
    global _send_code, _email_configured
    email = f"qa-{os.urandom(3).hex()}@example.com"
    passed = False
    # Isolate from this host's real config: force the honest on-screen dev_code path (no email provider) and
    # stub the transport (no live SMTP/ntfy network calls) so the test is deterministic — we're testing the
    # code logic, not the delivery channel.
    _saved_send, _send_code = _send_code, (lambda *a, **k: False)
    _saved_cfg, _email_configured = _email_configured, (lambda: False)
    try:
        bad = signup(email, "short")                                 # too-short password rejected
        pend = signup(email, "correct-horse-battery")                # signup -> PENDING, no token yet
        dup = signup(email, "correct-horse-battery")                 # dup rejected
        blocked = login(email, "correct-horse-battery")              # unverified account: login blocked, no token
        code = pend.get("dev_code")                                  # self-host fallback exposes the code
        wrong_code = "000000" if code != "000000" else "111111"
        badcode = verify_email(email, wrong_code)                    # wrong code rejected
        ver = verify_email(email, code)                              # right code -> normal token dict
        browser_session = create_session(ver["tenant_id"], email)
        session_live = tenant_for_session(browser_session) == ver["tenant_id"]
        good = login(email, "correct-horse-battery")                 # verified account can now log in + get a token
        wrong = login(email, "nope")                                 # wrong password rejected
        nouser = login("ghost@example.com", "whatever")
        provider_state = provider_resolved(ver["tenant_id"])         # may resolve to the configured host default
        no_tenant = provider_resolved("")                            # no tenant in context -> not gated (resolved)
        # --- password reset (Forgot password?) ---
        rst = issue_reset(email)                                     # existing account -> code issued
        rcode = rst.get("dev_code")                                  # self-host fallback exposes the reset code
        ghost_rst = issue_reset("ghost@example.com")                 # unknown email -> ok, no code (no existence leak)
        rwrong = "000000" if rcode != "000000" else "111111"
        rbad = verify_reset(email, rwrong, "brand-new-password")     # wrong code rejected, password unchanged
        rshort = verify_reset(email, rcode, "short")                 # too-short new password rejected
        old_still = login(email, "correct-horse-battery")            # neither failed attempt changed the password
        rok = verify_reset(email, rcode, "brand-new-password")       # right code -> password changed + token
        reset_revoked = tenant_for_session(browser_session) is None  # reset invalidates sessions stolen before reset
        logout_session = create_session(ver["tenant_id"], email)
        revoke_session(logout_session)
        logout_revoked = tenant_for_session(logout_session) is None
        new_login = login(email, "brand-new-password")               # new password works
        old_login = login(email, "correct-horse-battery")            # old password no longer works
        rate_client = "selftest-" + os.urandom(4).hex()
        account_limit = PUBLIC_RATE_POLICIES["login"][2]
        rate_results = [public_rate_limit("login", email, rate_client)
                        for _ in range(account_limit + 1)]
        rate_bounded = all(item["allowed"] for item in rate_results[:account_limit]) \
            and not rate_results[-1]["allowed"] and rate_results[-1]["retry_after_s"] > 0
        clear_public_rate_limit("login", email, rate_client)
        nstat = notify_status()                                      # onboarding nudge surface, honest + graceful
        passed = (bad.get("error")
                  and pend.get("pending_verification") is True and not pend.get("api_token")
                  and "notify_configured" in pend
                  and dup.get("error")
                  and "verify" in (blocked.get("error") or "") and not blocked.get("api_token")
                  and badcode.get("error") and not badcode.get("api_token")
                  and ver.get("api_token") and ver.get("tenant_id")
                  and session_live and reset_revoked and logout_revoked and rate_bounded
                  and good.get("tenant_id") == ver["tenant_id"] and good.get("api_token")
                  and wrong.get("error") == _BAD_LOGIN and nouser.get("error")
                  and isinstance(provider_state, bool) and no_tenant is True
                  and rst.get("ok") and not ghost_rst.get("dev_code")
                  and rbad.get("error") and rshort.get("error")
                  and old_still.get("api_token")
                  and rok.get("api_token") and rok.get("tenant_id") == ver["tenant_id"]
                  and new_login.get("api_token") and old_login.get("error") == _BAD_LOGIN
                  and ("configured" in nstat and "hint" in nstat))
        print(f"reject-short={bool(bad.get('error'))} signup-pending={pend.get('pending_verification') is True} "
              f"no-token-yet={not pend.get('api_token')} nudge-field={'notify_configured' in pend} "
              f"dup-blocked={bool(dup.get('error'))} "
              f"unverified-login-blocked={'verify' in (blocked.get('error') or '')} "
              f"wrong-code-rejected={bool(badcode.get('error'))} verify-token={bool(ver.get('api_token'))} "
              f"session-live={session_live} reset-revoked={reset_revoked} logout-revoked={logout_revoked} "
              f"rate-bounded={rate_bounded} "
              f"verified-login-token={bool(good.get('api_token'))} wrong-pw={wrong.get('error')==_BAD_LOGIN} "
              f"provider-gate(account={provider_state},no-tenant={no_tenant}) "
              f"reset-issued={bool(rst.get('ok'))} no-leak={not ghost_rst.get('dev_code')} "
              f"reset-badcode={bool(rbad.get('error'))} reset-shortpw={bool(rshort.get('error'))} "
              f"reset-token={bool(rok.get('api_token'))} newpw-works={bool(new_login.get('api_token'))} "
              f"oldpw-dead={old_login.get('error')==_BAD_LOGIN} notify-cfg={nstat.get('configured')}")
        print("PASS: verified accounts + rate limits + opaque sessions (signup→verify→login→reset→relogin) ✅"
              if passed else "FAIL")
    finally:
        _send_code, _email_configured = _saved_send, _saved_cfg
        with _conn() as c, c.cursor() as cur:
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
    elif a[0] == "reset" and len(a) >= 2:
        print(json.dumps(issue_reset(a[1])))
    elif a[0] == "reset-verify" and len(a) >= 4:
        r = verify_reset(a[1], a[2], a[3])
        if r.get("api_token"):
            r["api_token"] = "***"
        print(json.dumps(r))
    elif a[0] == "notify-status":
        print(json.dumps(notify_status()))
    else:
        sys.exit("usage: auth.py signup <email> <password> [name] | login <email> <password> | "
                 "verify <email> <code> | resend <email> | reset <email> | "
                 "reset-verify <email> <code> <new_password> | notify-status | selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
