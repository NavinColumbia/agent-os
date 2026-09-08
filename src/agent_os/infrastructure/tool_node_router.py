"""Fail-closed named dispatch for executable workflow tool nodes."""

from __future__ import annotations

from typing import Mapping

from agent_os.application.command_worker import FatalCommandError
from agent_os.domain.workflow import NodeKind, WorkflowDefinition, WorkflowNode
from agent_os.domain.workflow_runtime import WorkflowAction, WorkflowRunState
from agent_os.infrastructure.pydantic_graph_nodes import GraphNodeHandler


class GraphToolNodeRouter:
    def __init__(self, handlers: Mapping[str, GraphNodeHandler]) -> None:
        self._handlers = dict(handlers)
        if not self._handlers or any(not name.strip() for name in self._handlers):
            raise ValueError("at least one named tool handler is required")

    def handlers(self):
        return {NodeKind.TOOL: self.execute}

    def execute(
        self,
        tenant_id: str,
        run_id: str,
        definition: WorkflowDefinition,
        state: WorkflowRunState,
        action: WorkflowAction,
        node: WorkflowNode,
    ):
        tool = str(node.configuration.get("tool") or "")
        handler = self._handlers.get(tool)
        if handler is None:
            raise FatalCommandError(f"tool {tool or '<missing>'} is not registered")
        return handler(tenant_id, run_id, definition, state, action, node)
