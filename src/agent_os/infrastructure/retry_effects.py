"""Durable lifecycle retry-timer execution without sleeping a worker."""

from __future__ import annotations

from datetime import datetime, timezone
from math import ceil
from typing import Any, Callable, Mapping

from agent_os.application.command_worker import RetryableCommandError
from agent_os.application.lifecycle import CommandEnvelope
from agent_os.application.ports import WorkflowEngine
from agent_os.domain.lifecycle import Event, EventKind, LifecycleStatus, WaitKind


class RetryScheduleHandler:
    """Wake the exact retry wait, treating superseded timers as harmless."""

    def __init__(
        self,
        engine: WorkflowEngine,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._engine = engine
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def execute(self, item: CommandEnvelope) -> Mapping[str, Any]:
        correlation_id = str(item.command.payload.get("correlation_id") or "")
        if not correlation_id:
            raise ValueError("scheduled retry requires a correlation identity")
        state = self._engine.get_run(item.organization_id, item.run_id)
        if (
            state is None
            or state.status is not LifecycleStatus.WAITING
            or state.wait is None
            or state.wait.kind is not WaitKind.RETRY
            or state.wait.correlation_id != correlation_id
        ):
            return {"timer": "superseded", "correlation_id": correlation_id}

        if state.wait.retry_at is not None:
            due = datetime.fromisoformat(state.wait.retry_at.replace("Z", "+00:00"))
            now = self._clock()
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
            remaining = (due.astimezone(timezone.utc) - now.astimezone(timezone.utc)).total_seconds()
            if remaining > 0:
                raise RetryableCommandError(
                    "retry timer was claimed before its due time",
                    retry_after_seconds=max(1, ceil(remaining)),
                )

        event = Event(
            event_id=f"timer-{item.command_id}",
            kind=EventKind.WAIT_RESOLVED,
            expected_version=state.version,
            payload={"correlation_id": correlation_id},
        )
        return {
            "timer": "due",
            "correlation_id": correlation_id,
            "lifecycle_event": event.to_dict(),
        }
