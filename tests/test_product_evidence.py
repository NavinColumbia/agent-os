from __future__ import annotations

import pytest

from agent_os.domain.product_evidence import (
    EvidenceKind,
    MetricDirection,
    ProductDisposition,
    ProductMetric,
    ProductObservation,
    ProductStudy,
    SystemOutcome,
    compare_system_value,
    evaluate_product_study,
)


def study(**changes) -> ProductStudy:
    values = dict(
        study_id="onboarding-v2",
        revision=2,
        hypothesis="The guided flow helps a founder reach a first mission with less confusion.",
        baseline_variant_id="current",
        candidate_variant_id="guided",
        canonical_task_ids=("create-company",),
        metrics=(
            ProductMetric("completion", MetricDirection.HIGHER, 0.05, hard_gate=True),
            ProductMetric("seconds", MetricDirection.LOWER, 5),
        ),
        representative_segments=("nontechnical-founder",),
    )
    values.update(changes)
    return ProductStudy(**values)


def observation(
    observation_id: str,
    variant_id: str,
    *,
    kind: EvidenceKind,
    completion: float,
    seconds: float,
    evaluator_id: str = "evaluator-a",
    repeat_index: int = 0,
    position: int | None = None,
    blinded: bool = False,
    critical_failures: tuple[str, ...] = (),
) -> ProductObservation:
    return ProductObservation(
        observation_id=observation_id,
        study_id="onboarding-v2",
        study_revision=2,
        task_id="create-company",
        segment_id="nontechnical-founder",
        variant_id=variant_id,
        evaluator_id=evaluator_id,
        evidence_kind=kind,
        metrics={"completion": completion, "seconds": seconds},
        evidence_ids=(f"artifact:{observation_id}",),
        repeat_index=repeat_index,
        presentation_position=position,
        blinded=blinded,
        critical_failures=critical_failures,
    )


def valid_synthetic_rows() -> list[ProductObservation]:
    rows = []
    for repeat in range(3):
        rows.extend((
            observation(
                f"base-{repeat}", "current", kind=EvidenceKind.SYNTHETIC,
                completion=0.7, seconds=65, repeat_index=repeat,
                position=1 if repeat % 2 == 0 else 2, blinded=True,
            ),
            observation(
                f"candidate-{repeat}", "guided", kind=EvidenceKind.SYNTHETIC,
                completion=0.9, seconds=48, repeat_index=repeat,
                position=2 if repeat % 2 == 0 else 1, blinded=True,
            ),
        ))
    return rows


def test_synthetic_council_can_find_a_winner_but_cannot_adopt_it():
    decision = evaluate_product_study(study(), valid_synthetic_rows())

    assert decision.disposition is ProductDisposition.HUMAN_VALIDATION_REQUIRED
    assert decision.metric_deltas == {"completion": 0.2, "seconds": 17.0}
    assert decision.evidence_mix["synthetic"] == 6


def test_unblinded_or_position_biased_synthetic_judge_is_not_valid_evidence():
    rows = valid_synthetic_rows()
    rows = [
        ProductObservation(
            **{
                **row.__dict__,
                "blinded": False,
                "presentation_position": 1,
            }
        )
        for row in rows
    ]

    decision = evaluate_product_study(study(), rows)

    assert decision.disposition is ProductDisposition.INSUFFICIENT_EVIDENCE
    assert any("not blinded" in reason for reason in decision.reasons)
    assert any("counterbalanced" in reason for reason in decision.reasons)


def test_synthetic_repetitions_must_be_paired_not_two_unmatched_batches():
    rows = valid_synthetic_rows()
    rows = [
        ProductObservation(**{
            **row.__dict__,
            "repeat_index": (
                row.repeat_index if row.variant_id == "current" else row.repeat_index + 3
            ),
        })
        for row in rows
    ]

    decision = evaluate_product_study(study(), rows)

    assert decision.disposition is ProductDisposition.INSUFFICIENT_EVIDENCE
    assert any("is not paired" in reason for reason in decision.reasons)


def test_deterministic_critical_failure_or_hard_gate_regression_cannot_be_voted_away():
    rows = valid_synthetic_rows()
    rows.append(observation(
        "browser-guard", "guided", kind=EvidenceKind.DETERMINISTIC,
        completion=0.0, seconds=80, evaluator_id="playwright",
        critical_failures=("keyboard submit is broken",),
    ))

    decision = evaluate_product_study(study(), rows)

    assert decision.disposition is ProductDisposition.REJECT
    assert "keyboard submit is broken" in decision.reasons[0]


def test_baseline_failure_is_not_mislabeled_as_a_candidate_failure():
    rows = valid_synthetic_rows()
    rows.append(observation(
        "old-browser-guard", "current", kind=EvidenceKind.DETERMINISTIC,
        completion=0.0, seconds=80, evaluator_id="playwright",
        critical_failures=("old flow keyboard submit is broken",),
    ))

    decision = evaluate_product_study(study(), rows)

    assert decision.disposition is ProductDisposition.HUMAN_VALIDATION_REQUIRED


def test_human_evidence_admits_only_a_bounded_experiment_before_production():
    rows = valid_synthetic_rows() + [
        observation(
            "human-current", "current", kind=EvidenceKind.HUMAN,
            completion=0.6, seconds=70, evaluator_id="participant-1",
        ),
        observation(
            "human-guided", "guided", kind=EvidenceKind.HUMAN,
            completion=1.0, seconds=40, evaluator_id="participant-1",
        ),
    ]

    decision = evaluate_product_study(study(), rows)

    assert decision.disposition is ProductDisposition.CONTROLLED_EXPERIMENT
    assert decision.evidence_mix["human"] == 2


def test_representative_production_evidence_can_admit_the_candidate():
    rows = valid_synthetic_rows() + [
        observation(
            "production-current", "current", kind=EvidenceKind.PRODUCTION,
            completion=0.65, seconds=68, evaluator_id="telemetry-window-a",
        ),
        observation(
            "production-guided", "guided", kind=EvidenceKind.PRODUCTION,
            completion=0.94, seconds=43, evaluator_id="telemetry-window-b",
        ),
    ]

    decision = evaluate_product_study(study(), rows)

    assert decision.disposition is ProductDisposition.ADOPT


def test_real_human_regression_cannot_be_swamped_by_synthetic_votes():
    rows = valid_synthetic_rows() + [
        observation(
            "human-current", "current", kind=EvidenceKind.HUMAN,
            completion=0.9, seconds=45, evaluator_id="participant-1",
        ),
        observation(
            "human-guided", "guided", kind=EvidenceKind.HUMAN,
            completion=0.7, seconds=70, evaluator_id="participant-1",
        ),
    ]

    decision = evaluate_product_study(study(), rows)

    assert decision.disposition is ProductDisposition.REJECT
    assert "human evidence" in decision.reasons[0]


def test_every_registered_segment_requires_variant_evidence():
    decision = evaluate_product_study(
        study(representative_segments=("nontechnical-founder", "agency-owner")),
        valid_synthetic_rows(),
    )

    assert decision.disposition is ProductDisposition.INSUFFICIENT_EVIDENCE
    assert any("agency-owner" in reason for reason in decision.reasons)


def outcome(system_id: str, **changes) -> SystemOutcome:
    values = dict(
        system_id=system_id,
        task_count=20,
        success_rate=0.8,
        quality_score=0.8,
        reliability_rate=0.9,
        p95_latency_seconds=60,
        model_cost_cents=100,
        human_minutes=120,
        interventions=8,
        evidence_ids=(f"artifact:{system_id}",),
    )
    values.update(changes)
    return SystemOutcome(**values)


def test_expensive_multi_agent_system_must_prove_value_over_direct_baseline():
    receipt = compare_system_value(
        outcome("direct-model"),
        outcome(
            "agent-os", model_cost_cents=1_500, p95_latency_seconds=300,
            human_minutes=115, interventions=10,
        ),
        customer_price_cents=2_000,
    )

    assert receipt.dominates_baseline is False
    assert any("does not cover incremental cost" in reason for reason in receipt.reasons)


def test_measured_quality_reliability_and_human_time_can_prove_value():
    receipt = compare_system_value(
        outcome("manual-plus-copilot"),
        outcome(
            "agent-os", success_rate=0.95, quality_score=0.92,
            reliability_rate=0.96, model_cost_cents=900, human_minutes=20,
            interventions=2,
        ),
        customer_price_cents=2_000,
    )

    assert receipt.dominates_baseline is True
    assert receipt.human_minutes_saved == 100
    assert receipt.estimated_human_value_cents == 10_000


def test_identical_paid_system_has_no_measured_value_over_the_baseline():
    receipt = compare_system_value(
        outcome("direct-model"),
        outcome("agent-os"),
        customer_price_cents=1_000,
    )

    assert receipt.dominates_baseline is False
    assert any("no measured benefit" in reason for reason in receipt.reasons)


def test_value_receipt_can_enforce_a_preregistered_latency_budget():
    receipt = compare_system_value(
        outcome("direct-model"),
        outcome("agent-os", quality_score=0.9, p95_latency_seconds=100),
        maximum_latency_regression_seconds=10,
    )

    assert receipt.dominates_baseline is False
    assert any("latency regression" in reason for reason in receipt.reasons)


def test_study_and_observation_contracts_fail_closed_on_weak_provenance():
    with pytest.raises(ValueError, match="three repetitions"):
        study(synthetic_repetitions=1)
    with pytest.raises(ValueError, match="durable evidence"):
        ProductObservation(
            "obs", "onboarding-v2", 2, "create-company", "nontechnical-founder",
            "guided", "judge", EvidenceKind.SYNTHETIC,
            {"completion": 1.0, "seconds": 20}, (),
        )
