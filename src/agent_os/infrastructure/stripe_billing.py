"""Minimal Stripe HTTP adapter with raw-body webhook verification."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Callable, Mapping

import requests

from agent_os.application.billing import BillingPlan, PaymentGateway


class StripeBillingGateway(PaymentGateway):
    def __init__(
        self,
        *,
        secret_key: str,
        webhook_secret: str,
        public_base_url: str,
        api_version: str = "2025-06-30.basil",
        webhook_tolerance_seconds: int = 300,
        request_timeout_seconds: float = 15,
        session: requests.Session | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not secret_key.startswith(("sk_test_", "sk_live_")):
            raise ValueError("Stripe secret key must be an sk_test_ or sk_live_ key")
        if not webhook_secret.startswith("whsec_") or len(webhook_secret) < 16:
            raise ValueError("Stripe webhook secret is invalid")
        if not public_base_url.startswith("https://"):
            raise ValueError("Stripe redirect URLs require an HTTPS public base URL")
        if not api_version or any(character in api_version for character in "\r\n"):
            raise ValueError("Stripe API version is invalid")
        if not 60 <= webhook_tolerance_seconds <= 900 or not 1 <= request_timeout_seconds <= 60:
            raise ValueError("Stripe timeout/tolerance configuration is outside supported bounds")
        self._secret_key = secret_key
        self._webhook_secret = webhook_secret
        self._base_url = public_base_url.rstrip("/")
        self._api_version = api_version
        self._tolerance = webhook_tolerance_seconds
        self._timeout = request_timeout_seconds
        self._owns_session = session is None
        self._session = session or requests.Session()
        self._clock = clock

    def _post(
        self,
        path: str,
        data: Mapping[str, str],
        *,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        if not idempotency_key or len(idempotency_key) > 200 or any(
            character in idempotency_key for character in "\r\n\0"
        ):
            raise ValueError("Stripe idempotency key is invalid")
        try:
            response = self._session.post(
                f"https://api.stripe.com{path}",
                auth=(self._secret_key, ""),
                headers={
                    "Stripe-Version": self._api_version,
                    "Idempotency-Key": idempotency_key,
                    "User-Agent": "agent-os-v2/0.1",
                },
                data=dict(data),
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise ConnectionError("Stripe request failed before receiving a response") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise ConnectionError("Stripe returned a non-JSON response") from exc
        if response.status_code >= 400:
            error = payload.get("error", {}) if isinstance(payload, Mapping) else {}
            message = str(error.get("message") or "Stripe request failed")[:500]
            exception = ConnectionError(message)
            setattr(exception, "status_code", response.status_code)
            raise exception
        if not isinstance(payload, Mapping):
            raise ConnectionError("Stripe returned an invalid response")
        return payload

    def create_checkout_session(
        self,
        *,
        tenant_id: str,
        plan: BillingPlan,
        customer_id: str | None,
        idempotency_key: str,
    ) -> Mapping[str, str]:
        assert plan.stripe_price_id is not None
        data = {
            "mode": "subscription",
            "line_items[0][price]": plan.stripe_price_id,
            "line_items[0][quantity]": "1",
            "success_url": f"{self._base_url}/app?billing=success",
            "cancel_url": f"{self._base_url}/app?billing=cancelled",
            "client_reference_id": tenant_id,
            "metadata[agent_os_tenant_id]": tenant_id,
            "metadata[agent_os_plan_id]": plan.plan_id,
            "subscription_data[metadata][agent_os_tenant_id]": tenant_id,
            "subscription_data[metadata][agent_os_plan_id]": plan.plan_id,
            "automatic_tax[enabled]": "true",
            "allow_promotion_codes": "true",
        }
        if customer_id:
            data["customer"] = customer_id
        payload = self._post("/v1/checkout/sessions", data, idempotency_key=idempotency_key)
        identifier, url = payload.get("id"), payload.get("url")
        if not isinstance(identifier, str) or not identifier.startswith("cs_"):
            raise ConnectionError("Stripe Checkout response is missing its session ID")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise ConnectionError("Stripe Checkout response is missing its redirect URL")
        return {"session_id": identifier, "url": url}

    def create_portal_session(
        self,
        *,
        customer_id: str,
        idempotency_key: str,
    ) -> Mapping[str, str]:
        payload = self._post(
            "/v1/billing_portal/sessions",
            {"customer": customer_id, "return_url": f"{self._base_url}/app?billing=portal"},
            idempotency_key=idempotency_key,
        )
        identifier, url = payload.get("id"), payload.get("url")
        if not isinstance(identifier, str) or not identifier.startswith("bps_"):
            raise ConnectionError("Stripe portal response is missing its session ID")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise ConnectionError("Stripe portal response is missing its redirect URL")
        return {"session_id": identifier, "url": url}

    def verify_webhook(self, payload: bytes, signature: str) -> Mapping[str, Any]:
        if not payload or len(payload) > 1_000_000:
            raise ValueError("Stripe webhook body is empty or too large")
        timestamp = None
        signatures: list[str] = []
        for component in signature.split(","):
            key, separator, value = component.strip().partition("=")
            if not separator:
                continue
            if key == "t":
                try:
                    timestamp = int(value)
                except ValueError:
                    timestamp = None
            elif key == "v1" and value:
                signatures.append(value)
        now = int(self._clock())
        if timestamp is None or abs(now - timestamp) > self._tolerance or not signatures:
            raise ValueError("Stripe webhook signature timestamp is invalid")
        expected = hmac.new(
            self._webhook_secret.encode(),
            str(timestamp).encode() + b"." + payload,
            hashlib.sha256,
        ).hexdigest()
        if not any(hmac.compare_digest(expected, supplied) for supplied in signatures):
            raise ValueError("Stripe webhook signature is invalid")
        try:
            event = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Stripe webhook body is invalid JSON") from exc
        if not isinstance(event, Mapping):
            raise ValueError("Stripe webhook event must be an object")
        return event

    def close(self) -> None:
        if self._owns_session:
            self._session.close()
