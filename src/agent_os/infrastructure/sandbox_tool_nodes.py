"""Workflow tool-node bridge for the isolated sandbox runner port."""

from __future__ import annotations

from typing import Mapping

from agent_os.application.command_worker import FatalCommandError
from agent_os.application.ports import SandboxRunner
from agent_os.domain.workflow import NodeKind, WorkflowDefinition, WorkflowNode
from agent_os.domain.workflow_runtime import WorkflowAction, WorkflowRunState
from agent_os.infrastructure.graph_output_refs import resolve_prior_output


class SandboxToolNodeHandlers:
    def __init__(self, runner: SandboxRunner) -> None:
        self._runner = runner

    def named_handlers(self):
        return {"sandbox.run": self.execute}

    @staticmethod
    def _source_artifact(state: WorkflowRunState, node: WorkflowNode) -> str:
        direct = str(node.configuration.get("source_artifact_id") or "")
        source = node.configuration.get("source")
        if bool(direct) == (source is not None):
            raise FatalCommandError(
                "sandbox.run requires exactly one of source_artifact_id or source"
            )
        if direct:
            return direct
        artifact_id = resolve_prior_output(state, source, subject="sandbox")
        if not isinstance(artifact_id, str) or not artifact_id:
            raise FatalCommandError("sandbox source output is not an artifact ID")
        return artifact_id

    @staticmethod
    def _condition(
        definition: WorkflowDefinition,
        node: WorkflowNode,
        configuration_key: str,
        *,
        infer_single: bool,
    ) -> list[str]:
        available = {
            edge.condition for edge in definition.outgoing(node.node_id)
            if edge.condition != "always"
        }
        raw = node.configuration.get(configuration_key)
        if raw is None:
            return list(available) if infer_single and len(available) == 1 else []
        if not isinstance(raw, str) or not raw:
            raise FatalCommandError(f"sandbox {configuration_key} must be one condition string")
        if raw not in available:
            raise FatalCommandError(f"sandbox selected unknown condition: {raw}")
        return [raw]

    def execute(
        self,
        tenant_id: str,
        run_id: str,
        definition: WorkflowDefinition,
        state: WorkflowRunState,
        action: WorkflowAction,
        node: WorkflowNode,
    ) -> Mapping[str, Any]:
        del run_id
        if node.kind is not NodeKind.TOOL or node.configuration.get("tool") != "sandbox.run":
            raise FatalCommandError("sandbox handler received the wrong tool node")
        raw_command = node.configuration.get("command")
        if not isinstance(raw_command, (list, tuple)) or any(
            not isinstance(item, str) for item in raw_command
        ):
            raise FatalCommandError("sandbox.run command must be direct argv")
        result = dict(self._runner.run(
            organization_id=tenant_id,
            artifact_id=self._source_artifact(state, node),
            command=tuple(raw_command),
            idempotency_key=action.action_id,
        ))
        evidence = [
            str(result.get("output_artifact_id") or ""),
            str(result.get("result_artifact_id") or ""),
        ]
        if any(not item for item in evidence):
            raise FatalCommandError("sandbox runner did not return durable artifact evidence")
        if result.get("exit_code") == 0 and result.get("timed_out") is False:
            return {
                "disposition": "complete",
                "satisfied_conditions": self._condition(
                    definition, node, "success_condition", infer_single=True,
                ),
                "evidence_ids": evidence,
                "output": result,
            }
        failed_path = self._condition(
            definition, node, "failure_condition", infer_single=False,
        )
        if failed_path:
            return {
                "disposition": "complete",
                "satisfied_conditions": failed_path,
                "evidence_ids": evidence,
                "output": result,
            }
        return {
            "disposition": "fail",
            "reason": (
                f"sandbox command failed with exit {result.get('exit_code')}; "
                f"evidence={result['result_artifact_id']}"
            ),
            "retryable": False,
        }
