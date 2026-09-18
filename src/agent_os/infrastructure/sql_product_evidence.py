"""Append-only SQL adapter for evidence-backed product judgment."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Mapping

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    and_,
    create_engine,
    func,
    insert,
    or_,
    select,
    text,
)
from sqlalchemy.exc import IntegrityError

from agent_os.application.ports import ProductEvidenceStore
from agent_os.domain.product_evidence import (
    EvidenceKind,
    MetricDirection,
    ProductMetric,
    ProductObservation,
    ProductStudy,
    SystemOutcome,
    compare_system_value,
    calibrate_synthetic_judgments,
    evaluate_product_study,
)
from agent_os.infrastructure.dbos_lifecycle import sqlalchemy_url


product_evidence_metadata = MetaData()

product_studies = Table(
    "aos_v2_product_studies",
    product_evidence_metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("study_id", String(128), primary_key=True),
    Column("revision", Integer, primary_key=True),
    Column("study", JSON, nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("created_by", String(255), nullable=False),
    Column("idempotency_key", String(200), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint(
        "tenant_id", "created_by", "idempotency_key",
        name="uq_aos_v2_product_studies_idempotency",
    ),
)

product_observations = Table(
    "aos_v2_product_observations",
    product_evidence_metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("observation_id", String(128), primary_key=True),
    Column("study_id", String(128), nullable=False),
    Column("study_revision", Integer, nullable=False),
    Column("observation", JSON, nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("recorded_by", String(255), nullable=False),
    Column("idempotency_key", String(200), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ("tenant_id", "study_id", "study_revision"),
        (
            "aos_v2_product_studies.tenant_id",
            "aos_v2_product_studies.study_id",
            "aos_v2_product_studies.revision",
        ),
        ondelete="CASCADE",
    ),
    UniqueConstraint(
        "tenant_id", "recorded_by", "idempotency_key",
        name="uq_aos_v2_product_observations_idempotency",
    ),
)

product_decisions = Table(
    "aos_v2_product_decisions",
    product_evidence_metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("decision_id", String(80), primary_key=True),
    Column("study_id", String(128), nullable=False),
    Column("study_revision", Integer, nullable=False),
    Column("observation_set_sha256", String(64), nullable=False),
    Column("decision", JSON, nullable=False),
    Column("decided_by", String(255), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ("tenant_id", "study_id", "study_revision"),
        (
            "aos_v2_product_studies.tenant_id",
            "aos_v2_product_studies.study_id",
            "aos_v2_product_studies.revision",
        ),
        ondelete="CASCADE",
    ),
    UniqueConstraint(
        "tenant_id", "study_id", "study_revision", "observation_set_sha256",
        name="uq_aos_v2_product_decisions_observation_set",
    ),
)

product_value_receipts = Table(
    "aos_v2_product_value_receipts",
    product_evidence_metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("receipt_id", String(80), primary_key=True),
    Column("baseline_system_id", String(128), nullable=False),
    Column("candidate_system_id", String(128), nullable=False),
    Column("comparison", JSON, nullable=False),
    Column("receipt", JSON, nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("created_by", String(255), nullable=False),
    Column("idempotency_key", String(200), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint(
        "tenant_id", "created_by", "idempotency_key",
        name="uq_aos_v2_product_value_receipts_idempotency",
    ),
)

Index(
    "aos_v2_product_studies_timeline_idx",
    product_studies.c.tenant_id,
    product_studies.c.created_at.desc(),
    product_studies.c.study_id,
    product_studies.c.revision.desc(),
)
Index(
    "aos_v2_product_observations_study_idx",
    product_observations.c.tenant_id,
    product_observations.c.study_id,
    product_observations.c.study_revision,
    product_observations.c.created_at,
)
Index(
    "aos_v2_product_decisions_study_idx",
    product_decisions.c.tenant_id,
    product_decisions.c.study_id,
    product_decisions.c.study_revision,
    product_decisions.c.created_at.desc(),
)
Index(
    "aos_v2_product_value_receipts_timeline_idx",
    product_value_receipts.c.tenant_id,
    product_value_receipts.c.created_at.desc(),
)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _canonical(value: Mapping[str, Any] | list[Any]) -> str:
    return json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _digest(value: Mapping[str, Any] | list[Any]) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _study(raw: Mapping[str, Any]) -> ProductStudy:
    return ProductStudy(
        study_id=str(raw.get("study_id") or ""),
        revision=int(raw.get("revision") or 0),
        hypothesis=str(raw.get("hypothesis") or ""),
        baseline_variant_id=str(raw.get("baseline_variant_id") or ""),
        candidate_variant_id=str(raw.get("candidate_variant_id") or ""),
        canonical_task_ids=tuple(str(item) for item in raw.get("canonical_task_ids", ())),
        metrics=tuple(ProductMetric(
            metric_id=str(item.get("metric_id") or ""),
            direction=MetricDirection(str(item.get("direction") or "")),
            minimum_delta=float(item.get("minimum_delta", 0)),
            hard_gate=bool(item.get("hard_gate", False)),
        ) for item in raw.get("metrics", ())),
        representative_segments=tuple(
            str(item) for item in raw.get("representative_segments", ())
        ),
        synthetic_repetitions=int(raw.get("synthetic_repetitions", 3)),
        require_human_or_production=bool(raw.get("require_human_or_production", True)),
    )


def _study_dict(value: ProductStudy) -> dict[str, Any]:
    return {
        "study_id": value.study_id,
        "revision": value.revision,
        "hypothesis": value.hypothesis,
        "baseline_variant_id": value.baseline_variant_id,
        "candidate_variant_id": value.candidate_variant_id,
        "canonical_task_ids": list(value.canonical_task_ids),
        "metrics": [{
            "metric_id": item.metric_id,
            "direction": item.direction.value,
            "minimum_delta": item.minimum_delta,
            "hard_gate": item.hard_gate,
        } for item in value.metrics],
        "representative_segments": list(value.representative_segments),
        "synthetic_repetitions": value.synthetic_repetitions,
        "require_human_or_production": value.require_human_or_production,
    }


def _observation(raw: Mapping[str, Any]) -> ProductObservation:
    return ProductObservation(
        observation_id=str(raw.get("observation_id") or ""),
        study_id=str(raw.get("study_id") or ""),
        study_revision=int(raw.get("study_revision") or 0),
        task_id=str(raw.get("task_id") or ""),
        segment_id=str(raw.get("segment_id") or ""),
        variant_id=str(raw.get("variant_id") or ""),
        evaluator_id=str(raw.get("evaluator_id") or ""),
        evidence_kind=EvidenceKind(str(raw.get("evidence_kind") or "")),
        metrics={str(key): float(value) for key, value in dict(raw.get("metrics") or {}).items()},
        evidence_ids=tuple(str(item) for item in raw.get("evidence_ids", ())),
        repeat_index=int(raw.get("repeat_index", 0)),
        presentation_position=(
            None if raw.get("presentation_position") is None
            else int(raw["presentation_position"])
        ),
        blinded=bool(raw.get("blinded", False)),
        critical_failures=tuple(str(item) for item in raw.get("critical_failures", ())),
    )


def _observation_dict(value: ProductObservation) -> dict[str, Any]:
    return {
        "observation_id": value.observation_id,
        "study_id": value.study_id,
        "study_revision": value.study_revision,
        "task_id": value.task_id,
        "segment_id": value.segment_id,
        "variant_id": value.variant_id,
        "evaluator_id": value.evaluator_id,
        "evidence_kind": value.evidence_kind.value,
        "metrics": dict(value.metrics),
        "evidence_ids": list(value.evidence_ids),
        "repeat_index": value.repeat_index,
        "presentation_position": value.presentation_position,
        "blinded": value.blinded,
        "critical_failures": list(value.critical_failures),
    }


def _outcome(raw: Mapping[str, Any]) -> SystemOutcome:
    return SystemOutcome(
        system_id=str(raw.get("system_id") or ""),
        task_count=int(raw.get("task_count") or 0),
        success_rate=float(raw.get("success_rate", 0)),
        quality_score=float(raw.get("quality_score", 0)),
        reliability_rate=float(raw.get("reliability_rate", 0)),
        p95_latency_seconds=float(raw.get("p95_latency_seconds", 0)),
        model_cost_cents=float(raw.get("model_cost_cents", 0)),
        human_minutes=float(raw.get("human_minutes", 0)),
        interventions=int(raw.get("interventions", 0)),
        evidence_ids=tuple(str(item) for item in raw.get("evidence_ids", ())),
    )


def _outcome_dict(value: SystemOutcome) -> dict[str, Any]:
    return {
        "system_id": value.system_id,
        "task_count": value.task_count,
        "success_rate": value.success_rate,
        "quality_score": value.quality_score,
        "reliability_rate": value.reliability_rate,
        "p95_latency_seconds": value.p95_latency_seconds,
        "model_cost_cents": value.model_cost_cents,
        "human_minutes": value.human_minutes,
        "interventions": value.interventions,
        "evidence_ids": list(value.evidence_ids),
    }


class SQLProductEvidenceStore(ProductEvidenceStore):
    def __init__(self, database_url: str, *, create_schema: bool = False, clock=None) -> None:
        if not database_url.strip():
            raise ValueError("database_url is required")
        self._engine = create_engine(sqlalchemy_url(database_url), pool_pre_ping=True)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        if create_schema:
            product_evidence_metadata.create_all(self._engine)

    @contextmanager
    def _connection(self, tenant_id: str):
        tenant_id = tenant_id.strip()
        if not 1 <= len(tenant_id) <= 128 or "\0" in tenant_id:
            raise ValueError("product evidence tenant is invalid")
        with self._engine.begin() as connection:
            if connection.dialect.name == "postgresql":
                connection.execute(text("SET LOCAL ROLE agentos_app"))
                connection.execute(
                    text("SELECT set_config('app.tenant_id', :value, true)"),
                    {"value": tenant_id},
                )
            yield connection

    @staticmethod
    def _actor(actor_id: str, idempotency_key: str) -> tuple[str, str]:
        actor_id = actor_id.strip()
        idempotency_key = idempotency_key.strip()
        if not 1 <= len(actor_id) <= 255 or "\0" in actor_id:
            raise ValueError("product evidence actor is invalid")
        if not 8 <= len(idempotency_key) <= 200 or "\0" in idempotency_key:
            raise ValueError("product evidence idempotency key is invalid")
        return actor_id, idempotency_key

    @staticmethod
    def _study_record(row: Mapping[str, Any], *, duplicate: bool = False) -> dict[str, Any]:
        return {
            **dict(row["study"]),
            "created_by": row["created_by"],
            "created_at": _utc(row["created_at"]).isoformat(),
            "duplicate": duplicate,
        }

    @staticmethod
    def _observation_record(
        row: Mapping[str, Any], *, duplicate: bool = False,
    ) -> dict[str, Any]:
        return {
            **dict(row["observation"]),
            "recorded_by": row["recorded_by"],
            "created_at": _utc(row["created_at"]).isoformat(),
            "duplicate": duplicate,
        }

    @staticmethod
    def _decision_record(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "decision_id": row["decision_id"],
            "study_id": row["study_id"],
            "study_revision": row["study_revision"],
            "observation_set_sha256": row["observation_set_sha256"],
            **dict(row["decision"]),
            "decided_by": row["decided_by"],
            "created_at": _utc(row["created_at"]).isoformat(),
        }

    def create_study(
        self,
        *,
        tenant_id: str,
        study: Mapping[str, Any],
        created_by: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        created_by, idempotency_key = self._actor(created_by, idempotency_key)
        value = _study(study)
        payload = _study_dict(value)
        fingerprint = _digest(payload)
        now = _utc(self._clock())
        values = {
            "tenant_id": tenant_id,
            "study_id": value.study_id,
            "revision": value.revision,
            "study": payload,
            "fingerprint": fingerprint,
            "created_by": created_by,
            "idempotency_key": idempotency_key,
            "created_at": now,
        }
        try:
            with self._connection(tenant_id) as connection:
                prior = connection.execute(select(product_studies).where(and_(
                    product_studies.c.tenant_id == tenant_id,
                    (
                        and_(
                            product_studies.c.study_id == value.study_id,
                            product_studies.c.revision == value.revision,
                        )
                        | and_(
                            product_studies.c.created_by == created_by,
                            product_studies.c.idempotency_key == idempotency_key,
                        )
                    ),
                ))).mappings().first()
                if prior is not None:
                    if prior["fingerprint"] != fingerprint:
                        raise ValueError("product study identity or idempotency key was reused")
                    return self._study_record(prior, duplicate=True)
                connection.execute(insert(product_studies).values(**values))
        except IntegrityError as exc:
            raise ValueError("product study conflicted with concurrent immutable state") from exc
        return self._study_record(values)

    def append_observation(
        self,
        *,
        tenant_id: str,
        observation: Mapping[str, Any],
        recorded_by: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        recorded_by, idempotency_key = self._actor(recorded_by, idempotency_key)
        value = _observation(observation)
        payload = _observation_dict(value)
        fingerprint = _digest(payload)
        now = _utc(self._clock())
        values = {
            "tenant_id": tenant_id,
            "observation_id": value.observation_id,
            "study_id": value.study_id,
            "study_revision": value.study_revision,
            "observation": payload,
            "fingerprint": fingerprint,
            "recorded_by": recorded_by,
            "idempotency_key": idempotency_key,
            "created_at": now,
        }
        try:
            with self._connection(tenant_id) as connection:
                prior = connection.execute(select(product_observations).where(and_(
                    product_observations.c.tenant_id == tenant_id,
                    or_(
                        product_observations.c.observation_id == value.observation_id,
                        and_(
                            product_observations.c.recorded_by == recorded_by,
                            product_observations.c.idempotency_key == idempotency_key,
                        ),
                    ),
                ))).mappings().first()
                if prior is not None:
                    if prior["fingerprint"] != fingerprint:
                        raise ValueError("product observation identity or idempotency key was reused")
                    return self._observation_record(prior, duplicate=True)
                study_row = connection.execute(select(product_studies).where(and_(
                    product_studies.c.tenant_id == tenant_id,
                    product_studies.c.study_id == value.study_id,
                    product_studies.c.revision == value.study_revision,
                )).with_for_update()).mappings().one_or_none()
                if study_row is None:
                    raise ValueError("product observation study does not exist")
                admitted = _study(study_row["study"])
                if value.task_id not in admitted.canonical_task_ids:
                    raise ValueError("product observation task is not registered")
                if value.segment_id not in admitted.representative_segments:
                    raise ValueError("product observation segment is not registered")
                if value.variant_id not in {
                    admitted.baseline_variant_id, admitted.candidate_variant_id,
                }:
                    raise ValueError("product observation variant is not registered")
                if set(value.metrics) != {metric.metric_id for metric in admitted.metrics}:
                    raise ValueError("product observation metrics do not match the study")
                connection.execute(insert(product_observations).values(**values))
        except IntegrityError as exc:
            raise ValueError("product observation conflicted with concurrent immutable state") from exc
        return self._observation_record(values)

    def evaluate_study(
        self,
        *,
        tenant_id: str,
        study_id: str,
        revision: int,
        decided_by: str,
    ) -> Mapping[str, Any] | None:
        decided_by = decided_by.strip()
        if not 1 <= len(decided_by) <= 255 or revision < 1:
            raise ValueError("product decision identity is invalid")
        with self._connection(tenant_id) as connection:
            study_row = connection.execute(select(product_studies).where(and_(
                product_studies.c.tenant_id == tenant_id,
                product_studies.c.study_id == study_id,
                product_studies.c.revision == revision,
            )).with_for_update()).mappings().one_or_none()
            if study_row is None:
                return None
            rows = connection.execute(select(product_observations).where(and_(
                product_observations.c.tenant_id == tenant_id,
                product_observations.c.study_id == study_id,
                product_observations.c.study_revision == revision,
            )).order_by(product_observations.c.observation_id)).mappings().all()
            observation_set = [{
                "observation_id": row["observation_id"],
                "fingerprint": row["fingerprint"],
            } for row in rows]
            observation_set_sha256 = _digest(observation_set)
            decision_id = "product-decision-" + hashlib.sha256(
                f"{tenant_id}:{study_id}:{revision}:{observation_set_sha256}".encode()
            ).hexdigest()
            prior = connection.execute(select(product_decisions).where(and_(
                product_decisions.c.tenant_id == tenant_id,
                product_decisions.c.decision_id == decision_id,
            ))).mappings().one_or_none()
            if prior is not None:
                return self._decision_record(prior)
            decision = evaluate_product_study(
                _study(study_row["study"]),
                (_observation(row["observation"]) for row in rows),
            )
            values = {
                "tenant_id": tenant_id,
                "decision_id": decision_id,
                "study_id": study_id,
                "study_revision": revision,
                "observation_set_sha256": observation_set_sha256,
                "decision": decision.to_dict(),
                "decided_by": decided_by,
                "created_at": _utc(self._clock()),
            }
            connection.execute(insert(product_decisions).values(**values))
            return self._decision_record(values)

    def get_study(
        self, tenant_id: str, study_id: str, revision: int,
    ) -> Mapping[str, Any] | None:
        with self._connection(tenant_id) as connection:
            study_row = connection.execute(select(product_studies).where(and_(
                product_studies.c.tenant_id == tenant_id,
                product_studies.c.study_id == study_id,
                product_studies.c.revision == revision,
            ))).mappings().one_or_none()
            if study_row is None:
                return None
            observations = connection.execute(select(product_observations).where(and_(
                product_observations.c.tenant_id == tenant_id,
                product_observations.c.study_id == study_id,
                product_observations.c.study_revision == revision,
            )).order_by(
                product_observations.c.created_at,
                product_observations.c.observation_id,
            )).mappings().all()
            decisions = connection.execute(select(product_decisions).where(and_(
                product_decisions.c.tenant_id == tenant_id,
                product_decisions.c.study_id == study_id,
                product_decisions.c.study_revision == revision,
            )).order_by(
                product_decisions.c.created_at,
                product_decisions.c.decision_id,
            )).mappings().all()
        return {
            **self._study_record(study_row),
            "observations": [self._observation_record(row) for row in observations],
            "decisions": [self._decision_record(row) for row in decisions],
        }

    def list_studies(
        self, tenant_id: str, *, limit: int = 100,
    ) -> tuple[Mapping[str, Any], ...]:
        if not 1 <= limit <= 500:
            raise ValueError("product study limit is invalid")
        with self._connection(tenant_id) as connection:
            rows = connection.execute(select(product_studies).where(
                product_studies.c.tenant_id == tenant_id,
            ).order_by(
                product_studies.c.created_at.desc(),
                product_studies.c.study_id,
                product_studies.c.revision.desc(),
            ).limit(limit)).mappings().all()
            counts = {
                (row["study_id"], row["study_revision"]): int(row["count"])
                for row in connection.execute(select(
                    product_observations.c.study_id,
                    product_observations.c.study_revision,
                    func.count().label("count"),
                ).where(
                    product_observations.c.tenant_id == tenant_id,
                ).group_by(
                    product_observations.c.study_id,
                    product_observations.c.study_revision,
                )).mappings()
            }
            decisions = connection.execute(select(product_decisions).where(
                product_decisions.c.tenant_id == tenant_id,
            ).order_by(product_decisions.c.created_at.desc())).mappings().all()
        latest: dict[tuple[str, int], Mapping[str, Any]] = {}
        for row in decisions:
            latest.setdefault((row["study_id"], row["study_revision"]), row)
        return tuple({
            **self._study_record(row),
            "observation_count": counts.get((row["study_id"], row["revision"]), 0),
            "latest_decision": (
                self._decision_record(latest[(row["study_id"], row["revision"])])
                if (row["study_id"], row["revision"]) in latest else None
            ),
        } for row in rows)

    def calibration_report(
        self, tenant_id: str, study_id: str, revision: int,
    ) -> Mapping[str, Any] | None:
        with self._connection(tenant_id) as connection:
            study_row = connection.execute(select(product_studies).where(and_(
                product_studies.c.tenant_id == tenant_id,
                product_studies.c.study_id == study_id,
                product_studies.c.revision == revision,
            ))).mappings().one_or_none()
            if study_row is None:
                return None
            rows = connection.execute(select(product_observations).where(and_(
                product_observations.c.tenant_id == tenant_id,
                product_observations.c.study_id == study_id,
                product_observations.c.study_revision == revision,
            ))).mappings().all()
        return calibrate_synthetic_judgments(
            _study(study_row["study"]),
            (_observation(row["observation"]) for row in rows),
        ).to_dict()

    def record_value_receipt(
        self,
        *,
        tenant_id: str,
        baseline: Mapping[str, Any],
        candidate: Mapping[str, Any],
        customer_price_cents: float,
        human_hourly_value_cents: float,
        maximum_latency_regression_seconds: float | None,
        created_by: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        created_by, idempotency_key = self._actor(created_by, idempotency_key)
        baseline_value = _outcome(baseline)
        candidate_value = _outcome(candidate)
        comparison = {
            "baseline": _outcome_dict(baseline_value),
            "candidate": _outcome_dict(candidate_value),
            "customer_price_cents": customer_price_cents,
            "human_hourly_value_cents": human_hourly_value_cents,
            "maximum_latency_regression_seconds": maximum_latency_regression_seconds,
        }
        fingerprint = _digest(comparison)
        receipt = compare_system_value(
            baseline_value,
            candidate_value,
            customer_price_cents=customer_price_cents,
            human_hourly_value_cents=human_hourly_value_cents,
            maximum_latency_regression_seconds=maximum_latency_regression_seconds,
        ).to_dict()
        receipt_id = "value-receipt-" + hashlib.sha256(
            f"{tenant_id}:{created_by}:{idempotency_key}".encode()
        ).hexdigest()
        now = _utc(self._clock())
        values = {
            "tenant_id": tenant_id,
            "receipt_id": receipt_id,
            "baseline_system_id": baseline_value.system_id,
            "candidate_system_id": candidate_value.system_id,
            "comparison": comparison,
            "receipt": receipt,
            "fingerprint": fingerprint,
            "created_by": created_by,
            "idempotency_key": idempotency_key,
            "created_at": now,
        }
        try:
            with self._connection(tenant_id) as connection:
                prior = connection.execute(select(product_value_receipts).where(and_(
                    product_value_receipts.c.tenant_id == tenant_id,
                    product_value_receipts.c.created_by == created_by,
                    product_value_receipts.c.idempotency_key == idempotency_key,
                ))).mappings().one_or_none()
                if prior is not None:
                    if prior["fingerprint"] != fingerprint:
                        raise ValueError("value receipt idempotency key was reused")
                    return {
                        **dict(prior["receipt"]),
                        "receipt_id": prior["receipt_id"],
                        "comparison": dict(prior["comparison"]),
                        "created_by": prior["created_by"],
                        "created_at": _utc(prior["created_at"]).isoformat(),
                        "duplicate": True,
                    }
                connection.execute(insert(product_value_receipts).values(**values))
        except IntegrityError as exc:
            raise ValueError("value receipt conflicted with concurrent immutable state") from exc
        return {
            **receipt,
            "receipt_id": receipt_id,
            "comparison": comparison,
            "created_by": created_by,
            "created_at": now.isoformat(),
            "duplicate": False,
        }

    def list_value_receipts(
        self, tenant_id: str, *, limit: int = 100,
    ) -> tuple[Mapping[str, Any], ...]:
        if not 1 <= limit <= 500:
            raise ValueError("value receipt limit is invalid")
        with self._connection(tenant_id) as connection:
            rows = connection.execute(select(product_value_receipts).where(
                product_value_receipts.c.tenant_id == tenant_id,
            ).order_by(
                product_value_receipts.c.created_at.desc(),
                product_value_receipts.c.receipt_id,
            ).limit(limit)).mappings().all()
        return tuple({
            **dict(row["receipt"]),
            "receipt_id": row["receipt_id"],
            "comparison": dict(row["comparison"]),
            "created_by": row["created_by"],
            "created_at": _utc(row["created_at"]).isoformat(),
            "duplicate": False,
        } for row in rows)

    def close(self) -> None:
        self._engine.dispose()
