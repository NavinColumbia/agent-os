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

    monkeypatch.setenv("AOS_ENVIRONMENT", "production")
    monkeypatch.setenv("AOS_V2_SYSTEM_DATABASE_URL", "postgresql://db/system")
    monkeypatch.setenv("AOS_V2_APPLICATION_DATABASE_URL", "postgresql://db/application")
    monkeypatch.setenv("AOS_V2_AUTH_SECRET", "x" * 32)
    monkeypatch.setenv("AOS_V2_CREATE_SCHEMA", "0")
    monkeypatch.setenv("AOS_V2_MODEL", "provider:model")
    monkeypatch.delenv("AOS_V2_WORKER_ORGANIZATIONS", raising=False)
    with pytest.raises(ValueError, match="WORKER_ORGANIZATIONS"):
        WorkerSettings.from_env()

    monkeypatch.setenv("AOS_V2_WORKER_ORGANIZATIONS", "tenant-a,tenant-b,tenant-a")
    settings = WorkerSettings.from_env()
    assert settings.organization_ids == ("tenant-a", "tenant-b")
    assert settings.model == "provider:model"
