"""Crash-tolerant execution for arbitrary workflow-graph actions."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Event as ThreadEvent, Thread
from typing import Callable, Mapping, Any

from agent_os.application.command_worker import (
    CommandRunStatus,
    RetryPolicy,
    RetryableCommandError,
    is_retryable_execution_error,
)
from agent_os.application.ports import GraphActionExecutor, GraphActionOutbox
from agent_os.domain.workflow_runtime import WorkflowAction


@dataclass(frozen=True)
class GraphActionRunReport:
    status: CommandRunStatus
    action_id: str | None = None
    attempt: int | None = None
    retry_after_seconds: int | None = None
    error_type: str | None = None


class DurableGraphActionWorker:
    """Run one graph action while renewing its durable ownership lease."""

    def __init__(
        self,
        *,
        outbox: GraphActionOutbox,
        executor: GraphActionExecutor,
        worker_id: str,
        lease_seconds: int = 60,
        retry_policy: RetryPolicy | None = None,
        retry_classifier: Callable[[Exception], bool] = is_retryable_execution_error,
    ) -> None:
        if not worker_id.strip() or lease_seconds < 3:
            raise ValueError("worker_id and a lease of at least three seconds are required")
        self._outbox = outbox
        self._executor = executor
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._retry = retry_policy or RetryPolicy()
        self._retry_classifier = retry_classifier

    def run_one(self, tenant_id: str) -> GraphActionRunReport:
        lease = self._outbox.claim_graph_action(
            tenant_id,
            worker_id=self._worker_id,
            lease_seconds=self._lease_seconds,
        )
        if lease is None:
            return GraphActionRunReport(CommandRunStatus.IDLE)
        action_raw = lease.envelope.get("action")
        if not isinstance(action_raw, Mapping):
            raise ValueError("leased graph action envelope is malformed")
        action_id = WorkflowAction.from_dict(action_raw).action_id
        stopped = ThreadEvent()
        lease_lost = ThreadEvent()

        def renew() -> None:
            interval = max(1.0, self._lease_seconds / 3)
            while not stopped.wait(interval):
                try:
                    owned = self._outbox.heartbeat_graph_action(
                        tenant_id,
                        action_id,
                        worker_id=self._worker_id,
                        lease_seconds=self._lease_seconds,
                    )
                except Exception:
                    lease_lost.set()
                    return
                if not owned:
                    lease_lost.set()
                    return

        heartbeat = Thread(target=renew, name=f"aos-graph-lease-{action_id[:12]}", daemon=True)
        heartbeat.start()
        try:
            result = self._executor.execute(lease.envelope)
        except Exception as exc:
            stopped.set()
            heartbeat.join(timeout=1)
            if lease_lost.is_set():
                return GraphActionRunReport(
                    CommandRunStatus.LEASE_LOST, action_id, lease.attempt,
                    error_type=type(exc).__name__,
                )
            retryable = self._retry_classifier(exc)
            exhausted = self._retry.max_attempts is not None and lease.attempt >= self._retry.max_attempts
            error: Mapping[str, Any] = {
                "type": type(exc).__name__,
                "message": str(exc)[:2000],
                "retryable": retryable and not exhausted,
                "attempt": lease.attempt,
            }
            if retryable and not exhausted:
                hint = exc.retry_after_seconds if isinstance(exc, RetryableCommandError) else None
                delay = self._retry.delay(lease.attempt, hint)
                updated = self._outbox.retry_graph_action(
                    tenant_id,
                    action_id,
                    worker_id=self._worker_id,
                    error=error,
                    delay_seconds=delay,
                )
                return GraphActionRunReport(
                    CommandRunStatus.RETRY_SCHEDULED if updated else CommandRunStatus.LEASE_LOST,
                    action_id,
                    lease.attempt,
                    retry_after_seconds=delay,
                    error_type=type(exc).__name__,
                )
            updated = self._outbox.fail_graph_action(
                tenant_id,
                action_id,
                worker_id=self._worker_id,
                error=error,
            )
            return GraphActionRunReport(
                CommandRunStatus.FAILED if updated else CommandRunStatus.LEASE_LOST,
                action_id,
                lease.attempt,
                error_type=type(exc).__name__,
            )
        else:
            stopped.set()
            heartbeat.join(timeout=1)
            if lease_lost.is_set():
                return GraphActionRunReport(CommandRunStatus.LEASE_LOST, action_id, lease.attempt)
            updated = self._outbox.complete_graph_action(
                tenant_id,
                action_id,
                worker_id=self._worker_id,
                result=result,
            )
            return GraphActionRunReport(
                CommandRunStatus.SUCCEEDED if updated else CommandRunStatus.LEASE_LOST,
                action_id,
                lease.attempt,
            )
