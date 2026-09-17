from __future__ import annotations

from typing import Any, Mapping

import pytest

from agent_os.application.command_worker import CommandRunReport, CommandRunStatus, FatalCommandError
from agent_os.application.default_organization import default_organization
from agent_os.application.lifecycle import CommandEnvelope
from agent_os.application.worker_loop import CommandWorkerLoop
from agent_os.domain.lifecycle import Command, CommandKind
from agent_os.entrypoints.worker import WorkerSettings
from agent_os.infrastructure.command_router import LifecycleCommandRouter


class StubAgentExecutor:
    @staticmethod
    def supports(kind: CommandKind) -> bool:
        return kind is CommandKind.START_RESEARCH

    def execute(self, envelope: Mapping[str, Any]) -> Mapping[str, Any]:
        return {"agent": envelope["command_id"]}


def envelope(kind: CommandKind) -> dict[str, Any]:
    return CommandEnvelope(
        "a" * 64,
        "run-1",
        "tenant-1",
        "event-1",
        1,
        0,
        Command(kind, {}),
    ).to_dict()


def test_command_router_never_claims_an_unconfigured_external_effect_was_delivered():
    router = LifecycleCommandRouter(agent_executor=StubAgentExecutor())  # type: ignore[arg-type]

    assert router.execute(envelope(CommandKind.START_RESEARCH))["agent"] == "a" * 64
    with pytest.raises(FatalCommandError, match="not acknowledged as delivered"):
        router.execute(envelope(CommandKind.NOTIFY_HUMAN))


def test_command_router_uses_only_an_explicit_external_effect_handler():
    handled = []
    router = LifecycleCommandRouter(  # type: ignore[arg-type]
        agent_executor=StubAgentExecutor(),
        handlers={CommandKind.NOTIFY_HUMAN: lambda item: handled.append(item.command.kind) or {
            "delivery_id": "notification-1"
        }},
    )

    result = router.execute(envelope(CommandKind.NOTIFY_HUMAN))

    assert result == {"delivery_id": "notification-1"}
    assert handled == [CommandKind.NOTIFY_HUMAN]


class StubWorker:
    def __init__(self) -> None:
        self.organizations: list[str] = []

    def run_one(self, organization_id: str) -> CommandRunReport:
        self.organizations.append(organization_id)
        return CommandRunReport(CommandRunStatus.IDLE)


def test_worker_loop_polls_each_tenant_once_per_cycle_without_starvation():
    worker = StubWorker()
    observed = []
    loop = CommandWorkerLoop(  # type: ignore[arg-type]
        worker=worker,
        organization_ids=("tenant-a", "tenant-b", "tenant-a"),
        observer=observed.append,
    )

    reports = loop.run_cycle()

    assert worker.organizations == ["tenant-a", "tenant-b"]
    assert len(reports) == 2
    assert observed == []  # idle polling does not flood operational logs


def test_worker_loop_contains_one_tenant_failure_and_continues_other_tenants():
    observed = []

    class PartiallyBrokenWorker:
        def __init__(self) -> None:
            self.organizations: list[str] = []

        def run_one(self, organization_id: str) -> CommandRunReport:
            self.organizations.append(organization_id)
            if organization_id == "tenant-a":
                raise ConnectionError("database unavailable")
            return CommandRunReport(CommandRunStatus.IDLE)

    worker = PartiallyBrokenWorker()
    loop = CommandWorkerLoop(  # type: ignore[arg-type]
        worker=worker,
        organization_ids=("tenant-a", "tenant-b"),
        observer=observed.append,
    )

    reports = loop.run_cycle()

    assert len(reports) == 1
    assert observed[0]["event"] == "worker_cycle_error"
    assert worker.organizations == ["tenant-a", "tenant-b"]


def test_worker_loop_discovers_ready_tenants_with_a_fair_cursor():
    class StubSource:
        def __init__(self) -> None:
            self.calls = []

        def list_ready_tenants(self, *, after_tenant_id=None, limit=128):
            self.calls.append((after_tenant_id, limit))
            return ("tenant-b", "tenant-c") if len(self.calls) == 1 else ("tenant-a",)

    worker = StubWorker()
    source = StubSource()
    loop = CommandWorkerLoop(  # type: ignore[arg-type]
        worker=worker,
        organization_source=source,
        tenant_batch_size=2,
    )

    assert len(loop.run_cycle()) == 2
    assert len(loop.run_cycle()) == 1
    assert source.calls == [(None, 2), ("tenant-c", 2)]
    assert worker.organizations == ["tenant-b", "tenant-c", "tenant-a"]


def test_worker_loop_contains_tenant_discovery_failure_and_can_retry():
    observed = []

    class BrokenSource:
        def list_ready_tenants(self, **kwargs):
            del kwargs
            raise ConnectionError("catalog unavailable")

    worker = StubWorker()
    loop = CommandWorkerLoop(  # type: ignore[arg-type]
        worker=worker,
        organization_source=BrokenSource(),
        observer=observed.append,
    )

    assert loop.run_cycle() == ()
    assert worker.organizations == []
    assert observed == [{
        "event": "worker_discovery_error",
        "error_type": "ConnectionError",
        "message": "catalog unavailable",
    }]


def test_worker_loop_rejects_an_unbounded_or_duplicate_discovery_batch():
    class InvalidSource:
        def list_ready_tenants(self, **kwargs):
            del kwargs
            return ("tenant-a", "tenant-a")

    observed = []
    loop = CommandWorkerLoop(  # type: ignore[arg-type]
        worker=StubWorker(),
        organization_source=InvalidSource(),
        tenant_batch_size=2,
        observer=observed.append,
    )

    assert loop.run_cycle() == ()
    assert observed[0]["event"] == "worker_discovery_error"
    assert observed[0]["error_type"] == "ValueError"


def test_bootstrap_organization_contains_every_agent_lifecycle_role_and_ceo():
    organization = default_organization("tenant-a")
    required = {
        "agent:mission-manager",
        "agent:research-lead",
        "agent:product-architect",
        "agent:engineering-manager",
        "agent:quality-manager",
        "agent:repair-lead",
        "agent:release-manager",
    }
    assert required <= organization.agents.keys()
    assert "human:ceo" in organization.humans


def test_worker_settings_require_explicit_model_and_production_tenants(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AOS_ENVIRONMENT", "development")
    monkeypatch.delenv("AOS_V2_MODEL", raising=False)
    with pytest.raises(ValueError, match="AOS_V2_MODEL is required"):
        WorkerSettings.from_env()

    monkeypatch.setenv("AOS_V2_MODEL", "provider:model")
    monkeypatch.setenv("AOS_V2_WEB_PUSH_PUBLIC_KEY", "public-only")
    monkeypatch.delenv("AOS_V2_WEB_PUSH_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("AOS_V2_WEB_PUSH_SUBJECT", raising=False)
    with pytest.raises(ValueError, match="public/private VAPID keys"):
        WorkerSettings.from_env()
    monkeypatch.delenv("AOS_V2_WEB_PUSH_PUBLIC_KEY", raising=False)

    monkeypatch.setenv("AOS_ENVIRONMENT", "production")
    monkeypatch.setenv("AOS_V2_SYSTEM_DATABASE_URL", "postgresql://db/system")
    monkeypatch.setenv("AOS_V2_APPLICATION_DATABASE_URL", "postgresql://db/application")
    monkeypatch.setenv("AOS_V2_CAPABILITY_SECRET", "c" * 32)
    monkeypatch.setenv("AOS_V2_OIDC_ISSUER", "https://identity.example.test")
    monkeypatch.setenv("AOS_V2_OIDC_AUDIENCE", "agent-os-api")
    monkeypatch.setenv("AOS_V2_OIDC_JWKS_URL", "https://identity.example.test/jwks.json")
    monkeypatch.setenv("AOS_V2_OIDC_AUTHORIZATION_URL", "https://identity.example.test/authorize")
    monkeypatch.setenv("AOS_V2_OIDC_TOKEN_URL", "https://identity.example.test/oauth/token")
    monkeypatch.setenv("AOS_V2_OIDC_CLIENT_ID", "agent-os-browser")
    monkeypatch.setenv("AOS_V2_CREATE_SCHEMA", "0")
    monkeypatch.setenv("AOS_V2_PUBLIC_BASE_URL", "https://agent-os.example.test")
    monkeypatch.setenv("AOS_V2_ARTIFACT_BUCKET", "example-agentos-artifacts")
    monkeypatch.setenv("AOS_V2_OIDC_PERSONAL_TENANTS", "1")
    monkeypatch.delenv("AOS_V2_TENANT_DERIVATION_SECRET", raising=False)
    monkeypatch.setenv("AOS_V2_MODEL", "provider:model")
    monkeypatch.delenv("AOS_V2_WORKER_ORGANIZATIONS", raising=False)
    monkeypatch.delenv("AOS_V2_PUBLISHED_APP_BUCKET", raising=False)
    monkeypatch.delenv("AOS_V2_APPS_BASE_URL", raising=False)
    with pytest.raises(ValueError, match="production requires AOS_V2_PUBLISHED_APP_BUCKET"):
        WorkerSettings.from_env()
    monkeypatch.setenv("AOS_V2_PUBLISHED_APP_BUCKET", "example-published-apps")
    monkeypatch.setenv("AOS_V2_APPS_BASE_URL", "https://apps.example.test")
    settings = WorkerSettings.from_env()
    assert settings.organization_ids == ()
    assert settings.tenant_discovery_limit == 128
    assert settings.management_check_seconds == 30
    assert settings.slow_work_seconds == 300
    assert settings.management_escalation_checks == 3
    assert settings.published_app_bucket == "example-published-apps"
    assert settings.apps_base_url == "https://apps.example.test"

    monkeypatch.setenv("AOS_V2_APP_PROJECT_ID", "generated-apps")
    with pytest.raises(ValueError, match="requires app project, region, source bucket"):
        WorkerSettings.from_env()
    monkeypatch.setenv("AOS_V2_APP_REGION", "us-central1")
    monkeypatch.setenv("AOS_V2_APP_SOURCE_BUCKET", "generated-app-sources")
    monkeypatch.setenv("AOS_V2_APP_REPOSITORY", "customer-apps")
    monkeypatch.setenv(
        "AOS_V2_APP_BUILD_SERVICE_ACCOUNT",
        "builder@generated-apps.iam.gserviceaccount.com",
    )
    monkeypatch.setenv(
        "AOS_V2_APP_RUNTIME_SERVICE_ACCOUNT",
        "runtime@generated-apps.iam.gserviceaccount.com",
    )
    monkeypatch.setenv(
        "AOS_V2_APP_BUILDER_IMAGE", "builder@sha256:" + "a" * 64,
    )
    settings = WorkerSettings.from_env()
    assert settings.app_project_id == "generated-apps"
    assert settings.app_max_instances == 10

    monkeypatch.setenv("AOS_V2_WORKER_ORGANIZATIONS", "tenant-a,tenant-b,tenant-a")
    settings = WorkerSettings.from_env()
    assert settings.organization_ids == ("tenant-a", "tenant-b")
    assert settings.model == "provider:model"
    assert settings.sandbox_backend == "disabled"

    monkeypatch.setenv("AOS_V2_SANDBOX_BACKEND", "host")
    with pytest.raises(ValueError, match="must be disabled, docker, or cloud-run-job"):
        WorkerSettings.from_env()

    monkeypatch.setenv("AOS_V2_SANDBOX_BACKEND", "cloud-run-job")
    with pytest.raises(ValueError, match="requires project, region, job, bucket"):
        WorkerSettings.from_env()

    monkeypatch.setenv("AOS_V2_SANDBOX_PROJECT_ID", "sandbox-proj")
    monkeypatch.setenv("AOS_V2_SANDBOX_REGION", "us-central1")
    monkeypatch.setenv("AOS_V2_SANDBOX_JOB_NAME", "agentos-production-sandbox")
    monkeypatch.setenv("AOS_V2_SANDBOX_BUCKET", "control-artifacts")
    monkeypatch.setenv(
        "AOS_V2_SANDBOX_SIGNING_SERVICE_ACCOUNT",
        "worker@control-proj.iam.gserviceaccount.com",
    )
    monkeypatch.setenv("AOS_V2_SANDBOX_REVISION", "image@sha256:" + "a" * 64)
    assert WorkerSettings.from_env().sandbox_backend == "cloud-run-job"

    monkeypatch.setenv("AOS_V2_SANDBOX_BACKEND", "disabled")
    monkeypatch.setenv("AOS_V2_MANAGEMENT_ESCALATION_CHECKS", "101")
    with pytest.raises(ValueError, match="cannot exceed 100"):
        WorkerSettings.from_env()
