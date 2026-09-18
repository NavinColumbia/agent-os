"""Matched-task benchmark contracts for proving incremental agent-system value."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from math import ceil, isfinite
from statistics import fmean
from typing import Any, Iterable, Mapping

from agent_os.domain.product_evidence import SystemOutcome, ValueReceipt, compare_system_value


class FailureClass(str, Enum):
    NONE = "none"
    REQUIREMENTS = "requirements"
    MODEL = "model"
    TOOL = "tool"
    ORCHESTRATION = "orchestration"
    INFRASTRUCTURE = "infrastructure"
    POLICY = "policy"
    HUMAN_DEPENDENCY = "human_dependency"
    VERIFICATION = "verification"


@dataclass(frozen=True)
class BenchmarkTask:
    task_id: str
    segment_id: str
    acceptance_criteria: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.task_id.strip() or not self.segment_id.strip():
            raise ValueError("benchmark task identity is required")
        if not self.acceptance_criteria or any(
            not item.strip() for item in self.acceptance_criteria
        ):
            raise ValueError("benchmark task acceptance criteria are required")


@dataclass(frozen=True)
class BenchmarkManifest:
    benchmark_id: str
    revision: int
    baseline_system_id: str
    candidate_system_id: str
    tasks: tuple[BenchmarkTask, ...]
    repetitions: int
    rubric_version: str
    maximum_latency_regression_seconds: float | None = None

    def __post_init__(self) -> None:
        if any(not item.strip() for item in (
            self.benchmark_id, self.baseline_system_id,
            self.candidate_system_id, self.rubric_version,
        )):
            raise ValueError("benchmark identity, systems, and rubric are required")
        if self.baseline_system_id == self.candidate_system_id:
            raise ValueError("benchmark requires distinct baseline and candidate systems")
        if self.revision < 1 or self.repetitions < 1:
            raise ValueError("benchmark revision and repetitions must be positive")
        identities = {(task.task_id, task.segment_id) for task in self.tasks}
        if not self.tasks or len(identities) != len(self.tasks):
            raise ValueError("benchmark requires unique task and segment pairs")
        if (
            self.maximum_latency_regression_seconds is not None
            and (
                not isfinite(self.maximum_latency_regression_seconds)
                or self.maximum_latency_regression_seconds < 0
            )
        ):
            raise ValueError("benchmark latency envelope cannot be negative")


@dataclass(frozen=True)
class BenchmarkTrial:
    trial_id: str
    benchmark_id: str
    benchmark_revision: int
    task_id: str
    segment_id: str
    system_id: str
    repetition: int
    success: bool
    quality_score: float
    reliable: bool
    latency_seconds: float
    model_cost_cents: float
    human_minutes: float
    interventions: int
    failure_class: FailureClass
    made_by: str
    evaluated_by: str
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if any(not item.strip() for item in (
            self.trial_id, self.benchmark_id, self.task_id, self.segment_id,
            self.system_id, self.made_by, self.evaluated_by,
        )):
            raise ValueError("benchmark trial identity and provenance are required")
        if self.benchmark_revision < 1 or self.repetition < 0:
            raise ValueError("benchmark trial revision and repetition are invalid")
        if self.made_by == self.evaluated_by:
            raise ValueError("benchmark maker cannot be its own evaluator")
        if not isfinite(self.quality_score) or not 0 <= self.quality_score <= 1:
            raise ValueError("benchmark quality must be between zero and one")
        if any(
            isinstance(value, bool) or not isfinite(float(value)) or value < 0
            for value in (
                self.latency_seconds, self.model_cost_cents,
                self.human_minutes, self.interventions,
            )
        ):
            raise ValueError("benchmark latency, cost, effort, and interventions cannot be negative")
        if not self.evidence_ids or any(not item.strip() for item in self.evidence_ids):
            raise ValueError("benchmark trial requires durable raw evidence")
        if self.success and self.failure_class is not FailureClass.NONE:
            raise ValueError("successful benchmark trials cannot claim a failure class")
        if not self.success and self.failure_class is FailureClass.NONE:
            raise ValueError("failed benchmark trials require a failure class")


@dataclass(frozen=True)
class BenchmarkReport:
    benchmark_id: str
    benchmark_revision: int
    manifest_sha256: str
    trial_set_sha256: str
    baseline: SystemOutcome
    candidate: SystemOutcome
    value_receipt: ValueReceipt
    failures: Mapping[str, Mapping[str, int]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "benchmark_id": self.benchmark_id,
            "benchmark_revision": self.benchmark_revision,
            "manifest_sha256": self.manifest_sha256,
            "trial_set_sha256": self.trial_set_sha256,
            "baseline": _outcome_dict(self.baseline),
            "candidate": _outcome_dict(self.candidate),
            "value_receipt": self.value_receipt.to_dict(),
            "failures": {
                system_id: dict(counts) for system_id, counts in self.failures.items()
            },
        }


def _canonical(value: Mapping[str, Any] | list[Any]) -> str:
    return json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)


def _sha256(value: Mapping[str, Any] | list[Any]) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _manifest_dict(value: BenchmarkManifest) -> dict[str, Any]:
    return {
        "benchmark_id": value.benchmark_id,
        "revision": value.revision,
        "baseline_system_id": value.baseline_system_id,
        "candidate_system_id": value.candidate_system_id,
        "tasks": [{
            "task_id": task.task_id,
            "segment_id": task.segment_id,
            "acceptance_criteria": list(task.acceptance_criteria),
        } for task in value.tasks],
        "repetitions": value.repetitions,
        "rubric_version": value.rubric_version,
        "maximum_latency_regression_seconds": value.maximum_latency_regression_seconds,
    }


def _trial_dict(value: BenchmarkTrial) -> dict[str, Any]:
    return {
        "trial_id": value.trial_id,
        "benchmark_id": value.benchmark_id,
        "benchmark_revision": value.benchmark_revision,
        "task_id": value.task_id,
        "segment_id": value.segment_id,
        "system_id": value.system_id,
        "repetition": value.repetition,
        "success": value.success,
        "quality_score": value.quality_score,
        "reliable": value.reliable,
        "latency_seconds": value.latency_seconds,
        "model_cost_cents": value.model_cost_cents,
        "human_minutes": value.human_minutes,
        "interventions": value.interventions,
        "failure_class": value.failure_class.value,
        "made_by": value.made_by,
        "evaluated_by": value.evaluated_by,
        "evidence_ids": list(value.evidence_ids),
    }


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


def parse_benchmark_manifest(raw: Mapping[str, Any]) -> BenchmarkManifest:
    return BenchmarkManifest(
        benchmark_id=str(raw.get("benchmark_id") or ""),
        revision=int(raw.get("revision") or 0),
        baseline_system_id=str(raw.get("baseline_system_id") or ""),
        candidate_system_id=str(raw.get("candidate_system_id") or ""),
        tasks=tuple(BenchmarkTask(
            task_id=str(item.get("task_id") or ""),
            segment_id=str(item.get("segment_id") or ""),
            acceptance_criteria=tuple(
                str(criterion) for criterion in item.get("acceptance_criteria", ())
            ),
        ) for item in raw.get("tasks", ())),
        repetitions=int(raw.get("repetitions") or 0),
        rubric_version=str(raw.get("rubric_version") or ""),
        maximum_latency_regression_seconds=(
            None if raw.get("maximum_latency_regression_seconds") is None
            else float(raw["maximum_latency_regression_seconds"])
        ),
    )


def parse_benchmark_trial(raw: Mapping[str, Any]) -> BenchmarkTrial:
    success = raw.get("success")
    reliable = raw.get("reliable")
    if not isinstance(success, bool) or not isinstance(reliable, bool):
        raise ValueError("benchmark trial success and reliability must be booleans")
    return BenchmarkTrial(
        trial_id=str(raw.get("trial_id") or ""),
        benchmark_id=str(raw.get("benchmark_id") or ""),
        benchmark_revision=int(raw.get("benchmark_revision") or 0),
        task_id=str(raw.get("task_id") or ""),
        segment_id=str(raw.get("segment_id") or ""),
        system_id=str(raw.get("system_id") or ""),
        repetition=int(raw.get("repetition", -1)),
        success=success,
        quality_score=float(raw.get("quality_score", -1)),
        reliable=reliable,
        latency_seconds=float(raw.get("latency_seconds", -1)),
        model_cost_cents=float(raw.get("model_cost_cents", -1)),
        human_minutes=float(raw.get("human_minutes", -1)),
        interventions=int(raw.get("interventions", -1)),
        failure_class=FailureClass(str(raw.get("failure_class") or "")),
        made_by=str(raw.get("made_by") or ""),
        evaluated_by=str(raw.get("evaluated_by") or ""),
        evidence_ids=tuple(str(item) for item in raw.get("evidence_ids", ())),
    )


def _p95(values: Iterable[float]) -> float:
    ordered = sorted(float(value) for value in values)
    return ordered[max(0, ceil(len(ordered) * 0.95) - 1)]


def evaluate_benchmark(
    manifest: BenchmarkManifest,
    trials: Iterable[BenchmarkTrial],
    *,
    customer_price_cents: float,
    human_hourly_value_cents: float,
) -> BenchmarkReport:
    rows = tuple(trials)
    if len({row.trial_id for row in rows}) != len(rows):
        raise ValueError("benchmark trials contain duplicate identities")
    systems = (manifest.baseline_system_id, manifest.candidate_system_id)
    expected = {
        (task.task_id, task.segment_id, system_id, repetition)
        for task in manifest.tasks
        for system_id in systems
        for repetition in range(manifest.repetitions)
    }
    actual = set()
    for row in rows:
        if row.benchmark_id != manifest.benchmark_id or row.benchmark_revision != manifest.revision:
            raise ValueError(f"trial {row.trial_id} belongs to another benchmark revision")
        actual.add((row.task_id, row.segment_id, row.system_id, row.repetition))
    if actual != expected or len(rows) != len(expected):
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        raise ValueError(
            f"benchmark trial matrix is incomplete or unregistered; missing={missing[:10]}, "
            f"unexpected={unexpected[:10]}"
        )

    outcomes: dict[str, SystemOutcome] = {}
    failures: dict[str, dict[str, int]] = {}
    for system_id in systems:
        system_rows = [row for row in rows if row.system_id == system_id]
        failure_counts = {kind.value: 0 for kind in FailureClass if kind is not FailureClass.NONE}
        for row in system_rows:
            if row.failure_class is not FailureClass.NONE:
                failure_counts[row.failure_class.value] += 1
        failures[system_id] = failure_counts
        outcomes[system_id] = SystemOutcome(
            system_id=system_id,
            task_count=len(system_rows),
            success_rate=fmean(float(row.success) for row in system_rows),
            quality_score=fmean(row.quality_score for row in system_rows),
            reliability_rate=fmean(float(row.reliable) for row in system_rows),
            p95_latency_seconds=_p95(row.latency_seconds for row in system_rows),
            model_cost_cents=sum(row.model_cost_cents for row in system_rows),
            human_minutes=sum(row.human_minutes for row in system_rows),
            interventions=sum(row.interventions for row in system_rows),
            evidence_ids=tuple(dict.fromkeys(
                evidence_id for row in system_rows for evidence_id in row.evidence_ids
            )),
        )
    baseline = outcomes[manifest.baseline_system_id]
    candidate = outcomes[manifest.candidate_system_id]
    receipt = compare_system_value(
        baseline,
        candidate,
        customer_price_cents=customer_price_cents,
        human_hourly_value_cents=human_hourly_value_cents,
        maximum_latency_regression_seconds=manifest.maximum_latency_regression_seconds,
    )
    return BenchmarkReport(
        benchmark_id=manifest.benchmark_id,
        benchmark_revision=manifest.revision,
        manifest_sha256=_sha256(_manifest_dict(manifest)),
        trial_set_sha256=_sha256([
            _trial_dict(row) for row in sorted(rows, key=lambda item: item.trial_id)
        ]),
        baseline=baseline,
        candidate=candidate,
        value_receipt=receipt,
        failures=failures,
    )
