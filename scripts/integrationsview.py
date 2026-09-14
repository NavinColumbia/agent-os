#!/usr/bin/env python3
"""integrationsview.py — the Integrations marketplace + per-tenant connect status (Area 9).

A factory user wires their product to the outside world here: payments (Stripe), deploy targets
(GitHub/Vercel/Netlify), infra (AWS), their own model keys (OpenAI/Anthropic/DeepSeek), comms
(Twilio/SendGrid/Resend/Slack), CRM (Salesforce), analytics (Google Analytics), monitoring (Sentry),
and fleet/IoT (Samsara). The CATALOG is static (in code); per tenant we track only connect *status*
in tenant_integrations. Secrets for api_key/byo integrations are NEVER stored in that table — they go
to the scoped vault, so an agent requests the credential by role+env instead of reading a column.

This is a data/logic module — NO web server. A surface (front door / cockpit) renders status() and
calls connect()/disconnect().

    integrationsview.py selftest
    integrationsview.py json <tenant_id>      # catalog merged with the tenant's connect status
Run with the agent-os venv python.
"""
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit   # noqa: E402
from dbpool import connection, tenant_connection  # noqa: E402

# Static marketplace. auth: oauth (OAuth handshake), api_key (paste a key), byo (bring-your-own key,
# e.g. a model provider key the user pays for directly). api_key/byo connects store the secret in vault.
CATALOG = [
    {"slug": "stripe",       "name": "Stripe",           "category": "payments",   "auth": "oauth",
     "blurb": "Take payments, subscriptions, and payouts in your product."},
    {"slug": "github",       "name": "GitHub",           "category": "deploy",     "auth": "oauth",
     "blurb": "Push the generated repo and trigger CI/CD deploys."},
    {"slug": "vercel",       "name": "Vercel",           "category": "deploy",     "auth": "api_key",
     "blurb": "Deploy your web frontend to Vercel's edge network."},
    {"slug": "netlify",      "name": "Netlify",          "category": "deploy",     "auth": "api_key",
     "blurb": "Deploy and host your static/web app on Netlify."},
    {"slug": "aws",          "name": "AWS",              "category": "infra",      "auth": "api_key",
     "blurb": "Provision compute, storage, and databases on AWS."},
    {"slug": "openai",       "name": "OpenAI",           "category": "ai",         "auth": "byo",
     "blurb": "Use your own OpenAI key for GPT model calls in your app."},
    {"slug": "anthropic",    "name": "Anthropic",        "category": "ai",         "auth": "byo",
     "blurb": "Use your own Anthropic key for Claude model calls in your app."},
    {"slug": "deepseek",     "name": "DeepSeek",         "category": "ai",         "auth": "byo",
     "blurb": "Use your own DeepSeek key for low-cost model calls in your app."},
    {"slug": "twilio",       "name": "Twilio",           "category": "comms",      "auth": "api_key",
     "blurb": "Send SMS and place voice calls from your product."},
    {"slug": "sendgrid",     "name": "SendGrid",         "category": "comms",      "auth": "api_key",
     "blurb": "Send transactional email at scale via SendGrid."},
    {"slug": "resend",       "name": "Resend",           "category": "comms",      "auth": "api_key",
     "blurb": "Send developer-friendly transactional email via Resend."},
    {"slug": "slack",        "name": "Slack",            "category": "comms",      "auth": "oauth",
     "blurb": "Post build/launch notifications to your Slack channels."},
    {"slug": "samsara",      "name": "Samsara",          "category": "comms",      "auth": "api_key",
     "blurb": "Pull fleet/IoT telemetry from Samsara devices."},
    {"slug": "salesforce",   "name": "Salesforce",       "category": "crm",        "auth": "oauth",
     "blurb": "Sync leads, contacts, and opportunities with Salesforce CRM."},
    {"slug": "google-analytics", "name": "Google Analytics", "category": "analytics", "auth": "oauth",
     "blurb": "Track product usage and funnels with Google Analytics."},
    {"slug": "sentry",       "name": "Sentry",           "category": "monitoring", "auth": "api_key",
     "blurb": "Capture errors and performance traces with Sentry."},
]
_BY_SLUG = {i["slug"]: i for i in CATALOG}
# auth modes whose connect flow expects a secret (stored in the vault, not in tenant_integrations)
_NEEDS_SECRET = {"api_key", "byo"}


def _ensure():
    with connection() as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS tenant_integrations (
            tenant_id TEXT NOT NULL, slug TEXT NOT NULL,
            status TEXT DEFAULT 'disconnected', connected_at TIMESTAMPTZ,
            meta JSONB DEFAULT '{}', PRIMARY KEY (tenant_id, slug))""")


def catalog():
    """The static marketplace list (no tenant scoping)."""
    return CATALOG


def status(tid):
    """The catalog merged with this tenant's tenant_integrations rows: every item carries
    'status' (connected/disconnected) and 'connected_at'."""
    _ensure()
    with tenant_connection(tid) as c, c.cursor() as cur:
        cur.execute("SELECT slug, status, connected_at FROM tenant_integrations WHERE tenant_id=%s", (tid,))
        rows = {s: (st, at) for s, st, at in cur.fetchall()}
    out = []
    for item in CATALOG:
        st, at = rows.get(item["slug"], ("disconnected", None))
        out.append({**item, "status": st or "disconnected",
                    "connected_at": at.isoformat() if at else None})
    return out


def connect(tid, slug, secret=None):
    """Connect an integration for a tenant. If the integration needs a secret (api_key/byo) and one is
    provided, it's stashed in the scoped vault (never in tenant_integrations). Marks status=connected."""
    if slug not in _BY_SLUG:
        return {"ok": False, "error": f"unknown integration '{slug}'"}
    item = _BY_SLUG[slug]
    _ensure()
    if item["auth"] in _NEEDS_SECRET and secret:
        import vault
        vault.put_secret(name=slug, product=f"tenant:{tid}", environment="prod",
                         allowed_roles=["builder", "factory"], value=secret)
    with tenant_connection(tid) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO tenant_integrations (tenant_id, slug, status, connected_at)
                       VALUES (%s,%s,'connected', now())
                       ON CONFLICT (tenant_id, slug)
                       DO UPDATE SET status='connected', connected_at=now()""", (tid, slug))
    audit.append(actor=f"tenant:{tid}", action="IntegrationConnected", resource=slug,
                 decision="connected", payload={"category": item["category"], "auth": item["auth"]},
                 tenant_id=tid)
    return {"ok": True, "slug": slug, "status": "connected"}


def disconnect(tid, slug):
    """Disconnect an integration (status=disconnected). The vault secret, if any, is left in place but
    the integration no longer reports connected."""
    if slug not in _BY_SLUG:
        return {"ok": False, "error": f"unknown integration '{slug}'"}
    _ensure()
    with tenant_connection(tid) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO tenant_integrations (tenant_id, slug, status, connected_at)
                       VALUES (%s,%s,'disconnected', NULL)
                       ON CONFLICT (tenant_id, slug)
                       DO UPDATE SET status='disconnected', connected_at=NULL""", (tid, slug))
    audit.append(actor=f"tenant:{tid}", action="IntegrationDisconnected", resource=slug,
                 decision="disconnected", tenant_id=tid)
    return {"ok": True, "slug": slug, "status": "disconnected"}


def _selftest():
    import billing
    tid = billing.signup("integ-selftest", "free")["tenant_id"]
    try:
        cat = catalog()
        cat_ok = len(cat) >= 12
        # all disconnected for a fresh tenant
        s0 = status(tid)
        all_disc = all(i["status"] == "disconnected" for i in s0)
        # connect stripe -> flips to connected and status reflects it
        r = connect(tid, "stripe")
        s1 = {i["slug"]: i for i in status(tid)}
        connected = (r.get("ok") and r.get("status") == "connected"
                     and s1["stripe"]["status"] == "connected"
                     and s1["stripe"]["connected_at"] is not None)
        # disconnect -> flips back
        d = disconnect(tid, "stripe")
        s2 = {i["slug"]: i for i in status(tid)}
        disconnected = (d.get("ok") and s2["stripe"]["status"] == "disconnected"
                        and s2["stripe"]["connected_at"] is None)
        ok = cat_ok and all_disc and connected and disconnected
        print(f"catalog={len(cat)} all-disconnected={all_disc} "
              f"connect->{s1['stripe']['status']} disconnect->{s2['stripe']['status']}")
        print("PASS: integrations catalog + per-tenant connect/disconnect status ✅" if ok else "FAIL")
    finally:
        with connection() as c, c.cursor() as cur:
            cur.execute("DELETE FROM tenant_integrations WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,))
    sys.exit(0 if ok else 1)


def _main(a):
    import json
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "json" and len(a) > 1:
        print(json.dumps(status(a[1]), indent=2))
    else:
        sys.exit("usage: integrationsview.py json <tenant_id> | selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
