"""Tenant-scoped standing company directory backed by immutable events."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Mapping

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    and_,
    create_engine,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.exc import IntegrityError

from agent_os.application.default_organization import default_organization
from agent_os.application.ports import CompanyDirectory
from agent_os.domain.organization import AgentProfile, AgentStatus, Organization
from agent_os.infrastructure.dbos_lifecycle import sqlalchemy_url


company_metadata = MetaData()

companies = Table(
    "aos_v2_companies",
    company_metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("name", Text, nullable=False),
    Column("version", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

company_events = Table(
    "aos_v2_company_events",
    company_metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("event_id", String(128), primary_key=True),
    Column("stream_version", Integer, nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("kind", String(64), nullable=False),
    Column("actor_id", String(256), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("record", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("tenant_id", "stream_version"),
    ForeignKeyConstraint(
        ["tenant_id"], ["aos_v2_companies.tenant_id"], ondelete="CASCADE",
    ),
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fingerprint(raw: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(
            raw, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("company event must contain JSON-compatible primitives") from exc
    return hashlib.sha256(encoded.encode()).hexdigest()


def _event_id(tenant_id: str, idempotency_key: str, operation: str) -> str:
    material = f"agent-os:company-event:v1:{tenant_id}:{idempotency_key}:{operation}"
    return "company-event-" + hashlib.sha256(material.encode()).hexdigest()


def _agent_id(tenant_id: str, idempotency_key: str) -> str:
    material = f"agent-os:standing-agent:v1:{tenant_id}:{idempotency_key}"
    return "agent:custom-" + hashlib.sha256(material.encode()).hexdigest()[:32]


class SQLCompanyDirectory(CompanyDirectory):
    """Project a reusable company roster from an append-only tenant stream."""

    def __init__(self, database_url: str, *, create_schema: bool = False) -> None:
        if not database_url.strip():
            raise ValueError("database_url is required")
        self._engine = create_engine(sqlalchemy_url(database_url), pool_pre_ping=True)
        if create_schema:
            company_metadata.create_all(self._engine)

    @contextmanager
    def _tenant_connection(self, tenant_id: str):
        if not tenant_id.strip():
            raise ValueError("tenant_id is required")
        with self._engine.begin() as connection:
            if connection.dialect.name == "postgresql":
                connection.execute(text("SET LOCAL ROLE agentos_app"))
                connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": tenant_id},
                )
            yield connection

    def _ensure_company(self, tenant_id: str) -> None:
        base = default_organization(tenant_id)
        try:
            with self._tenant_connection(tenant_id) as connection:
                exists = connection.execute(select(companies.c.tenant_id).where(
                    companies.c.tenant_id == tenant_id,
                )).scalar_one_or_none()
                if exists is None:
                    now = _now()
                    connection.execute(insert(companies).values(
                        tenant_id=tenant_id,
                        name=base.name,
                        version=0,
                        created_at=now,
                        updated_at=now,
                    ))
        except IntegrityError:
            # Concurrent first use may create the same deterministic row.
            pass

    def list_company_events(
        self,
        tenant_id: str,
        *,
        after_version: int = 0,
        limit: int = 500,
    ) -> tuple[Mapping[str, Any], ...]:
        if after_version < 0 or not 1 <= limit <= 5_000:
            raise ValueError("after_version must be nonnegative and limit must be 1..5000")
        with self._tenant_connection(tenant_id) as connection:
            rows = connection.execute(select(
                company_events.c.record,
            ).where(and_(
                company_events.c.tenant_id == tenant_id,
                company_events.c.stream_version > after_version,
            )).order_by(company_events.c.stream_version).limit(limit)).scalars().all()
        return tuple(dict(row) for row in rows)

    def get_organization(self, tenant_id: str) -> Organization:
        base = default_organization(tenant_id)
        agents = dict(base.agents)
        events = self.list_company_events(tenant_id, limit=5_000)
        for event in events:
            payload = event.get("payload", {})
            if not isinstance(payload, Mapping):
                raise ValueError("persisted company event payload is malformed")
            kind = event.get("kind")
            if kind == "agent_hired":
                profile = AgentProfile(
                    agent_id=str(payload["agent_id"]),
                    role=str(payload["role"]),
                    team_id=str(payload["team_id"]),
                    manager_id=str(payload["manager_id"]),
                    capabilities=frozenset(str(item) for item in payload.get("capabilities", ())),
                    tool_grants=frozenset(str(item) for item in payload.get("tool_grants", ())),
                    hiring_authority=bool(payload.get("hiring_authority", False)),
                    spending_limit_cents=int(payload.get("spending_limit_cents", 0)),
                )
                if profile.agent_id in agents:
                    raise ValueError("company history hires the same agent identity twice")
                agents[profile.agent_id] = profile
            elif kind == "agent_retired":
                agent_id = str(payload.get("agent_id") or "")
                prior = agents.get(agent_id)
                if prior is None:
                    raise ValueError("company history retires an unknown agent")
                agents[agent_id] = replace(prior, status=AgentStatus.RETIRED)
        return Organization(
            base.tenant_id,
            base.organization_id,
            base.name,
            base.teams,
            agents,
            base.humans,
            base.services,
        )

    def _append(
        self,
        *,
        tenant_id: str,
        event_id: str,
        kind: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if not event_id or not kind or not actor_id:
            raise ValueError("company event identity, kind, and actor are required")
        semantic = {
            "event_id": event_id,
            "tenant_id": tenant_id,
            "kind": kind,
            "actor_id": actor_id,
            "payload": dict(payload),
        }
        fingerprint = _fingerprint(semantic)
        self._ensure_company(tenant_id)
        with self._tenant_connection(tenant_id) as connection:
            prior = connection.execute(select(
                company_events.c.fingerprint,
                company_events.c.record,
            ).where(and_(
                company_events.c.tenant_id == tenant_id,
                company_events.c.event_id == event_id,
            ))).mappings().first()
            if prior is not None:
                if prior["fingerprint"] != fingerprint:
                    raise ValueError("company event idempotency key was reused with different content")
                return {**dict(prior["record"]), "duplicate": True}
            row = connection.execute(select(
                companies.c.version,
            ).where(companies.c.tenant_id == tenant_id).with_for_update()).scalar_one()
            version = int(row) + 1
            now = _now()
            record = {
                **semantic,
                "stream_version": version,
                "created_at": now.isoformat(),
            }
            changed = connection.execute(update(companies).where(and_(
                companies.c.tenant_id == tenant_id,
                companies.c.version == row,
            )).values(version=version, updated_at=now))
            if changed.rowcount != 1:
                raise RuntimeError("concurrent company writer lost its version fence")
            connection.execute(insert(company_events).values(
                tenant_id=tenant_id,
                event_id=event_id,
                stream_version=version,
                fingerprint=fingerprint,
                kind=kind,
                actor_id=actor_id,
                payload=dict(payload),
                record=record,
                created_at=now,
            ))
        return {**record, "duplicate": False}

    def hire_agent(
        self,
        *,
        tenant_id: str,
        role: str,
        team_id: str,
        manager_id: str,
        capabilities: tuple[str, ...],
        tool_grants: tuple[str, ...],
        hiring_authority: bool,
        spending_limit_cents: int,
        actor_id: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        if not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        if len(capabilities) > 64 or len(tool_grants) > 64:
            raise ValueError("agent capabilities and tool grants are bounded to 64 each")
        if any(not item.strip() for item in (*capabilities, *tool_grants)):
            raise ValueError("agent capabilities and tool grants must be nonempty strings")
        organization = self.get_organization(tenant_id)
        if len(organization.agents) >= 1_000:
            raise ValueError("standing company agent limit reached")
        manager = organization.agents.get(manager_id)
        if team_id not in organization.teams:
            raise ValueError("agent team does not exist")
        if manager is None or manager.status is not AgentStatus.ACTIVE:
            raise ValueError("agent manager must be an active standing agent")
        profile = AgentProfile(
            agent_id=_agent_id(tenant_id, idempotency_key),
            role=role.strip(),
            team_id=team_id,
            manager_id=manager_id,
            capabilities=frozenset(capabilities),
            tool_grants=frozenset(tool_grants),
            hiring_authority=hiring_authority,
            spending_limit_cents=spending_limit_cents,
        )
        return self._append(
            tenant_id=tenant_id,
            event_id=_event_id(tenant_id, idempotency_key, "hire-agent"),
            kind="agent_hired",
            actor_id=actor_id,
            payload={
                "agent_id": profile.agent_id,
                "role": profile.role,
                "team_id": profile.team_id,
                "manager_id": profile.manager_id,
                "capabilities": sorted(profile.capabilities),
                "tool_grants": sorted(profile.tool_grants),
                "hiring_authority": profile.hiring_authority,
                "spending_limit_cents": profile.spending_limit_cents,
            },
        )

    def retire_agent(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        reason: str,
        actor_id: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        if not reason.strip() or not idempotency_key.strip():
            raise ValueError("retirement reason and idempotency_key are required")
        organization = self.get_organization(tenant_id)
        agent = organization.agents.get(agent_id)
        if agent is None:
            raise LookupError("standing agent does not exist")
        if agent.status is AgentStatus.RETIRED:
            raise ValueError("standing agent is already retired")
        if agent_id == "agent:mission-manager":
            raise ValueError("the accountable mission manager cannot be retired")
        if organization.direct_reports(agent_id):
            raise ValueError("reassign direct reports before retiring their manager")
        if any(team.manager_id == agent_id for team in organization.teams.values()):
            raise ValueError("replace the team manager before retiring this agent")
        return self._append(
            tenant_id=tenant_id,
            event_id=_event_id(tenant_id, idempotency_key, "retire-agent"),
            kind="agent_retired",
            actor_id=actor_id,
            payload={"agent_id": agent_id, "reason": reason.strip()},
        )

    def close(self) -> None:
        self._engine.dispose()
