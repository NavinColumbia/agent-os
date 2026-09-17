"""Fail-closed named dispatch for executable workflow tool nodes."""

from __future__ import annotations

from typing import Mapping

from agent_os.application.command_worker import (
    FatalCommandError,
    is_retryable_execution_error,
)
from agent_os.domain.workflow import NodeKind, WorkflowDefinition, WorkflowNode
from agent_os.domain.workflow_runtime import WorkflowAction, WorkflowRunState
from agent_os.infrastructure.pydantic_graph_nodes import GraphNodeHandler
from agent_os.application.runtime_effects import WorkflowEffectGuard


class GraphToolNodeRouter:
    def __init__(
        self,
        handlers: Mapping[str, GraphNodeHandler],
        *,
        effect_guard: WorkflowEffectGuard | None = None,
    ) -> None:
        self._handlers = dict(handlers)
        self._effect_guard = effect_guard
        if not self._handlers or any(not name.strip() for name in self._handlers):
            raise ValueError("at least one named tool handler is required")

    def handlers(self):
        return {NodeKind.TOOL: self.execute, NodeKind.SUBWORKFLOW: self.execute}

    def execute(
        self,
        tenant_id: str,
        run_id: str,
        definition: WorkflowDefinition,
        state: WorkflowRunState,
        action: WorkflowAction,
        node: WorkflowNode,
    ):
        tool = (
            "workflow.spawn"
            if node.kind is NodeKind.SUBWORKFLOW
            else str(node.configuration.get("tool") or "")
        )
        handler = self._handlers.get(tool)
        if handler is None:
            raise FatalCommandError(f"tool {tool or '<missing>'} is not registered")
        admission = None
        if self._effect_guard is not None:
            admission = self._effect_guard.admit(
                tenant_id=tenant_id,
                run_id=run_id,
                definition=definition,
                state=state,
                action=action,
                node=node,
            )
        try:
            result = handler(tenant_id, run_id, definition, state, action, node)
        except Exception as exc:
            # A transient transport/provider failure has an unknown external
            # outcome. Keep the same reservation admitted so the durable
            # action can retry with its original idempotency key. Settling it
            # as failed here would make a later successful replay disagree
            # with the assurance ledger.
            if admission is not None and not is_retryable_execution_error(exc):
                self._effect_guard.settle(admission, succeeded=False)
            raise
        if admission is not None:
            succeeded = not (
                isinstance(result, Mapping) and result.get("disposition") == "fail"
            )
            self._effect_guard.settle(admission, succeeded=succeeded)
        return result
