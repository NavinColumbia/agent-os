from agent_os.domain.improvement import (
    CanaryObservation,
    EvaluationReceipt,
    ImprovementCandidate,
    PromotionDisposition,
    evaluate_candidate,
    evaluate_canary,
)


def candidate(**changes):
    values = dict(
        candidate_id="candidate-1", tenant_id="tenant-a", mission_id="mission-1",
        proposed_by="agent:optimizer", base_revision="git:abc",
        artifact_ref="artifact:candidate", artifact_sha256="a" * 64,
        rollback_artifact_ref="artifact:baseline", scopes=("planner_prompt",),
        hypothesis="A smaller prompt improves accuracy and latency.",
    )
    values.update(changes)
    return ImprovementCandidate(**values)


def receipt(**changes):
    values = dict(
        receipt_id="eval-1", candidate_id="candidate-1", evaluator_id="agent:evaluator",
        dataset_id="mission-eval", dataset_version="v7",
        production_window="2026-08-01/2026-08-31", representative=True, holdout=True,
        baseline_metrics={"quality": 0.8, "safety": 1.0},
        candidate_metrics={"quality": 0.84, "safety": 1.0},
        critical_failures=(), deterministic_checks_passed=True,
        evidence_ids=("artifact:eval-report",),
    )
    values.update(changes)
    return EvaluationReceipt(**values)


def test_independent_holdout_can_only_admit_bounded_canary():
    decision = evaluate_candidate(candidate(), receipt())
    assert decision.disposition is PromotionDisposition.CANARY
    assert decision.traffic_percent == 5


def test_self_modifying_control_plane_and_regressions_are_rejected():
    decision = evaluate_candidate(
        candidate(scopes=("assurance_kernel",)),
        receipt(candidate_metrics={"quality": 0.79, "safety": 1.0}),
    )
    assert decision.disposition is PromotionDisposition.REJECT
    assert any("protected control" in reason for reason in decision.reasons)
    assert any("regressed" in reason for reason in decision.reasons)


def test_canary_rolls_back_on_safety_and_requires_human_to_promote():
    unsafe = evaluate_canary(CanaryObservation(
        "candidate-1", 10, 0.01, 0.01, 0.9, 0.8, 1, True,
    ))
    assert unsafe.disposition is PromotionDisposition.ROLLBACK
    passed = evaluate_canary(CanaryObservation(
        "candidate-1", 1_000, 0.01, 0.01, 0.9, 0.8, 0, True,
    ))
    assert passed.disposition is PromotionDisposition.HUMAN_REVIEW
    assert passed.requires_human is True
