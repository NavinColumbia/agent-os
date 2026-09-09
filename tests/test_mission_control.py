from __future__ import annotations

from datetime import datetime, timedelta, timezone

from agent_os.application.mission_control import project_mission_control
from agent_os.domain.workflow import NodeKind, WorkflowDefinition, WorkflowEdge, WorkflowNode
from agent_os.domain.workflow_runtime import NodeToken, TokenStatus, WorkflowRunState, WorkflowRunStatus


def definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        "mission", "tenant-a", "Managed mission", 1, "lead",
        (
            WorkflowNode("lead", NodeKind.AGENT, "Plan delivery", "engineering-manager"),
            WorkflowNode("qa", NodeKind.AGENT, "Verify delivery", "quality-specialist"),
            WorkflowNode("approval", NodeKind.HUMAN, "Approve launch"),
            WorkflowNode("done", NodeKind.TERMINAL, "Accept evidence"),
        ),
        (
            WorkflowEdge("lead", "qa"),
            WorkflowEdge("lead", "approval"),
            WorkflowEdge("qa", "done"),
            WorkflowEdge("approval", "done"),
        ),
        "agent:mission-architect",
    )


def test_management_projection_distinguishes_slow_owned_work_from_failure():
    now = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
    planning = WorkflowRunState(
        "planning", "tenant-a", "planner", 1, 1, WorkflowRunStatus.SUCCEEDED,
        (NodeToken("planner-done", "done", TokenStatus.SUCCEEDED, 1, evidence_ids=("plan",)),),
    )
    execution = WorkflowRunState(
        "execution", "tenant-a", "mission", 1, 7, WorkflowRunStatus.ACTIVE,
        (
            NodeToken(
                "lead-work", "lead", TokenStatus.SUCCEEDED, 1,
                evidence_ids=("plan-evidence",),
                output={
                    "summary": "Delegated independent verification.",
                    "organization_actions": {
                        "hiring_requests": [{
                            "role": "security-specialist", "reason": "Threat review is missing",
                            "capabilities": ["security"], "requested_count": 1,
                            "estimated_budget_cents": 0,
                        }],
                        "risks": ["Authentication has not been independently reviewed."],
                        "next_actions": ["Complete security review."],
                    },
                },
            ),
            NodeToken("qa-work", "qa", TokenStatus.RUNNING, 1, attempt=1),
            NodeToken(
                "human-work", "approval", TokenStatus.WAITING, 1,
                wait_correlation_id="approve-launch", wait_reason="Approve production launch",
            ),
        ),
    )
    observation = {
        "updated_at": (now - timedelta(seconds=10)).isoformat(),
        "actions": [{
            "action_id": "qa-action",
            "state_version": 6,
            "action": {"token_id": "qa-work", "node_id": "qa"},
            "status": "executing",
            "attempts": 1,
            "created_at": (now - timedelta(minutes=20)).isoformat(),
            "lease_expires_at": (now + timedelta(seconds=40)).isoformat(),
            "available_at": (now - timedelta(minutes=20)).isoformat(),
            "completed_at": None,
        }],
    }

    projected = project_mission_control(
        lifecycle_run_id="lifecycle",
        planning_state=planning,
        execution_state=execution,
        definition=definition(),
        observation=observation,
        now=now,
        slow_after_seconds=300,
    )

    qa = next(item for item in projected["work_items"] if item["work_id"] == "qa-work")
    assert qa["health"] == "slow_but_owned"
    assert qa["next_infrastructure_checkpoint_at"] == (now + timedelta(seconds=40)).isoformat()
    signal = next(item for item in projected["management_signals"] if item["work_id"] == "qa-work")
    assert signal["severity"] == "warning"
    assert "do not terminate" in signal["recommended_action"]
    assert projected["health"] == "degraded"
    assert projected["progress"]["materialized_completion_ratio"] == 1 / 3
    assert projected["hiring_requests"][0]["proposal"] is True
    assert projected["hiring_requests"][0]["proposal_id"].startswith("proposal-")
    assert projected["hiring_requests"][0]["status"] == "pending"
    assert projected["risks"][0]["text"].startswith("Authentication")
    assert projected["communications"][0]["correlation_id"] == "approve-launch"
    specialist = next(item for item in projected["team"] if item["role"] == "quality-specialist")
    assert specialist["manager_id"] == "agent:mission-manager"
    assert specialist["mission_scoped"] is True

    decided = project_mission_control(
        lifecycle_run_id="lifecycle",
        planning_state=planning,
        execution_state=execution,
        definition=definition(),
        observation=observation,
        company_events=({
            "kind": "hiring_proposal_decided",
            "payload": {
                "proposal_id": projected["hiring_requests"][0]["proposal_id"],
                "approved": True,
            },
        },),
        now=now,
    )
    assert decided["hiring_requests"][0]["status"] == "approved"


def test_expired_lease_is_recovering_work_not_a_business_timeout():
    now = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
    planning = WorkflowRunState(
        "planning", "tenant-a", "mission", 1, 1, WorkflowRunStatus.ACTIVE,
        (NodeToken("qa-work", "qa", TokenStatus.RUNNING, 1, attempt=2),),
    )
    observation = {"actions": [{
        "state_version": 1,
        "action": {"token_id": "qa-work"},
        "status": "executing",
        "attempts": 2,
        "created_at": (now - timedelta(hours=1)).isoformat(),
        "lease_expires_at": (now - timedelta(seconds=1)).isoformat(),
    }]}

    projected = project_mission_control(
        lifecycle_run_id="lifecycle",
        planning_state=planning,
        execution_state=None,
        definition=definition(),
        observation=observation,
        now=now,
    )

    assert projected["work_items"][0]["health"] == "recovering"
    assert "reclamation" in projected["management_signals"][0]["recommended_action"]
    assert projected["status"] == "active"
