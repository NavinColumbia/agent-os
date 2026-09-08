"""Allowlisted workflow tool nodes that turn durable output into real artifacts."""

from __future__ import annotations

import json
from typing import Any, Mapping

from agent_os.application.command_worker import FatalCommandError
from agent_os.application.ports import ArtifactStore
from agent_os.domain.workflow import NodeKind, WorkflowDefinition, WorkflowNode
from agent_os.domain.workflow_runtime import TokenStatus, WorkflowAction, WorkflowRunState


class ArtifactToolNodeHandlers:
    """Deterministic tool-node handlers; no model-controlled shell execution."""

    def __init__(self, store: ArtifactStore) -> None:
        self._store = store

    def handlers(self):
        return {NodeKind.TOOL: self.execute}

    def named_handlers(self):
        return {
            "artifact.publish_text": self.execute,
            "artifact.publish_json": self.execute,
        }

    @staticmethod
    def _source_value(state: WorkflowRunState, configuration: Mapping[str, Any]) -> Any:
        has_value = "value" in configuration
        source = configuration.get("source")
        if has_value == (source is not None):
            raise FatalCommandError("artifact tool requires exactly one of value or source")
        if has_value:
            return configuration["value"]
        if not isinstance(source, Mapping):
            raise FatalCommandError("artifact tool source must be an object")
        node_id = str(source.get("node_id") or "")
        path = source.get("output_path", ())
        if not node_id or not isinstance(path, (list, tuple)) or any(
            not isinstance(part, str) or not part for part in path
        ):
            raise FatalCommandError("artifact source requires node_id and a string output_path")
        candidates = [
            token for token in state.tokens
            if token.node_id == node_id and token.status is TokenStatus.SUCCEEDED
        ]
        if not candidates:
            raise FatalCommandError("artifact source node has no successful durable output")
        selected = max(candidates, key=lambda token: (token.iteration, token.token_id))
        value: Any = dict(selected.output)
        for part in path:
            if not isinstance(value, Mapping) or part not in value:
                raise FatalCommandError("artifact source output_path does not exist")
            value = value[part]
        return value

    @staticmethod
    def _conditions(definition: WorkflowDefinition, node: WorkflowNode) -> list[str]:
        raw = node.configuration.get("satisfied_conditions")
        available = {
            edge.condition for edge in definition.outgoing(node.node_id)
            if edge.condition != "always"
        }
        if raw is None:
            return list(available) if len(available) == 1 else []
        if not isinstance(raw, (list, tuple)) or any(not isinstance(item, str) for item in raw):
            raise FatalCommandError("artifact tool satisfied_conditions must be a string list")
        selected = list(dict.fromkeys(raw))
        unknown = set(selected) - available
        if unknown:
            raise FatalCommandError(
                f"artifact tool selected unknown conditions: {sorted(unknown)}"
            )
        return selected

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
        if node.kind is not NodeKind.TOOL:
            raise FatalCommandError("artifact tool handler received a non-tool node")
        tool = str(node.configuration.get("tool") or "")
        value = self._source_value(state, node.configuration)
        if tool == "artifact.publish_text":
            if not isinstance(value, str):
                raise FatalCommandError("artifact.publish_text requires string content")
            content = value.encode("utf-8")
            media_type = str(node.configuration.get("media_type") or "text/plain; charset=utf-8")
        elif tool == "artifact.publish_json":
            try:
                content = json.dumps(
                    value,
                    allow_nan=False,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            except (TypeError, ValueError) as exc:
                raise FatalCommandError("artifact.publish_json requires JSON-compatible content") from exc
            media_type = str(node.configuration.get("media_type") or "application/json")
        else:
            raise FatalCommandError(f"tool {tool or '<missing>'} is not registered")
        artifact_id = self._store.put(
            organization_id=tenant_id,
            content=content,
            media_type=media_type,
            idempotency_key=action.action_id,
        )
        return {
            "disposition": "complete",
            "satisfied_conditions": self._conditions(definition, node),
            "evidence_ids": [artifact_id],
            "output": {
                "tool": tool,
                "artifact_id": artifact_id,
                "media_type": media_type.strip().lower(),
                "byte_length": len(content),
            },
        }
