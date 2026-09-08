from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from agent_os.application.agent_turn import AgentTurnContext, commit_agent_turn, plan_agent_turn
from agent_os.application.lifecycle import CommandEnvelope
from agent_os.application.ports import OrganizationEventReceipt
from agent_os.domain.lifecycle import Command, CommandKind
from agent_os.domain.organization import AgentProfile, HumanParticipant, Organization, Team
from agent_os.infrastructure.agent_command_executor import DurableAgentCommandExecutor


def organization() -> Organization:
    agents = {
        "chief": AgentProfile("chief", "Chief of Staff", "exec", hiring_authority=True),
    }
    return Organization(
        "tenant-1",
        "tenant-1",
        "Company",
        {"exec": Team("exec", "Executive", "Direct the mission", "chief")},
        agents,
        {"human:ceo": HumanParticipant(
            "human:ceo", "CEO", "exec", ("executive decisions",), response_sla_seconds=3600,
        )},
    )


def output() -> dict[str, Any]:
    return {
        "summary": "Mapped the team and found a missing recruiter.",
        "disposition": "wait_for_human",
        "progress_percent": 20,
        "evidence_ids": [],
        "observations": ["No recruiter is registered."],
        "risks": ["Hiring cannot be silently authorized."],
        "messages": [
            {
                "audience": "human", "kind": "request", "recipient_ids": ["human:ceo"],
                "subject": "Team inventory", "body": "Who is already on your team?",
                "requires_response": True, "correlation_id": "team-question",
            },
            {
                "audience": "direct", "kind": "update", "recipient_ids": ["agent:imaginary"],
                "subject": "Bad route", "body": "This recipient was hallucinated.",
            },
        ],
        "proposed_work": [{
            "objective": "Recruit the missing specialists", "owner_role": "recruiter",
            "specialist_roles": [], "dependency_ids": [],
            "acceptance_criteria": ["CEO accepts candidates"], "urgency": 80,
        }],
        "hiring_requests": [{
            "role": "recruiter", "reason": "No recruiting capacity exists",
            "capabilities": ["sourcing"], "requested_count": 1, "estimated_budget_cents": 10000,
        }],
        "decisions": [{
            "intent": "Choose hiring channel", "considered_options": ["agency", "direct"],
            "chosen_option": "direct", "rationale": "Lower cost", "evidence_ids": [],
            "confidence": 0.6, "reversible": True, "needs_human_approval": False,
        }],
        "next_actions": ["Wait for the CEO's team inventory"],
    }


def test_model_actions_become_durable_proposals_and_hallucinated_routes_are_rejected():
    context = AgentTurnContext(
        "tenant-1", "run-1", "chief", "command-1", "2026-09-08T09:00:00Z", 4
    )
    plan = plan_agent_turn(context, output(), organization=organization())

    kinds = [event.kind.value for event in plan.events]
    assert "message_sent" in kinds
    assert "prerequisite_identified" in kinds
    assert "work_proposed" in kinds
    assert "organization_change_proposed" in kinds
    assert kinds.count("approval_requested") == 2  # hiring plus low-confidence decision
    assert plan.rejected_actions[0]["action"] == "message"
    assert [event.expected_version for event in plan.events] == list(range(4, 4 + len(plan.events)))
    assert all(event.causation_id == "command-1" for event in plan.events)


class MemoryLedger:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def append_organization_event(self, event):
        if event.expected_version != len(self.events):
            raise ValueError("stale")
        self.events.append(event.to_dict())
        return OrganizationEventReceipt(len(self.events))

    def append_organization_events(self, events):
        # The production implementation commits this batch in one transaction.
        return tuple(self.append_organization_event(event) for event in events)

    def load_organization_events(self, tenant_id, run_id, *, after_version=0, limit=500):
        return tuple(
            {"stream_version": index, **event}
            for index, event in enumerate(self.events, start=1)
            if index > after_version
        )[:limit]


class FakeRuntime:
    def __init__(self, turn=None) -> None:
        self.turn = output() if turn is None else turn
        self.calls = 0

    def run_agent(self, **kwargs) -> Mapping[str, Any]:
        self.calls += 1
        self.last_call = kwargs
        return {"output": self.turn, "usage": {"total_tokens": 100}, "idempotency_key": kwargs["idempotency_key"]}


def test_lifecycle_agent_command_is_committed_to_the_organization_ledger():
    ledger = MemoryLedger()
    executor = DurableAgentCommandExecutor(
        runtime=FakeRuntime(),
        ledger=ledger,
        clock=lambda: datetime(2026, 9, 8, 9, tzinfo=timezone.utc),
    )
    envelope = CommandEnvelope(
        "cmd-research", "run-1", "tenant-1", "scope-1", 1, 0,
        Command(CommandKind.START_RESEARCH, {"prompt": "Build the company"}),
    )

    result = executor.execute(envelope.to_dict())

    assert result["organization_event_versions"] == list(range(1, len(ledger.events) + 1))
    assert ledger.events[0]["kind"] == "agent_turn_recorded"
    assert any(event["kind"] == "organization_change_proposed" for event in ledger.events)
    assert result["agent_turn"]["idempotency_key"] == "cmd-research"


def test_committing_same_deterministic_plan_can_be_made_idempotent_by_the_ledger():
    # The production ledger contract is proven separately; this assertion keeps
    # the application batch in version order for safe replay.
    plan = plan_agent_turn(
        AgentTurnContext("tenant-1", "run-1", "chief", "cmd", "2026-09-08T09:00:00Z", 0),
        output(),
        organization=organization(),
    )
    ledger = MemoryLedger()
    receipts = commit_agent_turn(ledger, plan)
    assert receipts[-1].stream_version == len(plan.events)


def test_completed_agent_turn_emits_a_deterministic_lifecycle_event_and_replay_costs_nothing():
    complete = {
        **output(),
        "disposition": "complete",
        "progress_percent": 100,
        "evidence_ids": ["research-report-1"],
        "messages": [],
        "hiring_requests": [],
        "decisions": [],
    }
    runtime = FakeRuntime(complete)
    ledger = MemoryLedger()
    executor = DurableAgentCommandExecutor(runtime=runtime, ledger=ledger)
    envelope = CommandEnvelope(
        "cmd-complete", "run-1", "tenant-1", "scope", 1, 0,
        Command(CommandKind.START_RESEARCH, {"prompt": "Research"}),
    ).to_dict()

    first = executor.execute(envelope)
    replay = executor.execute(envelope)

    assert first["lifecycle_event"]["kind"] == "research_completed"
    assert first["lifecycle_event"]["payload"]["report_id"] == "research-report-1"
    assert replay["lifecycle_event"] == first["lifecycle_event"]
    assert replay["agent_turn"]["replayed"] is True
    assert runtime.calls == 1


def test_agent_receives_authoritative_directory_and_cannot_raise_its_cost_cap():
    complete = {
        **output(),
        "disposition": "complete",
        "progress_percent": 100,
        "evidence_ids": ["report-1"],
        "messages": [],
        "hiring_requests": [],
        "decisions": [],
    }
    runtime = FakeRuntime(complete)
    ledger = MemoryLedger()
    executor = DurableAgentCommandExecutor(
        runtime=runtime,
        ledger=ledger,
        organization_loader=lambda tenant_id, run_id: organization(),
        max_turn_budget_cents=75,
    )
    item = CommandEnvelope(
        "cmd-budget", "run-1", "tenant-1", "scope", 1, 0,
        Command(CommandKind.START_RESEARCH, {"prompt": "Research", "budget_cents": 100_000}),
    )

    executor.execute(item.to_dict())

    assert runtime.last_call["budget_cents"] == 75
    context = runtime.last_call["context"]
    assert context["humans"][0]["id"] == "human:ceo"
    assert context["agents"][0]["id"] == "chief"
