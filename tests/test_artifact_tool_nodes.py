from __future__ import annotations

from pathlib import Path

import pytest

from agent_os.application.command_worker import FatalCommandError
from agent_os.domain.workflow import NodeKind, WorkflowDefinition, WorkflowEdge, WorkflowNode
from agent_os.domain.workflow_runtime import begin_node, complete_node, start_workflow
from agent_os.infrastructure.artifact_tool_nodes import ArtifactToolNodeHandlers
from agent_os.infrastructure.sql_artifacts import SQLArtifactStore


def artifact_graph(tool: str = "artifact.publish_text") -> WorkflowDefinition:
    return WorkflowDefinition(
        "artifact-graph", "tenant-a", "Artifact graph", 1, "draft",
        (
            WorkflowNode("draft", NodeKind.AGENT, "Draft the page", "builder"),
            WorkflowNode("publish", NodeKind.TOOL, "Publish source evidence", configuration={
                "tool": tool,
                "source": {"node_id": "draft", "output_path": ["draft", "html"]},
                "media_type": "text/html; charset=utf-8",
                "satisfied_conditions": ["published"],
            }),
            WorkflowNode("done", NodeKind.TERMINAL, "Accept the artifact"),
        ),
        (
            WorkflowEdge("draft", "publish", "ready"),
            WorkflowEdge("publish", "done", "published"),
        ),
        "architect",
    )


def running_tool(definition: WorkflowDefinition):
    started = start_workflow(definition, run_id="run-artifact")
    draft_action = started.actions[0]
    draft_running = begin_node(started.state, draft_action.token_id, expected_version=0).state
    advanced = complete_node(
        definition,
        draft_running,
        draft_action.token_id,
        expected_version=1,
        satisfied_conditions=frozenset({"ready"}),
        evidence_ids=("draft-evidence",),
        output={"draft": {"html": "<h1>Launch</h1>"}},
    )
    tool_action = advanced.actions[0]
    tool_running = begin_node(
        advanced.state, tool_action.token_id, expected_version=advanced.state.version,
    ).state
    tool_node = next(node for node in definition.nodes if node.node_id == "publish")
    return tool_running, tool_action, tool_node


def test_tool_node_publishes_prior_durable_output_as_real_idempotent_evidence(tmp_path: Path):
    store = SQLArtifactStore(f"sqlite:///{tmp_path / 'tool.sqlite3'}", create_schema=True)
    definition = artifact_graph()
    state, action, node = running_tool(definition)
    tools = ArtifactToolNodeHandlers(store)

    first = tools.execute("tenant-a", "run-artifact", definition, state, action, node)
    replay = tools.execute("tenant-a", "run-artifact", definition, state, action, node)

    assert first == replay
    assert first["satisfied_conditions"] == ["published"]
    assert first["evidence_ids"] == [first["output"]["artifact_id"]]
    assert store.get("tenant-a", first["output"]["artifact_id"]) == b"<h1>Launch</h1>"
    store.close()


def test_tool_node_rejects_unregistered_tools_without_executing_them(tmp_path: Path):
    store = SQLArtifactStore(f"sqlite:///{tmp_path / 'tool.sqlite3'}", create_schema=True)
    definition = artifact_graph("shell.exec")
    state, action, node = running_tool(definition)

    with pytest.raises(FatalCommandError, match="not registered"):
        ArtifactToolNodeHandlers(store).execute(
            "tenant-a", "run-artifact", definition, state, action, node,
        )
    store.close()
