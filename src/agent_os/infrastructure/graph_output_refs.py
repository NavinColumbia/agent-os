"""Bounded references from one durable graph token to a later tool node."""

from __future__ import annotations

from typing import Any, Mapping

from agent_os.application.command_worker import FatalCommandError
from agent_os.domain.workflow_runtime import TokenStatus, WorkflowRunState


def resolve_prior_output(
    state: WorkflowRunState,
    source: object,
    *,
    subject: str,
) -> Any:
    """Resolve a key/index path only from a successful prior node output."""

    if not isinstance(source, Mapping):
        raise FatalCommandError(f"{subject} source must be an object")
    node_id = str(source.get("node_id") or "")
    output_path = source.get("output_path")
    if output_path is None:
        output_path = [str(source.get("output_key") or "artifact_id")]
    if (
        not isinstance(output_path, (list, tuple))
        or not output_path
        or len(output_path) > 16
        or any(
            isinstance(part, bool)
            or not isinstance(part, (str, int))
            or (isinstance(part, str) and not part)
            or (isinstance(part, int) and part < 0)
            for part in output_path
        )
    ):
        raise FatalCommandError(
            f"{subject} source output_path must contain bounded keys or indexes"
        )
    candidates = [
        token for token in state.tokens
        if token.node_id == node_id and token.status is TokenStatus.SUCCEEDED
    ]
    if not node_id or not candidates:
        raise FatalCommandError(f"{subject} source node has no successful durable output")
    selected = max(candidates, key=lambda token: (token.iteration, token.token_id))
    value: Any = dict(selected.output)
    for part in output_path:
        if isinstance(part, str) and isinstance(value, Mapping):
            value = value.get(part)
        elif isinstance(part, int) and isinstance(value, (list, tuple)):
            value = value[part] if part < len(value) else None
        else:
            value = None
        if value is None:
            break
    if value is None:
        raise FatalCommandError(f"{subject} source output_path does not exist")
    return value
