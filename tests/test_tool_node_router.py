from __future__ import annotations

import pytest

from agent_os.application.command_worker import FatalCommandError
from agent_os.domain.workflow import NodeKind, WorkflowNode
from agent_os.infrastructure.tool_node_router import GraphToolNodeRouter


def test_tool_router_dispatches_only_the_registered_name():
    received = []

    def registered(*arguments):
        received.append(arguments)
        return {"disposition": "complete"}

    router = GraphToolNodeRouter({"known.tool": registered})
    node = WorkflowNode(
        "tool", NodeKind.TOOL, "Known tool", configuration={"tool": "known.tool"},
    )

    result = router.execute("tenant-a", "run-a", object(), object(), object(), node)

    assert result == {"disposition": "complete"}
    assert len(received) == 1


def test_tool_router_rejects_unknown_and_empty_registries():
    with pytest.raises(ValueError, match="at least one"):
        GraphToolNodeRouter({})
    router = GraphToolNodeRouter({"known.tool": lambda *arguments: arguments})
    node = WorkflowNode(
        "tool", NodeKind.TOOL, "Unknown tool", configuration={"tool": "unknown.tool"},
    )

    with pytest.raises(FatalCommandError, match="not registered"):
        router.execute("tenant-a", "run-a", object(), object(), object(), node)
