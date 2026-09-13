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
from agent_os.domain.organization import (
    AgentProfile,
    AgentStatus,
    HumanParticipant,
    Organization,
    ServiceParticipant,
)
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


def _onboarding_id(tenant_id: str, proposal_id: str, position: int) -> str:
    material = f"agent-os:external-onboarding:v1:{tenant_id}:{proposal_id}:{position}"
    return "onboarding-" + hashlib.sha256(material.encode()).hexdigest()


def _vendor_id(tenant_id: str, onboarding_id: str) -> str:
    material = f"agent-os:vendor-participant:v1:{tenant_id}:{onboarding_id}"
    return "service:vendor-" + hashlib.sha256(material.encode()).hexdigest()[:32]


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
        humans = dict(base.humans)
        services = dict(base.services)
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
            elif kind == "hiring_proposal_decided" and payload.get("approved") is True:
                raw_agents = payload.get("agents", ())
                if not isinstance(raw_agents, list):
                    raise ValueError("approved hiring proposal has malformed agents")
                for raw in raw_agents:
                    if not isinstance(raw, Mapping):
                        raise ValueError("approved hiring proposal agent is malformed")
                    profile = AgentProfile(
                        agent_id=str(raw["agent_id"]),
                        role=str(raw["role"]),
                        team_id=str(raw["team_id"]),
                        manager_id=str(raw["manager_id"]),
                        capabilities=frozenset(str(item) for item in raw.get("capabilities", ())),
                        tool_grants=frozenset(str(item) for item in raw.get("tool_grants", ())),
                        hiring_authority=False,
                        spending_limit_cents=int(raw.get("spending_limit_cents", 0)),
                    )
                    if profile.agent_id in agents:
                        raise ValueError("company history hires the same agent identity twice")
                    agents[profile.agent_id] = profile
            elif kind == "external_participant_onboarded":
                participant_kind = str(payload.get("participant_kind") or "")
                participant_id = str(payload.get("participant_id") or "")
                if participant_id in agents or participant_id in humans or participant_id in services:
                    raise ValueError("company history onboards the same participant identity twice")
                if participant_kind == "human":
                    humans[participant_id] = HumanParticipant(
                        participant_id=participant_id,
                        display_name=str(payload["display_name"]),
                        team_id=str(payload["team_id"]),
                        responsibilities=tuple(
                            str(item) for item in payload.get("responsibilities", ())
                        ),
                        manager_id=str(payload["manager_id"]),
                        response_sla_seconds=int(payload["response_sla_seconds"]),
                        quality_criteria=tuple(
                            str(item) for item in payload.get("quality_criteria", ())
                        ),
                    )
                elif participant_kind == "vendor":
                    services[participant_id] = ServiceParticipant(
                        participant_id=participant_id,
                        name=str(payload["display_name"]),
                        capabilities=frozenset(
                            str(item) for item in payload.get("capabilities", ())
                        ),
                        owner_id=str(payload["manager_id"]),
                    )
                else:
                    raise ValueError("company history has an unknown external participant kind")
        return Organization(
            base.tenant_id,
            base.organization_id,
            base.name,
            base.teams,
            agents,
            humans,
            services,
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

    def _existing(
        self,
        *,
        tenant_id: str,
        event_id: str,
        kind: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        semantic = {
            "event_id": event_id,
            "tenant_id": tenant_id,
            "kind": kind,
            "actor_id": actor_id,
            "payload": dict(payload),
        }
        fingerprint = _fingerprint(semantic)
        with self._tenant_connection(tenant_id) as connection:
            prior = connection.execute(select(
                company_events.c.fingerprint,
                company_events.c.record,
            ).where(and_(
                company_events.c.tenant_id == tenant_id,
                company_events.c.event_id == event_id,
            ))).mappings().first()
        if prior is None:
            return None
        if prior["fingerprint"] != fingerprint:
            raise ValueError("company event idempotency key was reused with different content")
        return {**dict(prior["record"]), "duplicate": True}

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
        event_id = _event_id(tenant_id, idempotency_key, "hire-agent")
        payload = {
            "agent_id": profile.agent_id,
            "role": profile.role,
            "team_id": profile.team_id,
            "manager_id": profile.manager_id,
            "capabilities": sorted(profile.capabilities),
            "tool_grants": sorted(profile.tool_grants),
            "hiring_authority": profile.hiring_authority,
            "spending_limit_cents": profile.spending_limit_cents,
        }
        existing = self._existing(
            tenant_id=tenant_id, event_id=event_id, kind="agent_hired",
            actor_id=actor_id, payload=payload,
        )
        if existing is not None:
            return existing
        organization = self.get_organization(tenant_id)
        if len(organization.agents) >= 1_000:
            raise ValueError("standing company agent limit reached")
        manager = organization.agents.get(manager_id)
        if team_id not in organization.teams:
            raise ValueError("agent team does not exist")
        if manager is None or manager.status is not AgentStatus.ACTIVE:
            raise ValueError("agent manager must be an active standing agent")
        return self._append(
            tenant_id=tenant_id,
            event_id=event_id,
            kind="agent_hired",
            actor_id=actor_id,
            payload=payload,
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
        event_id = _event_id(tenant_id, idempotency_key, "retire-agent")
        payload = {"agent_id": agent_id, "reason": reason.strip()}
        existing = self._existing(
            tenant_id=tenant_id, event_id=event_id, kind="agent_retired",
            actor_id=actor_id, payload=payload,
        )
        if existing is not None:
            return existing
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
            event_id=event_id,
            kind="agent_retired",
            actor_id=actor_id,
            payload=payload,
        )

    def decide_hiring_proposal(
        self,
        *,
        tenant_id: str,
        proposal_id: str,
        approved: bool,
        participant_kind: str = "agent",
        reason: str,
        role: str,
        requested_count: int,
        team_id: str | None,
        manager_id: str | None,
        capabilities: tuple[str, ...],
        tool_grants: tuple[str, ...],
        spending_limit_cents: int,
        actor_id: str,
    ) -> Mapping[str, Any]:
        if not proposal_id.strip() or not reason.strip():
            raise ValueError("proposal identity and decision reason are required")
        participant_kind = participant_kind.strip().lower()
        if participant_kind not in {"agent", "human", "vendor"}:
            raise ValueError("staffing participant kind must be agent, human, or vendor")
        if not 1 <= requested_count <= 32:
            raise ValueError("one staffing decision may create between 1 and 32 AI agents")
        if len(capabilities) > 64 or len(tool_grants) > 64:
            raise ValueError("agent capabilities and tool grants are bounded to 64 each")
        if spending_limit_cents < 0:
            raise ValueError("agent spending limit cannot be negative")
        agents: list[dict[str, Any]] = []
        onboarding_cases: list[dict[str, Any]] = []
        if approved and participant_kind == "agent":
            if team_id is None or manager_id is None:
                raise ValueError("approved AI staffing requires a team and accountable manager")
            for position in range(requested_count):
                profile = AgentProfile(
                    agent_id=_agent_id(tenant_id, f"{proposal_id}:{position}"),
                    role=role.strip(),
                    team_id=team_id,
                    manager_id=manager_id,
                    capabilities=frozenset(capabilities),
                    tool_grants=frozenset(tool_grants),
                    hiring_authority=False,
                    spending_limit_cents=spending_limit_cents,
                )
                agents.append({
                    "agent_id": profile.agent_id,
                    "role": profile.role,
                    "team_id": profile.team_id,
                    "manager_id": profile.manager_id,
                    "capabilities": sorted(profile.capabilities),
                    "tool_grants": sorted(profile.tool_grants),
                    "hiring_authority": False,
                    "spending_limit_cents": profile.spending_limit_cents,
                })
        elif approved:
            if team_id is None or manager_id is None:
                raise ValueError(
                    "approved external staffing requires a team and accountable manager"
                )
            onboarding_cases = [{
                "onboarding_id": _onboarding_id(tenant_id, proposal_id, position),
                "proposal_id": proposal_id,
                "participant_kind": participant_kind,
                "role": role.strip(),
                "position": position,
                "team_id": team_id,
                "manager_id": manager_id,
                "capabilities": sorted(set(capabilities)),
                "requested_tool_grants": sorted(set(tool_grants)),
                "requested_spending_limit_cents": spending_limit_cents,
                "status": "awaiting_external_onboarding",
            } for position in range(requested_count)]
        event_id = _event_id(tenant_id, proposal_id, "decide-hiring-proposal")
        payload = {
            "proposal_id": proposal_id,
            "approved": approved,
            "participant_kind": participant_kind,
            "reason": reason.strip(),
            "agents": agents,
            "onboarding_cases": onboarding_cases,
        }
        existing = self._existing(
            tenant_id=tenant_id, event_id=event_id, kind="hiring_proposal_decided",
            actor_id=actor_id, payload=payload,
        )
        if existing is not None:
            return existing
        if approved:
            organization = self.get_organization(tenant_id)
            if participant_kind == "agent" and len(organization.agents) + requested_count > 1_000:
                raise ValueError("standing company agent limit reached")
            manager = organization.agents.get(str(manager_id))
            if team_id not in organization.teams:
                raise ValueError("staffing team does not exist")
            if manager is None or manager.status is not AgentStatus.ACTIVE:
                raise ValueError("staffing manager must be an active standing agent")
        return self._append(
            tenant_id=tenant_id,
            event_id=event_id,
            kind="hiring_proposal_decided",
            actor_id=actor_id,
            payload=payload,
        )

    def list_external_onboarding(
        self, tenant_id: str,
    ) -> tuple[Mapping[str, Any], ...]:
        cases: dict[str, dict[str, Any]] = {}
        for event in self.list_company_events(tenant_id, limit=5_000):
            payload = event.get("payload", {})
            if not isinstance(payload, Mapping):
                raise ValueError("persisted company event payload is malformed")
            if event.get("kind") == "hiring_proposal_decided":
                raw_cases = payload.get("onboarding_cases", ())
                if not isinstance(raw_cases, list):
                    raise ValueError("staffing decision onboarding cases are malformed")
                for raw in raw_cases:
                    if not isinstance(raw, Mapping):
                        raise ValueError("staffing onboarding case is malformed")
                    onboarding_id = str(raw.get("onboarding_id") or "")
                    if not onboarding_id or onboarding_id in cases:
                        raise ValueError("staffing onboarding identity is invalid or duplicated")
                    cases[onboarding_id] = {
                        **dict(raw),
                        "requested_at": event.get("created_at"),
                        "requested_by": event.get("actor_id"),
                    }
            elif event.get("kind") == "external_participant_onboarded":
                onboarding_id = str(payload.get("onboarding_id") or "")
                if onboarding_id not in cases:
                    raise ValueError("company history confirms unknown external onboarding")
                cases[onboarding_id].update({
                    "status": "active",
                    "participant_id": payload.get("participant_id"),
                    "display_name": payload.get("display_name"),
                    "confirmed_at": event.get("created_at"),
                    "confirmed_by": event.get("actor_id"),
                })
        return tuple(cases[key] for key in sorted(cases))

    def confirm_external_onboarding(
        self,
        *,
        tenant_id: str,
        onboarding_id: str,
        display_name: str,
        identity_subject: str | None,
        response_sla_seconds: int,
        quality_criteria: tuple[str, ...],
        attestations: tuple[str, ...],
        actor_id: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        onboarding_id = onboarding_id.strip()
        display_name = display_name.strip()
        identity_subject = None if identity_subject is None else identity_subject.strip()
        if not onboarding_id or not display_name or not idempotency_key.strip():
            raise ValueError("onboarding identity, display name, and idempotency key are required")
        if not 60 <= response_sla_seconds <= 31 * 24 * 60 * 60:
            raise ValueError("external participant response SLA must be 60 seconds through 31 days")
        if len(quality_criteria) > 32 or any(not item.strip() for item in quality_criteria):
            raise ValueError("external quality criteria must contain at most 32 nonempty items")
        case = next((
            item for item in self.list_external_onboarding(tenant_id)
            if item.get("onboarding_id") == onboarding_id
        ), None)
        if case is None:
            raise LookupError("external onboarding case does not exist")
        participant_kind = str(case.get("participant_kind") or "")
        required_attestations = {
            "identity_verified", "terms_accepted", "access_approved",
        }
        if participant_kind == "vendor":
            required_attestations.add("vendor_contract_approved")
        if not required_attestations.issubset(set(attestations)):
            raise ValueError(
                "external onboarding confirmation is missing required attestations"
            )
        if participant_kind == "human":
            if not identity_subject:
                raise ValueError("human onboarding requires its authenticated identity subject")
            participant_id = identity_subject
        elif participant_kind == "vendor":
            participant_id = _vendor_id(tenant_id, onboarding_id)
        else:
            raise ValueError("only human or vendor onboarding can be externally confirmed")
        payload = {
            "onboarding_id": onboarding_id,
            "proposal_id": case.get("proposal_id"),
            "participant_id": participant_id,
            "participant_kind": participant_kind,
            "display_name": display_name,
            "role": case.get("role"),
            "team_id": case.get("team_id"),
            "manager_id": case.get("manager_id"),
            "responsibilities": [str(case.get("role"))],
            "capabilities": list(case.get("capabilities", ())),
            "response_sla_seconds": response_sla_seconds,
            "quality_criteria": list(quality_criteria),
            "attestations": sorted(set(attestations)),
        }
        event_id = _event_id(
            tenant_id, f"{onboarding_id}:{idempotency_key}", "confirm-external-onboarding",
        )
        existing = self._existing(
            tenant_id=tenant_id,
            event_id=event_id,
            kind="external_participant_onboarded",
            actor_id=actor_id,
            payload=payload,
        )
        if existing is not None:
            return existing
        if case.get("status") != "awaiting_external_onboarding":
            raise ValueError("external onboarding case is no longer pending")
        organization = self.get_organization(tenant_id)
        identities = set(organization.agents) | set(organization.humans) | set(organization.services)
        if participant_id in identities:
            raise ValueError("external participant identity is already active")
        if case.get("team_id") not in organization.teams:
            raise ValueError("external participant team does not exist")
        manager = organization.agents.get(str(case.get("manager_id") or ""))
        if manager is None or manager.status is not AgentStatus.ACTIVE:
            raise ValueError("external participant manager must be an active standing agent")
        if participant_kind == "human":
            HumanParticipant(
                participant_id=participant_id,
                display_name=display_name,
                team_id=str(case["team_id"]),
                responsibilities=(str(case["role"]),),
                manager_id=str(case["manager_id"]),
                response_sla_seconds=response_sla_seconds,
                quality_criteria=quality_criteria,
            )
        else:
            ServiceParticipant(
                participant_id=participant_id,
                name=display_name,
                capabilities=frozenset(str(item) for item in case.get("capabilities", ())),
                owner_id=str(case["manager_id"]),
            )
        return self._append(
            tenant_id=tenant_id,
            event_id=event_id,
            kind="external_participant_onboarded",
            actor_id=actor_id,
            payload=payload,
        )

    def close(self) -> None:
        self._engine.dispose()
