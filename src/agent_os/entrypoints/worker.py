"""Composition root for the durable lifecycle command worker."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass, replace
import json
import os
import shutil
import signal
import socket
from threading import Event
from typing import Any, Mapping, Sequence

from agent_os.application.command_worker import DurableCommandWorker, RetryPolicy
from agent_os.application.default_organization import default_organization
from agent_os.application.graph_action_worker import DurableGraphActionWorker
from agent_os.application.work_multiplexer import TenantWorkMultiplexer
from agent_os.application.worker_loop import CommandWorkerLoop
from agent_os.domain.lifecycle import CommandKind
from agent_os.entrypoints.server import ServerSettings
from agent_os.infrastructure.agent_command_executor import DurableAgentCommandExecutor
from agent_os.infrastructure.artifact_tool_nodes import ArtifactToolNodeHandlers
from agent_os.infrastructure.command_router import LifecycleCommandRouter
from agent_os.infrastructure.dbos_lifecycle import DBOSLifecycleEngine
from agent_os.infrastructure.deployment_tool_nodes import DeploymentToolNodeHandlers
from agent_os.infrastructure.docker_sandbox import DEFAULT_PYTHON_IMAGE, DockerSandboxRunner
from agent_os.infrastructure.graph_action_executor import DurableGraphActionExecutor
from agent_os.infrastructure.mission_workflows import (
    MissionBootstrapHandler,
    MissionCancellationHandler,
    MissionGraphEffectHandlers,
    WorkflowLaunchToolNodeHandlers,
)
from agent_os.infrastructure.notification_effects import NotificationEffectHandlers
from agent_os.infrastructure.pydantic_agents import PydanticAgentRuntime
from agent_os.infrastructure.pydantic_graph_nodes import PydanticGraphNodeRuntime
from agent_os.infrastructure.sandbox_tool_nodes import SandboxToolNodeHandlers
from agent_os.infrastructure.sql_artifacts import SQLArtifactStore
from agent_os.infrastructure.sql_workflow_graph import SQLGraphWorkflowEngine
from agent_os.infrastructure.sql_notifications import SQLNotificationStore
from agent_os.infrastructure.sql_preview_deployments import SQLStaticPreviewDeployer
from agent_os.infrastructure.sql_ready_tenants import SQLReadyTenantSource
from agent_os.infrastructure.tool_node_router import GraphToolNodeRouter


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
    sandbox_backend: str
    sandbox_image: str
    sandbox_timeout_seconds: int
    tenant_discovery_limit: int

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
        max_attempts_raw = os.getenv("AOS_V2_RETRY_MAX_ATTEMPTS", "").strip()
        retry_max_attempts = None
        if max_attempts_raw:
            retry_max_attempts = _positive_int("AOS_V2_RETRY_MAX_ATTEMPTS", 1)
        sandbox_backend = os.getenv("AOS_V2_SANDBOX_BACKEND", "disabled").strip().lower()
        if sandbox_backend not in {"disabled", "docker"}:
            raise ValueError("AOS_V2_SANDBOX_BACKEND must be disabled or docker")
        tenant_discovery_limit = _positive_int("AOS_V2_TENANT_DISCOVERY_LIMIT", 128)
        if tenant_discovery_limit > 1_000:
            raise ValueError("AOS_V2_TENANT_DISCOVERY_LIMIT cannot exceed 1000")
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
            sandbox_backend=sandbox_backend,
            sandbox_image=os.getenv("AOS_V2_SANDBOX_IMAGE", DEFAULT_PYTHON_IMAGE).strip(),
            sandbox_timeout_seconds=_positive_int("AOS_V2_SANDBOX_TIMEOUT_SECONDS", 300),
            tenant_discovery_limit=tenant_discovery_limit,
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
    with ExitStack() as resources:
        engine = DBOSLifecycleEngine(
            system_database_url=settings.server.system_database_url,
            application_database_url=settings.server.application_database_url,
            application_version=settings.server.application_version,
            create_schema=settings.server.create_schema,
        )
        resources.callback(engine.close)
        graph_engine = SQLGraphWorkflowEngine(
            settings.server.application_database_url,
            create_schema=settings.server.create_schema,
        )
        resources.callback(graph_engine.close)
        notification_store = SQLNotificationStore(
            settings.server.application_database_url,
            create_schema=settings.server.create_schema,
        )
        resources.callback(notification_store.close)
        artifact_store = SQLArtifactStore(
            settings.server.application_database_url,
            create_schema=settings.server.create_schema,
        )
        resources.callback(artifact_store.close)
        preview_deployments = SQLStaticPreviewDeployer(
            settings.server.application_database_url,
            artifact_store,
            public_base_url=settings.server.public_base_url,
            capability_secret=settings.server.auth_secret,
            ttl_seconds=settings.server.preview_ttl_seconds,
            create_schema=settings.server.create_schema,
        )
        resources.callback(preview_deployments.close)
        notification_effects = NotificationEffectHandlers(notification_store)
        artifact_tools = ArtifactToolNodeHandlers(artifact_store)
        named_tool_handlers = dict(artifact_tools.named_handlers())
        named_tool_handlers.update(
            DeploymentToolNodeHandlers(preview_deployments).named_handlers()
        )
        named_tool_handlers.update(
            WorkflowLaunchToolNodeHandlers(
                graph_engine, artifact_store, engine,
            ).named_handlers()
        )
        if settings.sandbox_backend == "docker":
            docker_binary = shutil.which("docker")
            if docker_binary is None:
                raise ValueError("Docker sandbox backend is enabled but docker is unavailable")
            sandbox_runner = DockerSandboxRunner(
                artifact_store,
                image=settings.sandbox_image,
                docker_binary=docker_binary,
                timeout_seconds=settings.sandbox_timeout_seconds,
            )
            named_tool_handlers.update(SandboxToolNodeHandlers(sandbox_runner).named_handlers())
        tool_router = GraphToolNodeRouter(named_tool_handlers)
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
            artifact_store=artifact_store,
            max_turn_budget_cents=settings.max_turn_budget_cents,
        )
        lifecycle_handlers = dict(notification_effects.lifecycle_handlers())
        lifecycle_handlers[CommandKind.START_MISSION] = MissionBootstrapHandler(
            graph_engine, engine,
        ).execute
        lifecycle_handlers[CommandKind.CANCEL_ACTIVE_OPERATION] = MissionCancellationHandler(
            graph_engine, artifact_store,
        ).execute
        executor = LifecycleCommandRouter(
            agent_executor=agent_executor,
            handlers=lifecycle_handlers,
        )
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
            handlers=tool_router.handlers(),
            artifact_store=artifact_store,
            request_limit=settings.request_limit,
            output_tokens_limit=settings.output_tokens_limit,
            request_timeout_seconds=settings.request_timeout_seconds,
            max_turn_budget_cents=settings.max_turn_budget_cents,
        )
        graph_worker = DurableGraphActionWorker(
            outbox=graph_engine,
            executor=DurableGraphActionExecutor(
                engine=graph_engine,
                node_runtime=graph_runtime,
                effect_handlers=MissionGraphEffectHandlers(
                    lifecycle_engine=engine,
                    graph_engine=graph_engine,
                    notification_handlers=notification_effects.graph_handlers(),
                ).graph_handlers(),
            ),
            worker_id=settings.worker_id,
            lease_seconds=settings.lease_seconds,
            retry_policy=RetryPolicy(max_attempts=settings.retry_max_attempts),
        )
        tenant_worker = TenantWorkMultiplexer(
            lifecycle_worker=worker,
            graph_worker=graph_worker,
        )
        loop_options: dict[str, Any] = {"organization_ids": settings.organization_ids}
        if not settings.organization_ids:
            tenant_source = SQLReadyTenantSource(settings.server.application_database_url)
            resources.callback(tenant_source.close)
            loop_options = {
                "organization_source": tenant_source,
                "tenant_batch_size": settings.tenant_discovery_limit,
            }
        loop = CommandWorkerLoop(
            worker=tenant_worker,
            idle_poll_seconds=settings.idle_poll_seconds,
            error_backoff_seconds=settings.error_backoff_seconds,
            observer=_emit,
            **loop_options,
        )
        _emit({
            "event": "worker_started",
            "worker_id": settings.worker_id,
            "tenant_mode": "static" if settings.organization_ids else "dynamic",
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
            _emit({"event": "worker_stopped", "worker_id": settings.worker_id})


def install_shutdown_handlers(stop: Event) -> None:
    """Translate orchestrator SIGTERM/SIGINT into a clean lease-aware stop."""

    def request_stop(signum: int, _: object) -> None:
        _emit({"event": "worker_stop_requested", "signal": signum})
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
