from __future__ import annotations

import json

from click.testing import CliRunner
import pytest

from agent_os.domain.benchmark import (
    BenchmarkManifest,
    BenchmarkTask,
    BenchmarkTrial,
    FailureClass,
    evaluate_benchmark,
)
from agent_os.entrypoints.cli import main


def manifest() -> BenchmarkManifest:
    return BenchmarkManifest(
        benchmark_id="paid-workflows",
        revision=1,
        baseline_system_id="direct-model",
        candidate_system_id="agent-os",
        tasks=(
            BenchmarkTask("repair-api", "small-saas", ("tests pass", "change is scoped")),
            BenchmarkTask("ship-ui", "small-saas", ("journey passes", "evidence retained")),
        ),
        repetitions=2,
        rubric_version="rubric-2026-09-18",
        maximum_latency_regression_seconds=30,
    )


def trials() -> list[BenchmarkTrial]:
    rows = []
    for task_id in ("repair-api", "ship-ui"):
        for system_id in ("direct-model", "agent-os"):
            for repetition in range(2):
                candidate = system_id == "agent-os"
                success = candidate or repetition == 0
                rows.append(BenchmarkTrial(
                    trial_id=f"{task_id}-{system_id}-{repetition}",
                    benchmark_id="paid-workflows",
                    benchmark_revision=1,
                    task_id=task_id,
                    segment_id="small-saas",
                    system_id=system_id,
                    repetition=repetition,
                    success=success,
                    quality_score=0.9 if candidate else 0.7,
                    reliable=candidate or repetition == 0,
                    latency_seconds=25 if candidate else 20,
                    model_cost_cents=4 if candidate else 2,
                    human_minutes=2 if candidate else 10,
                    interventions=0 if candidate else 2,
                    failure_class=(
                        FailureClass.NONE if success else FailureClass.MODEL
                    ),
                    made_by=f"maker-{system_id}",
                    evaluated_by="independent-checker",
                    evidence_ids=(f"artifact-{task_id}-{system_id}-{repetition}",),
                ))
    return rows


def test_matched_benchmark_emits_reproducible_value_and_failure_receipt():
    first = evaluate_benchmark(
        manifest(), trials(), customer_price_cents=100,
        human_hourly_value_cents=6_000,
    )
    second = evaluate_benchmark(
        manifest(), reversed(trials()), customer_price_cents=100,
        human_hourly_value_cents=6_000,
    )
    assert first.manifest_sha256 == second.manifest_sha256
    assert first.trial_set_sha256 == second.trial_set_sha256
    assert first.baseline.task_count == first.candidate.task_count == 4
    assert first.value_receipt.dominates_baseline is True
    assert first.failures["direct-model"]["model"] == 2
    assert first.failures["agent-os"]["model"] == 0


def test_benchmark_fails_closed_on_missing_pair_or_non_independent_checker():
    with pytest.raises(ValueError, match="trial matrix"):
        evaluate_benchmark(
            manifest(), trials()[:-1], customer_price_cents=0,
            human_hourly_value_cents=6_000,
        )
    original = trials()[0]
    with pytest.raises(ValueError, match="own evaluator"):
        BenchmarkTrial(**{
            **original.__dict__, "evaluated_by": original.made_by,
        })


def test_benchmark_cli_validates_files_and_emits_machine_readable_report(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    trials_path = tmp_path / "trials.json"
    manifest_path.write_text(json.dumps({
        "benchmark_id": "paid-workflows",
        "revision": 1,
        "baseline_system_id": "direct-model",
        "candidate_system_id": "agent-os",
        "tasks": [
            {
                "task_id": task.task_id,
                "segment_id": task.segment_id,
                "acceptance_criteria": list(task.acceptance_criteria),
            }
            for task in manifest().tasks
        ],
        "repetitions": 2,
        "rubric_version": "rubric-2026-09-18",
        "maximum_latency_regression_seconds": 30,
    }))
    trials_path.write_text(json.dumps({"trials": [{
        **row.__dict__, "failure_class": row.failure_class.value,
        "evidence_ids": list(row.evidence_ids),
    } for row in trials()]}))

    result = CliRunner().invoke(main, [
        "benchmark-report", "--manifest", str(manifest_path),
        "--trials", str(trials_path), "--customer-price-cents", "100",
    ])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["value_receipt"]["dominates_baseline"] is True
    assert len(report["manifest_sha256"]) == 64
    assert len(report["trial_set_sha256"]) == 64
