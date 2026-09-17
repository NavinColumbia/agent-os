"""Deterministic trajectory, context, and human-attention governance.

These rules intentionally judge evidence and state change, not elapsed time.
Long work with new checkpoints continues; repeated identical failures,
oscillation, or a context window that grows without progress triggers a
bounded diagnostic/replan path instead of a blind timeout.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class TrajectoryAction(str, Enum):
    CONTINUE = "continue"
    DIAGNOSE = "diagnose"
    REPLAN = "replan"
    RESET_CONTEXT = "reset_context"
    ESCALATE = "escalate"
    FREEZE = "freeze"


@dataclass(frozen=True)
class TrajectorySample:
    state_version: int
    observed_at: str
    progress_score: float
    evidence_digest: str
    failure_signature: str | None = None
    iteration: int = 0
    cumulative_cost_cents: int = 0
    context_bytes: int = 0

    def __post_init__(self) -> None:
        if self.state_version < 0 or self.iteration < 0 or self.cumulative_cost_cents < 0:
            raise ValueError("trajectory counters cannot be negative")
        if not 0 <= self.progress_score <= 1:
            raise ValueError("trajectory progress must be between zero and one")
        if len(self.evidence_digest) != 64:
            raise ValueError("trajectory evidence digest must be a SHA-256 digest")

    def to_dict(self) -> dict[str, Any]:
        return {
            "state_version": self.state_version,
            "observed_at": self.observed_at,
            "progress_score": self.progress_score,
            "evidence_digest": self.evidence_digest,
            "failure_signature": self.failure_signature,
            "iteration": self.iteration,
            "cumulative_cost_cents": self.cumulative_cost_cents,
            "context_bytes": self.context_bytes,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TrajectorySample":
        return cls(
            state_version=int(raw["state_version"]),
            observed_at=str(raw["observed_at"]),
            progress_score=float(raw["progress_score"]),
            evidence_digest=str(raw["evidence_digest"]),
            failure_signature=(
                None if raw.get("failure_signature") is None
                else str(raw["failure_signature"])
            ),
            iteration=int(raw.get("iteration", 0)),
            cumulative_cost_cents=int(raw.get("cumulative_cost_cents", 0)),
            context_bytes=int(raw.get("context_bytes", 0)),
        )


@dataclass(frozen=True)
class TrajectoryDecision:
    action: TrajectoryAction
    reasons: tuple[str, ...]
    preserve_work: bool
    requires_human: bool = False
    checkpoint_required: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "reasons": list(self.reasons),
            "preserve_work": self.preserve_work,
            "requires_human": self.requires_human,
            "checkpoint_required": self.checkpoint_required,
        }


def evaluate_trajectory(
    samples: tuple[TrajectorySample, ...],
    *,
    plateau_window: int = 4,
    minimum_progress_delta: float = 0.01,
    repeated_failure_limit: int = 3,
    context_limit_bytes: int = 128_000,
    unsafe_invariant_broken: bool = False,
) -> TrajectoryDecision:
    if not samples:
        raise ValueError("trajectory evaluation requires at least one sample")
    if plateau_window < 2 or repeated_failure_limit < 2 or context_limit_bytes < 1:
        raise ValueError("trajectory evaluation bounds are invalid")
    ordered = tuple(sorted(samples, key=lambda item: item.state_version))
    latest = ordered[-1]
    if unsafe_invariant_broken:
        return TrajectoryDecision(
            TrajectoryAction.FREEZE,
            ("a deterministic safety invariant is broken",),
            preserve_work=True,
            requires_human=True,
            checkpoint_required=True,
        )

    failures = [item.failure_signature for item in ordered if item.failure_signature]
    if len(failures) >= repeated_failure_limit and len(set(failures[-repeated_failure_limit:])) == 1:
        return TrajectoryDecision(
            TrajectoryAction.ESCALATE,
            ("the same verified failure repeated without new diagnostic evidence",),
            preserve_work=True,
            requires_human=True,
            checkpoint_required=True,
        )

    window = ordered[-plateau_window:]
    improvement = max(item.progress_score for item in window) - min(
        item.progress_score for item in window
    )
    evidence_sequence = [item.evidence_digest for item in window]
    oscillating = (
        len(evidence_sequence) >= 4
        and evidence_sequence[-1] == evidence_sequence[-3]
        and evidence_sequence[-2] == evidence_sequence[-4]
        and evidence_sequence[-1] != evidence_sequence[-2]
    )
    if latest.context_bytes >= context_limit_bytes and improvement < minimum_progress_delta:
        return TrajectoryDecision(
            TrajectoryAction.RESET_CONTEXT,
            ("context grew to its admitted limit without material progress",),
            preserve_work=True,
            checkpoint_required=True,
        )
    if oscillating and improvement < minimum_progress_delta:
        return TrajectoryDecision(
            TrajectoryAction.REPLAN,
            ("execution is oscillating between previously observed states",),
            preserve_work=True,
            checkpoint_required=True,
        )
    if len(window) == plateau_window and improvement < minimum_progress_delta:
        return TrajectoryDecision(
            TrajectoryAction.DIAGNOSE,
            ("evidence-backed progress plateaued across the admitted review window",),
            preserve_work=True,
            checkpoint_required=True,
        )
    return TrajectoryDecision(
        TrajectoryAction.CONTINUE,
        ("new state or evidence remains within the healthy trajectory envelope",),
        preserve_work=True,
    )


class HumanInvolvementMode(str, Enum):
    AUTONOMOUS = "autonomous"
    BALANCED = "balanced"
    COLLABORATIVE = "collaborative"


class AttentionDisposition(str, Enum):
    INTERRUPT = "interrupt"
    BATCH = "batch"
    DEFER = "defer"


@dataclass(frozen=True)
class AttentionRequest:
    request_id: str
    recipient_id: str
    severity: str
    blocking: bool
    irreversible: bool
    deadline_minutes: int | None
    value_of_information: float

    def __post_init__(self) -> None:
        if not self.request_id or not self.recipient_id:
            raise ValueError("attention request identity and recipient are required")
        if self.severity not in {"info", "warning", "critical"}:
            raise ValueError("attention severity is invalid")
        if not 0 <= self.value_of_information <= 1:
            raise ValueError("attention value must be between zero and one")
        if self.deadline_minutes is not None and self.deadline_minutes < 0:
            raise ValueError("attention deadline cannot be negative")


@dataclass(frozen=True)
class AttentionPolicy:
    mode: HumanInvolvementMode = HumanInvolvementMode.BALANCED
    daily_interrupt_limit: int = 8

    def __post_init__(self) -> None:
        if not 0 <= self.daily_interrupt_limit <= 100:
            raise ValueError("daily interruption limit must be between zero and 100")


@dataclass(frozen=True)
class AttentionDecision:
    disposition: AttentionDisposition
    reason: str
    bypassed_budget: bool = False


def route_attention(
    request: AttentionRequest,
    policy: AttentionPolicy,
    *,
    interrupts_used_today: int,
) -> AttentionDecision:
    """Choose interruption vs batching without suppressing safety decisions."""

    if interrupts_used_today < 0:
        raise ValueError("used interruption count cannot be negative")
    safety_critical = request.severity == "critical" and (
        request.blocking or request.irreversible
    )
    if safety_critical:
        return AttentionDecision(
            AttentionDisposition.INTERRUPT,
            "critical blocking or irreversible decision bypasses the attention budget",
            bypassed_budget=interrupts_used_today >= policy.daily_interrupt_limit,
        )
    if interrupts_used_today >= policy.daily_interrupt_limit:
        return AttentionDecision(
            AttentionDisposition.BATCH,
            "daily interruption budget is exhausted; retain the item in the decision digest",
        )
    threshold = {
        HumanInvolvementMode.AUTONOMOUS: 0.9,
        HumanInvolvementMode.BALANCED: 0.65,
        HumanInvolvementMode.COLLABORATIVE: 0.35,
    }[policy.mode]
    urgency = request.value_of_information
    if request.blocking:
        urgency += 0.25
    if request.deadline_minutes is not None and request.deadline_minutes <= 60:
        urgency += 0.2
    if request.severity == "warning":
        urgency += 0.1
    if urgency >= threshold:
        return AttentionDecision(
            AttentionDisposition.INTERRUPT,
            "decision value and urgency exceed the configured involvement threshold",
        )
    if request.value_of_information >= 0.2 or request.blocking:
        return AttentionDecision(
            AttentionDisposition.BATCH,
            "retain for the next bounded decision digest",
        )
    return AttentionDecision(
        AttentionDisposition.DEFER,
        "low-value non-blocking update does not require human attention yet",
    )
