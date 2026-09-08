"""Crash-tolerant command delivery without a global story stopwatch."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from threading import Event as ThreadEvent, Thread
from typing import Any, Callable, Mapping

from agent_os.application.ports import CommandExecutor, CommandOutbox


class CommandRunStatus(str, Enum):
    IDLE = "idle"
    SUCCEEDED = "succeeded"
    RETRY_SCHEDULED = "retry_scheduled"
    FAILED = "failed"
    LEASE_LOST = "lease_lost"


class RetryableCommandError(RuntimeError):
    def __init__(self, message: str, *, retry_after_seconds: int | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class FatalCommandError(RuntimeError):
    pass


@dataclass(frozen=True)
class RetryPolicy:
    base_delay_seconds: int = 2
    max_delay_seconds: int = 300
    max_attempts: int | None = None

    def __post_init__(self) -> None:
        if self.base_delay_seconds < 0 or self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("invalid retry delay bounds")
        if self.max_attempts is not None and self.max_attempts < 1:
            raise ValueError("max_attempts must be positive or None")

    def delay(self, attempt: int, hint: int | None = None) -> int:
        if hint is not None:
            return min(max(0, hint), self.max_delay_seconds)
        return min(self.max_delay_seconds, self.base_delay_seconds * (2 ** max(0, attempt - 1)))


@dataclass(frozen=True)
class CommandRunReport:
    status: CommandRunStatus
    command_id: str | None = None
    attempt: int | None = None
    retry_after_seconds: int | None = None
    error_type: str | None = None


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, RetryableCommandError):
        return True
    if isinstance(exc, FatalCommandError):
        return False
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    status_code = getattr(exc, "status_code", None)
    return status_code in {408, 409, 425, 429} or (
        isinstance(status_code, int) and 500 <= status_code <= 599
    )


class DurableCommandWorker:
    """Runs one leased command and renews ownership during genuinely long work.

    There is intentionally no wall-clock limit for a whole story. Individual
    provider/tool calls impose their own bounded deadlines; this worker keeps a
    live lease while useful progress continues and retries transient failures
    from durable state.
    """

    def __init__(
        self,
        *,
        outbox: CommandOutbox,
        executor: CommandExecutor,
        worker_id: str,
        lease_seconds: int = 60,
        retry_policy: RetryPolicy | None = None,
        retry_classifier: Callable[[Exception], bool] = _is_retryable,
    ) -> None:
        if not worker_id.strip() or lease_seconds < 3:
            raise ValueError("worker_id and a lease of at least three seconds are required")
        self._outbox = outbox
        self._executor = executor
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._retry = retry_policy or RetryPolicy()
        self._retry_classifier = retry_classifier

    def run_one(self, organization_id: str) -> CommandRunReport:
        lease = self._outbox.claim_command(
            organization_id,
            worker_id=self._worker_id,
            lease_seconds=self._lease_seconds,
        )
        if lease is None:
            return CommandRunReport(CommandRunStatus.IDLE)
        envelope = lease.envelope
        command_id = str(envelope["command_id"])
        stopped = ThreadEvent()
        lease_lost = ThreadEvent()

        def renew() -> None:
            interval = max(1.0, self._lease_seconds / 3)
            while not stopped.wait(interval):
                try:
                    owned = self._outbox.heartbeat_command(
                        organization_id,
                        command_id,
                        worker_id=self._worker_id,
                        lease_seconds=self._lease_seconds,
                    )
                except Exception:
                    # Database/network loss makes ownership unknowable. Stop
                    # committing; a recovered worker will safely reclaim.
                    lease_lost.set()
                    return
                if not owned:
                    lease_lost.set()
                    return

        heartbeat = Thread(target=renew, name=f"aos-lease-{command_id[:12]}", daemon=True)
        heartbeat.start()
        try:
            result = self._executor.execute(envelope)
        except Exception as exc:
            stopped.set()
            heartbeat.join(timeout=1)
            if lease_lost.is_set():
                return CommandRunReport(
                    CommandRunStatus.LEASE_LOST, command_id, lease.attempt, error_type=type(exc).__name__
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
                updated = self._outbox.retry_command(
                    organization_id,
                    command_id,
                    worker_id=self._worker_id,
                    error=error,
                    delay_seconds=delay,
                )
                return CommandRunReport(
                    CommandRunStatus.RETRY_SCHEDULED if updated else CommandRunStatus.LEASE_LOST,
                    command_id,
                    lease.attempt,
                    retry_after_seconds=delay,
                    error_type=type(exc).__name__,
                )
            updated = self._outbox.fail_command(
                organization_id,
                command_id,
                worker_id=self._worker_id,
                error=error,
            )
            return CommandRunReport(
                CommandRunStatus.FAILED if updated else CommandRunStatus.LEASE_LOST,
                command_id,
                lease.attempt,
                error_type=type(exc).__name__,
            )
        else:
            stopped.set()
            heartbeat.join(timeout=1)
            if lease_lost.is_set():
                return CommandRunReport(CommandRunStatus.LEASE_LOST, command_id, lease.attempt)
            updated = self._outbox.complete_command(
                organization_id,
                command_id,
                worker_id=self._worker_id,
                result=result,
            )
            return CommandRunReport(
                CommandRunStatus.SUCCEEDED if updated else CommandRunStatus.LEASE_LOST,
                command_id,
                lease.attempt,
            )
