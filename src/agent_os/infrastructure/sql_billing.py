"""Stripe subscription projection with tenant RLS and event idempotency."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Mapping

from sqlalchemy import BigInteger, Boolean, Column, DateTime, Integer, MetaData, String, Table, and_, create_engine, insert, select, text
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from agent_os.application.billing import BillingAccountStore, BillingCatalog
from agent_os.infrastructure.dbos_lifecycle import sqlalchemy_url


billing_metadata = MetaData()
billing_accounts = Table(
    "aos_v2_billing_accounts", billing_metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("plan_id", String(64), nullable=False),
    Column("subscription_status", String(40), nullable=False),
    Column("stripe_customer_id", String(128)),
    Column("stripe_subscription_id", String(128)),
    Column("monthly_model_budget_cents", Integer, nullable=False),
    Column("current_period_end", DateTime(timezone=True)),
    Column("last_event_created", BigInteger, nullable=False),
    Column("last_event_id", String(128), nullable=False),
    Column("last_subscription_event_created", BigInteger, nullable=False),
    Column("last_subscription_event_id", String(128), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
billing_events = Table(
    "aos_v2_billing_events", billing_metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("event_id", String(128), primary_key=True),
    Column("event_type", String(128), nullable=False),
    Column("event_created", BigInteger, nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("applied", Boolean, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fingerprint(raw: Mapping[str, Any]) -> str:
    encoded = json.dumps(raw, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _text(name: str, value: Any, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise ValueError(f"Stripe event {name} is missing or invalid")
    return value.strip()


def _event_parts(event: Mapping[str, Any]) -> tuple[str, str, int, Mapping[str, Any]]:
    event_id = _text("id", event.get("id"), 128)
    event_type = _text("type", event.get("type"), 128)
    created = event.get("created")
    if isinstance(created, bool) or not isinstance(created, int) or created < 0:
        raise ValueError("Stripe event created timestamp is invalid")
    data = event.get("data")
    value = data.get("object") if isinstance(data, Mapping) else None
    if not isinstance(value, Mapping):
        raise ValueError("Stripe event data.object is invalid")
    return event_id, event_type, created, value


class SQLBillingStore(BillingAccountStore):
    def __init__(self, database_url: str, *, free_monthly_model_budget_cents: int, create_schema: bool = False) -> None:
        if not database_url.strip():
            raise ValueError("database_url is required")
        if not 1 <= free_monthly_model_budget_cents <= 1_000_000_000:
            raise ValueError("free model budget is outside supported bounds")
        self._free_budget = free_monthly_model_budget_cents
        self._engine = create_engine(sqlalchemy_url(database_url), pool_pre_ping=True)
        if create_schema:
            billing_metadata.create_all(self._engine)

    @contextmanager
    def _tenant_connection(self, tenant_id: str):
        tenant_id = _text("tenant", tenant_id, 128)
        with self._engine.begin() as connection:
            if connection.dialect.name == "postgresql":
                connection.execute(text("SET LOCAL ROLE agentos_app"))
                connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": tenant_id})
            yield connection

    def _free_account(self, tenant_id: str) -> Mapping[str, Any]:
        return {
            "tenant_id": tenant_id, "plan_id": "free", "subscription_status": "free",
            "stripe_customer_id": None, "stripe_subscription_id": None,
            "monthly_model_budget_cents": self._free_budget, "current_period_end": None,
            "last_event_created": 0, "last_event_id": "none",
            "last_subscription_event_created": 0, "last_subscription_event_id": "none",
        }

    def get_account(self, tenant_id: str) -> Mapping[str, Any]:
        tenant_id = _text("tenant", tenant_id, 128)
        with self._tenant_connection(tenant_id) as connection:
            row = connection.execute(select(billing_accounts).where(
                billing_accounts.c.tenant_id == tenant_id
            )).mappings().one_or_none()
        return self._free_account(tenant_id) if row is None else dict(row)

    @staticmethod
    def _tenant_and_plan(event_type: str, value: Mapping[str, Any], catalog: BillingCatalog) -> tuple[str, str]:
        metadata = value.get("metadata")
        if not isinstance(metadata, Mapping):
            raise ValueError("Stripe billing object is missing Agent OS metadata")
        tenant_id = _text("metadata tenant", metadata.get("agent_os_tenant_id"), 128)
        if event_type.startswith("customer.subscription."):
            items = value.get("items")
            rows = items.get("data") if isinstance(items, Mapping) else None
            if not isinstance(rows, list):
                raise ValueError("Stripe subscription line items are missing")
            matched = []
            for item in rows:
                price = item.get("price") if isinstance(item, Mapping) else None
                price_id = price.get("id") if isinstance(price, Mapping) else None
                if not isinstance(price_id, str):
                    continue
                try:
                    matched.append(catalog.plan_for_price(price_id))
                except ValueError:
                    continue
            if len(matched) != 1:
                if event_type == "customer.subscription.deleted" or value.get("status") not in {
                    "active", "trialing",
                }:
                    return tenant_id, catalog.free_plan.plan_id
                raise ValueError("Stripe subscription must contain exactly one configured Agent OS price")
            return tenant_id, matched[0].plan_id
        return tenant_id, _text("metadata plan", metadata.get("agent_os_plan_id"), 64)

    @staticmethod
    def _insert_default_account(connection, values: Mapping[str, Any]) -> None:
        if connection.dialect.name == "postgresql":
            statement = postgres_insert(billing_accounts).values(**values).on_conflict_do_nothing()
        elif connection.dialect.name == "sqlite":
            statement = sqlite_insert(billing_accounts).values(**values).on_conflict_do_nothing()
        else:
            statement = insert(billing_accounts).values(**values)
        connection.execute(statement)

    def apply_stripe_event(self, event: Mapping[str, Any], *, catalog: BillingCatalog) -> Mapping[str, Any]:
        event_id, event_type, created, value = _event_parts(event)
        if event_type not in {
            "checkout.session.completed", "customer.subscription.created",
            "customer.subscription.updated", "customer.subscription.deleted",
        }:
            return {"processed": False, "event_id": event_id, "event_type": event_type}
        tenant_id, plan_id = self._tenant_and_plan(event_type, value, catalog)
        plan = catalog.plan(plan_id)
        fingerprint = _fingerprint(event)
        now = _now()
        customer = value.get("customer")
        customer_id = customer if isinstance(customer, str) and customer else None
        subscription = value.get("subscription") if event_type == "checkout.session.completed" else value.get("id")
        subscription_id = subscription if isinstance(subscription, str) and subscription else None
        status = "checkout_complete" if event_type == "checkout.session.completed" else str(value.get("status") or "unknown")
        if event_type == "customer.subscription.deleted":
            status = "canceled"
        effective_plan = plan if status in {"active", "trialing"} else catalog.free_plan
        period_end = value.get("current_period_end")
        current_period_end = None
        if isinstance(period_end, int) and not isinstance(period_end, bool) and period_end >= 0:
            current_period_end = datetime.fromtimestamp(period_end, tz=timezone.utc)

        with self._tenant_connection(tenant_id) as connection:
            self._insert_default_account(connection, {
                **self._free_account(tenant_id), "updated_at": now,
            })
            account = connection.execute(select(billing_accounts).where(
                billing_accounts.c.tenant_id == tenant_id
            ).with_for_update()).mappings().one()
            prior_event = connection.execute(select(billing_events).where(and_(
                billing_events.c.tenant_id == tenant_id, billing_events.c.event_id == event_id,
            ))).mappings().one_or_none()
            if prior_event is not None:
                if prior_event["fingerprint"] != fingerprint:
                    raise ValueError("Stripe event ID was reused with different content")
                return {**dict(account), "processed": True, "duplicate": True}
            is_subscription = event_type.startswith("customer.subscription.")
            if is_subscription:
                newer = (created, event_id) > (
                    int(account["last_subscription_event_created"]),
                    str(account["last_subscription_event_id"]),
                )
            else:
                # Checkout is only a customer/session linkage event. Stripe can
                # deliver it after a subscription event, so it must never
                # downgrade authoritative active/cancelled subscription state.
                newer = account["subscription_status"] in {
                    "free", "checkout_complete",
                }
            connection.execute(insert(billing_events).values(
                tenant_id=tenant_id, event_id=event_id, event_type=event_type,
                event_created=created, fingerprint=fingerprint, applied=newer, created_at=now,
            ))
            if newer:
                account_values = {
                    "tenant_id": tenant_id, "plan_id": effective_plan.plan_id,
                    "subscription_status": status,
                    "stripe_customer_id": customer_id or account["stripe_customer_id"],
                    "stripe_subscription_id": subscription_id or account["stripe_subscription_id"],
                    "monthly_model_budget_cents": effective_plan.monthly_model_budget_cents,
                    "current_period_end": current_period_end,
                    "last_event_created": created, "last_event_id": event_id, "updated_at": now,
                    "last_subscription_event_created": (
                        created if is_subscription else (
                            account["last_subscription_event_created"]
                        )
                    ),
                    "last_subscription_event_id": (
                        event_id if is_subscription else (
                            account["last_subscription_event_id"]
                        )
                    ),
                }
                changes = {key: item for key, item in account_values.items() if key != "tenant_id"}
                if connection.dialect.name == "postgresql":
                    statement = postgres_insert(billing_accounts).values(**account_values).on_conflict_do_update(
                        index_elements=[billing_accounts.c.tenant_id], set_=changes,
                    )
                elif connection.dialect.name == "sqlite":
                    statement = sqlite_insert(billing_accounts).values(**account_values).on_conflict_do_update(
                        index_elements=[billing_accounts.c.tenant_id], set_=changes,
                    )
                else:
                    statement = insert(billing_accounts).values(**account_values)
                connection.execute(statement)
            result = connection.execute(select(billing_accounts).where(
                billing_accounts.c.tenant_id == tenant_id
            )).mappings().one_or_none()
        projected = self._free_account(tenant_id) if result is None else dict(result)
        return {**projected, "processed": True, "duplicate": False, "applied": newer}

    def close(self) -> None:
        self._engine.dispose()
