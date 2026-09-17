"""Eval-gated candidate and canary contracts for controlled self-improvement.

Agents may propose changes, but they cannot silently rewrite the assurance
kernel, policy, authority, evaluation sets, or their own promotion gate.  A
candidate is immutable, independently evaluated against a versioned holdout,
then exposed through a bounded canary with an explicit rollback artifact.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


_PROTECTED_SCOPES = frozenset({
    "assurance_kernel",
    "authority_model",
    "effect_policy",
    "evaluation_gate",
    "evaluation_dataset",
    "audit_ledger",
})


class PromotionDisposition(str, Enum):
    REJECT = "reject"
    HUMAN_REVIEW = "human_review"
    CANARY = "canary"
    PROMOTE = "promote"
    ROLLBACK = "rollback"


@dataclass(frozen=True)
class ImprovementCandidate:
    candidate_id: str
    tenant_id: str
    mission_id: str
    proposed_by: str
    base_revision: str
    artifact_ref: str
    artifact_sha256: str
    rollback_artifact_ref: str
    scopes: tuple[str, ...]
    hypothesis: str
    maximum_canary_percent: int = 5

    def __post_init__(self) -> None:
        if any(not value.strip() for value in (
            self.candidate_id, self.tenant_id, self.mission_id, self.proposed_by,
            self.base_revision, self.artifact_ref, self.rollback_artifact_ref,
            self.hypothesis,
        )):
            raise ValueError("improvement candidate identity and artifacts are required")
        if len(self.artifact_sha256) != 64:
            raise ValueError("candidate artifact requires a SHA-256 digest")
        if not self.scopes or not 1 <= self.maximum_canary_percent <= 25:
            raise ValueError("candidate scopes and a 1..25 percent canary are required")


@dataclass(frozen=True)
class EvaluationReceipt:
    receipt_id: str
    candidate_id: str
    evaluator_id: str
    dataset_id: str
    dataset_version: str
    production_window: str
    representative: bool
    holdout: bool
    baseline_metrics: Mapping[str, float]
    candidate_metrics: Mapping[str, float]
    critical_failures: tuple[str, ...]
    deterministic_checks_passed: bool
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if any(not value.strip() for value in (
            self.receipt_id, self.candidate_id, self.evaluator_id, self.dataset_id,
            self.dataset_version, self.production_window,
        )):
            raise ValueError("evaluation receipt identity and dataset provenance are required")
        if not self.baseline_metrics or set(self.baseline_metrics) != set(self.candidate_metrics):
            raise ValueError("baseline and candidate evaluations require the same metrics")
        if not self.evidence_ids:
            raise ValueError("evaluation receipt requires durable evidence")
        for value in (*self.baseline_metrics.values(), *self.candidate_metrics.values()):
            if not 0 <= value <= 1:
                raise ValueError("normalized evaluation metrics must be between zero and one")


@dataclass(frozen=True)
class PromotionDecision:
    disposition: PromotionDisposition
    reasons: tuple[str, ...]
    traffic_percent: int = 0
    requires_human: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "disposition": self.disposition.value,
            "reasons": list(self.reasons),
            "traffic_percent": self.traffic_percent,
            "requires_human": self.requires_human,
        }


def evaluate_candidate(
    candidate: ImprovementCandidate,
    receipt: EvaluationReceipt,
    *,
    minimum_mean_improvement: float = 0.01,
) -> PromotionDecision:
    reasons: list[str] = []
    if receipt.candidate_id != candidate.candidate_id:
        reasons.append("evaluation receipt belongs to another candidate")
    if receipt.evaluator_id == candidate.proposed_by:
        reasons.append("candidate maker cannot be its only evaluator")
    if not receipt.representative or not receipt.holdout:
        reasons.append("evaluation data is not a representative versioned holdout")
    if not receipt.deterministic_checks_passed:
        reasons.append("one or more deterministic checks failed")
    if receipt.critical_failures:
        reasons.append("candidate has critical evaluation failures")
    regressions = [
        name for name, baseline in receipt.baseline_metrics.items()
        if receipt.candidate_metrics[name] < baseline
    ]
    if regressions:
        reasons.append(f"candidate regressed metrics: {', '.join(sorted(regressions))}")
    mean_delta = sum(
        receipt.candidate_metrics[name] - baseline
        for name, baseline in receipt.baseline_metrics.items()
    ) / len(receipt.baseline_metrics)
    if mean_delta < minimum_mean_improvement:
        reasons.append("candidate improvement is below the admitted minimum")
    protected = sorted(set(candidate.scopes) & _PROTECTED_SCOPES)
    if protected:
        reasons.append(f"candidate changes protected control scopes: {', '.join(protected)}")
    if reasons:
        return PromotionDecision(PromotionDisposition.REJECT, tuple(reasons))
    return PromotionDecision(
        PromotionDisposition.CANARY,
        ("independent holdout evaluation admits a bounded reversible canary",),
        traffic_percent=candidate.maximum_canary_percent,
    )


@dataclass(frozen=True)
class CanaryObservation:
    candidate_id: str
    evaluated_requests: int
    error_rate: float
    baseline_error_rate: float
    quality_score: float
    baseline_quality_score: float
    safety_incidents: int
    rollback_verified: bool

    def __post_init__(self) -> None:
        if self.evaluated_requests < 0 or self.safety_incidents < 0:
            raise ValueError("canary counters cannot be negative")
        for value in (
            self.error_rate, self.baseline_error_rate,
            self.quality_score, self.baseline_quality_score,
        ):
            if not 0 <= value <= 1:
                raise ValueError("canary rates must be between zero and one")


def evaluate_canary(
    observation: CanaryObservation,
    *,
    minimum_requests: int = 100,
    maximum_error_increase: float = 0.005,
) -> PromotionDecision:
    if observation.safety_incidents or not observation.rollback_verified:
        return PromotionDecision(
            PromotionDisposition.ROLLBACK,
            ("safety incident or unverified rollback makes the canary unsafe",),
        )
    if observation.error_rate > observation.baseline_error_rate + maximum_error_increase:
        return PromotionDecision(
            PromotionDisposition.ROLLBACK,
            ("canary error rate exceeded the admitted regression envelope",),
        )
    if observation.evaluated_requests < minimum_requests:
        return PromotionDecision(
            PromotionDisposition.CANARY,
            ("continue the bounded canary until its sample gate is met",),
        )
    if observation.quality_score < observation.baseline_quality_score:
        return PromotionDecision(
            PromotionDisposition.ROLLBACK,
            ("online quality regressed below the baseline",),
        )
    return PromotionDecision(
        PromotionDisposition.HUMAN_REVIEW,
        ("canary passed; production promotion remains an attributable decision",),
        requires_human=True,
    )
