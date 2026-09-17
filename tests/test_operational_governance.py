from agent_os.domain.operational_governance import (
    AttentionDisposition,
    AttentionPolicy,
    AttentionRequest,
    HumanInvolvementMode,
    TrajectoryAction,
    TrajectorySample,
    evaluate_trajectory,
    route_attention,
)


def _sample(version, score, digest, *, failure=None, context=100):
    return TrajectorySample(
        state_version=version, observed_at=f"2026-09-17T12:00:0{version}+00:00",
        progress_score=score, evidence_digest=digest * 64,
        failure_signature=failure, context_bytes=context,
    )


def test_trajectory_preserves_long_work_with_new_evidence_and_detects_plateau():
    progressing = tuple(_sample(i, i / 10, str(i)) for i in range(1, 5))
    assert evaluate_trajectory(progressing).action is TrajectoryAction.CONTINUE
    plateau = tuple(_sample(i, 0.4, "a") for i in range(1, 5))
    decision = evaluate_trajectory(plateau)
    assert decision.action is TrajectoryAction.DIAGNOSE
    assert decision.preserve_work is True


def test_trajectory_escalates_repeated_failure_and_resets_saturated_context():
    repeated = tuple(
        _sample(i, 0.2, str(i), failure="same-failure") for i in range(1, 4)
    )
    assert evaluate_trajectory(repeated).action is TrajectoryAction.ESCALATE
    saturated = tuple(
        _sample(i, 0.2, "a", context=130_000) for i in range(1, 5)
    )
    assert evaluate_trajectory(saturated).action is TrajectoryAction.RESET_CONTEXT


def test_attention_budget_never_suppresses_critical_safety_decision():
    request = AttentionRequest(
        "attention-1", "human:ceo", "critical", True, True, 10, 1.0,
    )
    decision = route_attention(
        request, AttentionPolicy(daily_interrupt_limit=0), interrupts_used_today=10,
    )
    assert decision.disposition is AttentionDisposition.INTERRUPT
    assert decision.bypassed_budget is True


def test_attention_mode_batches_low_value_nonblocking_updates():
    request = AttentionRequest(
        "attention-2", "human:ceo", "info", False, False, None, 0.3,
    )
    autonomous = route_attention(
        request,
        AttentionPolicy(HumanInvolvementMode.AUTONOMOUS, daily_interrupt_limit=8),
        interrupts_used_today=0,
    )
    collaborative = route_attention(
        request,
        AttentionPolicy(HumanInvolvementMode.COLLABORATIVE, daily_interrupt_limit=8),
        interrupts_used_today=0,
    )
    assert autonomous.disposition is AttentionDisposition.BATCH
    assert collaborative.disposition is AttentionDisposition.BATCH
