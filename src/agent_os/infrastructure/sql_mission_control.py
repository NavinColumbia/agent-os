"""Transactional mission, assurance, provenance, and budget repository."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
from typing import Any, Mapping

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
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

from agent_os.application.assurance import AssuranceKernel, validate_delegation
from agent_os.domain.mission_model import (
    AssuranceDisposition,
    AuthorityGrant,
    Claim,
    EvidenceRef,
    EffectRequest,
    Hazard,
    MissionSpec,
    canonical_fingerprint,
)
from agent_os.infrastructure.dbos_lifecycle import sqlalchemy_url


mission_metadata = MetaData()

missions = Table(
    "aos_v2_missions",
    mission_metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("mission_id", String(256), primary_key=True),
    Column("revision", Integer, nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("status", String(32), nullable=False),
    Column("spec", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

mission_revisions = Table(
    "aos_v2_mission_revisions",
    mission_metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("mission_id", String(256), primary_key=True),
    Column("revision", Integer, primary_key=True),
    Column("fingerprint", String(64), nullable=False),
    Column("spec", JSON, nullable=False),
    Column("revised_by", String(256), nullable=False),
    Column("reason", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["tenant_id", "mission_id"],
        ["aos_v2_missions.tenant_id", "aos_v2_missions.mission_id"],
        ondelete="CASCADE",
    ),
)

mission_claims = Table(
    "aos_v2_mission_claims",
    mission_metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("mission_id", String(256), primary_key=True),
    Column("claim_id", String(256), primary_key=True),
    Column("fingerprint", String(64), nullable=False),
    Column("status", String(32), nullable=False),
    Column("valid_from", DateTime(timezone=True), nullable=False),
    Column("valid_to", DateTime(timezone=True)),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
    Column("claim", JSON, nullable=False),
    ForeignKeyConstraint(
        ["tenant_id", "mission_id"],
        ["aos_v2_missions.tenant_id", "aos_v2_missions.mission_id"],
        ondelete="CASCADE",
    ),
)

mission_evidence = Table(
    "aos_v2_mission_evidence",
    mission_metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("mission_id", String(256), primary_key=True),
    Column("evidence_id", String(256), primary_key=True),
    Column("fingerprint", String(64), nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("contains_personal_data", Boolean, nullable=False),
    Column("retention_until", DateTime(timezone=True)),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
    Column("evidence", JSON, nullable=False),
    ForeignKeyConstraint(
        ["tenant_id", "mission_id"],
        ["aos_v2_missions.tenant_id", "aos_v2_missions.mission_id"],
        ondelete="CASCADE",
    ),
)

evidence_tombstones = Table(
    "aos_v2_mission_evidence_tombstones",
    mission_metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("mission_id", String(256), primary_key=True),
    Column("evidence_id", String(256), primary_key=True),
    Column("erased_at", DateTime(timezone=True), nullable=False),
    Column("erased_by", String(256), nullable=False),
    Column("reason", Text, nullable=False),
    ForeignKeyConstraint(
        ["tenant_id", "mission_id", "evidence_id"],
        [
            "aos_v2_mission_evidence.tenant_id",
            "aos_v2_mission_evidence.mission_id",
            "aos_v2_mission_evidence.evidence_id",
        ],
        ondelete="CASCADE",
    ),
)

mission_hazards = Table(
    "aos_v2_mission_hazards",
    mission_metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("mission_id", String(256), primary_key=True),
    Column("hazard_id", String(256), primary_key=True),
    Column("fingerprint", String(64), nullable=False),
    Column("severity", String(32), nullable=False),
    Column("hazard", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["tenant_id", "mission_id"],
        ["aos_v2_missions.tenant_id", "aos_v2_missions.mission_id"],
        ondelete="CASCADE",
    ),
)

mission_authorities = Table(
    "aos_v2_mission_authorities",
    mission_metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("mission_id", String(256), primary_key=True),
    Column("grant_id", String(256), primary_key=True),
    Column("fingerprint", String(64), nullable=False),
    Column("delegate_id", String(256), nullable=False),
    Column("parent_grant_id", String(256)),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("revoked", Boolean, nullable=False, default=False),
    Column("revoked_at", DateTime(timezone=True)),
    Column("revoked_by", String(256)),
    Column("revocation_reason", Text),
    Column("grant_record", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["tenant_id", "mission_id"],
        ["aos_v2_missions.tenant_id", "aos_v2_missions.mission_id"],
        ondelete="CASCADE",
    ),
)

mission_effects = Table(
    "aos_v2_mission_effects",
    mission_metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("mission_id", String(256), primary_key=True),
    Column("effect_id", String(256), primary_key=True),
    Column("idempotency_key", String(256), nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("status", String(32), nullable=False),
    Column("request", JSON, nullable=False),
    Column("reservation_id", String(256)),
    Column("reserved_cents", BigInteger, nullable=False, default=0),
    Column("actual_cents", BigInteger),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True)),
    ForeignKeyConstraint(
        ["tenant_id", "mission_id"],
        ["aos_v2_missions.tenant_id", "aos_v2_missions.mission_id"],
        ondelete="CASCADE",
    ),
    UniqueConstraint("tenant_id", "mission_id", "idempotency_key"),
)

assurance_decisions = Table(
    "aos_v2_assurance_decisions",
    mission_metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("mission_id", String(256), primary_key=True),
    Column("decision_id", String(256), primary_key=True),
    Column("effect_id", String(256), nullable=False),
    Column("disposition", String(32), nullable=False),
    Column("decision", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["tenant_id", "mission_id", "effect_id"],
        [
            "aos_v2_mission_effects.tenant_id",
            "aos_v2_mission_effects.mission_id",
            "aos_v2_mission_effects.effect_id",
        ],
        ondelete="CASCADE",
    ),
)

budget_entries = Table(
    "aos_v2_mission_budget_entries",
    mission_metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("mission_id", String(256), primary_key=True),
    Column("transaction_id", String(256), primary_key=True),
    Column("position", Integer, primary_key=True),
    Column("account", String(32), nullable=False),
    Column("amount_cents", BigInteger, nullable=False),
    Column("effect_id", String(256)),
    Column("reason", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["tenant_id", "mission_id"],
        ["aos_v2_missions.tenant_id", "aos_v2_missions.mission_id"],
        ondelete="CASCADE",
    ),
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _time(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


class SQLMissionControl:
    """One tenant-fenced transaction boundary for mission effect admission."""

    def __init__(
        self,
        database_url: str,
        *,
        assurance_kernel: AssuranceKernel,
        create_schema: bool = False,
    ) -> None:
        if not database_url.strip():
            raise ValueError("database_url is required")
        self._engine = create_engine(sqlalchemy_url(database_url), pool_pre_ping=True)
        self._assurance = assurance_kernel
        if create_schema:
            mission_metadata.create_all(self._engine)

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

    @staticmethod
    def _mission_row(connection, tenant_id: str, mission_id: str, *, lock: bool = False):
        query = select(missions).where(and_(
            missions.c.tenant_id == tenant_id,
            missions.c.mission_id == mission_id,
        ))
        if lock:
            query = query.with_for_update()
        return connection.execute(query).mappings().first()

    def create_mission(self, spec: MissionSpec) -> Mapping[str, Any]:
        raw = spec.to_dict()
        fingerprint = canonical_fingerprint(raw)
        now = _now()
        with self._tenant_connection(spec.tenant_id) as connection:
            prior = self._mission_row(connection, spec.tenant_id, spec.mission_id, lock=True)
            if prior is not None:
                normalized = canonical_fingerprint(
                    MissionSpec.from_dict(prior["spec"]).to_dict()
                )
                if normalized != fingerprint:
                    raise ValueError("mission identity already exists with different content")
                return {**dict(prior["spec"]), "status": prior["status"], "duplicate": True}
            connection.execute(insert(missions).values(
                tenant_id=spec.tenant_id,
                mission_id=spec.mission_id,
                revision=spec.revision,
                fingerprint=fingerprint,
                status="active",
                spec=raw,
                created_at=now,
                updated_at=now,
            ))
            connection.execute(insert(mission_revisions).values(
                tenant_id=spec.tenant_id,
                mission_id=spec.mission_id,
                revision=spec.revision,
                fingerprint=fingerprint,
                spec=raw,
                revised_by=spec.principal_id,
                reason="mission created",
                created_at=now,
            ))
            # Opening balance is itself a balanced transaction.  The
            # authorization account is the source; available is spendable.
            self._insert_budget_transaction(
                connection,
                tenant_id=spec.tenant_id,
                mission_id=spec.mission_id,
                transaction_id=f"open:{spec.mission_id}:r{spec.revision}",
                lines=(("authorized", -spec.budget_limit_cents),
                       ("available", spec.budget_limit_cents)),
                effect_id=None,
                reason="mission budget authorized",
                now=now,
            )
        return {**raw, "status": "active", "duplicate": False}

    def revise_mission(
        self,
        spec: MissionSpec,
        *,
        expected_revision: int,
        revised_by: str,
        reason: str,
    ) -> Mapping[str, Any]:
        if expected_revision < 1 or not revised_by.strip() or not reason.strip():
            raise ValueError("mission revision requires expected version, actor, and reason")
        raw = spec.to_dict()
        fingerprint = canonical_fingerprint(raw)
        now = _now()
        with self._tenant_connection(spec.tenant_id) as connection:
            row = self._mission_row(
                connection, spec.tenant_id, spec.mission_id, lock=True,
            )
            if row is None:
                raise LookupError("mission does not exist")
            current = MissionSpec.from_dict(row["spec"])
            if current.revision == spec.revision:
                normalized = canonical_fingerprint(current.to_dict())
                if normalized != fingerprint:
                    raise ValueError("mission revision identity has different content")
                return {**raw, "status": row["status"], "duplicate": True}
            if current.revision != expected_revision:
                raise ValueError("mission revision is stale")
            if spec.revision != expected_revision + 1:
                raise ValueError("mission revision must advance exactly once")
            if (
                spec.principal_id != current.principal_id
                or spec.created_at != current.created_at
            ):
                raise ValueError("mission principal and creation time are immutable")
            if _time(spec.revised_at) <= _time(current.revised_at):
                raise ValueError("mission revised_at must advance")
            budget = self._budget_summary(connection, spec.tenant_id, spec.mission_id)
            delta = spec.budget_limit_cents - current.budget_limit_cents
            if delta < 0 and budget["available_cents"] < -delta:
                raise ValueError("mission budget cannot be reduced below committed funds")
            if delta:
                self._insert_budget_transaction(
                    connection,
                    tenant_id=spec.tenant_id,
                    mission_id=spec.mission_id,
                    transaction_id=f"revise-budget:{spec.mission_id}:r{spec.revision}",
                    lines=(
                        (("authorized", -delta), ("available", delta))
                        if delta > 0
                        else (("available", delta), ("authorized", -delta))
                    ),
                    effect_id=None,
                    reason=f"mission budget revised: {reason[:1_000]}",
                    now=now,
                )
            connection.execute(update(missions).where(and_(
                missions.c.tenant_id == spec.tenant_id,
                missions.c.mission_id == spec.mission_id,
                missions.c.revision == expected_revision,
            )).values(
                revision=spec.revision,
                fingerprint=fingerprint,
                spec=raw,
                updated_at=now,
            ))
            connection.execute(insert(mission_revisions).values(
                tenant_id=spec.tenant_id,
                mission_id=spec.mission_id,
                revision=spec.revision,
                fingerprint=fingerprint,
                spec=raw,
                revised_by=revised_by,
                reason=reason,
                created_at=now,
            ))
        return {**raw, "status": str(row["status"]), "duplicate": False}

    def get_mission(self, tenant_id: str, mission_id: str) -> MissionSpec | None:
        with self._tenant_connection(tenant_id) as connection:
            row = self._mission_row(connection, tenant_id, mission_id)
            return None if row is None else MissionSpec.from_dict(row["spec"])

    @staticmethod
    def _idempotent_record(
        connection,
        *,
        table: Table,
        key,
        fingerprint: str,
        values: Mapping[str, Any],
        label: str,
    ) -> bool:
        prior = connection.execute(select(table.c.fingerprint).where(key)).scalar_one_or_none()
        if prior is not None:
            if prior != fingerprint:
                raise ValueError(f"{label} identity already exists with different content")
            return False
        connection.execute(insert(table).values(**values))
        return True

    def add_evidence(self, tenant_id: str, evidence: EvidenceRef) -> bool:
        if evidence.mission_id == "" or tenant_id == "":
            raise ValueError("tenant and evidence mission are required")
        raw = evidence.to_dict()
        fingerprint = canonical_fingerprint(raw)
        with self._tenant_connection(tenant_id) as connection:
            if self._mission_row(connection, tenant_id, evidence.mission_id) is None:
                raise LookupError("mission does not exist")
            return self._idempotent_record(
                connection,
                table=mission_evidence,
                key=and_(
                    mission_evidence.c.tenant_id == tenant_id,
                    mission_evidence.c.mission_id == evidence.mission_id,
                    mission_evidence.c.evidence_id == evidence.evidence_id,
                ),
                fingerprint=fingerprint,
                values={
                    "tenant_id": tenant_id, "mission_id": evidence.mission_id,
                    "evidence_id": evidence.evidence_id, "fingerprint": fingerprint,
                    "sha256": evidence.sha256,
                    "contains_personal_data": evidence.contains_personal_data,
                    "retention_until": _time(evidence.retention_until),
                    "observed_at": _time(evidence.observed_at),
                    "recorded_at": _time(evidence.recorded_at), "evidence": raw,
                },
                label="evidence",
            )

    def add_claim(self, tenant_id: str, claim: Claim) -> bool:
        raw = claim.to_dict()
        fingerprint = canonical_fingerprint(raw)
        with self._tenant_connection(tenant_id) as connection:
            if self._mission_row(connection, tenant_id, claim.mission_id) is None:
                raise LookupError("mission does not exist")
            if claim.evidence_ids:
                rows = connection.execute(select(mission_evidence.c.evidence_id).where(and_(
                    mission_evidence.c.tenant_id == tenant_id,
                    mission_evidence.c.mission_id == claim.mission_id,
                    mission_evidence.c.evidence_id.in_(claim.evidence_ids),
                ))).scalars().all()
                if set(rows) != set(claim.evidence_ids):
                    raise ValueError("claim references unknown evidence")
            referenced = set(claim.depends_on_claim_ids)
            if claim.supersedes_claim_id is not None:
                referenced.add(claim.supersedes_claim_id)
            if referenced:
                rows = connection.execute(select(mission_claims.c.claim_id).where(and_(
                    mission_claims.c.tenant_id == tenant_id,
                    mission_claims.c.mission_id == claim.mission_id,
                    mission_claims.c.claim_id.in_(referenced),
                ))).scalars().all()
                if set(rows) != referenced:
                    raise ValueError("claim references unknown claim dependencies")
            return self._idempotent_record(
                connection,
                table=mission_claims,
                key=and_(
                    mission_claims.c.tenant_id == tenant_id,
                    mission_claims.c.mission_id == claim.mission_id,
                    mission_claims.c.claim_id == claim.claim_id,
                ),
                fingerprint=fingerprint,
                values={
                    "tenant_id": tenant_id, "mission_id": claim.mission_id,
                    "claim_id": claim.claim_id, "fingerprint": fingerprint,
                    "status": claim.status.value, "valid_from": _time(claim.valid_from),
                    "valid_to": _time(claim.valid_to), "recorded_at": _time(claim.recorded_at),
                    "claim": raw,
                },
                label="claim",
            )

    def tombstone_evidence(
        self,
        *,
        tenant_id: str,
        mission_id: str,
        evidence_id: str,
        erased_by: str,
        reason: str,
    ) -> Mapping[str, Any]:
        if not erased_by.strip() or not reason.strip():
            raise ValueError("evidence erasure requires actor and reason")
        with self._tenant_connection(tenant_id) as connection:
            exists = connection.execute(select(mission_evidence.c.evidence_id).where(and_(
                mission_evidence.c.tenant_id == tenant_id,
                mission_evidence.c.mission_id == mission_id,
                mission_evidence.c.evidence_id == evidence_id,
            ))).scalar_one_or_none()
            if exists is None:
                raise LookupError("evidence does not exist")
            prior = connection.execute(select(evidence_tombstones).where(and_(
                evidence_tombstones.c.tenant_id == tenant_id,
                evidence_tombstones.c.mission_id == mission_id,
                evidence_tombstones.c.evidence_id == evidence_id,
            ))).mappings().first()
            if prior is not None:
                if prior["erased_by"] != erased_by or prior["reason"] != reason:
                    raise ValueError("evidence was already erased with different facts")
                return {"evidence_id": evidence_id, "erased": True, "duplicate": True}
            connection.execute(insert(evidence_tombstones).values(
                tenant_id=tenant_id, mission_id=mission_id, evidence_id=evidence_id,
                erased_at=_now(), erased_by=erased_by, reason=reason,
            ))
            return {"evidence_id": evidence_id, "erased": True, "duplicate": False}

    def add_hazard(self, tenant_id: str, hazard: Hazard) -> bool:
        raw = hazard.to_dict()
        fingerprint = canonical_fingerprint(raw)
        with self._tenant_connection(tenant_id) as connection:
            if self._mission_row(connection, tenant_id, hazard.mission_id) is None:
                raise LookupError("mission does not exist")
            return self._idempotent_record(
                connection,
                table=mission_hazards,
                key=and_(
                    mission_hazards.c.tenant_id == tenant_id,
                    mission_hazards.c.mission_id == hazard.mission_id,
                    mission_hazards.c.hazard_id == hazard.hazard_id,
                ),
                fingerprint=fingerprint,
                values={
                    "tenant_id": tenant_id, "mission_id": hazard.mission_id,
                    "hazard_id": hazard.hazard_id, "fingerprint": fingerprint,
                    "severity": hazard.severity.value, "hazard": raw, "created_at": _now(),
                },
                label="hazard",
            )

    def grant_authority(self, grant: AuthorityGrant) -> bool:
        raw = grant.to_dict()
        fingerprint = canonical_fingerprint(raw)
        with self._tenant_connection(grant.tenant_id) as connection:
            mission = self._mission_row(connection, grant.tenant_id, grant.mission_id, lock=True)
            if mission is None:
                raise LookupError("mission does not exist")
            spec = MissionSpec.from_dict(mission["spec"])
            if grant.parent_grant_id is None:
                if grant.principal_id != spec.principal_id:
                    raise ValueError("root authority principal must match the mission principal")
                if grant.mission_revision != spec.revision:
                    raise ValueError("root authority must bind the current mission revision")
                if grant.budget_limit_cents > spec.budget_limit_cents:
                    raise ValueError("root authority cannot exceed the mission budget")
            else:
                parent_raw = connection.execute(select(
                    mission_authorities.c.grant_record,
                    mission_authorities.c.revoked,
                ).where(and_(
                    mission_authorities.c.tenant_id == grant.tenant_id,
                    mission_authorities.c.mission_id == grant.mission_id,
                    mission_authorities.c.grant_id == grant.parent_grant_id,
                ))).mappings().first()
                if parent_raw is None or parent_raw["revoked"]:
                    raise ValueError("parent authority is missing or revoked")
                validate_delegation(grant, AuthorityGrant.from_dict(parent_raw["grant_record"]))
            return self._idempotent_record(
                connection,
                table=mission_authorities,
                key=and_(
                    mission_authorities.c.tenant_id == grant.tenant_id,
                    mission_authorities.c.mission_id == grant.mission_id,
                    mission_authorities.c.grant_id == grant.grant_id,
                ),
                fingerprint=fingerprint,
                values={
                    "tenant_id": grant.tenant_id, "mission_id": grant.mission_id,
                    "grant_id": grant.grant_id, "fingerprint": fingerprint,
                    "delegate_id": grant.delegate_id,
                    "parent_grant_id": grant.parent_grant_id,
                    "expires_at": _time(grant.expires_at), "revoked": False,
                    "grant_record": raw, "created_at": _now(),
                },
                label="authority grant",
            )

    def revoke_authority(
        self,
        *,
        tenant_id: str,
        mission_id: str,
        grant_id: str,
        revoked_by: str,
        reason: str,
    ) -> Mapping[str, Any]:
        if not revoked_by.strip() or not reason.strip():
            raise ValueError("authority revocation requires actor and reason")
        with self._tenant_connection(tenant_id) as connection:
            row = connection.execute(select(mission_authorities).where(and_(
                mission_authorities.c.tenant_id == tenant_id,
                mission_authorities.c.mission_id == mission_id,
                mission_authorities.c.grant_id == grant_id,
            )).with_for_update()).mappings().first()
            if row is None:
                raise LookupError("authority grant does not exist")
            if row["revoked"]:
                if row["revoked_by"] != revoked_by or row["revocation_reason"] != reason:
                    raise ValueError("authority was already revoked with different facts")
                return {"grant_id": grant_id, "revoked": True, "duplicate": True}
            connection.execute(update(mission_authorities).where(and_(
                mission_authorities.c.tenant_id == tenant_id,
                mission_authorities.c.mission_id == mission_id,
                mission_authorities.c.grant_id == grant_id,
            )).values(
                revoked=True, revoked_at=_now(), revoked_by=revoked_by,
                revocation_reason=reason,
            ))
            return {"grant_id": grant_id, "revoked": True, "duplicate": False}

    @staticmethod
    def _insert_budget_transaction(
        connection,
        *,
        tenant_id: str,
        mission_id: str,
        transaction_id: str,
        lines: tuple[tuple[str, int], ...],
        effect_id: str | None,
        reason: str,
        now: datetime,
    ) -> None:
        if not lines or sum(amount for _, amount in lines) != 0:
            raise ValueError("budget transaction must contain balanced entries")
        prior = connection.execute(select(budget_entries.c.transaction_id).where(and_(
            budget_entries.c.tenant_id == tenant_id,
            budget_entries.c.mission_id == mission_id,
            budget_entries.c.transaction_id == transaction_id,
        )).limit(1)).scalar_one_or_none()
        if prior is not None:
            return
        connection.execute(insert(budget_entries), [{
            "tenant_id": tenant_id, "mission_id": mission_id,
            "transaction_id": transaction_id, "position": position,
            "account": account, "amount_cents": amount, "effect_id": effect_id,
            "reason": reason, "created_at": now,
        } for position, (account, amount) in enumerate(lines)])

    @staticmethod
    def _budget_summary(connection, tenant_id: str, mission_id: str) -> dict[str, int]:
        rows = connection.execute(select(
            budget_entries.c.account, budget_entries.c.amount_cents,
        ).where(and_(
            budget_entries.c.tenant_id == tenant_id,
            budget_entries.c.mission_id == mission_id,
        ))).all()
        balances = {"authorized": 0, "available": 0, "reserved": 0, "spent": 0}
        for account, amount in rows:
            balances[str(account)] = balances.get(str(account), 0) + int(amount)
        if sum(balances.values()) != 0:
            raise RuntimeError("mission budget ledger is unbalanced")
        return {
            "authorized_cents": -balances["authorized"],
            "available_cents": balances["available"],
            "reserved_cents": balances["reserved"],
            "spent_cents": balances["spent"],
        }

    def budget_summary(self, tenant_id: str, mission_id: str) -> Mapping[str, int]:
        with self._tenant_connection(tenant_id) as connection:
            if self._mission_row(connection, tenant_id, mission_id) is None:
                raise LookupError("mission does not exist")
            return self._budget_summary(connection, tenant_id, mission_id)

    def admit_effect(self, effect: EffectRequest) -> Mapping[str, Any]:
        raw = effect.to_dict()
        fingerprint = canonical_fingerprint(raw)
        now = _now()
        with self._tenant_connection(effect.tenant_id) as connection:
            existing = connection.execute(select(mission_effects).where(and_(
                mission_effects.c.tenant_id == effect.tenant_id,
                mission_effects.c.mission_id == effect.mission_id,
                mission_effects.c.effect_id == effect.effect_id,
            )).with_for_update()).mappings().first()
            if existing is not None:
                stored_request = EffectRequest.from_dict(existing["request"]).to_dict()
                retried_request = dict(raw)
                # Request time is observation metadata, not semantic effect
                # identity. A crash retry keeps the originally recorded time
                # while proving every authority/input/action field is equal.
                retried_request["requested_at"] = stored_request["requested_at"]
                if canonical_fingerprint(stored_request) != canonical_fingerprint(
                    retried_request
                ):
                    raise ValueError("effect identity already exists with different content")
                decision = connection.execute(select(
                    assurance_decisions.c.decision,
                ).where(and_(
                    assurance_decisions.c.tenant_id == effect.tenant_id,
                    assurance_decisions.c.mission_id == effect.mission_id,
                    assurance_decisions.c.effect_id == effect.effect_id,
                ))).scalar_one()
                return {"effect": dict(existing["request"]), "decision": decision,
                        "status": existing["status"], "duplicate": True}
            prior_idempotency = connection.execute(select(
                mission_effects.c.effect_id,
            ).where(and_(
                mission_effects.c.tenant_id == effect.tenant_id,
                mission_effects.c.mission_id == effect.mission_id,
                mission_effects.c.idempotency_key == effect.idempotency_key,
            ))).scalar_one_or_none()
            if prior_idempotency is not None:
                raise ValueError("effect idempotency key was reused for another effect")

            mission = self._mission_row(
                connection, effect.tenant_id, effect.mission_id, lock=True,
            )
            if mission is None:
                raise LookupError("mission does not exist")
            grant_row = connection.execute(select(
                mission_authorities.c.grant_record,
                mission_authorities.c.revoked,
            ).where(and_(
                mission_authorities.c.tenant_id == effect.tenant_id,
                mission_authorities.c.mission_id == effect.mission_id,
                mission_authorities.c.grant_id == effect.authority_grant_id,
            )).with_for_update()).mappings().first()
            if grant_row is None:
                raise LookupError("authority grant does not exist")
            hazard_rows = connection.execute(select(
                mission_hazards.c.hazard,
            ).where(and_(
                mission_hazards.c.tenant_id == effect.tenant_id,
                mission_hazards.c.mission_id == effect.mission_id,
            ))).scalars().all()
            budget = self._budget_summary(connection, effect.tenant_id, effect.mission_id)
            authority = AuthorityGrant.from_dict(grant_row["grant_record"])
            authority_rows = connection.execute(select(
                mission_authorities.c.grant_id,
                mission_authorities.c.parent_grant_id,
                mission_authorities.c.grant_record,
                mission_authorities.c.revoked,
            ).where(and_(
                mission_authorities.c.tenant_id == effect.tenant_id,
                mission_authorities.c.mission_id == effect.mission_id,
            ))).mappings().all()
            authorities_by_id = {str(row["grant_id"]): row for row in authority_rows}

            lineage: list[str] = []
            cursor: str | None = authority.grant_id
            while cursor is not None:
                if cursor in lineage:
                    raise RuntimeError("authority delegation lineage contains a cycle")
                row = authorities_by_id.get(cursor)
                if row is None:
                    raise RuntimeError("authority delegation lineage is incomplete")
                lineage.append(cursor)
                parent = row["parent_grant_id"]
                cursor = None if parent is None else str(parent)

            authority_committed = {grant_id: 0 for grant_id in lineage}
            prior_effects = connection.execute(select(
                mission_effects.c.request,
                mission_effects.c.reserved_cents,
                mission_effects.c.actual_cents,
                mission_effects.c.status,
            ).where(and_(
                mission_effects.c.tenant_id == effect.tenant_id,
                mission_effects.c.mission_id == effect.mission_id,
            ))).mappings().all()
            for prior in prior_effects:
                prior_grant_id = str(prior["request"].get("authority_grant_id") or "")
                amount = 0
                if prior["status"] == "admitted":
                    amount = int(prior["reserved_cents"])
                elif prior["status"] == "succeeded":
                    amount = int(prior["actual_cents"] or 0)
                if amount == 0:
                    continue
                seen: set[str] = set()
                while prior_grant_id and prior_grant_id not in seen:
                    seen.add(prior_grant_id)
                    if prior_grant_id in authority_committed:
                        authority_committed[prior_grant_id] += amount
                    prior_authority = authorities_by_id.get(prior_grant_id)
                    if prior_authority is None or prior_authority["parent_grant_id"] is None:
                        break
                    prior_grant_id = str(prior_authority["parent_grant_id"])
            lineage_available = min(
                max(
                    0,
                    AuthorityGrant.from_dict(authorities_by_id[grant_id]["grant_record"])
                    .budget_limit_cents
                    - authority_committed[grant_id],
                )
                for grant_id in lineage
            )
            lineage_revoked = any(
                bool(authorities_by_id[grant_id]["revoked"]) for grant_id in lineage
            )
            decision = self._assurance.decide(
                mission=MissionSpec.from_dict(mission["spec"]),
                effect=effect,
                authority=authority,
                hazards=tuple(Hazard.from_dict(item) for item in hazard_rows),
                available_budget_cents=budget["available_cents"],
                available_authority_budget_cents=lineage_available,
                authority_revoked=lineage_revoked,
                now=now,
            )
            reservation_id = None
            status = decision.disposition.value
            if decision.disposition is AssuranceDisposition.ALLOWED:
                reservation_id = "reservation-" + hashlib.sha256(
                    f"agent-os:budget:v1:{effect.tenant_id}:{effect.effect_id}".encode()
                ).hexdigest()[:40]
                decision = replace(decision, reservation_id=reservation_id)
                status = "admitted"
            connection.execute(insert(mission_effects).values(
                tenant_id=effect.tenant_id, mission_id=effect.mission_id,
                effect_id=effect.effect_id, idempotency_key=effect.idempotency_key,
                fingerprint=fingerprint, status=status, request=raw,
                reservation_id=reservation_id,
                reserved_cents=(
                    effect.estimated_cost_cents if reservation_id is not None else 0
                ),
                created_at=now,
            ))
            if reservation_id is not None:
                self._insert_budget_transaction(
                    connection,
                    tenant_id=effect.tenant_id,
                    mission_id=effect.mission_id,
                    transaction_id=reservation_id,
                    lines=(("available", -effect.estimated_cost_cents),
                           ("reserved", effect.estimated_cost_cents)),
                    effect_id=effect.effect_id,
                    reason="effect budget reserved after assurance admission",
                    now=now,
                )
            connection.execute(insert(assurance_decisions).values(
                tenant_id=effect.tenant_id, mission_id=effect.mission_id,
                decision_id=decision.decision_id, effect_id=effect.effect_id,
                disposition=decision.disposition.value, decision=decision.to_dict(),
                created_at=now,
            ))
            return {
                "effect": raw, "decision": decision.to_dict(), "status": status,
                "duplicate": False,
            }

    def settle_effect(
        self,
        *,
        tenant_id: str,
        mission_id: str,
        effect_id: str,
        actual_cost_cents: int,
        succeeded: bool,
    ) -> Mapping[str, Any]:
        if actual_cost_cents < 0:
            raise ValueError("actual effect cost cannot be negative")
        now = _now()
        with self._tenant_connection(tenant_id) as connection:
            row = connection.execute(select(mission_effects).where(and_(
                mission_effects.c.tenant_id == tenant_id,
                mission_effects.c.mission_id == mission_id,
                mission_effects.c.effect_id == effect_id,
            )).with_for_update()).mappings().first()
            if row is None:
                raise LookupError("effect does not exist")
            if row["status"] in {"succeeded", "failed"}:
                if int(row["actual_cents"] or 0) != actual_cost_cents:
                    raise ValueError("effect was already settled with a different cost")
                expected_status = "succeeded" if succeeded else "failed"
                if row["status"] != expected_status:
                    raise ValueError("effect was already settled with a different outcome")
                return {"effect_id": effect_id, "status": row["status"], "duplicate": True}
            if row["status"] != "admitted" or row["reservation_id"] is None:
                raise ValueError("only an admitted and reserved effect can be settled")
            reserved = int(row["reserved_cents"])
            if actual_cost_cents > reserved:
                raise ValueError("actual cost exceeds the admitted reservation")
            transaction_id = f"settle:{row['reservation_id']}"
            lines: tuple[tuple[str, int], ...]
            if succeeded:
                lines = (("reserved", -reserved), ("spent", actual_cost_cents),
                         ("available", reserved - actual_cost_cents))
                status = "succeeded"
                reason = "effect completed and reservation reconciled"
            else:
                lines = (("reserved", -reserved), ("available", reserved))
                status = "failed"
                reason = "failed effect released its reservation"
            self._insert_budget_transaction(
                connection,
                tenant_id=tenant_id,
                mission_id=mission_id,
                transaction_id=transaction_id,
                lines=lines,
                effect_id=effect_id,
                reason=reason,
                now=now,
            )
            connection.execute(update(mission_effects).where(and_(
                mission_effects.c.tenant_id == tenant_id,
                mission_effects.c.mission_id == mission_id,
                mission_effects.c.effect_id == effect_id,
            )).values(status=status, actual_cents=actual_cost_cents, completed_at=now))
            return {"effect_id": effect_id, "status": status, "duplicate": False}

    def control_view(self, tenant_id: str, mission_id: str) -> Mapping[str, Any] | None:
        with self._tenant_connection(tenant_id) as connection:
            mission = self._mission_row(connection, tenant_id, mission_id)
            if mission is None:
                return None
            where = lambda table: and_(
                table.c.tenant_id == tenant_id, table.c.mission_id == mission_id,
            )
            claims = [dict(item) for item in connection.execute(select(mission_claims.c.claim).where(
                where(mission_claims)
            ).order_by(mission_claims.c.recorded_at)).scalars().all()]
            invalidated = {
                str(item.get("claim_id"))
                for item in claims if item.get("status") == "invalidated"
            }
            invalidated.update(
                str(item["supersedes_claim_id"])
                for item in claims
                if item.get("status") == "invalidated" and item.get("supersedes_claim_id")
            )
            changed = True
            while changed:
                changed = False
                for item in claims:
                    claim_id = str(item.get("claim_id") or "")
                    if claim_id in invalidated:
                        continue
                    dependencies = {str(value) for value in item.get("depends_on_claim_ids", ())}
                    if dependencies & invalidated:
                        invalidated.add(claim_id)
                        changed = True
            for item in claims:
                item["effective_status"] = (
                    "invalidated_by_dependency"
                    if item.get("claim_id") in invalidated and item.get("status") != "invalidated"
                    else item.get("status")
                )
            evidence = [dict(item) for item in connection.execute(select(mission_evidence.c.evidence).where(
                where(mission_evidence)
            ).order_by(mission_evidence.c.recorded_at)).scalars().all()]
            tombstones = {
                row["evidence_id"]: row
                for row in connection.execute(select(evidence_tombstones).where(
                    where(evidence_tombstones)
                )).mappings()
            }
            for item in evidence:
                tombstone = tombstones.get(item.get("evidence_id"))
                if tombstone is None:
                    item["erased"] = False
                    continue
                item["erased"] = True
                item["erased_at"] = tombstone["erased_at"].isoformat()
                item["erased_by"] = tombstone["erased_by"]
                item["erasure_reason"] = tombstone["reason"]
                item["artifact_ref"] = f"erased:{item['sha256']}"
                item["source_uri"] = None
            hazards = connection.execute(select(mission_hazards.c.hazard).where(
                where(mission_hazards)
            ).order_by(mission_hazards.c.hazard_id)).scalars().all()
            authorities = connection.execute(select(
                mission_authorities.c.grant_record,
                mission_authorities.c.revoked,
                mission_authorities.c.revoked_at,
                mission_authorities.c.revoked_by,
                mission_authorities.c.revocation_reason,
            ).where(where(mission_authorities)).order_by(
                mission_authorities.c.created_at
            )).mappings().all()
            revisions = [{
                **dict(row["spec"]),
                "revised_by": row["revised_by"],
                "revision_reason": row["reason"],
                "revision_recorded_at": row["created_at"].isoformat(),
            } for row in connection.execute(select(mission_revisions).where(
                where(mission_revisions)
            ).order_by(mission_revisions.c.revision)).mappings().all()]
            effects = connection.execute(select(mission_effects).where(
                where(mission_effects)
            ).order_by(mission_effects.c.created_at.desc())).mappings().all()
            decisions = {
                row["effect_id"]: row["decision"]
                for row in connection.execute(select(
                    assurance_decisions.c.effect_id,
                    assurance_decisions.c.decision,
                ).where(where(assurance_decisions))).mappings()
            }
            return {
                "mission": {**dict(mission["spec"]), "status": mission["status"]},
                "mission_revisions": revisions,
                "budget": self._budget_summary(connection, tenant_id, mission_id),
                "claims": claims,
                "evidence": list(evidence),
                "hazards": list(hazards),
                "authorities": [
                    {
                        **dict(row["grant_record"]), "revoked": bool(row["revoked"]),
                        "revoked_at": (
                            None if row["revoked_at"] is None else row["revoked_at"].isoformat()
                        ),
                        "revoked_by": row["revoked_by"],
                        "revocation_reason": row["revocation_reason"],
                    }
                    for row in authorities
                ],
                "effects": [{
                    "request": dict(row["request"]),
                    "status": row["status"],
                    "reservation_id": row["reservation_id"],
                    "reserved_cents": int(row["reserved_cents"]),
                    "actual_cents": row["actual_cents"],
                    "decision": decisions.get(row["effect_id"]),
                } for row in effects],
            }

    def close(self) -> None:
        self._engine.dispose()
