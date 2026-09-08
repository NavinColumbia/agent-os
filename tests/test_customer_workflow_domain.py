from __future__ import annotations

import pytest

from agent_os.domain.workflow import NodeKind, WorkflowDefinition, WorkflowEdge, WorkflowNode


def jira_workflow() -> WorkflowDefinition:
    nodes = (
        WorkflowNode("triage", NodeKind.AGENT, "Understand ticket and uncertainty", "product-engineer"),
        WorkflowNode("clarify", NodeKind.HUMAN, "Ask the appropriate customer team"),
        WorkflowNode("implement", NodeKind.AGENT, "Implement and test", "product-engineer"),
        WorkflowNode("jira", NodeKind.TOOL, "Update vendor Jira"),
        WorkflowNode("done", NodeKind.TERMINAL, "Deliver accepted ticket"),
    )
    edges = (
        WorkflowEdge("triage", "clarify", "uncertainty_above_threshold", 10),
        WorkflowEdge("triage", "implement", "requirements_clear", 5),
        WorkflowEdge("clarify", "triage", "correlated_response_received", 10),
        WorkflowEdge("implement", "triage", "verification_failed", 10),
        WorkflowEdge("implement", "jira", "verification_passed", 5),
        WorkflowEdge("jira", "done", "vendor_update_confirmed"),
    )
    return WorkflowDefinition("jira-agent", "tenant-1", "Jira delivery", 1, "triage", nodes, edges, "architect-agent")


def test_customer_workflows_support_branching_human_waits_and_repair_loops():
    workflow = jira_workflow()
    assert len(workflow.outgoing("triage")) == 2
    assert workflow.outgoing("triage")[0].target == "clarify"
    assert any(edge.source == "clarify" and edge.target == "triage" for edge in workflow.edges)
    assert any(edge.source == "implement" and edge.target == "triage" for edge in workflow.edges)


def test_workflow_requires_reachable_explicit_terminal_and_valid_edges():
    with pytest.raises(ValueError, match="unreachable"):
        WorkflowDefinition(
            "bad", "tenant", "Bad", 1, "start",
            (WorkflowNode("start", NodeKind.AGENT, "Start", "worker"), WorkflowNode("done", NodeKind.TERMINAL, "Done")),
            (), "author",
        )
