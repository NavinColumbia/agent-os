from __future__ import annotations

from typing import Any, Mapping

from fastapi.testclient import TestClient

from agent_os.api.app import create_app
from agent_os.infrastructure.memory import InMemoryWorkflowEngine


class Identity:
    def authenticate(self, authorization: str | None, session: str | None) -> Mapping[str, Any]:
        if authorization == "Bearer owner":
            return {"sub": "owner-a", "org": "tenant-a", "roles": ["owner"]}
        if authorization == "Bearer viewer":
            return {"sub": "viewer-a", "org": "tenant-a", "roles": ["viewer"]}
        raise ValueError("authentication required")


class Billing:
    def __init__(self):
        self.webhook_call = None

    def account(self, tenant_id):
        return {"tenant_id": tenant_id, "plan_id": "free", "available_plans": []}

    def checkout(self, tenant_id, plan_id, idempotency_key):
        return {"session_id": "cs_test", "url": "https://checkout.stripe.test"}

    def portal(self, tenant_id, idempotency_key):
        raise ValueError("billing portal is unavailable before the first Checkout")

    def webhook(self, payload, signature):
        self.webhook_call = (payload, signature)
        if signature == "invalid":
            raise ValueError("Stripe webhook signature is invalid")
        return {"processed": True, "event_id": "evt_1"}


def test_billing_api_is_tenant_authenticated_owner_governed_and_webhook_signed():
    billing = Billing()
    api = TestClient(create_app(
        engine=InMemoryWorkflowEngine(), identity=Identity(), billing_service=billing,
        client_identity_config={"identity_mode": "hmac", "billing_mode": "stripe"},
    ))
    assert api.get("/v2/client-config").json()["billing_mode"] == "stripe"
    assert api.get("/v2/billing").status_code == 401
    assert api.get("/v2/billing", headers={"Authorization": "Bearer owner"}).json()["tenant_id"] == "tenant-a"
    assert api.post(
        "/v2/billing/checkout",
        headers={"Authorization": "Bearer viewer", "Idempotency-Key": "checkout-viewer"},
        json={"plan_id": "starter"},
    ).status_code == 403
    checkout = api.post(
        "/v2/billing/checkout",
        headers={"Authorization": "Bearer owner", "Idempotency-Key": "checkout-owner"},
        json={"plan_id": "starter"},
    )
    assert checkout.status_code == 201
    assert checkout.json()["url"].startswith("https://")
    assert api.post(
        "/v2/billing/portal",
        headers={"Authorization": "Bearer owner", "Idempotency-Key": "portal-owner"},
    ).status_code == 409

    raw = b'{"id":"evt_1"}'
    webhook = api.post(
        "/v2/billing/webhooks/stripe", content=raw,
        headers={"Stripe-Signature": "valid", "Content-Type": "application/json"},
    )
    assert webhook.status_code == 200
    assert webhook.json()["received"] is True
    assert billing.webhook_call == (raw, "valid")
    assert api.post(
        "/v2/billing/webhooks/stripe", content=raw,
        headers={"Stripe-Signature": "invalid"},
    ).status_code == 400
    assert api.post("/v2/billing/webhooks/stripe", content=raw).status_code == 400
    assert api.post(
        "/v2/billing/webhooks/stripe", content=b"x" * 1_000_001,
        headers={"Stripe-Signature": "valid"},
    ).status_code == 413
