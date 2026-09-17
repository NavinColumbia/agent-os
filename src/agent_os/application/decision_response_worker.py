"""Crash-recoverable application of structured human decision responses."""

from __future__ import annotations

from typing import Any, Mapping

from agent_os.application.command_worker import (
    CommandRunReport,
    CommandRunStatus,
    RetryPolicy,
    RetryableCommandError,
    is_retryable_execution_error,
)
from agent_os.application.ports import GraphWorkflowEngine, NotificationStore
from agent_os.domain.workflow_runtime import (
    TokenStatus,
    WorkflowEvent,
    WorkflowEventKind,
)


class DurableDecisionResponseWorker:
    """Turn durable human intent into one idempotent workflow event.

    The intent is committed before execution. If this process stops after the
    graph event commits, the deterministic event ID makes lease recovery a
    harmless duplicate and the worker can still settle the personal inbox.
    """

    def __init__(
        self,
        *,
        store: NotificationStore,
        graph: GraphWorkflowEngine,
        worker_id: str,
        lease_seconds: int = 60,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        if not worker_id.strip() or lease_seconds < 3:
            raise ValueError("decision worker and a lease of at least three seconds are required")
        self._store = store
        self._graph = graph
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._retry = retry_policy or RetryPolicy()

    def run_one(self, tenant_id: str) -> CommandRunReport:
        lease = self._store.claim_decision_response(
            tenant_id,
            worker_id=self._worker_id,
            lease_seconds=self._lease_seconds,
        )
        if lease is None:
            return CommandRunReport(CommandRunStatus.IDLE)
        event = WorkflowEvent(
            lease.event_id,
            WorkflowEventKind.WAIT_RESUMED,
            lease.expected_version,
            {
                "correlation_id": lease.correlation_id,
                "response": dict(lease.response),
                "responded_by": lease.actor_id,
                "notification_id": lease.notification_id,
            },
        )
        try:
            receipt = self._graph.submit_graph_event(tenant_id, lease.run_id, event)
        except ValueError as exc:
            current = self._graph.get_graph_run(tenant_id, lease.run_id)
            still_waiting = bool(
                current is not None
                and any(
                    token.status is TokenStatus.WAITING
                    and token.wait_correlation_id == lease.correlation_id
                    for token in current.tokens
                )
            )
            error = self._error(exc, lease.attempt, retryable=still_waiting)
            if still_waiting and current is not None and current.version != lease.expected_version:
                changed = self._store.rebase_decision_response(
                    tenant_id,
                    lease.response_id,
                    worker_id=self._worker_id,
                    expected_version=current.version,
                    error=error,
                )
                return CommandRunReport(
                    CommandRunStatus.RETRY_SCHEDULED if changed else CommandRunStatus.LEASE_LOST,
                    lease.response_id,
                    lease.attempt,
                    retry_after_seconds=0 if changed else None,
                    error_type=type(exc).__name__,
                )
            if current is None:
                return self._fail(tenant_id, lease, exc, retryable=False)
            if not still_waiting:
                changed = self._store.complete_decision_response(
                    tenant_id,
                    lease.response_id,
                    worker_id=self._worker_id,
                    outcome="superseded",
                    result={
                        "reason": "the requested wait was already resolved or no longer exists",
                        "graph_version": None if current is None else current.version,
                    },
                )
                return CommandRunReport(
                    CommandRunStatus.SUCCEEDED if changed else CommandRunStatus.LEASE_LOST,
                    lease.response_id,
                    lease.attempt,
                )
            return self._fail(tenant_id, lease, exc, retryable=False)
        except Exception as exc:
            return self._fail(
                tenant_id,
                lease,
                exc,
                retryable=is_retryable_execution_error(exc),
            )
        changed = self._store.complete_decision_response(
            tenant_id,
            lease.response_id,
            worker_id=self._worker_id,
            outcome="applied",
            result={
                "event_id": lease.event_id,
                "graph_version": receipt.state.version,
                "duplicate_event": receipt.duplicate,
            },
        )
        return CommandRunReport(
            CommandRunStatus.SUCCEEDED if changed else CommandRunStatus.LEASE_LOST,
            lease.response_id,
            lease.attempt,
        )

    @staticmethod
    def _error(
        exc: Exception, attempt: int, *, retryable: bool,
    ) -> Mapping[str, Any]:
        return {
            "type": type(exc).__name__,
            "message": str(exc)[:2_000],
            "retryable": retryable,
            "attempt": attempt,
        }

    def _fail(self, tenant_id, lease, exc: Exception, *, retryable: bool) -> CommandRunReport:
        exhausted = (
            self._retry.max_attempts is not None
            and lease.attempt >= self._retry.max_attempts
        )
        error = self._error(exc, lease.attempt, retryable=retryable and not exhausted)
        if retryable and not exhausted:
            hint = exc.retry_after_seconds if isinstance(exc, RetryableCommandError) else None
            delay = self._retry.delay(lease.attempt, hint)
            changed = self._store.retry_decision_response(
                tenant_id,
                lease.response_id,
                worker_id=self._worker_id,
                error=error,
                delay_seconds=delay,
            )
            return CommandRunReport(
                CommandRunStatus.RETRY_SCHEDULED if changed else CommandRunStatus.LEASE_LOST,
                lease.response_id,
                lease.attempt,
                retry_after_seconds=delay if changed else None,
                error_type=type(exc).__name__,
            )
        changed = self._store.fail_decision_response(
            tenant_id,
            lease.response_id,
            worker_id=self._worker_id,
            error=error,
        )
        return CommandRunReport(
            CommandRunStatus.FAILED if changed else CommandRunStatus.LEASE_LOST,
            lease.response_id,
            lease.attempt,
            error_type=type(exc).__name__,
        )
