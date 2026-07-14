#!/usr/bin/env python3
"""consent.py — the mandatory AI-consent gate (a hard publish-readiness requirement).

Before any tenant data is sent to a third-party AI provider, the user must give NAMED, explicit, revocable
consent — required by Apple Guideline 5.1.2(i) (eff. 2025-11-13), Google Play's AI policy, and EU AI Act
Art. 50 (applies 2026-08-02). A generic "uses AI" line is not enough; the provider must be named. This
module is that ledger + gate: require_consent() returns False until the current disclosure is accepted,
so the front door can block builds, and revoke() lets a user withdraw it. Re-consent is forced when
DISCLOSURE_VERSION changes (the disclosure text materially changed).

    consent.py status <tenant>          # is consent on file for the current disclosure?
    consent.py accept <tenant>
    consent.py revoke <tenant>
    consent.py disclosure                # print the current named-provider disclosure text
    consent.py selftest
Run with the agent-os venv python.
"""
import sys
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit   # noqa: E402

from aoscfg import ENV, DB

PROVIDER = "Anthropic Claude"           # the named third-party model provider data is sent to
DISCLOSURE_VERSION = "2026-06-v1"       # bump when the disclosure text below materially changes
DISCLOSURE = (
    f"To build your product, agent-os sends the text you provide (your product description and the code "
    f"generated for you) to {PROVIDER}, a third-party AI provider, for processing. Your prompts are not "
    f"used to train their models on enterprise/API traffic. You can revoke this consent at any time in "
    f"Settings; revoking disables AI builds. By accepting you consent to this processing."
)


def _ensure():
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS ai_consent (
            id BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL, provider TEXT NOT NULL,
            disclosure_version TEXT NOT NULL, accepted_at TIMESTAMPTZ, revoked_at TIMESTAMPTZ,
            UNIQUE (tenant_id, provider, disclosure_version))""")
        c.commit()


def require_consent(tenant_id, provider=PROVIDER):
    """True iff valid (accepted, not revoked) consent for the CURRENT disclosure version is on file.
    A gate calls this and refuses the AI action when it returns False."""
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT accepted_at, revoked_at FROM ai_consent
                       WHERE tenant_id=%s AND provider=%s AND disclosure_version=%s""",
                    (tenant_id, provider, DISCLOSURE_VERSION))
        row = cur.fetchone()
    return bool(row and row[0] and not row[1])


def record(tenant_id, provider=PROVIDER):
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO ai_consent (tenant_id, provider, disclosure_version, accepted_at)
                       VALUES (%s,%s,%s, now())
                       ON CONFLICT (tenant_id, provider, disclosure_version)
                       DO UPDATE SET accepted_at=now(), revoked_at=NULL""",
                    (tenant_id, provider, DISCLOSURE_VERSION))
        c.commit()
    audit.append(actor="consent", action="ConsentAccepted", resource=tenant_id, decision="accepted",
                 payload={"provider": provider, "version": DISCLOSURE_VERSION})
    return True


def revoke(tenant_id, provider=PROVIDER):
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""UPDATE ai_consent SET revoked_at=now()
                       WHERE tenant_id=%s AND provider=%s AND disclosure_version=%s""",
                    (tenant_id, provider, DISCLOSURE_VERSION))
        c.commit()
    audit.append(actor="consent", action="ConsentRevoked", resource=tenant_id, decision="revoked",
                 payload={"provider": provider, "version": DISCLOSURE_VERSION})
    return True


def state(tenant_id, provider=PROVIDER):
    """What a consent screen needs to render: whether it's required + the named disclosure."""
    return {"required": not require_consent(tenant_id, provider), "accepted": require_consent(tenant_id, provider),
            "provider": provider, "version": DISCLOSURE_VERSION, "disclosure": DISCLOSURE}


def _selftest():
    tid = "t-consent-" + __import__("os").urandom(3).hex()
    before = require_consent(tid)                 # no record -> must be False (build blocked)
    record(tid)
    after = require_consent(tid)                   # accepted -> True (build allowed)
    revoke(tid)
    revoked = require_consent(tid)                 # revoked -> False again
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("DELETE FROM ai_consent WHERE tenant_id=%s", (tid,)); c.commit()
    named = PROVIDER in DISCLOSURE                 # disclosure must NAME the provider (generic = rejected)
    ok = (not before) and after and (not revoked) and named
    print(f"pre={before} accepted={after} post-revoke={revoked} provider-named={named}")
    print("PASS: consent gate blocks pre-consent, allows after, re-blocks on revoke ✅" if ok else "FAIL")
    sys.exit(0 if ok else 1)


def _main(a):
    import json
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "disclosure":
        print(f"[{PROVIDER} · {DISCLOSURE_VERSION}]\n{DISCLOSURE}")
    elif a[0] == "status" and len(a) > 1:
        print(json.dumps(state(a[1]), indent=2))
    elif a[0] == "accept" and len(a) > 1:
        print(json.dumps({"accepted": record(a[1])}))
    elif a[0] == "revoke" and len(a) > 1:
        print(json.dumps({"revoked": revoke(a[1])}))
    else:
        sys.exit("usage: consent.py status <tenant> | accept <tenant> | revoke <tenant> | disclosure | selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
