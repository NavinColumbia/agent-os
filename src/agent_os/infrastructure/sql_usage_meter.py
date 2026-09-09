"""Tenant-scoped model usage reservations, quotas, and settlement."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Mapping

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    and_,
    case,
    create_engine,
    func,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from agent_os.application.ports import UsageMeter
from agent_os.infrastructure.dbos_lifecycle import sqlalchemy_url


usage_metadata = MetaData()

usage_accounts = Table(
    "aos_v2_usage_accounts",
    usage_metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("monthly_budget_cents", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

usage_events = Table(
    "aos_v2_usage_events",
    usage_metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("source_id", String(256), primary_key=True),
    Column("run_id", String(256), nullable=False),
    Column("billing_period", String(7), nullable=False),
    Column("category", String(64), nullable=False),
    Column("model", String(256), nullable=False),
    Column("maximum_cost_cents", Integer, nullable=False),
    Column("charged_cost_cents", Integer),
    Column("provider_cost_usd_micros", BigInteger),
    Column("requests", Integer),
    Column("tool_calls", Integer),
    Column("input_tokens", BigInteger),
    Column("output_tokens", BigInteger),
    Column("total_tokens", BigInteger),
    Column("status", String(16), nullable=False),
    Column("reservation_fingerprint", String(64), nullable=False),
    Column("settlement_fingerprint", String(64)),
    Column("usage", JSON),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("settled_at", DateTime(timezone=True)),
)


class UsageQuotaExceeded(ValueError):
    """A new model turn would exceed the tenant's durable monthly ceiling."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical(raw: Mapping[str, Any]) -> str:
    return json.dumps(raw, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _fingerprint(raw: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(raw).encode()).hexdigest()


def _period(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m")


def _bounded_text(name: str, value: str, maximum: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{name} must contain 1 to {maximum} characters")
    return normalized


def _usage(raw: Mapping[str, Any]) -> dict[str, int | None]:
    normalized: dict[str, int | None] = {}
    for key in (
        "requests", "tool_calls", "input_tokens", "output_tokens", "total_tokens",
        "provider_cost_usd_micros",
    ):
        value = raw.get(key)
        if value is None and key == "provider_cost_usd_micros":
            normalized[key] = None
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"usage {key} must be a non-negative integer")
        normalized[key] = value
    if normalized["total_tokens"] != normalized["input_tokens"] + normalized["output_tokens"]:
        raise ValueError("usage total_tokens must equal input_tokens plus output_tokens")
    return normalized


class SQLUsageMeter(UsageMeter):
    """Serialize spend reservations per tenant before any provider request."""

    def __init__(
        self,
        database_url: str,
        *,
        monthly_budget_cents: int = 10_000,
        create_schema: bool = False,
    ) -> None:
        if not database_url.strip():
            raise ValueError("database_url is required")
        if not 1 <= monthly_budget_cents <= 1_000_000_000:
            raise ValueError("monthly model budget must be between 1 and 1000000000 cents")
        self._monthly_budget_cents = monthly_budget_cents
        self._engine = create_engine(sqlalchemy_url(database_url), pool_pre_ping=True)
        if create_schema:
            usage_metadata.create_all(self._engine)

    @contextmanager
    def _tenant_connection(self, tenant_id: str):
        normalized = _bounded_text("tenant_id", tenant_id, 128)
        with self._engine.begin() as connection:
            if connection.dialect.name == "postgresql":
                connection.execute(text("SET LOCAL ROLE agentos_app"))
                connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": normalized},
                )
            yield connection

    def _lock_account(self, connection, tenant_id: str, now: datetime) -> int:
        values = {
            "tenant_id": tenant_id,
            "monthly_budget_cents": self._monthly_budget_cents,
            "created_at": now,
            "updated_at": now,
        }
        if connection.dialect.name == "postgresql":
            statement = postgres_insert(usage_accounts).values(**values).on_conflict_do_nothing()
        elif connection.dialect.name == "sqlite":
            statement = sqlite_insert(usage_accounts).values(**values).on_conflict_do_nothing()
        else:
            statement = insert(usage_accounts).values(**values)
        connection.execute(statement)
        return int(connection.execute(select(
            usage_accounts.c.monthly_budget_cents
        ).where(
            usage_accounts.c.tenant_id == tenant_id
        ).with_for_update()).scalar_one())

    @staticmethod
    def _committed_expression():
        return case(
            (usage_events.c.status == "settled", usage_events.c.charged_cost_cents),
            else_=usage_events.c.maximum_cost_cents,
        )

    def reserve_model_turn(
        self,
        *,
        tenant_id: str,
        source_id: str,
        run_id: str,
        category: str,
        model: str,
        maximum_cost_cents: int,
    ) -> Mapping[str, Any]:
        tenant_id = _bounded_text("tenant_id", tenant_id, 128)
        source_id = _bounded_text("source_id", source_id, 256)
        run_id = _bounded_text("run_id", run_id, 256)
        category = _bounded_text("category", category, 64)
        model = _bounded_text("model", model, 256)
        if not 1 <= maximum_cost_cents <= 100_000_000:
            raise ValueError("maximum model-turn cost must be between 1 and 100000000 cents")
        now = _now()
        billing_period = _period(now)
        reservation = {
            "tenant_id": tenant_id,
            "source_id": source_id,
            "run_id": run_id,
            "billing_period": billing_period,
            "category": category,
            "model": model,
            "maximum_cost_cents": maximum_cost_cents,
        }
        fingerprint = _fingerprint({
            key: value for key, value in reservation.items() if key != "billing_period"
        })
        with self._tenant_connection(tenant_id) as connection:
            limit = self._lock_account(connection, tenant_id, now)
            existing = connection.execute(select(usage_events).where(and_(
                usage_events.c.tenant_id == tenant_id,
                usage_events.c.source_id == source_id,
            ))).mappings().one_or_none()
            if existing is not None:
                if existing["reservation_fingerprint"] != fingerprint:
                    raise ValueError("usage source_id was reused with different reservation content")
                return {**dict(existing), "duplicate": True, "monthly_budget_cents": limit}
            committed = int(connection.execute(select(func.coalesce(func.sum(
                self._committed_expression()
            ), 0)).where(and_(
                usage_events.c.tenant_id == tenant_id,
                usage_events.c.billing_period == billing_period,
            ))).scalar_one())
            if committed + maximum_cost_cents > limit:
                raise UsageQuotaExceeded(
                    f"monthly model budget exhausted: {committed} committed + "
                    f"{maximum_cost_cents} requested > {limit} cents"
                )
            connection.execute(insert(usage_events).values(
                **reservation,
                status="reserved",
                reservation_fingerprint=fingerprint,
                created_at=now,
            ))
        return {
            **reservation,
            "status": "reserved",
            "duplicate": False,
            "monthly_budget_cents": limit,
        }

    def settle_model_turn(
        self,
        *,
        tenant_id: str,
        source_id: str,
        usage: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        tenant_id = _bounded_text("tenant_id", tenant_id, 128)
        source_id = _bounded_text("source_id", source_id, 256)
        normalized = _usage(usage)
        fingerprint = _fingerprint(normalized)
        now = _now()
        with self._tenant_connection(tenant_id) as connection:
            row = connection.execute(select(usage_events).where(and_(
                usage_events.c.tenant_id == tenant_id,
                usage_events.c.source_id == source_id,
            )).with_for_update()).mappings().one_or_none()
            if row is None:
                raise LookupError("usage reservation not found")
            if row["status"] == "settled":
                if row["settlement_fingerprint"] != fingerprint:
                    raise ValueError("usage source_id was settled with different usage")
                return {**dict(row), "duplicate": True}
            provider_micros = normalized["provider_cost_usd_micros"]
            charged = int(row["maximum_cost_cents"])
            if provider_micros is not None:
                charged = (provider_micros + 9_999) // 10_000
            connection.execute(update(usage_events).where(and_(
                usage_events.c.tenant_id == tenant_id,
                usage_events.c.source_id == source_id,
                usage_events.c.status == "reserved",
            )).values(
                status="settled",
                charged_cost_cents=charged,
                provider_cost_usd_micros=provider_micros,
                requests=normalized["requests"],
                tool_calls=normalized["tool_calls"],
                input_tokens=normalized["input_tokens"],
                output_tokens=normalized["output_tokens"],
                total_tokens=normalized["total_tokens"],
                usage=normalized,
                settlement_fingerprint=fingerprint,
                settled_at=now,
            ))
            settled = connection.execute(select(usage_events).where(and_(
                usage_events.c.tenant_id == tenant_id,
                usage_events.c.source_id == source_id,
            ))).mappings().one()
        return {**dict(settled), "duplicate": False}

    def usage_summary(self, tenant_id: str) -> Mapping[str, Any]:
        tenant_id = _bounded_text("tenant_id", tenant_id, 128)
        billing_period = _period(_now())
        with self._tenant_connection(tenant_id) as connection:
            account = connection.execute(select(
                usage_accounts.c.monthly_budget_cents
            ).where(usage_accounts.c.tenant_id == tenant_id)).scalar_one_or_none()
            limit = self._monthly_budget_cents if account is None else int(account)
            row = connection.execute(select(
                func.count().label("events"),
                func.coalesce(func.sum(case(
                    (usage_events.c.status == "reserved", usage_events.c.maximum_cost_cents), else_=0,
                )), 0).label("reserved_cents"),
                func.coalesce(func.sum(case(
                    (usage_events.c.status == "settled", usage_events.c.charged_cost_cents), else_=0,
                )), 0).label("settled_cents"),
                func.coalesce(func.sum(usage_events.c.provider_cost_usd_micros), 0).label("known_micros"),
                func.coalesce(func.sum(usage_events.c.input_tokens), 0).label("input_tokens"),
                func.coalesce(func.sum(usage_events.c.output_tokens), 0).label("output_tokens"),
                func.coalesce(func.sum(usage_events.c.total_tokens), 0).label("total_tokens"),
                func.coalesce(func.sum(case((and_(
                    usage_events.c.status == "settled",
                    usage_events.c.provider_cost_usd_micros.is_(None),
                ), 1), else_=0)), 0).label("unknown_cost_events"),
            ).where(and_(
                usage_events.c.tenant_id == tenant_id,
                usage_events.c.billing_period == billing_period,
            ))).mappings().one()
        committed = int(row["reserved_cents"]) + int(row["settled_cents"])
        return {
            "billing_period": billing_period,
            "monthly_budget_cents": limit,
            "committed_cents": committed,
            "remaining_cents": max(0, limit - committed),
            "reserved_ceiling_cents": int(row["reserved_cents"]),
            "settled_charged_cents": int(row["settled_cents"]),
            "known_provider_cost_usd_micros": int(row["known_micros"]),
            "unknown_cost_events": int(row["unknown_cost_events"]),
            "events": int(row["events"]),
            "input_tokens": int(row["input_tokens"]),
            "output_tokens": int(row["output_tokens"]),
            "total_tokens": int(row["total_tokens"]),
        }

    def set_monthly_budget(
        self,
        *,
        tenant_id: str,
        monthly_budget_cents: int,
    ) -> Mapping[str, Any]:
        tenant_id = _bounded_text("tenant_id", tenant_id, 128)
        if not 1 <= monthly_budget_cents <= 1_000_000_000:
            raise ValueError("monthly model budget must be between 1 and 1000000000 cents")
        now = _now()
        values = {
            "tenant_id": tenant_id,
            "monthly_budget_cents": monthly_budget_cents,
            "created_at": now,
            "updated_at": now,
        }
        with self._tenant_connection(tenant_id) as connection:
            if connection.dialect.name == "postgresql":
                statement = postgres_insert(usage_accounts).values(**values).on_conflict_do_update(
                    index_elements=[usage_accounts.c.tenant_id],
                    set_={"monthly_budget_cents": monthly_budget_cents, "updated_at": now},
                )
            elif connection.dialect.name == "sqlite":
                statement = sqlite_insert(usage_accounts).values(**values).on_conflict_do_update(
                    index_elements=[usage_accounts.c.tenant_id],
                    set_={"monthly_budget_cents": monthly_budget_cents, "updated_at": now},
                )
            else:
                statement = update(usage_accounts).where(
                    usage_accounts.c.tenant_id == tenant_id
                ).values(monthly_budget_cents=monthly_budget_cents, updated_at=now)
            connection.execute(statement)
        return {"tenant_id": tenant_id, "monthly_budget_cents": monthly_budget_cents}

    def list_usage_events(
        self,
        tenant_id: str,
        *,
        limit: int = 100,
    ) -> tuple[Mapping[str, Any], ...]:
        if not 1 <= limit <= 500:
            raise ValueError("usage event limit must be between 1 and 500")
        with self._tenant_connection(tenant_id) as connection:
            rows = connection.execute(select(usage_events).where(
                usage_events.c.tenant_id == tenant_id
            ).order_by(
                usage_events.c.created_at.desc(), usage_events.c.source_id.desc(),
            ).limit(limit)).mappings().all()
        return tuple(dict(row) for row in rows)

    def close(self) -> None:
        self._engine.dispose()
