"""Fair, supervised polling loop for horizontally replicated workers."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from threading import Event
from typing import Any, Mapping, Protocol

from agent_os.application.command_worker import CommandRunReport, CommandRunStatus
from agent_os.application.ports import ReadyTenantSource


WorkerObserver = Callable[[Mapping[str, Any]], None]


class TenantWorker(Protocol):
    def run_one(self, organization_id: str) -> CommandRunReport: ...


class CommandWorkerLoop:
    """Poll configured tenant shards fairly and survive transient control-plane loss."""

    def __init__(
        self,
        *,
        worker: TenantWorker,
        organization_ids: Sequence[str] = (),
        organization_source: ReadyTenantSource | None = None,
        tenant_batch_size: int = 128,
        idle_poll_seconds: float = 1,
        error_backoff_seconds: float = 5,
        observer: WorkerObserver | None = None,
    ) -> None:
        organizations = tuple(dict.fromkeys(item.strip() for item in organization_ids if item.strip()))
        if bool(organizations) == (organization_source is not None):
            raise ValueError("configure exactly one static organization list or dynamic source")
        if idle_poll_seconds <= 0 or error_backoff_seconds <= 0:
            raise ValueError("worker polling intervals must be positive")
        if tenant_batch_size < 1 or tenant_batch_size > 1_000:
            raise ValueError("tenant batch size must be between 1 and 1000")
        self._worker = worker
        self._organizations = organizations
        self._organization_source = organization_source
        self._tenant_batch_size = tenant_batch_size
        self._tenant_cursor: str | None = None
        self._idle_poll_seconds = idle_poll_seconds
        self._error_backoff_seconds = error_backoff_seconds
        self._observer = observer or (lambda _: None)
        self._last_cycle_had_error = False

    @property
    def organization_ids(self) -> tuple[str, ...]:
        return self._organizations

    def run_cycle(self) -> tuple[CommandRunReport, ...]:
        reports: list[CommandRunReport] = []
        self._last_cycle_had_error = False
        organizations = self._organizations
        if self._organization_source is not None:
            try:
                organizations = self._organization_source.list_ready_tenants(
                    after_tenant_id=self._tenant_cursor,
                    limit=self._tenant_batch_size,
                )
                if (
                    len(organizations) > self._tenant_batch_size
                    or any(not item.strip() for item in organizations)
                    or len(set(organizations)) != len(organizations)
                ):
                    raise ValueError("dynamic tenant source returned invalid tenant IDs")
                if organizations:
                    self._tenant_cursor = organizations[-1]
            except Exception as exc:
                self._last_cycle_had_error = True
                self._observer({
                    "event": "worker_discovery_error",
                    "error_type": type(exc).__name__,
                    "message": str(exc)[:2000],
                })
                return ()
        for organization_id in organizations:
            try:
                report = self._worker.run_one(organization_id)
            except Exception as exc:
                self._last_cycle_had_error = True
                self._observer({
                    "event": "worker_cycle_error",
                    "organization_id": organization_id,
                    "error_type": type(exc).__name__,
                    "message": str(exc)[:2000],
                })
                continue
            reports.append(report)
            if report.status is not CommandRunStatus.IDLE:
                self._observer({
                    "event": "command_run",
                    "organization_id": organization_id,
                    "command_id": report.command_id,
                    "status": report.status.value,
                    "attempt": report.attempt,
                    "retry_after_seconds": report.retry_after_seconds,
                    "error_type": report.error_type,
                })
        return tuple(reports)

    def run_forever(self, stop: Event) -> None:
        while not stop.is_set():
            reports = self.run_cycle()
            useful = any(report.status is not CommandRunStatus.IDLE for report in reports)
            if not useful:
                delay = self._error_backoff_seconds if self._last_cycle_had_error else self._idle_poll_seconds
                stop.wait(delay)
