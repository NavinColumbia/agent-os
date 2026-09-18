"""Evidence contracts for product judgment and customer-value claims.

Synthetic users and LLM judges are useful discovery instruments, but they are
not customers and cannot be the sole authority for a product or release
decision.  This module keeps that boundary deterministic:

* canonical tasks and measures are versioned before observations arrive;
* every observation cites durable evidence;
* synthetic comparisons must be blinded, repeated and counterbalanced;
* deterministic failures cannot be voted away;
* adoption requires representative human or production evidence; and
* extra agent cost/latency is compared with a named simpler baseline.

The contracts are deliberately provider and UI-framework independent.  A
browser explorer, a Figma adapter, a human study, and production telemetry can
all produce observations without any one of them becoming the source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import isfinite
from statistics import fmean
from typing import Any, Iterable, Mapping


class EvidenceKind(str, Enum):
    DETERMINISTIC = "deterministic"
    SYNTHETIC = "synthetic"
    HUMAN = "human"
    PRODUCTION = "production"


class MetricDirection(str, Enum):
    HIGHER = "higher"
    LOWER = "lower"


class ProductDisposition(str, Enum):
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    REJECT = "reject"
    HUMAN_VALIDATION_REQUIRED = "human_validation_required"
    CONTROLLED_EXPERIMENT = "controlled_experiment"
    ADOPT = "adopt"


@dataclass(frozen=True)
class ProductMetric:
    metric_id: str
    direction: MetricDirection
    minimum_delta: float = 0.0
    hard_gate: bool = False

    def __post_init__(self) -> None:
        if not self.metric_id.strip():
            raise ValueError("product metric ID is required")
        if not isfinite(self.minimum_delta) or self.minimum_delta < 0:
            raise ValueError("product metric minimum delta cannot be negative")


@dataclass(frozen=True)
class ProductStudy:
    study_id: str
    revision: int
    hypothesis: str
    baseline_variant_id: str
    candidate_variant_id: str
    canonical_task_ids: tuple[str, ...]
    metrics: tuple[ProductMetric, ...]
    representative_segments: tuple[str, ...]
    synthetic_repetitions: int = 3
    require_human_or_production: bool = True

    def __post_init__(self) -> None:
        identity = (
            self.study_id,
            self.hypothesis,
            self.baseline_variant_id,
            self.candidate_variant_id,
        )
        if any(not value.strip() for value in identity):
            raise ValueError("product study identity and hypothesis are required")
        if self.revision < 1:
            raise ValueError("product study revision must be positive")
        if self.baseline_variant_id == self.candidate_variant_id:
            raise ValueError("product study requires distinct baseline and candidate variants")
        if (
            not self.canonical_task_ids
            or any(not task_id.strip() for task_id in self.canonical_task_ids)
            or len(set(self.canonical_task_ids)) != len(self.canonical_task_ids)
        ):
            raise ValueError("product study requires unique canonical tasks")
        if not self.metrics or len({metric.metric_id for metric in self.metrics}) != len(
            self.metrics
        ):
            raise ValueError("product study requires unique metrics")
        if (
            not self.representative_segments
            or any(not segment.strip() for segment in self.representative_segments)
            or len(set(self.representative_segments)) != len(self.representative_segments)
        ):
            raise ValueError("product study requires unique representative segments")
        if self.synthetic_repetitions < 3:
            raise ValueError("synthetic evaluation requires at least three repetitions")


@dataclass(frozen=True)
class ProductObservation:
    observation_id: str
    study_id: str
    study_revision: int
    task_id: str
    segment_id: str
    variant_id: str
    evaluator_id: str
    evidence_kind: EvidenceKind
    metrics: Mapping[str, float]
    evidence_ids: tuple[str, ...]
    repeat_index: int = 0
    presentation_position: int | None = None
    blinded: bool = False
    critical_failures: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        identity = (
            self.observation_id,
            self.study_id,
            self.task_id,
            self.segment_id,
            self.variant_id,
            self.evaluator_id,
        )
        if any(not value.strip() for value in identity):
            raise ValueError("product observation identity and provenance are required")
        if self.study_revision < 1 or self.repeat_index < 0:
            raise ValueError("product observation revision and repeat index are invalid")
        if self.presentation_position not in (None, 1, 2):
            raise ValueError("presentation position must be one, two, or absent")
        if not self.metrics:
            raise ValueError("product observation requires measured results")
        if not self.evidence_ids or any(not evidence_id.strip() for evidence_id in self.evidence_ids):
            raise ValueError("product observation requires durable evidence")
        if any(
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not isfinite(float(value))
            for value in self.metrics.values()
        ):
            raise ValueError("product observation metrics must be numeric")


@dataclass(frozen=True)
class ProductDecision:
    disposition: ProductDisposition
    reasons: tuple[str, ...]
    metric_deltas: Mapping[str, float]
    evidence_mix: Mapping[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "disposition": self.disposition.value,
            "reasons": list(self.reasons),
            "metric_deltas": dict(self.metric_deltas),
            "evidence_mix": dict(self.evidence_mix),
        }


@dataclass(frozen=True)
class SystemOutcome:
    system_id: str
    task_count: int
    success_rate: float
    quality_score: float
    reliability_rate: float
    p95_latency_seconds: float
    model_cost_cents: float
    human_minutes: float
    interventions: int
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.system_id.strip() or self.task_count < 1:
            raise ValueError("system outcome identity and task count are required")
        for value in (self.success_rate, self.quality_score, self.reliability_rate):
            if not isfinite(value) or not 0 <= value <= 1:
                raise ValueError("system outcome rates must be between zero and one")
        if any(
            isinstance(value, bool) or not isfinite(float(value)) or value < 0
            for value in (
                self.p95_latency_seconds,
                self.model_cost_cents,
                self.human_minutes,
                self.interventions,
            )
        ):
            raise ValueError("system outcome costs, latency and effort cannot be negative")
        if not self.evidence_ids or any(not evidence_id.strip() for evidence_id in self.evidence_ids):
            raise ValueError("system outcome requires durable evidence")


@dataclass(frozen=True)
class ValueReceipt:
    baseline_system_id: str
    candidate_system_id: str
    dominates_baseline: bool
    quality_delta: float
    success_delta: float
    reliability_delta: float
    latency_delta_seconds: float
    incremental_cost_cents: float
    human_minutes_saved: float
    estimated_human_value_cents: float
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline_system_id": self.baseline_system_id,
            "candidate_system_id": self.candidate_system_id,
            "dominates_baseline": self.dominates_baseline,
            "quality_delta": self.quality_delta,
            "success_delta": self.success_delta,
            "reliability_delta": self.reliability_delta,
            "latency_delta_seconds": self.latency_delta_seconds,
            "incremental_cost_cents": self.incremental_cost_cents,
            "human_minutes_saved": self.human_minutes_saved,
            "estimated_human_value_cents": self.estimated_human_value_cents,
            "reasons": list(self.reasons),
        }


def _means(
    observations: Iterable[ProductObservation],
    variant_id: str,
    metric_id: str,
    evidence_kind: EvidenceKind | None = None,
) -> float:
    values = [
        float(observation.metrics[metric_id])
        for observation in observations
        if observation.variant_id == variant_id
        and metric_id in observation.metrics
        and (evidence_kind is None or observation.evidence_kind is evidence_kind)
    ]
    if not values:
        raise ValueError(f"variant {variant_id} has no observations for metric {metric_id}")
    return fmean(values)


def _synthetic_validity_reasons(
    study: ProductStudy, observations: tuple[ProductObservation, ...],
) -> list[str]:
    synthetic = [
        observation for observation in observations
        if observation.evidence_kind is EvidenceKind.SYNTHETIC
    ]
    if not synthetic:
        return []
    reasons: list[str] = []
    if any(not observation.blinded for observation in synthetic):
        reasons.append("synthetic comparison was not blinded")
    groups: dict[tuple[str, str, str], list[ProductObservation]] = {}
    for observation in synthetic:
        groups.setdefault(
            (observation.evaluator_id, observation.task_id, observation.segment_id), []
        ).append(observation)
    expected_variants = {study.baseline_variant_id, study.candidate_variant_id}
    for (evaluator_id, task_id, segment_id), group in groups.items():
        variants = {observation.variant_id for observation in group}
        positions = {observation.presentation_position for observation in group}
        repeats = {observation.repeat_index for observation in group}
        if variants != expected_variants:
            reasons.append(
                f"synthetic evaluator {evaluator_id} did not compare both variants on "
                f"{task_id}/{segment_id}"
            )
        if positions != {1, 2}:
            reasons.append(
                f"synthetic evaluator {evaluator_id} was not position-counterbalanced on "
                f"{task_id}/{segment_id}"
            )
        if len(repeats) < study.synthetic_repetitions:
            reasons.append(
                f"synthetic evaluator {evaluator_id} has fewer than "
                f"{study.synthetic_repetitions} repetitions on {task_id}/{segment_id}"
            )
        for repeat_index in repeats:
            repeat_group = [
                observation for observation in group
                if observation.repeat_index == repeat_index
            ]
            if {observation.variant_id for observation in repeat_group} != expected_variants:
                reasons.append(
                    f"synthetic evaluator {evaluator_id} repeat {repeat_index} is not paired on "
                    f"{task_id}/{segment_id}"
                )
        for variant_id in expected_variants:
            variant_positions = {
                observation.presentation_position for observation in group
                if observation.variant_id == variant_id
            }
            if variant_positions != {1, 2}:
                reasons.append(
                    f"synthetic evaluator {evaluator_id} did not rotate {variant_id} across "
                    f"positions on {task_id}/{segment_id}"
                )
    return reasons


def evaluate_product_study(
    study: ProductStudy, observations: Iterable[ProductObservation],
) -> ProductDecision:
    """Evaluate a pre-registered product study without letting model taste become truth."""

    rows = tuple(observations)
    mix = {kind.value: 0 for kind in EvidenceKind}
    reasons: list[str] = []
    if not rows:
        return ProductDecision(
            ProductDisposition.INSUFFICIENT_EVIDENCE,
            ("the study has no observations",), {}, mix,
        )
    if len({row.observation_id for row in rows}) != len(rows):
        return ProductDecision(
            ProductDisposition.INSUFFICIENT_EVIDENCE,
            ("product observations contain duplicate identities",), {}, mix,
        )
    metric_ids = {metric.metric_id for metric in study.metrics}
    variants = {study.baseline_variant_id, study.candidate_variant_id}
    tasks = set(study.canonical_task_ids)
    segments = set(study.representative_segments)
    for row in rows:
        mix[row.evidence_kind.value] += 1
        if row.study_id != study.study_id or row.study_revision != study.revision:
            reasons.append(f"observation {row.observation_id} belongs to another study revision")
        if row.variant_id not in variants:
            reasons.append(f"observation {row.observation_id} names an unknown variant")
        if row.task_id not in tasks:
            reasons.append(f"observation {row.observation_id} names an unknown task")
        if row.segment_id not in segments:
            reasons.append(f"observation {row.observation_id} names an unregistered segment")
        missing = metric_ids - set(row.metrics)
        if missing:
            reasons.append(
                f"observation {row.observation_id} omits metrics: {', '.join(sorted(missing))}"
            )
    for task_id in tasks:
        for segment_id in segments:
            for variant_id in variants:
                if not any(
                    row.task_id == task_id
                    and row.segment_id == segment_id
                    and row.variant_id == variant_id
                    for row in rows
                ):
                    reasons.append(
                        f"task {task_id}/{segment_id} has no evidence for variant {variant_id}"
                    )
    for evidence_kind in (EvidenceKind.HUMAN, EvidenceKind.PRODUCTION):
        kind_rows = [row for row in rows if row.evidence_kind is evidence_kind]
        if not kind_rows:
            continue
        for task_id in tasks:
            for segment_id in segments:
                represented_variants = {
                    row.variant_id
                    for row in kind_rows
                    if row.task_id == task_id and row.segment_id == segment_id
                }
                if represented_variants != variants:
                    reasons.append(
                        f"{evidence_kind.value} evidence does not compare both variants on "
                        f"{task_id}/{segment_id}"
                    )
    if reasons:
        return ProductDecision(
            ProductDisposition.INSUFFICIENT_EVIDENCE,
            tuple(dict.fromkeys(reasons)), {}, mix,
        )

    critical = sorted({
        failure
        for row in rows
        if row.variant_id == study.candidate_variant_id
        for failure in row.critical_failures
    })
    if critical:
        return ProductDecision(
            ProductDisposition.REJECT,
            (f"critical failures remain: {', '.join(critical)}",), {}, mix,
        )

    synthetic_reasons = _synthetic_validity_reasons(study, rows)
    deltas: dict[str, float] = {}
    regressions: list[str] = []
    missed_targets: list[str] = []
    evidence_priority = (
        EvidenceKind.PRODUCTION,
        EvidenceKind.HUMAN,
        EvidenceKind.SYNTHETIC,
        EvidenceKind.DETERMINISTIC,
    )
    for metric in study.metrics:
        kind_deltas: dict[EvidenceKind, float] = {}
        for evidence_kind in evidence_priority:
            kind_rows = [row for row in rows if row.evidence_kind is evidence_kind]
            kind_variants = {row.variant_id for row in kind_rows}
            if kind_variants != variants:
                continue
            baseline = _means(
                kind_rows, study.baseline_variant_id, metric.metric_id, evidence_kind,
            )
            candidate = _means(
                kind_rows, study.candidate_variant_id, metric.metric_id, evidence_kind,
            )
            kind_deltas[evidence_kind] = (
                candidate - baseline
                if metric.direction is MetricDirection.HIGHER
                else baseline - candidate
            )
        if not kind_deltas:
            missed_targets.append(metric.metric_id)
            continue
        authoritative_kind = next(kind for kind in evidence_priority if kind in kind_deltas)
        signed_delta = kind_deltas[authoritative_kind]
        deltas[metric.metric_id] = round(signed_delta, 8)
        regressing_kinds = [
            kind.value for kind, delta in kind_deltas.items() if delta < 0
        ]
        if regressing_kinds:
            regressions.append(
                f"{metric.metric_id} ({', '.join(regressing_kinds)})"
            )
        if signed_delta < metric.minimum_delta:
            missed_targets.append(metric.metric_id)
        if metric.hard_gate and regressing_kinds:
            return ProductDecision(
                ProductDisposition.REJECT,
                (
                    f"candidate regressed hard-gate metric {metric.metric_id} in "
                    f"{', '.join(regressing_kinds)} evidence",
                ),
                deltas, mix,
            )
    if regressions:
        return ProductDecision(
            ProductDisposition.REJECT,
            (f"candidate regressed metrics: {', '.join(sorted(regressions))}",),
            deltas, mix,
        )
    if missed_targets:
        return ProductDecision(
            ProductDisposition.REJECT,
            (f"candidate missed admitted improvements: {', '.join(sorted(missed_targets))}",),
            deltas, mix,
        )
    if synthetic_reasons:
        return ProductDecision(
            ProductDisposition.INSUFFICIENT_EVIDENCE,
            tuple(dict.fromkeys(synthetic_reasons)), deltas, mix,
        )

    grounded = mix[EvidenceKind.HUMAN.value] + mix[EvidenceKind.PRODUCTION.value]
    if study.require_human_or_production and grounded == 0:
        return ProductDecision(
            ProductDisposition.HUMAN_VALIDATION_REQUIRED,
            ("synthetic and deterministic evidence may discover issues but cannot represent customers",),
            deltas, mix,
        )
    if mix[EvidenceKind.PRODUCTION.value] == 0:
        return ProductDecision(
            ProductDisposition.CONTROLLED_EXPERIMENT,
            ("offline evidence passed; validate the candidate in a bounded reversible experiment",),
            deltas, mix,
        )
    return ProductDecision(
        ProductDisposition.ADOPT,
        ("representative production evidence meets every pre-registered measure",),
        deltas, mix,
    )


def compare_system_value(
    baseline: SystemOutcome,
    candidate: SystemOutcome,
    *,
    customer_price_cents: float = 0,
    human_hourly_value_cents: float = 6_000,
    maximum_latency_regression_seconds: float | None = None,
) -> ValueReceipt:
    """Compare Agent OS with a named simpler baseline on matched tasks.

    This is intentionally conservative: better prose does not become invented
    dollars.  Only measured human time saved is monetized, while quality,
    success and reliability must be non-regressing constraints.
    """

    if baseline.task_count != candidate.task_count:
        raise ValueError("value comparison requires matched task counts")
    if any(
        not isfinite(value) or value < 0
        for value in (customer_price_cents, human_hourly_value_cents)
    ):
        raise ValueError("value comparison prices cannot be negative")
    if (
        maximum_latency_regression_seconds is not None
        and (
            not isfinite(maximum_latency_regression_seconds)
            or maximum_latency_regression_seconds < 0
        )
    ):
        raise ValueError("maximum latency regression cannot be negative")
    quality_delta = candidate.quality_score - baseline.quality_score
    success_delta = candidate.success_rate - baseline.success_rate
    reliability_delta = candidate.reliability_rate - baseline.reliability_rate
    latency_delta = candidate.p95_latency_seconds - baseline.p95_latency_seconds
    incremental_cost = (
        candidate.model_cost_cents + customer_price_cents - baseline.model_cost_cents
    )
    minutes_saved = baseline.human_minutes - candidate.human_minutes
    human_value = max(0.0, minutes_saved) * human_hourly_value_cents / 60
    reasons: list[str] = []
    if quality_delta < 0:
        reasons.append("quality regressed against the simpler baseline")
    if success_delta < 0:
        reasons.append("task success regressed against the simpler baseline")
    if reliability_delta < 0:
        reasons.append("reliability regressed against the simpler baseline")
    if incremental_cost > human_value:
        reasons.append("measured human-time value does not cover incremental cost")
    if candidate.interventions > baseline.interventions and minutes_saved <= 0:
        reasons.append("candidate requires more intervention without saving human time")
    if (
        maximum_latency_regression_seconds is not None
        and latency_delta > maximum_latency_regression_seconds
    ):
        reasons.append("candidate exceeded the admitted latency regression")
    meaningful_gain = any((
        quality_delta > 0,
        success_delta > 0,
        reliability_delta > 0,
        latency_delta < 0,
        incremental_cost < 0,
        minutes_saved > 0,
        candidate.interventions < baseline.interventions,
    ))
    if not meaningful_gain:
        reasons.append("candidate has no measured benefit over the simpler baseline")
    dominates = not reasons
    if dominates:
        reasons.append("candidate improves or preserves outcomes and covers incremental cost")
    return ValueReceipt(
        baseline.system_id,
        candidate.system_id,
        dominates,
        round(quality_delta, 8),
        round(success_delta, 8),
        round(reliability_delta, 8),
        round(latency_delta, 8),
        round(incremental_cost, 2),
        round(minutes_saved, 2),
        round(human_value, 2),
        tuple(reasons),
    )
