from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent_os.application.command_worker import RetryableCommandError
from agent_os.application.lifecycle import CommandEnvelope, plan_transition
from agent_os.domain.lifecycle import Command, CommandKind, Event, EventKind, LifecycleState
from agent_os.infrastructure.retry_effects import RetryScheduleHandler


def retry_state(retry_at: str):
    decision = plan_transition(
        LifecycleState(run_id="run-retry", organization_id="org-a"),
        Event(
            "failure-1", EventKind.OPERATION_FAILED, 0,
            {
                "operation": "research",
                "reason": "provider throttled",
                "retryable": True,
                "retry_at": retry_at,
                "resume_command": CommandKind.START_RESEARCH.value,
                "correlation_id": "retry-1",
            },
        ),
    )
    return decision.transition.state


class Engine:
    def __init__(self, state):
        self.state = state

    def get_run(self, organization_id, run_id):
        assert (organization_id, run_id) == ("org-a", "run-retry")
        return self.state


def envelope():
    return CommandEnvelope(
        command_id="a" * 64,
        run_id="run-retry",
        organization_id="org-a",
        event_id="failure-1",
        aggregate_version=1,
        index=0,
        command=Command(CommandKind.SCHEDULE_RETRY, {"correlation_id": "retry-1"}),
    )


def test_retry_timer_emits_the_exact_idempotent_wait_resolution_when_due():
    now = datetime(2026, 9, 13, tzinfo=timezone.utc)
    result = RetryScheduleHandler(
        Engine(retry_state((now - timedelta(seconds=1)).isoformat())),  # type: ignore[arg-type]
        clock=lambda: now,
    ).execute(envelope())

    assert result["timer"] == "due"
    assert result["lifecycle_event"] == {
        "event_id": "timer-" + "a" * 64,
        "kind": "wait_resolved",
        "expected_version": 1,
        "payload": {"correlation_id": "retry-1"},
    }


def test_retry_timer_defers_clock_skew_and_ignores_a_superseded_wait():
    now = datetime(2026, 9, 13, tzinfo=timezone.utc)
    handler = RetryScheduleHandler(
        Engine(retry_state((now + timedelta(seconds=91)).isoformat())),  # type: ignore[arg-type]
        clock=lambda: now,
    )
    with pytest.raises(RetryableCommandError) as raised:
        handler.execute(envelope())
    assert raised.value.retry_after_seconds == 91

    stale = Engine(None)
    assert RetryScheduleHandler(stale).execute(envelope()) == {  # type: ignore[arg-type]
        "timer": "superseded", "correlation_id": "retry-1",
    }


def test_retry_wait_rejects_an_ambiguous_wall_clock_timestamp():
    with pytest.raises(Exception, match="timezone"):
        retry_state("2026-09-13T12:00:00")
