"""Bounded DBOS adapter proof using isolated SQLite databases."""

from __future__ import annotations

from pathlib import Path

import pytest

from dbos import DBOS
from agent_os.application.command_worker import CommandRunStatus, DurableCommandWorker
from agent_os.domain.lifecycle import CommandKind, Event, EventKind, LifecyclePhase, LifecycleState
from agent_os.domain.organization_events import OrganizationEvent, OrganizationEventKind
from agent_os.infrastructure.command_router import LifecycleCommandRouter
from agent_os.infrastructure.dbos_lifecycle import DBOSLifecycleEngine, sqlalchemy_url
from agent_os.infrastructure.retry_effects import RetryScheduleHandler


@pytest.fixture
def engine(tmp_path: Path):
    runtime = DBOSLifecycleEngine(
        system_database_url=f"sqlite:///{tmp_path / 'system.sqlite3'}",
        application_database_url=f"sqlite:///{tmp_path / 'application.sqlite3'}",
        application_version="contract-test-v1",
        create_schema=True,
    )
    try:
        yield runtime
    finally:
        runtime.close()
        DBOS.destroy(destroy_registry=True)


def test_dbos_adapter_persists_state_event_and_replay_stable_command(engine):
    initial = LifecycleState(run_id="run-1", organization_id="org-1")
    started = engine.start_run(initial)
    assert engine.get_result(started.workflow_id)["created"] is True

    event = Event("scope-1", EventKind.SCOPE_ACCEPTED, expected_version=0, payload={"brief_id": "brief-1"})
    receipt = engine.submit_event("org-1", "run-1", event)
    result = engine.get_result(receipt.workflow_id)

    state = engine.get_run("org-1", "run-1")
    assert state is not None and state.phase is LifecyclePhase.RESEARCH and state.version == 1
    assert result["state"] == state.to_dict()
    command = engine.list_commands("org-1", "run-1")[0]
    assert command == result["commands"][0]
    assert command["command"]["kind"] == "start_mission"

    duplicate = engine.submit_event("org-1", "run-1", event)
    assert duplicate.workflow_id == receipt.workflow_id
    assert duplicate.duplicate is True
    assert engine.get_result(duplicate.workflow_id) == result
    assert len(engine.list_commands("org-1", "run-1")) == 1


def test_start_run_can_atomically_accept_the_initial_directive(engine):
    initial = LifecycleState(run_id="run-directive", organization_id="org-1")
    first = Event(
        "directive-accepted",
        EventKind.SCOPE_ACCEPTED,
        expected_version=0,
        payload={"prompt": "Build a human-governed Jira engineering team"},
    )

    receipt = engine.start_run(initial, first)
    result = engine.get_result(receipt.workflow_id)

    assert result["created"] is True
    assert result["state"]["phase"] == "research"
    assert engine.get_run("org-1", "run-directive").phase is LifecyclePhase.RESEARCH
    assert engine.list_commands("org-1", "run-directive")[0]["command"]["kind"] == "start_mission"
    activity = engine.load_organization_events("org-1", "run-directive")
    assert len(activity) == 1
    assert activity[0]["kind"] == "mission_chartered"
    assert activity[0]["payload"]["outcome"] == "Build a human-governed Jira engineering team"


def test_run_inventory_is_ordered_bounded_and_tenant_scoped(engine):
    for tenant_id, run_id, title in (
        ("org-1", "run-first", "First"),
        ("org-2", "run-hidden", "Hidden"),
        ("org-1", "run-latest", "Latest"),
    ):
        receipt = engine.start_run(
            LifecycleState(run_id=run_id, organization_id=tenant_id),
            Event(
                f"scope-{run_id}", EventKind.SCOPE_ACCEPTED, 0,
                {"title": title, "prompt": f"Build {title.lower()}"},
            ),
        )
        engine.get_result(receipt.workflow_id)

    visible = engine.list_runs("org-1", limit=1)
    assert len(visible) == 1
    assert visible[0].run_id == "run-latest"
    assert visible[0].title == "Latest"
    assert all(item.organization_id == "org-1" for item in engine.list_runs("org-1"))


def test_dbos_adapter_fences_tenants_versions_and_conflicting_event_ids(engine):
    initial = LifecycleState(run_id="same-run", organization_id="org-a")
    receipt = engine.start_run(initial)
    engine.get_result(receipt.workflow_id)

    assert engine.get_run("org-b", "same-run") is None

    valid = Event("event-1", EventKind.SCOPE_ACCEPTED, 0, {"brief_id": "one"})
    accepted = engine.submit_event("org-a", "same-run", valid)
    engine.get_result(accepted.workflow_id)

    stale = engine.submit_event(
        "org-a", "same-run", Event("event-2", EventKind.RESEARCH_COMPLETED, 0)
    )
    with pytest.raises(Exception, match="stale event version"):
        engine.get_result(stale.workflow_id)

    conflict = engine.submit_event(
        "org-a", "same-run", Event("event-1", EventKind.SCOPE_ACCEPTED, 0, {"brief_id": "two"})
    )
    with pytest.raises(Exception, match="event_id was reused"):
        engine.get_result(conflict.workflow_id)


def test_postgres_urls_select_psycopg3_driver():
    assert sqlalchemy_url("postgresql://u:p@db/name") == "postgresql+psycopg://u:p@db/name"
    assert sqlalchemy_url("postgres://u:p@db/name") == "postgresql+psycopg://u:p@db/name"
    assert sqlalchemy_url("sqlite:///local.db") == "sqlite:///local.db"


def test_command_outbox_is_tenant_fenced_retryable_and_worker_lease_owned(engine):
    initial = LifecycleState(run_id="run-outbox", organization_id="org-a")
    receipt = engine.start_run(
        initial,
        Event("scope-outbox", EventKind.SCOPE_ACCEPTED, 0, {"prompt": "Begin"}),
    )
    result = engine.get_result(receipt.workflow_id)
    command_id = result["commands"][0]["command_id"]

    first = engine.claim_command("org-a", worker_id="worker-a", lease_seconds=30)
    assert first is not None
    assert first.envelope["command_id"] == command_id
    assert first.attempt == 1
    assert engine.claim_command("org-a", worker_id="worker-b") is None
    assert engine.claim_command("org-b", worker_id="worker-b") is None
    assert engine.complete_command(
        "org-a", command_id, worker_id="worker-b", result={"forged": True}
    ) is False
    assert engine.heartbeat_command(
        "org-a", command_id, worker_id="worker-a", lease_seconds=45
    ) is True

    assert engine.retry_command(
        "org-a",
        command_id,
        worker_id="worker-a",
        error={"kind": "provider_throttled", "retryable": True},
        delay_seconds=0,
    ) is True
    retry = engine.claim_command("org-a", worker_id="worker-b")
    assert retry is not None and retry.attempt == 2
    assert engine.complete_command(
        "org-a", command_id, worker_id="worker-b", result={"evidence_ids": ["artifact-1"]}
    ) is True

    assert engine.claim_command("org-a", worker_id="worker-c") is None
    record = engine.get_command_record("org-a", command_id)
    assert record is not None
    assert record["status"] == "succeeded"
    assert record["attempts"] == 2
    assert record["result"] == {"evidence_ids": ["artifact-1"]}
    assert record["last_error"] is None


def test_retry_timer_command_is_not_claimable_before_its_durable_due_time(engine):
    started = engine.start_run(LifecycleState(run_id="run-timer", organization_id="org-a"))
    engine.get_result(started.workflow_id)
    failed = engine.submit_event(
        "org-a", "run-timer",
        Event(
            "provider-throttled", EventKind.OPERATION_FAILED, 0,
            {
                "operation": "research",
                "reason": "provider rate limited",
                "retryable": True,
                "retry_at": "2099-01-01T00:00:00Z",
                "resume_command": CommandKind.START_RESEARCH.value,
                "correlation_id": "retry-future",
            },
        ),
    )
    result = engine.get_result(failed.workflow_id)
    command_id = result["commands"][0]["command_id"]

    assert engine.claim_command("org-a", worker_id="worker-a") is None
    record = engine.get_command_record("org-a", command_id)
    assert record is not None
    assert record["status"] == "pending"
    assert record["available_at"].year == 2099


def test_due_retry_timer_resumes_the_lifecycle_and_emits_its_original_command(engine):
    started = engine.start_run(LifecycleState(run_id="run-due-timer", organization_id="org-a"))
    engine.get_result(started.workflow_id)
    failed = engine.submit_event(
        "org-a", "run-due-timer",
        Event(
            "provider-throttled-due", EventKind.OPERATION_FAILED, 0,
            {
                "operation": "research",
                "reason": "provider rate limited",
                "retryable": True,
                "retry_at": "2020-01-01T00:00:00Z",
                "resume_command": CommandKind.START_RESEARCH.value,
                "correlation_id": "retry-due",
            },
        ),
    )
    result = engine.get_result(failed.workflow_id)
    timer_id = result["commands"][0]["command_id"]

    class NoAgentCommands:
        @staticmethod
        def supports(kind):
            return False

        def execute(self, envelope):
            raise AssertionError(f"unexpected agent command: {envelope}")

    worker = DurableCommandWorker(
        outbox=engine,
        executor=LifecycleCommandRouter(
            agent_executor=NoAgentCommands(),
            handlers={CommandKind.SCHEDULE_RETRY: RetryScheduleHandler(engine).execute},
        ),
        worker_id="timer-worker",
        lease_seconds=3,
        workflow_engine=engine,
        workflow_result_waiter=engine.get_result,
    )

    report = worker.run_one("org-a")

    assert report.status is CommandRunStatus.SUCCEEDED
    assert engine.get_command_record("org-a", timer_id)["status"] == "succeeded"
    state = engine.get_run("org-a", "run-due-timer")
    assert state is not None and state.version == 2 and state.wait is None
    assert engine.list_commands("org-a", "run-due-timer")[-1]["command"]["kind"] == "start_research"


def test_real_worker_routes_initial_directive_to_the_mission_bootstrap_handler(engine):
    started = engine.start_run(
        LifecycleState(run_id="run-worker", organization_id="org-a"),
        Event("scope-worker", EventKind.SCOPE_ACCEPTED, 0, {"prompt": "Build the product"}),
    )
    start_result = engine.get_result(started.workflow_id)
    first_command_id = start_result["commands"][0]["command_id"]

    calls = []

    class NoAgentCommands:
        @staticmethod
        def supports(kind):
            return False

        def execute(self, envelope):
            raise AssertionError(f"unexpected agent command: {envelope}")

    def bootstrap(item):
        calls.append(item)
        return {"planning_run_id": "plan-run-worker"}

    worker = DurableCommandWorker(
        outbox=engine,
        executor=LifecycleCommandRouter(
            agent_executor=NoAgentCommands(),
            handlers={CommandKind.START_MISSION: bootstrap},
        ),
        worker_id="worker-integration",
        lease_seconds=3,
        workflow_engine=engine,
        workflow_result_waiter=engine.get_result,
    )

    report = worker.run_one("org-a")

    assert report.status is CommandRunStatus.SUCCEEDED
    assert engine.get_command_record("org-a", first_command_id)["status"] == "succeeded"
    state = engine.get_run("org-a", "run-worker")
    assert state is not None and state.phase is LifecyclePhase.RESEARCH and state.version == 1
    assert calls[0].command.kind is CommandKind.START_MISSION
    assert calls[0].command.payload["prompt"] == "Build the product"


def test_internal_organization_history_is_durable_versioned_and_tenant_isolated(engine):
    started = engine.start_run(LifecycleState(run_id="company-run", organization_id="org-a"))
    engine.get_result(started.workflow_id)
    first = OrganizationEvent(
        event_id="mission-1",
        tenant_id="org-a",
        run_id="company-run",
        actor_id="agent:chief-of-staff",
        kind=OrganizationEventKind.MISSION_CHARTERED,
        expected_version=0,
        occurred_at="2026-09-08T08:00:00Z",
        payload={"outcome": "Build the requested company", "budget_limit_cents": 100_000},
        correlation_id="ceo-directive-1",
    )
    accepted = engine.append_organization_event(first)
    duplicate = engine.append_organization_event(first)
    second = engine.append_organization_event(OrganizationEvent(
        event_id="message-1",
        tenant_id="org-a",
        run_id="company-run",
        actor_id="agent:chief-of-staff",
        kind=OrganizationEventKind.MESSAGE_SENT,
        expected_version=1,
        occurred_at="2026-09-08T08:00:01Z",
        payload={
            "audience": "human",
            "recipient_ids": ["human:ceo"],
            "subject": "Team inventory",
            "body": "Who is already on your team?",
            "requires_response": True,
        },
        causation_id="mission-1",
        correlation_id="question-team-1",
    ))

    assert accepted.stream_version == 1 and accepted.duplicate is False
    assert duplicate.stream_version == 1 and duplicate.duplicate is True
    assert second.stream_version == 2
    assert [item["kind"] for item in engine.load_organization_events("org-a", "company-run")] == [
        "mission_chartered", "message_sent",
    ]
    assert engine.load_organization_events("org-b", "company-run") == ()

    with pytest.raises(ValueError, match="different content"):
        engine.append_organization_event(OrganizationEvent.from_dict(
            {**first.to_dict(), "payload": {"outcome": "forged"}}
        ))
    with pytest.raises(ValueError, match="stale organization event version"):
        engine.append_organization_event(OrganizationEvent(
            event_id="stale",
            tenant_id="org-a",
            run_id="company-run",
            actor_id="agent:manager",
            kind=OrganizationEventKind.WORK_PROPOSED,
            expected_version=0,
            occurred_at="2026-09-08T08:00:02Z",
            payload={"objective": "stale write"},
        ))
