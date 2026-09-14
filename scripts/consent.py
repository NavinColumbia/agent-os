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

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import aoscfg  # noqa: E402
import audit   # noqa: E402
from dbpool import connection, tenant_connection  # noqa: E402

DB = aoscfg.DB  # compatibility for older tests/tools; runtime paths use dbpool.

PROVIDER = "OpenAI"                     # the DEFAULT named provider (platform Codex/OpenAI path)
DISCLOSURE_VERSION = "2026-06-v1"       # bump when the disclosure text below materially changes

# The disclosure must NAME the provider the tenant's data is ACTUALLY sent to (Apple 5.1.2(i) / Play AI /
# EU AI Act Art. 50 — a generic or WRONG provider name fails the requirement). A Codex/OpenAI-routed tenant
# must consent to OpenAI, not Anthropic. Map the resolved engine/provider to its legal display name here.
PROVIDER_NAMES = {"claude": "Anthropic Claude", "anthropic": "Anthropic Claude", "opus": "Anthropic Claude",
                  "codex": "OpenAI", "openai": "OpenAI", "gpt": "OpenAI"}


def provider_name(engine_or_name):
    """Resolve an engine id / provider string to the NAMED legal provider for the disclosure. Unknown ->
    the configured platform default, so a resolution gap fails safe to the platform default rather than a
    blank name."""
    if not engine_or_name:
        return PROVIDER
    key = str(engine_or_name).strip().lower()
    return PROVIDER_NAMES.get(key) or (engine_or_name if engine_or_name in PROVIDER_NAMES.values() else PROVIDER)


def disclosure_for(provider=PROVIDER):
    """The named-provider disclosure text for the SPECIFIC provider the tenant's data goes to."""
    return (
        f"To build your product, agent-os sends the text you provide (your product description and the code "
        f"generated for you) to {provider}, a third-party AI provider, for processing. Your prompts are not "
        f"used to train their models on enterprise/API traffic. You can revoke this consent at any time in "
        f"Settings; revoking disables AI builds. By accepting you consent to this processing.")


DISCLOSURE = disclosure_for(PROVIDER)   # back-compat: the default-provider text


def for_tenant(tenant_id):
    """The named provider a tenant's data will ACTUALLY go to — resolved from their connected provider
    (engine), defaulting to the platform default when none is connected yet. This is what makes consent
    correct per-tenant: a Codex tenant consents to OpenAI, an Anthropic tenant to Anthropic."""
    try:
        import tenantproviders
        r = tenantproviders.resolve(tenant_id) or {}
        return provider_name(r.get("engine"))
    except Exception:
        return PROVIDER


def _resolve(tenant_id, provider):
    """provider=None -> resolve the tenant's actual provider; else normalize the given one. So every gate
    caller that passes nothing automatically checks/records consent for the RIGHT provider."""
    return provider_name(provider) if provider is not None else for_tenant(tenant_id)


def _ensure():
    with connection() as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS ai_consent (
            id BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL, provider TEXT NOT NULL,
            disclosure_version TEXT NOT NULL, accepted_at TIMESTAMPTZ, revoked_at TIMESTAMPTZ,
            UNIQUE (tenant_id, provider, disclosure_version))""")


def require_consent(tenant_id, provider=None):
    """True iff valid (accepted, not revoked) consent for the CURRENT disclosure version is on file.
    A gate calls this and refuses the AI action when it returns False."""
    provider = _resolve(tenant_id, provider)
    _ensure()
    with tenant_connection(tenant_id) as c, c.cursor() as cur:
        cur.execute("""SELECT accepted_at, revoked_at FROM ai_consent
                       WHERE tenant_id=%s AND provider=%s AND disclosure_version=%s""",
                    (tenant_id, provider, DISCLOSURE_VERSION))
        row = cur.fetchone()
    return bool(row and row[0] and not row[1])


def record(tenant_id, provider=None):
    provider = _resolve(tenant_id, provider)
    _ensure()
    with tenant_connection(tenant_id) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO ai_consent (tenant_id, provider, disclosure_version, accepted_at)
                       VALUES (%s,%s,%s, now())
                       ON CONFLICT (tenant_id, provider, disclosure_version)
                       DO UPDATE SET accepted_at=now(), revoked_at=NULL""",
                    (tenant_id, provider, DISCLOSURE_VERSION))
    audit.append(actor="consent", action="ConsentAccepted", resource=tenant_id, decision="accepted",
                 payload={"provider": provider, "version": DISCLOSURE_VERSION}, tenant_id=tenant_id)
    return True


def revoke(tenant_id, provider=None):
    provider = _resolve(tenant_id, provider)
    _ensure()
    with tenant_connection(tenant_id) as c, c.cursor() as cur:
        cur.execute("""UPDATE ai_consent SET revoked_at=now()
                       WHERE tenant_id=%s AND provider=%s AND disclosure_version=%s""",
                    (tenant_id, provider, DISCLOSURE_VERSION))
    audit.append(actor="consent", action="ConsentRevoked", resource=tenant_id, decision="revoked",
                 payload={"provider": provider, "version": DISCLOSURE_VERSION}, tenant_id=tenant_id)
    return True


def state(tenant_id, provider=None):
    """What a consent screen needs to render: whether it's required + the named disclosure FOR THAT provider."""
    provider = _resolve(tenant_id, provider)
    accepted = require_consent(tenant_id, provider)
    return {"required": not accepted, "accepted": accepted,
            "provider": provider, "version": DISCLOSURE_VERSION, "disclosure": disclosure_for(provider)}


def _selftest():
    tid = "t-consent-" + __import__("os").urandom(3).hex()
    before = require_consent(tid)                 # no record -> must be False (build blocked)
    record(tid)
    after = require_consent(tid)                   # accepted -> True (build allowed)
    revoke(tid)
    revoked = require_consent(tid)                 # revoked -> False again
    with connection() as c, c.cursor() as cur:
        cur.execute("DELETE FROM ai_consent WHERE tenant_id=%s", (tid,))
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
