"""Contract tests for the framework-neutral Agent OS v2 lifecycle."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_os.domain.lifecycle import (  # noqa: E402
    CommandKind,
    Event,
    EventKind,
    LifecyclePhase,
    LifecycleState,
    LifecycleStatus,
    TransitionRejected,
    WaitKind,
    evolve,
)


def new_state() -> LifecycleState:
    return LifecycleState(run_id="run-1", organization_id="org-1")


def apply(state: LifecycleState, kind: EventKind, **payload):
    event = Event(
        event_id=f"event-{state.version}-{kind.value}",
        kind=kind,
        expected_version=state.version,
        payload=payload,
    )
    transition = evolve(state, event)
    return transition.state, transition.commands


def advance_to_verify() -> LifecycleState:
    state = new_state()
    state, _ = apply(state, EventKind.SCOPE_ACCEPTED, brief_id="brief-1")
    state, _ = apply(state, EventKind.RESEARCH_COMPLETED, report_id="report-1")
    state, _ = apply(state, EventKind.SPECIFICATION_APPROVED, specification_id="spec-1")
    state, _ = apply(state, EventKind.BUILD_COMPLETED, artifact_revision="sha256:one")
    return state


def test_happy_path_is_small_monotonic_and_emits_one_command_per_boundary():
    state = new_state()
    cases = [
        (EventKind.SCOPE_ACCEPTED, LifecyclePhase.RESEARCH, CommandKind.START_MISSION,
         {"brief_id": "brief-1"}),
        (EventKind.RESEARCH_COMPLETED, LifecyclePhase.SPECIFY, CommandKind.START_SPECIFICATION,
         {"report_id": "report-1"}),
        (EventKind.SPECIFICATION_APPROVED, LifecyclePhase.BUILD, CommandKind.START_BUILD,
         {"specification_id": "spec-1"}),
        (EventKind.BUILD_COMPLETED, LifecyclePhase.VERIFY, CommandKind.START_VERIFICATION,
         {"artifact_revision": "sha256:one"}),
        (EventKind.VERIFICATION_PASSED, LifecyclePhase.RELEASE, CommandKind.START_RELEASE,
         {"evidence_id": "evidence-1"}),
        (EventKind.RELEASE_COMPLETED, LifecyclePhase.RELEASE, CommandKind.PUBLISH_COMPLETION,
         {"deployment_id": "deployment-1"}),
    ]

    for kind, phase, command, payload in cases:
        state, commands = apply(state, kind, **payload)
        assert state.phase is phase
        assert commands[0].kind is command
        assert len(commands) == 1

    assert state.status is LifecycleStatus.SUCCEEDED
    assert state.version == len(cases)
    assert state.artifact_revision == "sha256:one"


def test_dynamic_mission_graph_can_project_directly_to_verified_completion():
    state = new_state()
    state, commands = apply(
        state, EventKind.SCOPE_ACCEPTED,
        prompt="Build an application", title="Customer portal",
    )
    assert commands[0].kind is CommandKind.START_MISSION
    assert state.objective == "Build an application"
    assert state.title == "Customer portal"

    state, commands = apply(
        state,
        EventKind.MISSION_COMPLETED,
        summary="The generated mission graph completed.",
        evidence_ids=["artifact-release", "artifact-test-report"],
    )

    assert state.phase is LifecyclePhase.RELEASE
    assert state.status is LifecycleStatus.SUCCEEDED
    assert state.artifact_revision == "artifact-release"
    assert commands[0].kind is CommandKind.PUBLISH_COMPLETION
    assert commands[0].payload["evidence_ids"] == [
        "artifact-release", "artifact-test-report",
    ]


def test_dynamic_mission_completion_requires_evidence():
    state, _ = apply(new_state(), EventKind.SCOPE_ACCEPTED)
    with pytest.raises(TransitionRejected, match="durable evidence"):
        apply(state, EventKind.MISSION_COMPLETED, summary="Unsupported claim", evidence_ids=[])


def test_phase_skips_and_backwards_transitions_are_rejected():
    state = new_state()
    with pytest.raises(TransitionRejected, match="invalid"):
        apply(state, EventKind.BUILD_COMPLETED, artifact_revision="sha256:bad")

    state, _ = apply(state, EventKind.SCOPE_ACCEPTED)
    with pytest.raises(TransitionRejected, match="invalid"):
        apply(state, EventKind.SCOPE_ACCEPTED)


def test_waiting_is_orthogonal_to_phase_and_resumes_an_explicit_command():
    state = new_state()
    state, _ = apply(state, EventKind.SCOPE_ACCEPTED)
    original_phase = state.phase

    state, commands = apply(
        state,
        EventKind.WAIT_REQUESTED,
        wait_kind=WaitKind.HUMAN.value,
        correlation_id="approval-7",
        reason="customer must approve external publication",
        resume_command=CommandKind.START_RESEARCH.value,
    )
    assert state.phase is original_phase
    assert state.status is LifecycleStatus.WAITING
    assert state.wait is not None and state.wait.kind is WaitKind.HUMAN
    assert commands[0].kind is CommandKind.NOTIFY_HUMAN

    with pytest.raises(TransitionRejected, match="correlation_id"):
        apply(state, EventKind.WAIT_RESOLVED, correlation_id="wrong")

    state, commands = apply(
        state,
        EventKind.WAIT_RESOLVED,
        correlation_id="approval-7",
        answer="approved",
    )
    assert state.phase is original_phase
    assert state.status is LifecycleStatus.ACTIVE
    assert state.wait is None
    assert commands[0].kind is CommandKind.START_RESEARCH
    assert commands[0].payload == {"answer": "approved"}


def test_qa_repairs_are_iterations_inside_verify_not_backwards_phases():
    state = advance_to_verify()

    state, commands = apply(
        state,
        EventKind.VERIFICATION_REPAIR_REQUIRED,
        finding_ids=["finding-1"],
    )
    assert state.phase is LifecyclePhase.VERIFY
    assert state.verification_cycle == 1
    assert commands[0].kind is CommandKind.START_REPAIR

    state, commands = apply(
        state,
        EventKind.REPAIR_COMPLETED,
        artifact_revision="sha256:two",
    )
    assert state.phase is LifecyclePhase.VERIFY
    assert state.artifact_revision == "sha256:two"
    assert commands[0].kind is CommandKind.START_VERIFICATION

    state, _ = apply(state, EventKind.VERIFICATION_PASSED)
    assert state.phase is LifecyclePhase.RELEASE


def test_retryable_failure_waits_for_a_durable_timer_without_failing_the_run():
    state = new_state()
    state, commands = apply(
        state,
        EventKind.OPERATION_FAILED,
        operation="research",
        reason="provider rate limited",
        retryable=True,
        retry_at="2026-09-08T00:00:00Z",
        resume_command=CommandKind.START_RESEARCH.value,
        correlation_id="retry-1",
    )
    assert state.status is LifecycleStatus.WAITING
    assert state.wait is not None and state.wait.kind is WaitKind.RETRY
    assert commands[0].kind is CommandKind.SCHEDULE_RETRY

    state, commands = apply(state, EventKind.WAIT_RESOLVED, correlation_id="retry-1")
    assert state.status is LifecycleStatus.ACTIVE
    assert commands[0].kind is CommandKind.START_RESEARCH


def test_recoverable_failure_can_resume_but_nonrecoverable_failure_cannot():
    state = new_state()
    state, commands = apply(
        state,
        EventKind.OPERATION_FAILED,
        operation="specification",
        reason="invalid structured result",
        recoverable=True,
    )
    assert state.status is LifecycleStatus.FAILED
    assert commands[0].kind is CommandKind.NOTIFY_OPERATOR

    state, commands = apply(
        state,
        EventKind.RECOVERY_REQUESTED,
        resume_command=CommandKind.START_SPECIFICATION.value,
    )
    assert state.status is LifecycleStatus.ACTIVE
    assert state.failure is None
    assert commands[0].kind is CommandKind.START_SPECIFICATION

    state, _ = apply(
        state,
        EventKind.OPERATION_FAILED,
        operation="policy",
        reason="tenant authorization invariant violated",
        recoverable=False,
    )
    with pytest.raises(TransitionRejected, match="not recoverable"):
        apply(state, EventKind.RECOVERY_REQUESTED)


def test_event_versions_fence_stale_writers_and_last_delivery_is_idempotent():
    state = new_state()
    event = Event("scope-1", EventKind.SCOPE_ACCEPTED, expected_version=0)
    transition = evolve(state, event)

    duplicate = evolve(transition.state, event)
    assert duplicate.duplicate is True
    assert duplicate.state == transition.state
    assert duplicate.commands == ()

    stale = Event("different-event", EventKind.RESEARCH_COMPLETED, expected_version=0)
    with pytest.raises(TransitionRejected, match="stale event version"):
        evolve(transition.state, stale)


def test_cancel_is_terminal_and_clears_a_wait():
    state = new_state()
    state, _ = apply(
        state,
        EventKind.WAIT_REQUESTED,
        wait_kind=WaitKind.EXTERNAL.value,
        correlation_id="external-1",
        reason="waiting for deployment callback",
    )
    state, commands = apply(state, EventKind.CANCEL_REQUESTED, reason="customer cancelled")
    assert state.status is LifecycleStatus.CANCELLED
    assert state.wait is None
    assert commands[0].kind is CommandKind.CANCEL_ACTIVE_OPERATION

    with pytest.raises(TransitionRejected, match="terminal"):
        apply(state, EventKind.WAIT_RESOLVED, correlation_id="external-1")


def test_state_round_trips_without_framework_objects():
    state = advance_to_verify()
    restored = LifecycleState.from_dict(state.to_dict())
    assert restored == state
    assert restored.to_dict()["phase"] == "verify"


def test_deadlines_and_timeouts_are_not_product_lifecycle_events():
    values = {kind.value for kind in EventKind}
    assert not any("timeout" in value or "deadline" in value for value in values)


def test_domain_package_cannot_import_runtime_or_infrastructure_frameworks():
    forbidden = {
        "asyncio", "fastapi", "os", "psycopg", "pydantic_ai", "requests",
        "sqlalchemy", "subprocess", "temporalio", "threading", "time",
    }
    domain_dir = ROOT / "src" / "agent_os" / "domain"
    violations = []
    for path in domain_dir.glob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name.split(".", 1)[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module.split(".", 1)[0]]
            else:
                continue
            violations.extend(f"{path.name}:{module}" for module in modules if module in forbidden)
    assert violations == []
