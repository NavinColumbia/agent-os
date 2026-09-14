"""Authority-controlled workflow tools for publishing generated applications."""

from __future__ import annotations

from typing import Mapping

from agent_os.application.command_worker import FatalCommandError
from agent_os.application.ports import (
    ApplicationDeployer,
    Deployer,
    PreviewDeploymentStore,
    StaticSiteDeployer,
)
from agent_os.domain.workflow import NodeKind, WorkflowDefinition, WorkflowNode
from agent_os.domain.workflow_runtime import WorkflowAction, WorkflowRunState
from agent_os.infrastructure.graph_output_refs import resolve_prior_output


class DeploymentToolNodeHandlers:
    def __init__(
        self,
        deployer: Deployer,
        static_deployer: StaticSiteDeployer | None = None,
        service_deployer: ApplicationDeployer | None = None,
    ) -> None:
        self._deployer = deployer
        self._static_deployer = static_deployer
        self._service_deployer = service_deployer

    def named_handlers(self):
        handlers = {"deploy.preview": self.execute}
        if isinstance(self._deployer, PreviewDeploymentStore):
            handlers["preview.fetch"] = self.verify_preview_fetch
        if self._static_deployer is not None:
            handlers["deploy.static"] = self.execute_static
        if self._service_deployer is not None:
            handlers["deploy.service"] = self.execute_service
        return handlers

    @staticmethod
    def _success_condition(
        definition: WorkflowDefinition,
        node: WorkflowNode,
        *,
        subject: str,
    ) -> str:
        available = {
            edge.condition for edge in definition.outgoing(node.node_id)
            if edge.condition != "always"
        }
        configured = node.configuration.get("success_condition")
        if configured is None and len(available) == 1:
            configured = next(iter(available))
        if not isinstance(configured, str) or configured not in available:
            raise FatalCommandError(f"{subject} success condition is not declared")
        return configured

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
        configured = self._success_condition(
            definition, node, subject="preview deployment",
        )
        evidence = [artifact_id]
        deployed_artifact_id = result.get("artifact_id")
        if (
            isinstance(deployed_artifact_id, str)
            and deployed_artifact_id
            and deployed_artifact_id not in evidence
        ):
            evidence.append(deployed_artifact_id)
        evidence.append(receipt_artifact_id)
        return {
            "disposition": "complete",
            "satisfied_conditions": [configured],
            "evidence_ids": evidence,
            "output": result,
        }

    def verify_preview_fetch(
        self,
        tenant_id: str,
        run_id: str,
        definition: WorkflowDefinition,
        state: WorkflowRunState,
        action: WorkflowAction,
        node: WorkflowNode,
    ) -> Mapping[str, object]:
        del run_id
        if (
            node.kind is not NodeKind.TOOL
            or node.configuration.get("tool") != "preview.fetch"
            or not isinstance(self._deployer, PreviewDeploymentStore)
        ):
            raise FatalCommandError("preview fetch handler received the wrong tool node")
        public_url = resolve_prior_output(
            state, node.configuration.get("source"), subject="preview fetch",
        )
        if not isinstance(public_url, str) or not public_url:
            raise FatalCommandError("preview fetch source is not a public URL")
        result = dict(self._deployer.verify_fetch(
            organization_id=tenant_id,
            public_url=public_url,
            idempotency_key=action.action_id,
        ))
        evidence_id = result.get("verification_artifact_id")
        artifact_id = result.get("artifact_id")
        if not isinstance(evidence_id, str) or not evidence_id:
            raise FatalCommandError("preview fetch returned no durable verification evidence")
        condition_key = "success_condition" if result.get("verified") is True else "failure_condition"
        available = {
            edge.condition for edge in definition.outgoing(node.node_id)
            if edge.condition != "always"
        }
        condition = node.configuration.get(condition_key)
        if not isinstance(condition, str) or condition not in available:
            raise FatalCommandError(f"preview fetch {condition_key} is not declared")
        evidence = [evidence_id]
        if isinstance(artifact_id, str) and artifact_id:
            evidence.insert(0, artifact_id)
        return {
            "disposition": "complete",
            "satisfied_conditions": [condition],
            "evidence_ids": evidence,
            "output": result,
        }

    def execute_service(
        self,
        tenant_id: str,
        run_id: str,
        definition: WorkflowDefinition,
        state: WorkflowRunState,
        action: WorkflowAction,
        node: WorkflowNode,
    ) -> Mapping[str, object]:
        del run_id
        if node.kind is not NodeKind.TOOL or node.configuration.get("tool") != "deploy.service":
            raise FatalCommandError("service deployment handler received the wrong tool node")
        if self._service_deployer is None:
            raise FatalCommandError("production service deployment is not configured")
        approval_reference = node.configuration.get("approval")
        approval_node_id = (
            approval_reference.get("node_id")
            if isinstance(approval_reference, Mapping)
            else None
        )
        approval_node = next(
            (candidate for candidate in definition.nodes if candidate.node_id == approval_node_id),
            None,
        )
        if approval_node is None or approval_node.kind is not NodeKind.HUMAN:
            raise FatalCommandError("service deployment approval must come from a human node")
        approved = resolve_prior_output(
            state, approval_reference, subject="service deployment approval",
        )
        if approved is not True:
            raise FatalCommandError("service deployment requires an explicit human approval")
        artifact_id = resolve_prior_output(
            state, node.configuration.get("source"), subject="service deployment",
        )
        if not isinstance(artifact_id, str) or not artifact_id:
            raise FatalCommandError("service deployment source is not an artifact ID")
        app_slug = node.configuration.get("app_slug")
        if not isinstance(app_slug, str) or not app_slug:
            raise FatalCommandError("service deployment requires an app_slug")
        health_path = node.configuration.get("health_path", "/health")
        if not isinstance(health_path, str) or not health_path:
            raise FatalCommandError("service deployment requires a health_path")
        result = dict(self._service_deployer.deploy_service(
            organization_id=tenant_id,
            artifact_id=artifact_id,
            app_slug=app_slug,
            health_path=health_path,
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
            raise FatalCommandError(
                "service deployer returned no durable receipt or public URL"
            )
        configured = self._success_condition(
            definition, node, subject="service deployment",
        )
        return {
            "disposition": "complete",
            "satisfied_conditions": [configured],
            "evidence_ids": [artifact_id, receipt_artifact_id],
            "output": result,
        }

    def execute_static(
        self,
        tenant_id: str,
        run_id: str,
        definition: WorkflowDefinition,
        state: WorkflowRunState,
        action: WorkflowAction,
        node: WorkflowNode,
    ) -> Mapping[str, object]:
        del run_id
        if node.kind is not NodeKind.TOOL or node.configuration.get("tool") != "deploy.static":
            raise FatalCommandError("static deployment handler received the wrong tool node")
        if self._static_deployer is None:
            raise FatalCommandError("production static deployment is not configured")
        approval_reference = node.configuration.get("approval")
        approval_node_id = (
            approval_reference.get("node_id")
            if isinstance(approval_reference, Mapping)
            else None
        )
        approval_node = next(
            (candidate for candidate in definition.nodes if candidate.node_id == approval_node_id),
            None,
        )
        if approval_node is None or approval_node.kind is not NodeKind.HUMAN:
            raise FatalCommandError("static deployment approval must come from a human node")
        approved = resolve_prior_output(
            state, approval_reference, subject="static deployment approval",
        )
        if approved is not True:
            raise FatalCommandError("static deployment requires an explicit human approval")
        artifact_id = resolve_prior_output(
            state, node.configuration.get("source"), subject="static deployment",
        )
        if not isinstance(artifact_id, str) or not artifact_id:
            raise FatalCommandError("static deployment source is not an artifact ID")
        app_slug = node.configuration.get("app_slug")
        if not isinstance(app_slug, str) or not app_slug:
            raise FatalCommandError("static deployment requires an app_slug")
        result = dict(self._static_deployer.deploy_static(
            organization_id=tenant_id,
            artifact_id=artifact_id,
            app_slug=app_slug,
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
            raise FatalCommandError(
                "static deployer returned no durable receipt or public URL"
            )
        configured = self._success_condition(
            definition, node, subject="static deployment",
        )
        return {
            "disposition": "complete",
            "satisfied_conditions": [configured],
            "evidence_ids": [artifact_id, receipt_artifact_id],
            "output": result,
        }
