"""Advance graph tokens from durable action execution results."""

from __future__ import annotations

import hashlib
from typing import Callable, Mapping, Any

from agent_os.application.command_worker import FatalCommandError
from agent_os.application.ports import GraphActionExecutor, GraphNodeRuntime, GraphWorkflowEngine
from agent_os.domain.workflow_runtime import (
    TokenStatus,
    WorkflowAction,
    WorkflowActionKind,
    WorkflowEvent,
    WorkflowEventKind,
)


GraphEffectHandler = Callable[[Mapping[str, Any], WorkflowAction], Mapping[str, Any]]


def _event_id(action_id: str, stage: str) -> str:
    digest = hashlib.sha256(f"agent-os:graph-action:v1:{action_id}:{stage}".encode()).hexdigest()
    return f"graph-action-{stage}-{digest}"


class DurableGraphActionExecutor(GraphActionExecutor):
    """Turn a leased action into version-fenced begin/result events.

    Node runtimes receive the action ID as their idempotency key. If a process
    dies after the result event commits but before outbox acknowledgement, the
    replacement observes the terminal token and does not repeat the side effect.
    """

    def __init__(
        self,
        *,
        engine: GraphWorkflowEngine,
        node_runtime: GraphNodeRuntime,
        effect_handlers: Mapping[WorkflowActionKind, GraphEffectHandler] | None = None,
    ) -> None:
        self._engine = engine
        self._node_runtime = node_runtime
        self._effect_handlers = dict(effect_handlers or {})
        if WorkflowActionKind.EXECUTE_NODE in self._effect_handlers:
            raise ValueError("execute_node cannot be shadowed by an effect handler")

    def execute(self, envelope: Mapping[str, Any]) -> Mapping[str, Any]:
        tenant_id = str(envelope.get("tenant_id") or "")
        run_id = str(envelope.get("run_id") or "")
        action_raw = envelope.get("action")
        if not tenant_id or not run_id or not isinstance(action_raw, Mapping):
            raise FatalCommandError("graph action envelope requires tenant, run, and action")
        action = WorkflowAction.from_dict(action_raw)
        if action.kind is not WorkflowActionKind.EXECUTE_NODE:
            handler = self._effect_handlers.get(action.kind)
            if handler is None:
                raise FatalCommandError(
                    f"no executor is configured for graph effect {action.kind.value}; "
                    "the action was not acknowledged as delivered"
                )
            result = handler(envelope, action)
            if not isinstance(result, Mapping):
                raise FatalCommandError(f"graph effect {action.kind.value} returned no result object")
            return dict(result)
        if not action.token_id or not action.node_id:
            raise FatalCommandError("execute_node action requires token_id and node_id")

        state = self._engine.get_graph_run(tenant_id, run_id)
        if state is None:
            raise FatalCommandError("graph run does not exist for this action")
        token = state.token(action.token_id)
        if token.node_id != action.node_id:
            raise FatalCommandError("graph action node does not match its token")
        if token.status is TokenStatus.READY:
            began = self._engine.submit_graph_event(
                tenant_id,
                run_id,
                WorkflowEvent(
                    _event_id(action.action_id, "began"),
                    WorkflowEventKind.NODE_BEGAN,
                    state.version,
                    {"token_id": action.token_id, "action_id": action.action_id},
                ),
            )
            state = began.state
            token = state.token(action.token_id)
        elif token.status is not TokenStatus.RUNNING:
            return {
                "action_id": action.action_id,
                "replayed": True,
                "token_status": token.status.value,
                "state_version": state.version,
            }

        definition = self._engine.get_workflow_definition(
            tenant_id,
            state.workflow_id,
            state.workflow_version,
        )
        if definition is None:
            raise FatalCommandError("workflow definition disappeared while its run is active")
        try:
            result = self._node_runtime.execute_node(
                tenant_id=tenant_id,
                run_id=run_id,
                definition=definition,
                state=state,
                action=action,
                idempotency_key=action.action_id,
            )
        except FatalCommandError as exc:
            current = self._engine.get_graph_run(tenant_id, run_id)
            if current is None:
                raise FatalCommandError("graph run disappeared while recording node failure") from exc
            current_token = current.token(action.token_id)
            if current_token.status is not TokenStatus.RUNNING:
                return {
                    "action_id": action.action_id,
                    "replayed": True,
                    "token_status": current_token.status.value,
                    "state_version": current.version,
                }
            receipt = self._engine.submit_graph_event(
                tenant_id,
                run_id,
                WorkflowEvent(
                    _event_id(action.action_id, "result"),
                    WorkflowEventKind.NODE_FAILED,
                    current.version,
                    {
                        "token_id": action.token_id,
                        "action_id": action.action_id,
                        "reason": str(exc)[:2_000],
                        "retryable": False,
                    },
                ),
            )
            return {
                "action_id": action.action_id,
                "replayed": False,
                "token_status": receipt.state.token(action.token_id).status.value,
                "state_version": receipt.state.version,
                "emitted_actions": [item.action_id for item in receipt.actions],
            }
        if not isinstance(result, Mapping):
            raise FatalCommandError("graph node runtime must return a result object")

        current = self._engine.get_graph_run(tenant_id, run_id)
        if current is None:
            raise FatalCommandError("graph run disappeared while its node was executing")
        current_token = current.token(action.token_id)
        if current_token.status is not TokenStatus.RUNNING:
            return {
                "action_id": action.action_id,
                "replayed": True,
                "token_status": current_token.status.value,
                "state_version": current.version,
            }
        disposition = str(result.get("disposition") or "")
        payload: dict[str, Any] = {"token_id": action.token_id, "action_id": action.action_id}
        if disposition == "complete":
            evidence = [str(item) for item in result.get("evidence_ids", ()) if str(item)]
            if not evidence:
                raise FatalCommandError("graph node completion requires durable evidence IDs")
            output = result.get("output", {})
            if not isinstance(output, Mapping):
                raise FatalCommandError("graph node output must be an object")
            payload.update({
                "satisfied_conditions": [
                    str(item) for item in result.get("satisfied_conditions", ()) if str(item)
                ],
                "evidence_ids": evidence,
                "output": dict(output),
            })
            kind = WorkflowEventKind.NODE_COMPLETED
        elif disposition == "wait":
            recipients = [str(item) for item in result.get("recipient_ids", ()) if str(item)]
            correlation_id = str(result.get("correlation_id") or "")
            reason = str(result.get("reason") or "")
            if not recipients or not correlation_id or not reason:
                raise FatalCommandError("graph wait requires recipients, correlation ID, and reason")
            payload.update({
                "recipient_ids": recipients,
                "correlation_id": correlation_id,
                "reason": reason,
            })
            kind = WorkflowEventKind.NODE_WAITED
        elif disposition == "fail":
            reason = str(result.get("reason") or "")
            if not reason:
                raise FatalCommandError("graph node failure requires a reason")
            payload.update({"reason": reason, "retryable": bool(result.get("retryable", False))})
            kind = WorkflowEventKind.NODE_FAILED
        else:
            raise FatalCommandError("graph node disposition must be complete, wait, or fail")

        receipt = self._engine.submit_graph_event(
            tenant_id,
            run_id,
            WorkflowEvent(
                _event_id(action.action_id, "result"),
                kind,
                current.version,
                payload,
            ),
        )
        return {
            "action_id": action.action_id,
            "replayed": False,
            "token_status": receipt.state.token(action.token_id).status.value,
            "state_version": receipt.state.version,
            "emitted_actions": [item.action_id for item in receipt.actions],
        }
