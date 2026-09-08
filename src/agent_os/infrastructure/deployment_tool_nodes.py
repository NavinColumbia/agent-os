"""Authority-controlled workflow tool for publishing a static application preview."""

from __future__ import annotations

from typing import Mapping

from agent_os.application.command_worker import FatalCommandError
from agent_os.application.ports import Deployer
from agent_os.domain.workflow import NodeKind, WorkflowDefinition, WorkflowNode
from agent_os.domain.workflow_runtime import WorkflowAction, WorkflowRunState
from agent_os.infrastructure.graph_output_refs import resolve_prior_output


class DeploymentToolNodeHandlers:
    def __init__(self, deployer: Deployer) -> None:
        self._deployer = deployer

    def named_handlers(self):
        return {"deploy.preview": self.execute}

    def execute(
        self,
        tenant_id: str,
        run_id: str,
        definition: WorkflowDefinition,
        state: WorkflowRunState,
        action: WorkflowAction,
        node: WorkflowNode,
    ) -> Mapping[str, object]:
        del run_id
        if node.kind is not NodeKind.TOOL or node.configuration.get("tool") != "deploy.preview":
            raise FatalCommandError("preview deployment handler received the wrong tool node")
        artifact_id = resolve_prior_output(
            state, node.configuration.get("source"), subject="preview deployment",
        )
        if not isinstance(artifact_id, str) or not artifact_id:
            raise FatalCommandError("preview deployment source is not an artifact ID")
        result = dict(self._deployer.deploy(
            organization_id=tenant_id,
            artifact_id=artifact_id,
            idempotency_key=action.action_id,
        ))
        receipt_artifact_id = result.get("receipt_artifact_id")
        public_url = result.get("public_url")
        if (
            not isinstance(receipt_artifact_id, str)
            or not receipt_artifact_id
            or not isinstance(public_url, str)
            or not public_url
        ):
            raise FatalCommandError("preview deployer returned no durable receipt or public URL")
        available = {
            edge.condition for edge in definition.outgoing(node.node_id)
            if edge.condition != "always"
        }
        configured = node.configuration.get("success_condition")
        if configured is None and len(available) == 1:
            configured = next(iter(available))
        if not isinstance(configured, str) or configured not in available:
            raise FatalCommandError("preview deployment success condition is not declared")
        return {
            "disposition": "complete",
            "satisfied_conditions": [configured],
            "evidence_ids": [artifact_id, receipt_artifact_id],
            "output": result,
        }
