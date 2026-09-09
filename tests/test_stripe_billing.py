from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from agent_os.application.billing import BillingPlan
from agent_os.infrastructure.stripe_billing import StripeBillingGateway


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def gateway(session, *, clock=lambda: 1_700_000_000):
    return StripeBillingGateway(
        secret_key="sk_test_billing_contract",
        webhook_secret="whsec_billing_contract",
        public_base_url="https://agentos.example.test",
        session=session,
        clock=clock,
    )


def test_checkout_and_portal_are_hosted_idempotent_and_tenant_tagged():
    session = FakeSession([
        FakeResponse({"id": "cs_test_123", "url": "https://checkout.stripe.test/session"}),
        FakeResponse({"id": "bps_123", "url": "https://billing.stripe.test/portal"}),
    ])
    adapter = gateway(session)
    plan = BillingPlan("starter", "Starter", 50_000, "price_starter")

    checkout = adapter.create_checkout_session(
        tenant_id="tenant-a", plan=plan, customer_id="cus_123",
        idempotency_key="checkout-attempt-123",
    )
    portal = adapter.create_portal_session(
        customer_id="cus_123", idempotency_key="portal-attempt-123",
    )

    assert checkout["session_id"] == "cs_test_123"
    assert portal["session_id"] == "bps_123"
    checkout_call = session.calls[0]
    assert checkout_call[0] == "https://api.stripe.com/v1/checkout/sessions"
    assert checkout_call[1]["headers"]["Idempotency-Key"] == "checkout-attempt-123"
    assert checkout_call[1]["data"]["line_items[0][price]"] == "price_starter"
    assert checkout_call[1]["data"]["metadata[agent_os_tenant_id]"] == "tenant-a"
    assert checkout_call[1]["data"]["subscription_data[metadata][agent_os_tenant_id]"] == "tenant-a"
    assert checkout_call[1]["data"]["customer"] == "cus_123"
    assert session.calls[1][1]["data"]["return_url"].endswith("/app?billing=portal")


def test_webhook_verification_uses_exact_raw_body_signature_and_timestamp():
    adapter = gateway(FakeSession([]))
    payload = json.dumps({"id": "evt_1", "type": "invoice.paid"}, separators=(",", ":")).encode()
    timestamp = 1_700_000_000
    digest = hmac.new(
        b"whsec_billing_contract", str(timestamp).encode() + b"." + payload, hashlib.sha256,
    ).hexdigest()

    assert adapter.verify_webhook(payload, f"t={timestamp},v1=bad,v1={digest}")["id"] == "evt_1"
    with pytest.raises(ValueError, match="signature is invalid"):
        adapter.verify_webhook(payload + b" ", f"t={timestamp},v1={digest}")
    with pytest.raises(ValueError, match="timestamp"):
        adapter.verify_webhook(payload, f"t={timestamp - 301},v1={digest}")


def test_stripe_errors_do_not_leak_credentials_or_accept_bad_redirects():
    session = FakeSession([FakeResponse({"error": {"message": "card setup unavailable"}}, 503)])
    adapter = gateway(session)
    with pytest.raises(ConnectionError, match="card setup unavailable") as caught:
        adapter.create_checkout_session(
            tenant_id="tenant-a",
            plan=BillingPlan("starter", "Starter", 50_000, "price_starter"),
            customer_id=None,
            idempotency_key="checkout-attempt-123",
        )
    assert getattr(caught.value, "status_code") == 503
    assert "sk_test" not in str(caught.value)

    with pytest.raises(ValueError, match="HTTPS"):
        StripeBillingGateway(
            secret_key="sk_test_billing_contract",
            webhook_secret="whsec_billing_contract",
            public_base_url="http://agentos.example.test",
        )
