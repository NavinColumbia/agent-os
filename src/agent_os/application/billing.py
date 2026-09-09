"""Provider-neutral subscription, entitlement, and usage-billing service."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from agent_os.application.ports import UsageMeter


@dataclass(frozen=True)
class BillingPlan:
    plan_id: str
    display_name: str
    monthly_model_budget_cents: int
    stripe_price_id: str | None = None

    def __post_init__(self) -> None:
        if not self.plan_id or len(self.plan_id) > 64 or not self.display_name:
            raise ValueError("billing plan requires a bounded ID and display name")
        if not 1 <= self.monthly_model_budget_cents <= 1_000_000_000:
            raise ValueError("billing plan model budget is outside supported bounds")
        if self.stripe_price_id is not None and not self.stripe_price_id.startswith("price_"):
            raise ValueError("Stripe plan prices must use a price_ identifier")


class BillingCatalog:
    def __init__(self, free_plan: BillingPlan, paid_plans: tuple[BillingPlan, ...]) -> None:
        if free_plan.stripe_price_id is not None:
            raise ValueError("the free plan cannot have a Stripe price")
        plans = (free_plan, *paid_plans)
        if len({item.plan_id for item in plans}) != len(plans):
            raise ValueError("billing plan IDs must be unique")
        prices = [item.stripe_price_id for item in paid_plans]
        if not paid_plans or any(price is None for price in prices) or len(set(prices)) != len(prices):
            raise ValueError("paid plan Stripe prices must be present and unique")
        self._plans = {item.plan_id: item for item in plans}
        self._price_plans = {str(item.stripe_price_id): item for item in paid_plans}
        self.free_plan = free_plan

    @property
    def paid_plans(self) -> tuple[BillingPlan, ...]:
        return tuple(item for item in self._plans.values() if item.stripe_price_id is not None)

    def plan(self, plan_id: str) -> BillingPlan:
        try:
            return self._plans[plan_id]
        except KeyError as exc:
            raise ValueError("unknown billing plan") from exc

    def plan_for_price(self, price_id: str) -> BillingPlan:
        try:
            return self._price_plans[price_id]
        except KeyError as exc:
            raise ValueError("subscription contains an unconfigured Stripe price") from exc


class BillingAccountStore(Protocol):
    def get_account(self, tenant_id: str) -> Mapping[str, Any]: ...
    def apply_stripe_event(
        self,
        event: Mapping[str, Any],
        *,
        catalog: BillingCatalog,
    ) -> Mapping[str, Any]: ...


class PaymentGateway(Protocol):
    def create_checkout_session(
        self,
        *,
        tenant_id: str,
        plan: BillingPlan,
        customer_id: str | None,
        idempotency_key: str,
    ) -> Mapping[str, str]: ...
    def create_portal_session(
        self,
        *,
        customer_id: str,
        idempotency_key: str,
    ) -> Mapping[str, str]: ...
    def verify_webhook(self, payload: bytes, signature: str) -> Mapping[str, Any]: ...


class BillingService:
    def __init__(
        self,
        *,
        catalog: BillingCatalog,
        accounts: BillingAccountStore,
        gateway: PaymentGateway,
        usage_meter: UsageMeter,
    ) -> None:
        self._catalog = catalog
        self._accounts = accounts
        self._gateway = gateway
        self._usage_meter = usage_meter

    def account(self, tenant_id: str) -> Mapping[str, Any]:
        account = dict(self._accounts.get_account(tenant_id))
        plan = self._catalog.plan(str(account.get("plan_id") or ""))
        return {
            **account,
            "display_name": plan.display_name,
            "available_plans": [{
                "plan_id": item.plan_id,
                "display_name": item.display_name,
                "monthly_model_budget_cents": item.monthly_model_budget_cents,
            } for item in self._catalog.paid_plans],
        }

    def checkout(self, tenant_id: str, plan_id: str, idempotency_key: str) -> Mapping[str, str]:
        plan = self._catalog.plan(plan_id)
        if plan.stripe_price_id is None:
            raise ValueError("the free plan does not require Checkout")
        account = self._accounts.get_account(tenant_id)
        customer = account.get("stripe_customer_id")
        return self._gateway.create_checkout_session(
            tenant_id=tenant_id,
            plan=plan,
            customer_id=customer if isinstance(customer, str) and customer else None,
            idempotency_key=idempotency_key,
        )

    def portal(self, tenant_id: str, idempotency_key: str) -> Mapping[str, str]:
        account = self._accounts.get_account(tenant_id)
        customer = account.get("stripe_customer_id")
        if not isinstance(customer, str) or not customer:
            raise ValueError("billing portal is unavailable before the first Checkout")
        return self._gateway.create_portal_session(
            customer_id=customer,
            idempotency_key=idempotency_key,
        )

    def webhook(self, payload: bytes, signature: str) -> Mapping[str, Any]:
        event = self._gateway.verify_webhook(payload, signature)
        result = dict(self._accounts.apply_stripe_event(event, catalog=self._catalog))
        tenant_id = result.get("tenant_id")
        budget = result.get("monthly_model_budget_cents")
        if isinstance(tenant_id, str) and isinstance(budget, int):
            # Reconcile on every replay. A crash after the account commit but
            # before this write is repaired by Stripe's webhook retry.
            self._usage_meter.set_monthly_budget(
                tenant_id=tenant_id,
                monthly_budget_cents=budget,
            )
        return result
