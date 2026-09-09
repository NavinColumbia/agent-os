from __future__ import annotations

from agent_os.application.billing import BillingCatalog, BillingPlan, BillingService
from agent_os.infrastructure.sql_billing import SQLBillingStore
from agent_os.infrastructure.sql_usage_meter import SQLUsageMeter


def catalog() -> BillingCatalog:
    return BillingCatalog(
        BillingPlan("free", "Free", 10_000),
        (
            BillingPlan("starter", "Starter", 50_000, "price_starter"),
            BillingPlan("growth", "Growth", 250_000, "price_growth"),
        ),
    )


def stripe_event(event_id, event_type, created, tenant_id, *, status="active", price="price_starter"):
    if event_type == "checkout.session.completed":
        value = {
            "id": "cs_123", "customer": "cus_123", "subscription": "sub_123",
            "metadata": {"agent_os_tenant_id": tenant_id, "agent_os_plan_id": "starter"},
        }
    else:
        value = {
            "id": "sub_123", "customer": "cus_123", "status": status,
            "current_period_end": 1_800_000_000,
            "metadata": {"agent_os_tenant_id": tenant_id, "agent_os_plan_id": "starter"},
            "items": {"data": [{"price": {"id": price}}]},
        }
    return {"id": event_id, "type": event_type, "created": created, "data": {"object": value}}


def test_subscription_projection_is_idempotent_ordered_and_never_trusts_checkout_for_entitlement(tmp_path):
    store = SQLBillingStore(
        f"sqlite:///{tmp_path / 'billing.sqlite3'}",
        free_monthly_model_budget_cents=10_000,
        create_schema=True,
    )
    plans = catalog()
    try:
        assert store.get_account("tenant-a")["plan_id"] == "free"
        checkout = store.apply_stripe_event(
            stripe_event("evt_checkout", "checkout.session.completed", 200, "tenant-a"),
            catalog=plans,
        )
        assert checkout["plan_id"] == "free"
        assert checkout["stripe_customer_id"] == "cus_123"

        # A subscription event created before Checkout still controls plan
        # truth because each Stripe object stream has independent ordering.
        active_event = stripe_event(
            "evt_subscription", "customer.subscription.created", 190, "tenant-a",
        )
        active = store.apply_stripe_event(active_event, catalog=plans)
        assert active["plan_id"] == "starter"
        assert active["monthly_model_budget_cents"] == 50_000
        assert store.apply_stripe_event(active_event, catalog=plans)["duplicate"] is True

        late_checkout = store.apply_stripe_event(
            stripe_event("evt_late_checkout", "checkout.session.completed", 300, "tenant-a"),
            catalog=plans,
        )
        assert late_checkout["applied"] is False
        assert late_checkout["plan_id"] == "starter"

        old_cancel = store.apply_stripe_event(
            stripe_event("evt_old_cancel", "customer.subscription.deleted", 180, "tenant-a"),
            catalog=plans,
        )
        assert old_cancel["applied"] is False
        assert old_cancel["plan_id"] == "starter"

        canceled = store.apply_stripe_event(
            stripe_event("evt_cancel", "customer.subscription.deleted", 400, "tenant-a"),
            catalog=plans,
        )
        assert canceled["subscription_status"] == "canceled"
        assert canceled["plan_id"] == "free"
        assert canceled["monthly_model_budget_cents"] == 10_000
        unknown_old_price = store.apply_stripe_event(
            stripe_event(
                "evt_cancel_old_catalog", "customer.subscription.deleted", 500,
                "tenant-a", price="price_retired",
            ),
            catalog=plans,
        )
        assert unknown_old_price["plan_id"] == "free"
        assert store.get_account("tenant-b")["stripe_customer_id"] is None
    finally:
        store.close()


class FakeGateway:
    def __init__(self, event):
        self.event = event
        self.checkout_call = None

    def verify_webhook(self, payload, signature):
        assert payload == b"raw" and signature == "signed"
        return self.event

    def create_checkout_session(self, **kwargs):
        self.checkout_call = kwargs
        return {"session_id": "cs_test", "url": "https://checkout.stripe.test"}

    def create_portal_session(self, **kwargs):
        return {"session_id": "bps_test", "url": "https://billing.stripe.test"}


def test_service_reconciles_verified_entitlement_into_pre_provider_spend_cap(tmp_path):
    plans = catalog()
    store = SQLBillingStore(
        f"sqlite:///{tmp_path / 'accounts.sqlite3'}", free_monthly_model_budget_cents=10_000,
        create_schema=True,
    )
    meter = SQLUsageMeter(
        f"sqlite:///{tmp_path / 'usage.sqlite3'}", monthly_budget_cents=10_000,
        create_schema=True,
    )
    gateway = FakeGateway(stripe_event(
        "evt_active", "customer.subscription.updated", 500, "tenant-a", price="price_growth",
    ))
    service = BillingService(catalog=plans, accounts=store, gateway=gateway, usage_meter=meter)
    try:
        result = service.webhook(b"raw", "signed")
        assert result["plan_id"] == "growth"
        assert meter.usage_summary("tenant-a")["monthly_budget_cents"] == 250_000
        checkout = service.checkout("tenant-a", "starter", "checkout-key")
        assert checkout["session_id"] == "cs_test"
        assert gateway.checkout_call["customer_id"] == "cus_123"
        assert service.account("tenant-a")["display_name"] == "Growth"
    finally:
        meter.close()
        store.close()
