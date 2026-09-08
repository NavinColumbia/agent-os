from __future__ import annotations

from agent_os.domain.workflow import NodeKind, WorkflowDefinition, WorkflowEdge, WorkflowNode
from agent_os.domain.workflow_runtime import begin_node, start_workflow
from agent_os.infrastructure.sandbox_tool_nodes import SandboxToolNodeHandlers


class StubRunner:
    def __init__(self, exit_code: int):
        self.exit_code = exit_code

    def run(self, **kwargs):
        return {
            "exit_code": self.exit_code,
            "timed_out": False,
            "output_artifact_id": "artifact-output",
            "result_artifact_id": "artifact-result",
            "command": list(kwargs["command"]),
        }


def running_tool():
    definition = WorkflowDefinition(
        "sandbox", "tenant-a", "Sandbox", 1, "test",
        (
            WorkflowNode("test", NodeKind.TOOL, "Run tests", configuration={
                "tool": "sandbox.run",
                "source_artifact_id": "artifact-source",
                "command": ["python", "-m", "unittest"],
                "success_condition": "passed",
                "failure_condition": "repair",
            }),
            WorkflowNode("done", NodeKind.TERMINAL, "Done"),
            WorkflowNode("fix", NodeKind.AGENT, "Repair", "engineer"),
        ),
        (
            WorkflowEdge("test", "done", "passed"),
            WorkflowEdge("test", "fix", "repair"),
            WorkflowEdge("fix", "done", "always"),
        ),
        "architect",
    )
    started = start_workflow(definition, run_id="run-sandbox")
    action = started.actions[0]
    running = begin_node(started.state, action.token_id, expected_version=0).state
    node = next(item for item in definition.nodes if item.node_id == "test")
    return definition, running, action, node


def test_sandbox_tool_routes_success_with_durable_evidence():
    definition, state, action, node = running_tool()

    result = SandboxToolNodeHandlers(StubRunner(0)).execute(
        "tenant-a", "run-sandbox", definition, state, action, node,
    )

    assert result["disposition"] == "complete"
    assert result["satisfied_conditions"] == ["passed"]
    assert result["evidence_ids"] == ["artifact-output", "artifact-result"]


def test_sandbox_tool_routes_test_failure_to_the_declared_repair_loop():
    definition, state, action, node = running_tool()

    result = SandboxToolNodeHandlers(StubRunner(7)).execute(
        "tenant-a", "run-sandbox", definition, state, action, node,
    )

    assert result["disposition"] == "complete"
    assert result["satisfied_conditions"] == ["repair"]
    assert result["output"]["exit_code"] == 7
