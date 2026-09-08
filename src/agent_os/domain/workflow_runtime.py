"""Pure token runtime for customer/agent-designed workflow graphs.

The six product lifecycle phases are a coarse CEO progress projection. Actual
missions run on this graph: branches may fan out, agents may loop after review,
and human waits carry durable correlation IDs. No model or workflow framework
owns these transition rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
import hashlib
import json
from typing import Any, Mapping

from agent_os.domain.workflow import NodeKind, WorkflowDefinition


class WorkflowRunStatus(str, Enum):
    ACTIVE = "active"
    WAITING = "waiting"
    FAILED = "failed"
    SUCCEEDED = "succeeded"
    CANCELLED = "cancelled"


class TokenStatus(str, Enum):
    READY = "ready"
    RUNNING = "running"
    WAITING = "waiting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkflowActionKind(str, Enum):
    EXECUTE_NODE = "execute_node"
    NOTIFY_HUMAN = "notify_human"
    RUN_SUCCEEDED = "run_succeeded"
    RUN_FAILED = "run_failed"
    RUN_CANCELLED = "run_cancelled"


@dataclass(frozen=True)
class NodeToken:
    token_id: str
    node_id: str
    status: TokenStatus
    iteration: int
    attempt: int = 0
    evidence_ids: tuple[str, ...] = ()
    output: Mapping[str, Any] = field(default_factory=dict)
    wait_correlation_id: str | None = None
    wait_reason: str | None = None
    last_error: str | None = None

    def __post_init__(self) -> None:
        if not self.token_id or not self.node_id or self.iteration < 1 or self.attempt < 0:
            raise ValueError("workflow token requires identity and positive iteration")
        waiting = self.status is TokenStatus.WAITING
        if waiting != bool(self.wait_correlation_id and self.wait_reason):
            raise ValueError("waiting tokens require correlation and reason exclusively")
        if self.status is TokenStatus.SUCCEEDED and not self.evidence_ids:
            raise ValueError("succeeded workflow tokens require evidence")

    def to_dict(self) -> dict[str, Any]:
        return {
            "token_id": self.token_id,
            "node_id": self.node_id,
            "status": self.status.value,
            "iteration": self.iteration,
            "attempt": self.attempt,
            "evidence_ids": list(self.evidence_ids),
            "output": dict(self.output),
            "wait_correlation_id": self.wait_correlation_id,
            "wait_reason": self.wait_reason,
            "last_error": self.last_error,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "NodeToken":
        output = raw.get("output", {})
        if not isinstance(output, Mapping):
            raise ValueError("workflow token output must be an object")
        return cls(
            token_id=str(raw["token_id"]),
            node_id=str(raw["node_id"]),
            status=TokenStatus(str(raw["status"])),
            iteration=int(raw["iteration"]),
            attempt=int(raw.get("attempt", 0)),
            evidence_ids=tuple(str(item) for item in raw.get("evidence_ids", ())),
            output=dict(output),
            wait_correlation_id=raw.get("wait_correlation_id"),
            wait_reason=raw.get("wait_reason"),
            last_error=raw.get("last_error"),
        )


@dataclass(frozen=True)
class WorkflowRunState:
    run_id: str
    tenant_id: str
    workflow_id: str
    workflow_version: int
    version: int
    status: WorkflowRunStatus
    tokens: tuple[NodeToken, ...]
    context: Mapping[str, Any] = field(default_factory=dict)
    terminal_token_ids: tuple[str, ...] = ()
    failure: str | None = None

    def __post_init__(self) -> None:
        if not self.run_id or not self.tenant_id or not self.workflow_id:
            raise ValueError("workflow run identity is required")
        if self.workflow_version < 1 or self.version < 0:
            raise ValueError("workflow versions must be valid")
        if len({token.token_id for token in self.tokens}) != len(self.tokens):
            raise ValueError("workflow token IDs must be unique")
        if self.status is WorkflowRunStatus.FAILED and not self.failure:
            raise ValueError("failed workflow run requires a reason")
        if self.status is not WorkflowRunStatus.FAILED and self.failure:
            raise ValueError("only failed workflow runs carry a failure")

    def token(self, token_id: str) -> NodeToken:
        found = next((token for token in self.tokens if token.token_id == token_id), None)
        if found is None:
            raise LookupError("workflow token does not exist")
        return found

    def ready(self) -> tuple[NodeToken, ...]:
        return tuple(token for token in self.tokens if token.status is TokenStatus.READY)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "tenant_id": self.tenant_id,
            "workflow_id": self.workflow_id,
            "workflow_version": self.workflow_version,
            "version": self.version,
            "status": self.status.value,
            "tokens": [token.to_dict() for token in self.tokens],
            "context": dict(self.context),
            "terminal_token_ids": list(self.terminal_token_ids),
            "failure": self.failure,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "WorkflowRunState":
        context = raw.get("context", {})
        if not isinstance(context, Mapping):
            raise ValueError("workflow run context must be an object")
        return cls(
            run_id=str(raw["run_id"]),
            tenant_id=str(raw["tenant_id"]),
            workflow_id=str(raw["workflow_id"]),
            workflow_version=int(raw["workflow_version"]),
            version=int(raw["version"]),
            status=WorkflowRunStatus(str(raw["status"])),
            tokens=tuple(NodeToken.from_dict(item) for item in raw.get("tokens", ())),
            context=dict(context),
            terminal_token_ids=tuple(str(item) for item in raw.get("terminal_token_ids", ())),
            failure=raw.get("failure"),
        )


@dataclass(frozen=True)
class WorkflowAction:
    action_id: str
    kind: WorkflowActionKind
    token_id: str | None
    node_id: str | None
    payload: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "kind": self.kind.value,
            "token_id": self.token_id,
            "node_id": self.node_id,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "WorkflowAction":
        payload = raw.get("payload", {})
        if not isinstance(payload, Mapping):
            raise ValueError("workflow action payload must be an object")
        return cls(
            action_id=str(raw["action_id"]),
            kind=WorkflowActionKind(str(raw["kind"])),
            token_id=raw.get("token_id"),
            node_id=raw.get("node_id"),
            payload=dict(payload),
        )


@dataclass(frozen=True)
class WorkflowMutation:
    state: WorkflowRunState
    actions: tuple[WorkflowAction, ...]


class WorkflowTransitionRejected(ValueError):
    pass


class WorkflowEventKind(str, Enum):
    NODE_BEGAN = "node_began"
    NODE_COMPLETED = "node_completed"
    NODE_WAITED = "node_waited"
    WAIT_RESUMED = "wait_resumed"
    NODE_FAILED = "node_failed"
    RUN_CANCELLED = "run_cancelled"


@dataclass(frozen=True)
class WorkflowEvent:
    event_id: str
    kind: WorkflowEventKind
    expected_version: int
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.event_id.strip() or self.expected_version < 0:
            raise ValueError("workflow event identity and nonnegative expected version are required")
        workflow_event_fingerprint(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "kind": self.kind.value,
            "expected_version": self.expected_version,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "WorkflowEvent":
        payload = raw.get("payload", {})
        if not isinstance(payload, Mapping):
            raise ValueError("workflow event payload must be an object")
        return cls(
            event_id=str(raw["event_id"]),
            kind=WorkflowEventKind(str(raw["kind"])),
            expected_version=int(raw["expected_version"]),
            payload=dict(payload),
        )


def workflow_event_fingerprint(event: WorkflowEvent) -> str:
    try:
        encoded = json.dumps(
            event.to_dict(), allow_nan=False, ensure_ascii=False,
            separators=(",", ":"), sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("workflow event payload must contain JSON-compatible primitives") from exc
    return hashlib.sha256(encoded.encode()).hexdigest()


_LIVE = {TokenStatus.READY, TokenStatus.RUNNING, TokenStatus.WAITING}
_TERMINAL_RUN = {
    WorkflowRunStatus.FAILED,
    WorkflowRunStatus.SUCCEEDED,
    WorkflowRunStatus.CANCELLED,
}


def _id(*parts: object) -> str:
    return hashlib.sha256(":".join(str(part) for part in parts).encode()).hexdigest()


def _action(
    run_id: str,
    version: int,
    position: int,
    kind: WorkflowActionKind,
    token: NodeToken | None,
    payload: Mapping[str, Any] | None = None,
) -> WorkflowAction:
    return WorkflowAction(
        action_id=_id("agent-os", "workflow-action", "v1", run_id, version, position, kind.value),
        kind=kind,
        token_id=None if token is None else token.token_id,
        node_id=None if token is None else token.node_id,
        payload={} if payload is None else dict(payload),
    )


def _replace_token(state: WorkflowRunState, updated: NodeToken) -> tuple[NodeToken, ...]:
    return tuple(updated if token.token_id == updated.token_id else token for token in state.tokens)


def _ensure_mutable(state: WorkflowRunState, expected_version: int) -> None:
    if state.status in _TERMINAL_RUN:
        raise WorkflowTransitionRejected(f"workflow run is {state.status.value}")
    if state.version != expected_version:
        raise WorkflowTransitionRejected(
            f"stale workflow version {expected_version}; current version is {state.version}"
        )


def start_workflow(
    definition: WorkflowDefinition,
    *,
    run_id: str,
    context: Mapping[str, Any] | None = None,
) -> WorkflowMutation:
    if not run_id.strip():
        raise ValueError("run_id is required")
    token = NodeToken(
        token_id=_id("agent-os", "workflow-token", "v1", run_id, "entry"),
        node_id=definition.entry_node_id,
        status=TokenStatus.READY,
        iteration=1,
    )
    state = WorkflowRunState(
        run_id=run_id,
        tenant_id=definition.tenant_id,
        workflow_id=definition.workflow_id,
        workflow_version=definition.version,
        version=0,
        status=WorkflowRunStatus.ACTIVE,
        tokens=(token,),
        context={} if context is None else dict(context),
    )
    return WorkflowMutation(state, (_action(run_id, 0, 0, WorkflowActionKind.EXECUTE_NODE, token),))


def begin_node(
    state: WorkflowRunState,
    token_id: str,
    *,
    expected_version: int,
) -> WorkflowMutation:
    _ensure_mutable(state, expected_version)
    token = state.token(token_id)
    if token.status is not TokenStatus.READY:
        raise WorkflowTransitionRejected("only a ready workflow token can begin")
    updated = replace(token, status=TokenStatus.RUNNING, attempt=token.attempt + 1)
    return WorkflowMutation(replace(
        state,
        version=state.version + 1,
        status=WorkflowRunStatus.ACTIVE,
        tokens=_replace_token(state, updated),
    ), ())


def wait_node(
    state: WorkflowRunState,
    token_id: str,
    *,
    expected_version: int,
    correlation_id: str,
    reason: str,
    recipient_ids: tuple[str, ...],
) -> WorkflowMutation:
    _ensure_mutable(state, expected_version)
    token = state.token(token_id)
    if token.status is not TokenStatus.RUNNING:
        raise WorkflowTransitionRejected("only a running workflow token can wait")
    if not correlation_id.strip() or not reason.strip() or not recipient_ids:
        raise WorkflowTransitionRejected("a human wait requires correlation, reason, and recipients")
    updated = replace(
        token,
        status=TokenStatus.WAITING,
        wait_correlation_id=correlation_id,
        wait_reason=reason,
    )
    tokens = _replace_token(state, updated)
    status = WorkflowRunStatus.WAITING if not any(t.status in {
        TokenStatus.READY, TokenStatus.RUNNING,
    } for t in tokens) else WorkflowRunStatus.ACTIVE
    version = state.version + 1
    action = _action(state.run_id, version, 0, WorkflowActionKind.NOTIFY_HUMAN, updated, {
        "correlation_id": correlation_id,
        "reason": reason,
        "recipient_ids": list(recipient_ids),
    })
    return WorkflowMutation(replace(state, version=version, status=status, tokens=tokens), (action,))


def resume_wait(
    state: WorkflowRunState,
    *,
    expected_version: int,
    correlation_id: str,
    response: Mapping[str, Any],
) -> WorkflowMutation:
    _ensure_mutable(state, expected_version)
    matches = [
        token for token in state.tokens
        if token.status is TokenStatus.WAITING and token.wait_correlation_id == correlation_id
    ]
    if len(matches) != 1:
        raise WorkflowTransitionRejected("wait correlation must match exactly one token")
    token = matches[0]
    updated = replace(
        token,
        status=TokenStatus.READY,
        output={**dict(token.output), "human_response": dict(response)},
        wait_correlation_id=None,
        wait_reason=None,
    )
    version = state.version + 1
    next_state = replace(
        state,
        version=version,
        status=WorkflowRunStatus.ACTIVE,
        tokens=_replace_token(state, updated),
    )
    return WorkflowMutation(
        next_state,
        (_action(state.run_id, version, 0, WorkflowActionKind.EXECUTE_NODE, updated, {
            "resumed_from": correlation_id,
        }),),
    )


def complete_node(
    definition: WorkflowDefinition,
    state: WorkflowRunState,
    token_id: str,
    *,
    expected_version: int,
    satisfied_conditions: frozenset[str],
    evidence_ids: tuple[str, ...],
    output: Mapping[str, Any] | None = None,
) -> WorkflowMutation:
    _ensure_mutable(state, expected_version)
    if definition.workflow_id != state.workflow_id or definition.version != state.workflow_version:
        raise WorkflowTransitionRejected("workflow definition/version does not match the run")
    token = state.token(token_id)
    if token.status is not TokenStatus.RUNNING:
        raise WorkflowTransitionRejected("only a running workflow token can complete")
    if not evidence_ids:
        raise WorkflowTransitionRejected("node completion requires evidence")
    completed = replace(
        token,
        status=TokenStatus.SUCCEEDED,
        evidence_ids=evidence_ids,
        output={} if output is None else dict(output),
    )
    tokens = list(_replace_token(state, completed))
    node = next(node for node in definition.nodes if node.node_id == token.node_id)
    outgoing = tuple(
        edge for edge in definition.outgoing(token.node_id)
        if edge.condition == "always" or edge.condition in satisfied_conditions
    )
    version = state.version + 1
    actions: list[WorkflowAction] = []
    terminal_ids = state.terminal_token_ids

    if node.kind is NodeKind.TERMINAL:
        terminal_ids = (*terminal_ids, token.token_id)
    elif not outgoing:
        reason = f"node {node.node_id} completed without a satisfied outgoing path"
        failed_state = replace(
            state,
            version=version,
            status=WorkflowRunStatus.FAILED,
            tokens=tuple(tokens),
            failure=reason,
        )
        return WorkflowMutation(
            failed_state,
            (_action(state.run_id, version, 0, WorkflowActionKind.RUN_FAILED, completed, {
                "reason": reason,
            }),),
        )
    else:
        activations: dict[str, int] = {}
        for edge in outgoing:
            activations[edge.target] = activations.get(edge.target, 0) + 1
            prior_iterations = sum(1 for existing in tokens if existing.node_id == edge.target)
            target = next(node for node in definition.nodes if node.node_id == edge.target)
            max_iterations = int(target.configuration.get("max_iterations", 1000))
            iteration = prior_iterations + activations[edge.target]
            if iteration > max_iterations:
                reason = f"node {edge.target} exceeded its configured iteration guard"
                failed_state = replace(
                    state,
                    version=version,
                    status=WorkflowRunStatus.FAILED,
                    tokens=tuple(tokens),
                    failure=reason,
                )
                return WorkflowMutation(
                    failed_state,
                    (_action(state.run_id, version, 0, WorkflowActionKind.RUN_FAILED, completed, {
                        "reason": reason,
                    }),),
                )
            spawned = NodeToken(
                token_id=_id(
                    "agent-os", "workflow-token", "v1", state.run_id,
                    token.token_id, edge.target, version, len(actions),
                ),
                node_id=edge.target,
                status=TokenStatus.READY,
                iteration=iteration,
            )
            tokens.append(spawned)
            actions.append(_action(
                state.run_id, version, len(actions), WorkflowActionKind.EXECUTE_NODE, spawned,
                {"source_token_id": token.token_id, "condition": edge.condition},
            ))

    live = [item for item in tokens if item.status in _LIVE]
    if terminal_ids and not live:
        status = WorkflowRunStatus.SUCCEEDED
        actions.append(_action(
            state.run_id, version, len(actions), WorkflowActionKind.RUN_SUCCEEDED, completed,
            {"terminal_token_ids": list(terminal_ids)},
        ))
    elif live and all(item.status is TokenStatus.WAITING for item in live):
        status = WorkflowRunStatus.WAITING
    else:
        status = WorkflowRunStatus.ACTIVE
    return WorkflowMutation(replace(
        state,
        version=version,
        status=status,
        tokens=tuple(tokens),
        terminal_token_ids=terminal_ids,
    ), tuple(actions))


def fail_node(
    state: WorkflowRunState,
    token_id: str,
    *,
    expected_version: int,
    reason: str,
    retryable: bool,
) -> WorkflowMutation:
    _ensure_mutable(state, expected_version)
    token = state.token(token_id)
    if token.status is not TokenStatus.RUNNING or not reason.strip():
        raise WorkflowTransitionRejected("a running token and failure reason are required")
    version = state.version + 1
    if retryable:
        updated = replace(token, status=TokenStatus.READY, last_error=reason)
        next_state = replace(state, version=version, tokens=_replace_token(state, updated))
        return WorkflowMutation(next_state, (
            _action(state.run_id, version, 0, WorkflowActionKind.EXECUTE_NODE, updated, {
                "retry": True, "prior_error": reason,
            }),
        ))
    updated = replace(token, status=TokenStatus.FAILED, last_error=reason)
    next_state = replace(
        state,
        version=version,
        status=WorkflowRunStatus.FAILED,
        tokens=_replace_token(state, updated),
        failure=reason,
    )
    return WorkflowMutation(next_state, (
        _action(state.run_id, version, 0, WorkflowActionKind.RUN_FAILED, updated, {"reason": reason}),
    ))


def cancel_workflow(
    state: WorkflowRunState,
    *,
    expected_version: int,
    reason: str,
) -> WorkflowMutation:
    _ensure_mutable(state, expected_version)
    tokens = tuple(
        replace(token, status=TokenStatus.CANCELLED)
        if token.status in _LIVE else token
        for token in state.tokens
    )
    version = state.version + 1
    next_state = replace(state, version=version, status=WorkflowRunStatus.CANCELLED, tokens=tokens)
    return WorkflowMutation(next_state, (
        _action(state.run_id, version, 0, WorkflowActionKind.RUN_CANCELLED, None, {"reason": reason}),
    ))


def evolve_workflow(
    definition: WorkflowDefinition,
    state: WorkflowRunState,
    event: WorkflowEvent,
) -> WorkflowMutation:
    """Apply one typed durable graph event through the pure transition API."""

    payload = event.payload
    if event.kind is WorkflowEventKind.NODE_BEGAN:
        return begin_node(
            state, str(payload.get("token_id") or ""), expected_version=event.expected_version,
        )
    if event.kind is WorkflowEventKind.NODE_COMPLETED:
        output = payload.get("output", {})
        if not isinstance(output, Mapping):
            raise WorkflowTransitionRejected("node output must be an object")
        return complete_node(
            definition,
            state,
            str(payload.get("token_id") or ""),
            expected_version=event.expected_version,
            satisfied_conditions=frozenset(str(item) for item in payload.get("satisfied_conditions", ())),
            evidence_ids=tuple(str(item) for item in payload.get("evidence_ids", ())),
            output=output,
        )
    if event.kind is WorkflowEventKind.NODE_WAITED:
        return wait_node(
            state,
            str(payload.get("token_id") or ""),
            expected_version=event.expected_version,
            correlation_id=str(payload.get("correlation_id") or ""),
            reason=str(payload.get("reason") or ""),
            recipient_ids=tuple(str(item) for item in payload.get("recipient_ids", ())),
        )
    if event.kind is WorkflowEventKind.WAIT_RESUMED:
        response = payload.get("response", {})
        if not isinstance(response, Mapping):
            raise WorkflowTransitionRejected("human response must be an object")
        return resume_wait(
            state,
            expected_version=event.expected_version,
            correlation_id=str(payload.get("correlation_id") or ""),
            response=response,
        )
    if event.kind is WorkflowEventKind.NODE_FAILED:
        return fail_node(
            state,
            str(payload.get("token_id") or ""),
            expected_version=event.expected_version,
            reason=str(payload.get("reason") or ""),
            retryable=bool(payload.get("retryable", False)),
        )
    if event.kind is WorkflowEventKind.RUN_CANCELLED:
        return cancel_workflow(
            state,
            expected_version=event.expected_version,
            reason=str(payload.get("reason") or "cancelled"),
        )
    raise WorkflowTransitionRejected(f"unsupported workflow event {event.kind.value}")
