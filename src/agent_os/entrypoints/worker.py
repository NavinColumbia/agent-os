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
from urllib.parse import urlparse

from agent_os.application.command_worker import DurableCommandWorker, RetryPolicy
from agent_os.application.graph_action_worker import DurableGraphActionWorker
from agent_os.application.management_monitor import DurableManagementMonitor
from agent_os.application.work_multiplexer import TenantWorkMultiplexer
from agent_os.application.worker_loop import CommandWorkerLoop
from agent_os.domain.lifecycle import CommandKind
from agent_os.entrypoints.server import ServerSettings
from agent_os.infrastructure.agent_command_executor import DurableAgentCommandExecutor
from agent_os.infrastructure.artifact_tool_nodes import ArtifactToolNodeHandlers
from agent_os.infrastructure.command_router import LifecycleCommandRouter
from agent_os.infrastructure.cloud_run_apps import CloudRunServiceDeployer
from agent_os.infrastructure.cloud_run_sandbox import CloudRunJobSandboxRunner
from agent_os.infrastructure.dbos_lifecycle import DBOSLifecycleEngine
from agent_os.infrastructure.deployment_tool_nodes import DeploymentToolNodeHandlers
from agent_os.infrastructure.docker_sandbox import DEFAULT_PYTHON_IMAGE, DockerSandboxRunner
from agent_os.infrastructure.graph_action_executor import DurableGraphActionExecutor
from agent_os.infrastructure.graph_organization_effects import GraphOrganizationEffectHandler
from agent_os.infrastructure.gcs_artifacts import build_artifact_store
from agent_os.infrastructure.gcs_static_sites import GCSStaticSiteDeployer
from agent_os.infrastructure.http_connector_tools import (
    FileConnectorSecretResolver,
    GCPConnectorSecretResolver,
    HTTPConnectorToolNodeHandlers,
)
from agent_os.infrastructure.mission_workflows import (
    MissionBootstrapHandler,
    MissionCancellationHandler,
    MissionGraphEffectHandlers,
    WorkflowLaunchToolNodeHandlers,
)
from agent_os.infrastructure.notification_effects import NotificationEffectHandlers
from agent_os.infrastructure.notification_delivery import (
    DurableNotificationDeliveryWorker,
    GovernedNotificationSender,
)
from agent_os.infrastructure.pydantic_agents import PydanticAgentRuntime
from agent_os.infrastructure.pydantic_graph_nodes import PydanticGraphNodeRuntime
from agent_os.infrastructure.retry_effects import RetryScheduleHandler
from agent_os.infrastructure.sandbox_tool_nodes import SandboxToolNodeHandlers
from agent_os.infrastructure.sql_company_directory import SQLCompanyDirectory
from agent_os.infrastructure.sql_connectors import SQLConnectorRegistry
from agent_os.infrastructure.sql_workflow_graph import SQLGraphWorkflowEngine
from agent_os.infrastructure.sql_notifications import SQLNotificationStore
from agent_os.infrastructure.sql_preview_deployments import SQLStaticPreviewDeployer
from agent_os.infrastructure.sql_ready_tenants import SQLReadyTenantSource
from agent_os.infrastructure.sql_usage_meter import SQLUsageMeter
from agent_os.infrastructure.sql_tenant_models import SQLTenantModelStore
from agent_os.infrastructure.tenant_model_resolver import TenantModelResolver
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
    sandbox_workspace_root: str
    sandbox_project_id: str
    sandbox_region: str
    sandbox_job_name: str
    sandbox_bucket: str
    sandbox_signing_service_account: str
    sandbox_revision: str
    published_app_bucket: str
    apps_base_url: str
    app_project_id: str
    app_region: str
    app_source_bucket: str
    app_repository: str
    app_build_service_account: str
    app_runtime_service_account: str
    app_builder_image: str
    app_max_instances: int
    tenant_discovery_limit: int
    management_check_seconds: int
    slow_work_seconds: int
    management_escalation_checks: int
    connector_secret_directory: str
    connector_secret_backend: str
    connector_secret_project_id: str

    @classmethod
    def from_env(cls, *, organization_ids: Sequence[str] = ()) -> "WorkerSettings":
        # Payment credentials belong only in the API process. The worker uses
        # shared identity/database/capability settings but never validates or
        # receives Stripe secrets.
        server = ServerSettings.from_env(require_billing=False, require_identity=False)
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
        if sandbox_backend not in {"disabled", "docker", "cloud-run-job"}:
            raise ValueError(
                "AOS_V2_SANDBOX_BACKEND must be disabled, docker, or cloud-run-job"
            )
        sandbox_workspace_root = os.getenv(
            "AOS_V2_SANDBOX_WORKSPACE_ROOT", "",
        ).strip()
        if sandbox_workspace_root and not os.path.isabs(sandbox_workspace_root):
            raise ValueError("AOS_V2_SANDBOX_WORKSPACE_ROOT must be absolute")
        sandbox_project_id = os.getenv("AOS_V2_SANDBOX_PROJECT_ID", "").strip()
        sandbox_region = os.getenv("AOS_V2_SANDBOX_REGION", "").strip()
        sandbox_job_name = os.getenv("AOS_V2_SANDBOX_JOB_NAME", "").strip()
        sandbox_bucket = os.getenv("AOS_V2_SANDBOX_BUCKET", "").strip()
        sandbox_signing_service_account = os.getenv(
            "AOS_V2_SANDBOX_SIGNING_SERVICE_ACCOUNT", "",
        ).strip()
        sandbox_revision = os.getenv("AOS_V2_SANDBOX_REVISION", "").strip()
        if sandbox_backend == "cloud-run-job" and any(not value for value in (
            sandbox_project_id,
            sandbox_region,
            sandbox_job_name,
            sandbox_bucket,
            sandbox_signing_service_account,
            sandbox_revision,
        )):
            raise ValueError(
                "cloud-run-job sandbox requires project, region, job, bucket, "
                "signing service account, and revision settings"
            )
        published_app_bucket = os.getenv("AOS_V2_PUBLISHED_APP_BUCKET", "").strip()
        apps_base_url = os.getenv("AOS_V2_APPS_BASE_URL", "").strip()
        if bool(published_app_bucket) != bool(apps_base_url):
            raise ValueError(
                "production static publishing requires both bucket and public base URL"
            )
        if apps_base_url:
            parsed_apps_url = urlparse(apps_base_url)
            if (
                parsed_apps_url.scheme != "https"
                or not parsed_apps_url.netloc
                or parsed_apps_url.path not in {"", "/"}
                or parsed_apps_url.query
                or parsed_apps_url.fragment
                or parsed_apps_url.username
                or parsed_apps_url.password
            ):
                raise ValueError("AOS_V2_APPS_BASE_URL must be an HTTPS origin")
            if apps_base_url.rstrip("/") == server.public_base_url:
                raise ValueError("published apps require an origin separate from the control API")
        if server.environment == "production" and not published_app_bucket:
            raise ValueError(
                "production requires AOS_V2_PUBLISHED_APP_BUCKET and AOS_V2_APPS_BASE_URL"
            )
        app_project_id = os.getenv("AOS_V2_APP_PROJECT_ID", "").strip()
        app_region = os.getenv("AOS_V2_APP_REGION", "").strip()
        app_source_bucket = os.getenv("AOS_V2_APP_SOURCE_BUCKET", "").strip()
        app_repository = os.getenv("AOS_V2_APP_REPOSITORY", "").strip()
        app_build_service_account = os.getenv(
            "AOS_V2_APP_BUILD_SERVICE_ACCOUNT", "",
        ).strip()
        app_runtime_service_account = os.getenv(
            "AOS_V2_APP_RUNTIME_SERVICE_ACCOUNT", "",
        ).strip()
        app_builder_image = os.getenv("AOS_V2_APP_BUILDER_IMAGE", "").strip()
        app_configuration = (
            app_project_id,
            app_region,
            app_source_bucket,
            app_repository,
            app_build_service_account,
            app_runtime_service_account,
            app_builder_image,
        )
        if any(app_configuration) and not all(app_configuration):
            raise ValueError(
                "production service deployment requires app project, region, source bucket, "
                "repository, build/runtime service accounts, and digest-pinned builder image"
            )
        if app_project_id and sandbox_project_id and app_project_id == sandbox_project_id:
            raise ValueError("generated applications and untrusted QA sandboxes require separate projects")
        app_max_instances = _positive_int("AOS_V2_APP_MAX_INSTANCES", 10)
        if app_max_instances > 100:
            raise ValueError("AOS_V2_APP_MAX_INSTANCES cannot exceed 100")
        tenant_discovery_limit = _positive_int("AOS_V2_TENANT_DISCOVERY_LIMIT", 128)
        if tenant_discovery_limit > 1_000:
            raise ValueError("AOS_V2_TENANT_DISCOVERY_LIMIT cannot exceed 1000")
        management_check_seconds = _positive_int("AOS_V2_MANAGEMENT_CHECK_SECONDS", 30)
        slow_work_seconds = _positive_int("AOS_V2_SLOW_WORK_SECONDS", 300)
        management_escalation_checks = _positive_int("AOS_V2_MANAGEMENT_ESCALATION_CHECKS", 3)
        if management_check_seconds > 3_600:
            raise ValueError("AOS_V2_MANAGEMENT_CHECK_SECONDS cannot exceed 3600")
        if slow_work_seconds > 7 * 24 * 60 * 60:
            raise ValueError("AOS_V2_SLOW_WORK_SECONDS cannot exceed 604800")
        if management_escalation_checks > 100:
            raise ValueError("AOS_V2_MANAGEMENT_ESCALATION_CHECKS cannot exceed 100")
        connector_secret_directory = os.getenv(
            "AOS_V2_CONNECTOR_SECRET_DIR", "/run/secrets/agent-os-connectors",
        ).strip()
        if not os.path.isabs(connector_secret_directory):
            raise ValueError("AOS_V2_CONNECTOR_SECRET_DIR must be absolute")
        connector_secret_backend = os.getenv(
            "AOS_V2_CONNECTOR_SECRET_BACKEND",
            "file",
        ).strip().lower()
        if connector_secret_backend not in {"file", "gcp"}:
            raise ValueError("AOS_V2_CONNECTOR_SECRET_BACKEND must be file or gcp")
        connector_secret_project_id = os.getenv(
            "AOS_V2_CONNECTOR_SECRET_PROJECT_ID", "",
        ).strip()
        if connector_secret_backend == "gcp" and not connector_secret_project_id:
            raise ValueError(
                "GCP connector secrets require AOS_V2_CONNECTOR_SECRET_PROJECT_ID"
            )
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
            sandbox_workspace_root=sandbox_workspace_root,
            sandbox_project_id=sandbox_project_id,
            sandbox_region=sandbox_region,
            sandbox_job_name=sandbox_job_name,
            sandbox_bucket=sandbox_bucket,
            sandbox_signing_service_account=sandbox_signing_service_account,
            sandbox_revision=sandbox_revision,
            published_app_bucket=published_app_bucket,
            apps_base_url=apps_base_url,
            app_project_id=app_project_id,
            app_region=app_region,
            app_source_bucket=app_source_bucket,
            app_repository=app_repository,
            app_build_service_account=app_build_service_account,
            app_runtime_service_account=app_runtime_service_account,
            app_builder_image=app_builder_image,
            app_max_instances=app_max_instances,
            tenant_discovery_limit=tenant_discovery_limit,
            management_check_seconds=management_check_seconds,
            slow_work_seconds=slow_work_seconds,
            management_escalation_checks=management_escalation_checks,
            connector_secret_directory=connector_secret_directory,
            connector_secret_backend=connector_secret_backend,
            connector_secret_project_id=connector_secret_project_id,
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
        company_directory = SQLCompanyDirectory(
            settings.server.application_database_url,
            create_schema=settings.server.create_schema,
        )
        resources.callback(company_directory.close)
        connector_registry = SQLConnectorRegistry(
            settings.server.application_database_url,
            create_schema=settings.server.create_schema,
        )
        resources.callback(connector_registry.close)
        tenant_model_store = SQLTenantModelStore(
            settings.server.application_database_url,
            create_schema=settings.server.create_schema,
        )
        resources.callback(tenant_model_store.close)
        notification_store = SQLNotificationStore(
            settings.server.application_database_url,
            create_schema=settings.server.create_schema,
        )
        resources.callback(notification_store.close)
        artifact_store = build_artifact_store(
            settings.server.application_database_url,
            backend=settings.server.artifact_backend,
            bucket_name=settings.server.artifact_bucket,
            create_schema=settings.server.create_schema,
            max_content_bytes=settings.server.artifact_max_content_bytes,
            retention_days=settings.server.artifact_retention_days,
        )
        resources.callback(artifact_store.close)
        usage_meter = SQLUsageMeter(
            settings.server.application_database_url,
            monthly_budget_cents=settings.server.tenant_monthly_model_budget_cents,
            create_schema=settings.server.create_schema,
        )
        resources.callback(usage_meter.close)
        preview_deployments = SQLStaticPreviewDeployer(
            settings.server.application_database_url,
            artifact_store,
            public_base_url=settings.server.public_base_url,
            capability_secret=settings.server.capability_secret,
            ttl_seconds=settings.server.preview_ttl_seconds,
            create_schema=settings.server.create_schema,
        )
        resources.callback(preview_deployments.close)
        static_deployer = None
        if settings.published_app_bucket:
            static_deployer = GCSStaticSiteDeployer(
                artifact_store,
                bucket_name=settings.published_app_bucket,
                public_base_url=settings.apps_base_url,
                capability_secret=settings.server.capability_secret,
            )
        service_deployer = None
        if settings.app_project_id:
            service_deployer = CloudRunServiceDeployer(
                artifact_store,
                project_id=settings.app_project_id,
                region=settings.app_region,
                source_bucket=settings.app_source_bucket,
                repository=settings.app_repository,
                build_service_account_email=settings.app_build_service_account,
                runtime_service_account_email=settings.app_runtime_service_account,
                builder_image=settings.app_builder_image,
                maximum_instances=settings.app_max_instances,
            )
        notification_effects = NotificationEffectHandlers(notification_store)
        artifact_tools = ArtifactToolNodeHandlers(artifact_store)
        named_tool_handlers = dict(artifact_tools.named_handlers())
        named_tool_handlers.update(
            DeploymentToolNodeHandlers(
                preview_deployments, static_deployer, service_deployer,
            ).named_handlers()
        )
        connector_secrets = (
            GCPConnectorSecretResolver(settings.connector_secret_project_id)
            if settings.connector_secret_backend == "gcp"
            else FileConnectorSecretResolver(settings.connector_secret_directory)
        )
        tenant_model_resolver = TenantModelResolver(
            tenant_model_store,
            connector_secrets,
            fallback_model=settings.model,
        )
        named_tool_handlers.update(HTTPConnectorToolNodeHandlers(
            connector_registry,
            artifact_store,
            connector_secrets,
        ).named_handlers())
        if settings.sandbox_backend == "docker":
            docker_binary = shutil.which("docker")
            if docker_binary is None:
                raise ValueError("Docker sandbox backend is enabled but docker is unavailable")
            sandbox_runner = DockerSandboxRunner(
                artifact_store,
                image=settings.sandbox_image,
                docker_binary=docker_binary,
                timeout_seconds=settings.sandbox_timeout_seconds,
                workspace_root=settings.sandbox_workspace_root or None,
            )
            named_tool_handlers.update(SandboxToolNodeHandlers(sandbox_runner).named_handlers())
        elif settings.sandbox_backend == "cloud-run-job":
            sandbox_runner = CloudRunJobSandboxRunner(
                artifact_store,
                project_id=settings.sandbox_project_id,
                region=settings.sandbox_region,
                job_name=settings.sandbox_job_name,
                bucket_name=settings.sandbox_bucket,
                signing_service_account_email=settings.sandbox_signing_service_account,
                sandbox_revision=settings.sandbox_revision,
                timeout_seconds=settings.sandbox_timeout_seconds,
            )
            named_tool_handlers.update(SandboxToolNodeHandlers(sandbox_runner).named_handlers())
        available_mission_tools = frozenset({
            "deploy.preview", "deploy.static", "deploy.service", "sandbox.run",
            "connector.invoke",
        }) & frozenset(named_tool_handlers)
        named_tool_handlers.update(
            WorkflowLaunchToolNodeHandlers(
                graph_engine, artifact_store, engine,
                available_tools=available_mission_tools,
            ).named_handlers()
        )
        tool_router = GraphToolNodeRouter(named_tool_handlers)
        runtime = PydanticAgentRuntime(
            settings.model,
            request_limit=settings.request_limit,
            output_tokens_limit=settings.output_tokens_limit,
            request_timeout_seconds=settings.request_timeout_seconds,
            usage_meter=usage_meter,
            model_name=settings.model,
            model_selector=tenant_model_resolver,
        )
        agent_executor = DurableAgentCommandExecutor(
            runtime=runtime,
            ledger=engine,
            organization_loader=lambda tenant_id, _: company_directory.get_organization(tenant_id),
            artifact_store=artifact_store,
            max_turn_budget_cents=settings.max_turn_budget_cents,
        )
        lifecycle_handlers = dict(notification_effects.lifecycle_handlers())
        lifecycle_handlers[CommandKind.SCHEDULE_RETRY] = RetryScheduleHandler(engine).execute
        lifecycle_handlers[CommandKind.START_MISSION] = MissionBootstrapHandler(
            graph_engine,
            engine,
            available_tools=available_mission_tools,
            capability_context=lambda tenant_id: {
                "configured_connectors": [{
                    "connector_id": item["connector_id"],
                    "display_name": item["display_name"],
                    "allowed_path_prefixes": item["allowed_path_prefixes"],
                    "allowed_methods": item["allowed_methods"],
                    "active": item["active"],
                    "authentication_configured": item["auth_kind"] != "none",
                } for item in connector_registry.list_connectors(tenant_id)],
            },
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
            organization_loader=company_directory.get_organization,
            usage_meter=usage_meter,
            model_name=settings.model,
            model_selector=tenant_model_resolver,
        )
        graph_effect_handlers = MissionGraphEffectHandlers(
            lifecycle_engine=engine,
            graph_engine=graph_engine,
            notification_handlers=notification_effects.graph_handlers(),
        ).graph_handlers()
        graph_effect_handlers.update(
            GraphOrganizationEffectHandler(engine, company_directory).handlers()
        )
        graph_worker = DurableGraphActionWorker(
            outbox=graph_engine,
            executor=DurableGraphActionExecutor(
                engine=graph_engine,
                node_runtime=graph_runtime,
                effect_handlers=graph_effect_handlers,
            ),
            worker_id=settings.worker_id,
            lease_seconds=settings.lease_seconds,
            retry_policy=RetryPolicy(max_attempts=settings.retry_max_attempts),
        )
        management_worker = DurableManagementMonitor(
            watches=graph_engine,
            graph=graph_engine,
            inspector=graph_engine,
            notifications=notification_store,
            worker_id=settings.worker_id,
            lease_seconds=settings.lease_seconds,
            check_interval_seconds=settings.management_check_seconds,
            slow_after_seconds=settings.slow_work_seconds,
            escalation_checks=settings.management_escalation_checks,
            retry_delay_seconds=max(1, int(settings.error_backoff_seconds)),
        )
        notification_worker = DurableNotificationDeliveryWorker(
            store=notification_store,
            sender=GovernedNotificationSender(
                connector_registry,
                connector_secrets,
            ),
            worker_id=settings.worker_id,
            lease_seconds=settings.lease_seconds,
            retry_policy=RetryPolicy(max_attempts=settings.retry_max_attempts),
        )
        tenant_worker = TenantWorkMultiplexer(
            lifecycle_worker=worker,
            graph_worker=graph_worker,
            management_worker=management_worker,
            notification_worker=notification_worker,
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
            "management_check_seconds": settings.management_check_seconds,
            "slow_work_seconds": settings.slow_work_seconds,
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
