from __future__ import annotations

from datetime import datetime, timezone

from agent_os.application.default_organization import default_organization
from agent_os.application.ports import OrganizationEventReceipt
from agent_os.domain.workflow import NodeKind, WorkflowDefinition, WorkflowEdge, WorkflowNode
from agent_os.domain.workflow_runtime import (
    WorkflowAction,
    WorkflowActionKind,
    begin_node,
    complete_node,
    start_workflow,
)
from agent_os.infrastructure.graph_organization_effects import GraphOrganizationEffectHandler


class MemoryLedger:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def load_organization_events(
        self, tenant_id, run_id, *, after_version=0, limit=500,
    ):
        return tuple(
            item for item in self.events
            if item["tenant_id"] == tenant_id
            and item["run_id"] == run_id
            and item["stream_version"] > after_version
        )[:limit]

    def append_organization_events(self, events):
        prior = {item["event_id"]: item for item in self.events}
        if all(event.event_id in prior for event in events):
            return tuple(OrganizationEventReceipt(
                prior[event.event_id]["stream_version"], duplicate=True,
            ) for event in events)
        current = len(self.events)
        assert events[0].expected_version == current
        receipts = []
        for event in events:
            current += 1
            self.events.append({**event.to_dict(), "stream_version": current})
            receipts.append(OrganizationEventReceipt(current))
        return tuple(receipts)

    def append_organization_event(self, event):
        return self.append_organization_events((event,))[0]


class MemoryCompany:
    def get_organization(self, tenant_id):
        return default_organization(tenant_id)


def test_completed_agent_node_emits_a_separate_durable_organization_effect():
    definition = WorkflowDefinition(
        "workflow", "tenant-a", "Work", 1, "work",
        (
            WorkflowNode("work", NodeKind.AGENT, "Do governed work", "engineer"),
            WorkflowNode("done", NodeKind.TERMINAL, "Accept evidence"),
        ),
        (WorkflowEdge("work", "done", "ready"),),
        "system:test",
    )
    started = start_workflow(definition, run_id="graph-run", context={
        "lifecycle_run_id": "lifecycle-run", "mission_execution": True,
    })
    running = begin_node(started.state, started.state.tokens[0].token_id, expected_version=0)
    completed = complete_node(
        definition, running.state, running.state.tokens[0].token_id,
        expected_version=1, satisfied_conditions=frozenset({"ready"}),
        evidence_ids=("artifact-work",), output={
            "summary": "Implemented the governed work.",
            "organization_actions": {"risks": ["A dependency may change."]},
        },
    )

    effect = next(
        action for action in completed.actions
        if action.kind is WorkflowActionKind.RECORD_ORGANIZATION
    )
    assert effect.payload["organization_run_id"] == "lifecycle-run"
    assert effect.payload["actor_role"] == "engineer"
    assert any(action.kind is WorkflowActionKind.EXECUTE_NODE for action in completed.actions)


def test_organization_effect_records_messages_staffing_risks_and_replays_without_duplicates():
    ledger = MemoryLedger()
    handler = GraphOrganizationEffectHandler(
        ledger, MemoryCompany(),
        clock=lambda: datetime(2026, 9, 13, tzinfo=timezone.utc),
    )
    action = WorkflowAction(
        "organization-action", WorkflowActionKind.RECORD_ORGANIZATION,
        "token", "work", {
            "organization_run_id": "lifecycle-run",
            "actor_role": "security-program-lead",
            "summary": "Diagnosed a missing security specialist.",
            "evidence_ids": ["artifact-diagnosis"],
            "organization_actions": {
                "observations": ["Security capacity is missing."],
                "risks": ["Release assurance is not independent."],
                "messages": [{
                    "audience": "human", "kind": "request",
                    "recipient_ids": ["human:ceo"], "subject": "Staffing decision",
                    "body": "Approve a security specialist?", "requires_response": True,
                    "correlation_id": "security-staffing",
                }],
                "proposed_work": [{"objective": "Perform independent threat review."}],
                "hiring_requests": [{
                    "role": "security-specialist", "reason": "Independent review is required",
                    "participant_kind": "agent", "capabilities": ["security-review"],
                    "requested_count": 1, "estimated_budget_cents": 0,
                }],
                "decisions": [],
                "next_actions": ["Continue non-security implementation while approval is pending."],
            },
        },
    )

    first = handler.execute({"tenant_id": "tenant-a", "run_id": "graph-run"}, action)
    replay = handler.execute({"tenant_id": "tenant-a", "run_id": "graph-run"}, action)

    kinds = [item["kind"] for item in ledger.events]
    assert "message_sent" in kinds
    assert "prerequisite_identified" in kinds
    assert "organization_change_proposed" in kinds
    assert "approval_requested" in kinds
    assert "risk_raised" in kinds
    assert first["duplicate"] is False
    assert replay["duplicate"] is True
    assert len(ledger.events) == len(first["event_versions"])
    assert next(item for item in ledger.events if item["kind"] == "message_sent")[
        "actor_id"
    ] == "agent:security-program-lead"


def test_admitted_program_is_copied_to_the_one_organization_history():
    ledger = MemoryLedger()
    handler = GraphOrganizationEffectHandler(
        ledger, clock=lambda: datetime(2026, 9, 13, tzinfo=timezone.utc),
    )
    action = WorkflowAction(
        "record-program-action", WorkflowActionKind.RECORD_PROGRAM,
        "token", "launch", {
            "organization_run_id": "lifecycle-run", "actor_role": "mission-architect",
            "program_artifact_id": "artifact-program-v2",
            "program": {
                "format": "agent-os.mission-program.v1", "revision": 2,
                "objective": "Build the revised product",
            },
        },
    )

    first = handler.record_program({"tenant_id": "tenant-a", "run_id": "graph-run"}, action)
    replay = handler.record_program({"tenant_id": "tenant-a", "run_id": "graph-run"}, action)

    assert first["program_revision"] == 2
    assert replay["duplicate"] is True
    assert len(ledger.events) == 1
    assert ledger.events[0]["kind"] == "program_admitted"
    assert ledger.events[0]["payload"]["program_artifact_id"] == "artifact-program-v2"
