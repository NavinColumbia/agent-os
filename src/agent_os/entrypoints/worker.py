"""Composition root for the durable lifecycle command worker."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
import os
import signal
import socket
from threading import Event
from typing import Any, Mapping, Sequence

from agent_os.application.command_worker import DurableCommandWorker, RetryPolicy
from agent_os.application.default_organization import default_organization
from agent_os.application.graph_action_worker import DurableGraphActionWorker
from agent_os.application.work_multiplexer import TenantWorkMultiplexer
from agent_os.application.worker_loop import CommandWorkerLoop
from agent_os.entrypoints.server import ServerSettings
from agent_os.infrastructure.agent_command_executor import DurableAgentCommandExecutor
from agent_os.infrastructure.command_router import LifecycleCommandRouter
from agent_os.infrastructure.dbos_lifecycle import DBOSLifecycleEngine
from agent_os.infrastructure.graph_action_executor import DurableGraphActionExecutor
from agent_os.infrastructure.pydantic_agents import PydanticAgentRuntime
from agent_os.infrastructure.pydantic_graph_nodes import PydanticGraphNodeRuntime
from agent_os.infrastructure.sql_workflow_graph import SQLGraphWorkflowEngine


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < 1:
        raise ValueError(f"{name} must be positive")
    return value


def _positive_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _organization_ids(raw: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item.strip() for item in raw.split(",") if item.strip()))


@dataclass(frozen=True)
class WorkerSettings:
    server: ServerSettings
    model: str
    organization_ids: tuple[str, ...]
    worker_id: str
    lease_seconds: int
    idle_poll_seconds: float
    error_backoff_seconds: float
    request_limit: int
    output_tokens_limit: int
    request_timeout_seconds: float
    max_turn_budget_cents: int
    retry_max_attempts: int | None

    @classmethod
    def from_env(cls, *, organization_ids: Sequence[str] = ()) -> "WorkerSettings":
        server = ServerSettings.from_env()
        model = os.getenv("AOS_V2_MODEL", "").strip()
        if not model:
            raise ValueError(
                "AOS_V2_MODEL is required; choose the provider/model explicitly so a worker "
                "cannot spend against an accidental default"
            )
        configured = tuple(dict.fromkeys(item.strip() for item in organization_ids if item.strip()))
        if not configured:
            configured = _organization_ids(os.getenv("AOS_V2_WORKER_ORGANIZATIONS", ""))
        if not configured and server.environment in {"development", "test"}:
            configured = ("local-company",)
        if not configured:
            raise ValueError("AOS_V2_WORKER_ORGANIZATIONS is required in staging/production")
        max_attempts_raw = os.getenv("AOS_V2_RETRY_MAX_ATTEMPTS", "").strip()
        retry_max_attempts = None
        if max_attempts_raw:
            retry_max_attempts = _positive_int("AOS_V2_RETRY_MAX_ATTEMPTS", 1)
        worker_id = os.getenv("AOS_V2_WORKER_ID", "").strip()
        if not worker_id:
            worker_id = f"{socket.gethostname()}:{os.getpid()}"
        return cls(
            server=server,
            model=model,
            organization_ids=configured,
            worker_id=worker_id,
            lease_seconds=max(3, _positive_int("AOS_V2_LEASE_SECONDS", 60)),
            idle_poll_seconds=_positive_float("AOS_V2_IDLE_POLL_SECONDS", 1),
            error_backoff_seconds=_positive_float("AOS_V2_ERROR_BACKOFF_SECONDS", 5),
            request_limit=_positive_int("AOS_V2_MODEL_REQUEST_LIMIT", 12),
            output_tokens_limit=_positive_int("AOS_V2_MODEL_OUTPUT_TOKENS_LIMIT", 8_000),
            request_timeout_seconds=_positive_float("AOS_V2_MODEL_REQUEST_TIMEOUT_SECONDS", 120),
            max_turn_budget_cents=_positive_int("AOS_V2_MAX_TURN_COST_CENTS", 100),
            retry_max_attempts=retry_max_attempts,
        )

    def with_organizations(self, organization_ids: Sequence[str]) -> "WorkerSettings":
        configured = tuple(dict.fromkeys(item.strip() for item in organization_ids if item.strip()))
        return self if not configured else replace(self, organization_ids=configured)


def _emit(record: Mapping[str, Any]) -> None:
    print(json.dumps(dict(record), separators=(",", ":"), sort_keys=True), flush=True)


def run_worker(
    settings: WorkerSettings,
    *,
    once: bool = False,
    stop: Event | None = None,
) -> tuple[Mapping[str, Any], ...] | None:
    """Build and run one worker process; replicas coordinate through leases."""

    stop = stop or Event()
    engine = DBOSLifecycleEngine(
        system_database_url=settings.server.system_database_url,
        application_database_url=settings.server.application_database_url,
        application_version=settings.server.application_version,
        create_schema=settings.server.create_schema,
    )
    graph_engine = SQLGraphWorkflowEngine(
        settings.server.application_database_url,
        create_schema=settings.server.create_schema,
    )
    runtime = PydanticAgentRuntime(
        settings.model,
        request_limit=settings.request_limit,
        output_tokens_limit=settings.output_tokens_limit,
        request_timeout_seconds=settings.request_timeout_seconds,
    )
    agent_executor = DurableAgentCommandExecutor(
        runtime=runtime,
        ledger=engine,
        organization_loader=default_organization,
        max_turn_budget_cents=settings.max_turn_budget_cents,
    )
    executor = LifecycleCommandRouter(agent_executor=agent_executor)
    worker = DurableCommandWorker(
        outbox=engine,
        executor=executor,
        worker_id=settings.worker_id,
        lease_seconds=settings.lease_seconds,
        retry_policy=RetryPolicy(max_attempts=settings.retry_max_attempts),
        workflow_engine=engine,
        workflow_result_waiter=engine.get_result,
    )
    graph_runtime = PydanticGraphNodeRuntime(
        settings.model,
        request_limit=settings.request_limit,
        output_tokens_limit=settings.output_tokens_limit,
        request_timeout_seconds=settings.request_timeout_seconds,
        max_turn_budget_cents=settings.max_turn_budget_cents,
    )
    graph_worker = DurableGraphActionWorker(
        outbox=graph_engine,
        executor=DurableGraphActionExecutor(engine=graph_engine, node_runtime=graph_runtime),
        worker_id=settings.worker_id,
        lease_seconds=settings.lease_seconds,
        retry_policy=RetryPolicy(max_attempts=settings.retry_max_attempts),
    )
    tenant_worker = TenantWorkMultiplexer(
        lifecycle_worker=worker,
        graph_worker=graph_worker,
    )
    loop = CommandWorkerLoop(
        worker=tenant_worker,
        organization_ids=settings.organization_ids,
        idle_poll_seconds=settings.idle_poll_seconds,
        error_backoff_seconds=settings.error_backoff_seconds,
        observer=_emit,
    )
    _emit({
        "event": "worker_started",
        "worker_id": settings.worker_id,
        "organizations": list(settings.organization_ids),
        "model": settings.model,
    })
    try:
        if once:
            reports = loop.run_cycle()
            return tuple({
                "command_id": report.command_id,
                "status": report.status.value,
                "attempt": report.attempt,
                "error_type": report.error_type,
            } for report in reports)
        loop.run_forever(stop)
        return None
    finally:
        try:
            graph_engine.close()
        finally:
            engine.close()
            _emit({"event": "worker_stopped", "worker_id": settings.worker_id})


def install_shutdown_handlers(stop: Event) -> None:
    """Translate orchestrator SIGTERM/SIGINT into a clean lease-aware stop."""

    def request_stop(signum: int, _: object) -> None:
        _emit({"event": "worker_stop_requested", "signal": signum})
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
