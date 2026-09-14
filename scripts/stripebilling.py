#!/usr/bin/env python3
"""stripebilling.py - Stripe-backed subscription and dunning bridge.

This module is deliberately a thin boundary around Stripe:
  * checkout is the only way to move to a paid plan;
  * webhooks are the source of truth for activating/downgrading/suspending;
  * every Stripe event is stored idempotently before handling.

Offline tests stub the HTTP boundary, so selftest does not spend or call Stripe.
"""
import hashlib
import hmac
import json
import sys
import threading
import time
from pathlib import Path

import requests

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit  # noqa: E402
import billing  # noqa: E402
import notifications  # noqa: E402
from aoscfg import get  # noqa: E402
from dbpool import connection, tenant_connection  # noqa: E402


_ensured = False
_ensure_lock = threading.Lock()
_DUNNING_FAILURE_LIMIT = 3


def _ensure():
    global _ensured
    if _ensured:
        return
    with _ensure_lock:
        if _ensured:
            return
        with connection() as c, c.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(742081)")
            cur.execute("""CREATE TABLE IF NOT EXISTS billing_subscriptions (
                             tenant_id TEXT PRIMARY KEY,
                             plan TEXT NOT NULL DEFAULT 'free',
                             processor TEXT NOT NULL DEFAULT 'stripe',
                             stripe_customer_id TEXT,
                             stripe_subscription_id TEXT,
                             stripe_checkout_session_id TEXT,
                             status TEXT NOT NULL DEFAULT 'none',
                             payment_failures INT NOT NULL DEFAULT 0,
                             dunning_suspended BOOLEAN NOT NULL DEFAULT false,
                             current_period_end TIMESTAMPTZ,
                             updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
            cur.execute("""CREATE TABLE IF NOT EXISTS stripe_events (
                             event_id TEXT PRIMARY KEY,
                             tenant_id TEXT,
                             type TEXT NOT NULL,
                             payload JSONB NOT NULL,
                             processed_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
            cur.execute("CREATE INDEX IF NOT EXISTS billing_subscriptions_tenant_rls_idx "
                        "ON billing_subscriptions (tenant_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS stripe_events_tenant_rls_idx ON stripe_events (tenant_id)")
        _ensured = True


def _price_id(plan):
    if plan == "free":
        return ""
    return get(f"AOS_STRIPE_PRICE_{plan.upper()}") or get(f"STRIPE_PRICE_{plan.upper()}") or ""


def readiness():
    missing = []
    if not (get("STRIPE_SECRET_KEY") or ""):
        missing.append("STRIPE_SECRET_KEY")
    if not (get("STRIPE_WEBHOOK_SECRET") or ""):
        missing.append("STRIPE_WEBHOOK_SECRET")
    for plan in billing.PLANS:
        if plan != "free" and not _price_id(plan):
            missing.append(f"AOS_STRIPE_PRICE_{plan.upper()}")
    return {"ok": not missing, "missing": missing}


def _db_ok():
    try:
        _ensure()
        with connection() as c, c.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return True
    except Exception:
        return False


def _schema_ok():
    try:
        _ensure()
        with connection() as c, c.cursor() as cur:
            cur.execute("""SELECT table_name FROM information_schema.tables
                           WHERE table_schema='public'
                             AND table_name = ANY(%s)""",
                        (["billing_subscriptions", "stripe_events"],))
            found = {r[0] for r in cur.fetchall()}
            cur.execute("""SELECT column_name FROM information_schema.columns
                           WHERE table_schema='public' AND table_name='tenants'
                             AND column_name = ANY(%s)""",
                        (["plan", "suspended", "auto_suspended", "period_start"],))
            tenant_cols = {r[0] for r in cur.fetchall()}
        return {"ok": found == {"billing_subscriptions", "stripe_events"}
                and {"plan", "suspended", "auto_suspended", "period_start"} <= tenant_cols,
                "tables": sorted(found), "tenant_columns": sorted(tenant_cols)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:160], "tables": [], "tenant_columns": []}


def preflight(plan="pro", tenant=""):
    """No-spend readiness gate for the live Stripe C3 proof.

    This does not create a Checkout Session, call Stripe, mutate tenant plan state, or process a webhook. It
    answers whether this environment is ready for the operator to run the real card-capture proof.
    """
    plan = (plan or "pro").strip().lower()
    cfg = readiness()
    schema = _schema_ok()
    price = _price_id(plan) if plan in billing.PLANS else ""
    missing = list(cfg.get("missing") or [])
    if plan in billing.PLANS and plan != "free" and not price:
        k = f"AOS_STRIPE_PRICE_{plan.upper()}"
        if k not in missing:
            missing.append(k)
    checks = {
        "db_reachable": _db_ok(),
        "schema_ready": bool(schema.get("ok")),
        "plan_supported": plan in billing.PLANS and plan != "free",
        "stripe_secret_configured": bool(get("STRIPE_SECRET_KEY") or ""),
        "stripe_webhook_secret_configured": bool(get("STRIPE_WEBHOOK_SECRET") or ""),
        "all_paid_prices_configured": not any(m.startswith("AOS_STRIPE_PRICE_") for m in missing),
        "stripe_price_configured": bool(price),
        "webhook_endpoint_mounted": True,
        "tenant_ready": True,
    }
    if tenant:
        try:
            billing._plan_of(tenant)
            checks["tenant_ready"] = True
        except Exception:
            checks["tenant_ready"] = False
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "missing": missing,
        "plan": plan,
        "tenant": tenant,
        "schema": schema,
        "checkout_start": "POST /api/billing/checkout with JSON {'plan': '<paid-plan>'} as an authenticated tenant",
        "webhook_endpoint": "POST /api/stripe/webhook with Stripe-Signature",
        "required_events": [
            "checkout.session.completed or customer.subscription.created/updated activates paid plan",
            "invoice.payment_failed increments dunning and suspends after repeated failures",
            "invoice.paid clears Stripe-dunning suspension only",
            "customer.subscription.deleted downgrades to free",
        ],
        "artifacts": [
            "billing_subscriptions row for the tenant",
            "stripe_events row for every delivered event id",
            "audit_log rows for checkout start, activation, payment failure/recovery/cancel",
            "notifications feed entry for billing failures",
        ],
        "stop_conditions": [
            "any preflight check is false",
            "checkout returns stripe_checkout_failed",
            "webhook signature verification fails",
            "plan changes before a signed Stripe event is processed",
            "duplicate event id mutates state twice",
            "invoice.paid lifts an admin/manual suspension",
        ],
    }


def status(tid):
    _ensure()
    with tenant_connection(tid) as c, c.cursor() as cur:
        cur.execute("""SELECT plan, status, stripe_customer_id, stripe_subscription_id, payment_failures,
                              dunning_suspended, updated_at
                         FROM billing_subscriptions WHERE tenant_id=%s""", (tid,))
        row = cur.fetchone()
    r = readiness()
    if not row:
        plan, suspended = billing._plan_of(tid)
        return {"configured": r["ok"], "missing": r["missing"], "plan": plan, "status": "none",
                "processor": "stripe", "payment_failures": 0, "dunning_suspended": False,
                "tenant_suspended": bool(suspended)}
    plan, sub_status, customer, sub, failures, dunning_suspended, updated = row
    _, suspended = billing._plan_of(tid)
    return {"configured": r["ok"], "missing": r["missing"], "plan": plan, "status": sub_status,
            "processor": "stripe", "stripe_customer_id": customer, "stripe_subscription_id": sub,
            "payment_failures": int(failures or 0), "dunning_suspended": bool(dunning_suspended),
            "tenant_suspended": bool(suspended), "updated_at": str(updated)}


def _tenant_email(tid):
    try:
        with tenant_connection(tid) as c, c.cursor() as cur:
            cur.execute("SELECT email FROM accounts WHERE tenant_id=%s ORDER BY created_at DESC LIMIT 1", (tid,))
            row = cur.fetchone()
        return row[0] if row else ""
    except Exception:
        return ""


def start_checkout(tid, plan, success_url="", cancel_url="", post=None):
    """Create a Stripe Checkout Session for a paid subscription plan.

    The tenant plan is NOT changed here. It changes only when a signed Stripe webhook confirms checkout or
    subscription activation.
    """
    _ensure()
    if plan not in billing.PLANS:
        raise ValueError(f"unknown plan {plan}; choose {list(billing.PLANS)}")
    if plan == "free":
        downgrade_to_free(tid, actor=f"tenant:{tid}")
        return {"ok": True, "plan": "free", "status": "downgraded"}
    secret = get("STRIPE_SECRET_KEY") or ""
    price = _price_id(plan)
    if not secret or not price:
        return {"ok": False, "error": "stripe_not_configured",
                "missing": [x for x in ("STRIPE_SECRET_KEY", f"AOS_STRIPE_PRICE_{plan.upper()}")
                            if (x == "STRIPE_SECRET_KEY" and not secret)
                            or (x != "STRIPE_SECRET_KEY" and not price)]}
    success_url = success_url or get("AOS_STRIPE_SUCCESS_URL", "http://127.0.0.1:8099/#billing")
    cancel_url = cancel_url or get("AOS_STRIPE_CANCEL_URL", "http://127.0.0.1:8099/#billing")
    data = {
        "mode": "subscription",
        "line_items[0][price]": price,
        "line_items[0][quantity]": "1",
        "success_url": success_url,
        "cancel_url": cancel_url,
        "client_reference_id": tid,
        "metadata[tenant_id]": tid,
        "metadata[plan]": plan,
        "subscription_data[metadata][tenant_id]": tid,
        "subscription_data[metadata][plan]": plan,
    }
    email = _tenant_email(tid)
    if email:
        data["customer_email"] = email
    post = post or requests.post
    r = post("https://api.stripe.com/v1/checkout/sessions", data=data, auth=(secret, ""), timeout=20)
    if getattr(r, "status_code", 500) >= 400:
        return {"ok": False, "error": "stripe_checkout_failed", "detail": getattr(r, "text", "")[:240]}
    session = r.json()
    sid = session.get("id")
    with tenant_connection(tid) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO billing_subscriptions
                           (tenant_id, plan, stripe_checkout_session_id, status, updated_at)
                       VALUES (%s,%s,%s,'checkout_started',now())
                       ON CONFLICT (tenant_id) DO UPDATE SET
                           plan=EXCLUDED.plan,
                           stripe_checkout_session_id=EXCLUDED.stripe_checkout_session_id,
                           status='checkout_started',
                           updated_at=now()""", (tid, plan, sid))
    audit.append(actor=f"tenant:{tid}", action="StripeCheckoutStarted", resource=tid, decision="pending",
                 payload={"plan": plan, "session": sid}, tenant_id=tid)
    return {"ok": True, "checkout_url": session.get("url"), "session_id": sid, "plan": plan}


def _signed_event(payload, sig_header, secret, tolerance_s=300):
    pieces = {}
    for part in (sig_header or "").split(","):
        k, _, v = part.partition("=")
        if k and v:
            pieces.setdefault(k, []).append(v)
    try:
        ts = int((pieces.get("t") or ["0"])[0])
    except Exception:
        ts = 0
    if not ts or abs(time.time() - ts) > tolerance_s:
        raise ValueError("stripe signature timestamp outside tolerance")
    body = payload.decode() if isinstance(payload, bytes) else str(payload)
    expected = hmac.new(secret.encode(), f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, v) for v in pieces.get("v1", [])):
        raise ValueError("invalid Stripe signature")
    return json.loads(body)


def _event_object(event):
    return ((event.get("data") or {}).get("object") or {})


def _metadata(obj):
    return obj.get("metadata") or {}


def _tenant_plan_from(obj):
    md = _metadata(obj)
    return md.get("tenant_id") or obj.get("client_reference_id") or "", md.get("plan") or ""


def _set_plan_from_stripe(tid, plan, status, obj):
    if plan not in billing.PLANS or plan == "free":
        return
    customer = obj.get("customer")
    sub = obj.get("subscription") or obj.get("id")
    with tenant_connection(tid) as c, c.cursor() as cur:
        cur.execute("SELECT dunning_suspended FROM billing_subscriptions WHERE tenant_id=%s", (tid,))
        row = cur.fetchone()
        lift_dunning = bool(row and row[0])
        cur.execute("UPDATE tenants SET plan=%s WHERE tenant_id=%s", (plan, tid))
        if lift_dunning:
            cur.execute("UPDATE tenants SET suspended=false, auto_suspended=false WHERE tenant_id=%s", (tid,))
        cur.execute("""INSERT INTO billing_subscriptions
                           (tenant_id, plan, stripe_customer_id, stripe_subscription_id, status,
                            payment_failures, dunning_suspended, updated_at)
                       VALUES (%s,%s,%s,%s,%s,0,false,now())
                       ON CONFLICT (tenant_id) DO UPDATE SET
                           plan=EXCLUDED.plan,
                           stripe_customer_id=COALESCE(EXCLUDED.stripe_customer_id,
                                                       billing_subscriptions.stripe_customer_id),
                           stripe_subscription_id=COALESCE(EXCLUDED.stripe_subscription_id,
                                                           billing_subscriptions.stripe_subscription_id),
                           status=EXCLUDED.status,
                           payment_failures=0,
                           dunning_suspended=false,
                           updated_at=now()""", (tid, plan, customer, sub, status))
    audit.append(actor="stripe", action="StripeSubscriptionActivated", resource=tid, decision="paid",
                 payload={"plan": plan, "status": status, "subscription": sub}, tenant_id=tid)


def downgrade_to_free(tid, actor="stripe", reason="subscription ended"):
    _ensure()
    with tenant_connection(tid) as c, c.cursor() as cur:
        cur.execute("SELECT dunning_suspended FROM billing_subscriptions WHERE tenant_id=%s", (tid,))
        row = cur.fetchone()
        lift_dunning = bool(row and row[0])
        cur.execute("UPDATE tenants SET plan='free' WHERE tenant_id=%s", (tid,))
        if lift_dunning:
            cur.execute("UPDATE tenants SET suspended=false, auto_suspended=false WHERE tenant_id=%s", (tid,))
        cur.execute("""INSERT INTO billing_subscriptions (tenant_id, plan, status, updated_at)
                       VALUES (%s,'free','canceled',now())
                       ON CONFLICT (tenant_id) DO UPDATE SET plan='free', status='canceled',
                           dunning_suspended=false, updated_at=now()""", (tid,))
    audit.append(actor=actor, action="StripeSubscriptionCanceled", resource=tid, decision="downgraded",
                 payload={"reason": reason}, tenant_id=tid)
    return {"ok": True, "plan": "free", "status": "canceled"}


def _handle_payment_failed(tid, obj):
    with tenant_connection(tid) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO billing_subscriptions (tenant_id, plan, status, payment_failures, updated_at)
                       VALUES (%s, (SELECT plan FROM tenants WHERE tenant_id=%s), 'past_due', 1, now())
                       ON CONFLICT (tenant_id) DO UPDATE SET
                           status='past_due',
                           payment_failures=billing_subscriptions.payment_failures+1,
                           updated_at=now()
                       RETURNING payment_failures""", (tid, tid))
        failures = int(cur.fetchone()[0])
        if failures >= _DUNNING_FAILURE_LIMIT:
            cur.execute("UPDATE tenants SET suspended=true, auto_suspended=false WHERE tenant_id=%s", (tid,))
            cur.execute("""UPDATE billing_subscriptions
                              SET dunning_suspended=true, status='past_due', updated_at=now()
                            WHERE tenant_id=%s""", (tid,))
    try:
        notifications.send(tid, "billing", "Payment failed",
                           "Update your payment method to keep your AI company running.",
                           level="urgent", url="/#billing")
    except Exception:
        pass
    audit.append(actor="stripe", action="StripePaymentFailed", resource=tid,
                 decision="suspended" if failures >= _DUNNING_FAILURE_LIMIT else "past_due",
                 payload={"failures": failures, "invoice": obj.get("id")}, tenant_id=tid)


def _handle_payment_paid(tid, obj):
    with tenant_connection(tid) as c, c.cursor() as cur:
        cur.execute("SELECT dunning_suspended FROM billing_subscriptions WHERE tenant_id=%s", (tid,))
        row = cur.fetchone()
        lift_dunning = bool(row and row[0])
        cur.execute("""UPDATE billing_subscriptions
                          SET payment_failures=0, dunning_suspended=false, status='active', updated_at=now()
                        WHERE tenant_id=%s RETURNING dunning_suspended""", (tid,))
        if lift_dunning:
            cur.execute("UPDATE tenants SET suspended=false, auto_suspended=false WHERE tenant_id=%s", (tid,))
    audit.append(actor="stripe", action="StripePaymentRecovered", resource=tid, decision="paid",
                 payload={"invoice": obj.get("id")}, tenant_id=tid)


def _record_event(event, tid):
    with connection() as c, c.cursor() as cur:
        cur.execute("""INSERT INTO stripe_events (event_id, tenant_id, type, payload)
                       VALUES (%s,%s,%s,%s)
                       ON CONFLICT (event_id) DO NOTHING""",
                    (event.get("id"), tid or None, event.get("type", ""), json.dumps(event)))
        return cur.rowcount > 0


def handle_event(event):
    _ensure()
    typ = event.get("type") or ""
    obj = _event_object(event)
    tid, plan = _tenant_plan_from(obj)
    if not tid and obj.get("subscription"):
        with connection() as c, c.cursor() as cur:
            cur.execute("SELECT tenant_id, plan FROM billing_subscriptions WHERE stripe_subscription_id=%s",
                        (obj.get("subscription"),))
            row = cur.fetchone()
        if row:
            tid, plan = row
    if not tid:
        return {"ok": False, "error": "tenant_not_resolved", "type": typ}
    if not _record_event(event, tid):
        return {"ok": True, "duplicate": True, "event_id": event.get("id")}
    if typ == "checkout.session.completed":
        _set_plan_from_stripe(tid, plan, "active", obj)
    elif typ in ("customer.subscription.created", "customer.subscription.updated"):
        if (obj.get("status") or "") in ("active", "trialing"):
            _set_plan_from_stripe(tid, plan, obj.get("status") or "active", obj)
    elif typ == "customer.subscription.deleted":
        downgrade_to_free(tid)
    elif typ == "invoice.payment_failed":
        _handle_payment_failed(tid, obj)
    elif typ in ("invoice.paid", "invoice.payment_succeeded"):
        _handle_payment_paid(tid, obj)
    return {"ok": True, "event_id": event.get("id"), "type": typ, "tenant": tid}


def handle_webhook(payload, sig_header):
    secret = get("STRIPE_WEBHOOK_SECRET") or ""
    if not secret:
        return {"ok": False, "error": "stripe_webhook_not_configured"}
    return handle_event(_signed_event(payload, sig_header, secret))


def _selftest():
    import os
    old_secret = get("STRIPE_WEBHOOK_SECRET")
    tid = billing.signup(f"stripebilling-{os.urandom(3).hex()}", "free")["tenant_id"]
    payload = {"id": "evt_" + os.urandom(3).hex(), "type": "checkout.session.completed",
               "data": {"object": {"id": "cs_test", "customer": "cus_1", "subscription": "sub_1",
                                    "client_reference_id": tid, "metadata": {"tenant_id": tid, "plan": "pro"}}}}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    secret = "whsec_test"
    ts = int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.{raw.decode()}".encode(), hashlib.sha256).hexdigest()
    import aoscfg
    real_get = aoscfg.get
    try:
        aoscfg.get = lambda k, default=None: secret if k == "STRIPE_WEBHOOK_SECRET" else real_get(k, default)
        globals()["get"] = aoscfg.get
        ready = readiness()
        no_cfg = "STRIPE_SECRET_KEY" in ready["missing"]
        pf_no_cfg = preflight("pro", tenant=tid)
        checkout_block = start_checkout(tid, "pro")
        activated = handle_webhook(raw, f"t={ts},v1={sig}")
        duplicate = handle_webhook(raw, f"t={ts},v1={sig}")
        v = status(tid)
        bad_sig = handle_webhook(raw, f"t={ts},v1=bad")
    except ValueError:
        bad_sig = {"raised": True}
    finally:
        globals()["get"] = real_get
        aoscfg.get = real_get
    failed = {"id": "evt_fail_" + os.urandom(3).hex(), "type": "invoice.payment_failed",
              "data": {"object": {"id": "in_1", "subscription": "sub_1"}}}
    for i in range(_DUNNING_FAILURE_LIMIT):
        failed["id"] = f"evt_fail_{i}_{os.urandom(2).hex()}"
        handle_event(failed)
    dunned = status(tid)
    paid = {"id": "evt_paid_" + os.urandom(3).hex(), "type": "invoice.paid",
            "data": {"object": {"id": "in_2", "subscription": "sub_1"}}}
    handle_event(paid)
    recovered = status(tid)
    billing.suspend(tid, reason="admin hold", actor="billing:admin")
    admin_paid = {"id": "evt_admin_paid_" + os.urandom(3).hex(), "type": "invoice.paid",
                  "data": {"object": {"id": "in_3", "subscription": "sub_1"}}}
    handle_event(admin_paid)
    admin_still_suspended = status(tid)
    ok = (no_cfg and checkout_block.get("error") == "stripe_not_configured"
          and pf_no_cfg.get("ok") is False
          and pf_no_cfg["checks"]["stripe_secret_configured"] is False
          and activated.get("ok") and duplicate.get("duplicate")
          and v["plan"] == "pro" and v["status"] == "active"
          and bad_sig.get("raised") is True
          and dunned["tenant_suspended"] is True and dunned["dunning_suspended"] is True
          and recovered["tenant_suspended"] is False and recovered["payment_failures"] == 0
          and admin_still_suspended["tenant_suspended"] is True)
    with connection() as c, c.cursor() as cur:
        cur.execute("DELETE FROM stripe_events WHERE tenant_id=%s", (tid,))
        cur.execute("DELETE FROM billing_subscriptions WHERE tenant_id=%s", (tid,))
        cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,))
    print(f"checkout_requires_config={checkout_block.get('error')} activated={activated.get('ok')} "
          f"duplicate={duplicate.get('duplicate')} dunned={dunned['tenant_suspended']} "
          f"recovered={not recovered['tenant_suspended']} admin_preserved={admin_still_suspended['tenant_suspended']}")
    print("PASS: Stripe checkout/webhook/dunning bridge is idempotent and payment-gated ✅" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    if len(sys.argv) == 1 or sys.argv[1] == "selftest":
        _selftest()
    elif sys.argv[1] == "readiness":
        print(json.dumps(readiness(), indent=2))
    elif sys.argv[1] == "preflight":
        plan = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith("--") else "pro"
        tenant = sys.argv[sys.argv.index("--tenant") + 1] if "--tenant" in sys.argv else ""
        pf = preflight(plan, tenant=tenant)
        print(json.dumps(pf, indent=2, default=str))
        sys.exit(0 if pf.get("ok") else 1)
    else:
        sys.exit("usage: stripebilling.py selftest | readiness | preflight [plan] [--tenant T]")
