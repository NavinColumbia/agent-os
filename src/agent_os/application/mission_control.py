"""Truthful CEO/manager projection over durable mission execution.

The projection never interprets elapsed time as mission failure.  It separates
business progress from infrastructure liveness: a long-running action with a
renewed lease remains owned, while an expired lease is explicitly recoverable
queue work.  Model-authored management actions remain labelled as proposals.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Mapping, Sequence

from agent_os.domain.workflow import NodeKind, WorkflowDefinition
from agent_os.domain.workflow_runtime import TokenStatus, WorkflowRunState


_TERMINAL_TOKEN = {
    TokenStatus.SUCCEEDED,
    TokenStatus.FAILED,
    TokenStatus.CANCELLED,
}

_ROLE_MANAGERS = {
    "mission-manager": "human:ceo",
    "mission-architect": "agent:mission-manager",
    "research-lead": "agent:mission-manager",
    "product-architect": "agent:mission-manager",
    "engineering-manager": "agent:mission-manager",
    "quality-manager": "agent:mission-manager",
    "release-manager": "agent:mission-manager",
    "repair-lead": "agent:engineering-manager",
}


def _time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: Any) -> str | None:
    parsed = _time(value)
    return None if parsed is None else parsed.isoformat()


def _latest_actions(observation: Mapping[str, Any] | None) -> dict[str, Mapping[str, Any]]:
    latest: dict[str, Mapping[str, Any]] = {}
    if observation is None:
        return latest
    raw_actions = observation.get("actions", ())
    if not isinstance(raw_actions, Sequence) or isinstance(raw_actions, (str, bytes)):
        return latest
    for raw in raw_actions:
        if not isinstance(raw, Mapping):
            continue
        action = raw.get("action", {})
        if not isinstance(action, Mapping):
            continue
        token_id = action.get("token_id")
        if not isinstance(token_id, str) or not token_id:
            continue
        prior = latest.get(token_id)
        if prior is None or int(raw.get("state_version", -1)) >= int(prior.get("state_version", -1)):
            latest[token_id] = raw
    return latest


def _manager_for(role: str | None) -> str:
    if role is None:
        return "agent:mission-manager"
    return _ROLE_MANAGERS.get(role, "agent:mission-manager")


def _token_health(
    status: TokenStatus,
    action: Mapping[str, Any] | None,
    *,
    now: datetime,
    slow_after_seconds: int,
) -> tuple[str, str | None, str | None]:
    """Return health, diagnostic reason, and the next infrastructure checkpoint."""

    if status is TokenStatus.SUCCEEDED:
        return "completed", None, None
    if status is TokenStatus.FAILED:
        return "failed", "The work item recorded a terminal failure.", None
    if status is TokenStatus.CANCELLED:
        return "cancelled", None, None
    if status is TokenStatus.WAITING:
        return "waiting_for_human", "A correlated human response is required.", None
    if action is None:
        return "attention_required", "No durable queue action was found for live work.", None

    queue_status = str(action.get("status") or "")
    created = _time(action.get("created_at"))
    available = _time(action.get("available_at"))
    lease_expires = _time(action.get("lease_expires_at"))
    checkpoint = _iso(action.get("lease_expires_at") or action.get("available_at"))
    elapsed = None if created is None else max(0.0, (now - created).total_seconds())

    if queue_status == "executing":
        if lease_expires is None or lease_expires <= now:
            return (
                "recovering",
                "The prior worker lease expired; durable work is eligible for safe reclamation.",
                checkpoint,
            )
        if elapsed is not None and elapsed >= slow_after_seconds:
            return (
                "slow_but_owned",
                "Execution exceeded its progress-review interval, but the worker lease is still healthy.",
                checkpoint,
            )
        return "running_owned", None, checkpoint
    if queue_status == "pending":
        if available is not None and available > now:
            return "retry_scheduled", "A bounded retry is waiting for its durable backoff.", checkpoint
        if elapsed is not None and elapsed >= slow_after_seconds:
            return "dispatch_delayed", "Ready work has waited beyond its dispatch-review interval.", checkpoint
        return "queued", None, checkpoint
    if queue_status == "failed":
        return "attention_required", "The queue action failed before the work token became terminal.", None
    if queue_status == "cancelled":
        return "cancelled", None, None
    if queue_status == "succeeded" and status is TokenStatus.RUNNING:
        return "attention_required", "The queue result and work token disagree.", None
    return "attention_required", "The live work item has an unknown queue state.", checkpoint


def _organization_actions(
    state: WorkflowRunState,
    company_events: Sequence[Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    collected: dict[str, list[dict[str, Any]]] = {
        "messages": [],
        "proposed_work": [],
        "hiring_requests": [],
        "decisions": [],
        "risks": [],
        "observations": [],
        "next_actions": [],
    }
    for token in state.tokens:
        raw = token.output.get("organization_actions", {})
        if not isinstance(raw, Mapping):
            continue
        for key in collected:
            values = raw.get(key, ())
            if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
                continue
            for position, value in enumerate(values):
                item: dict[str, Any]
                if isinstance(value, Mapping):
                    item = dict(value)
                elif key in {"risks", "observations", "next_actions"} and str(value).strip():
                    item = {"text": str(value)}
                else:
                    continue
                item["source_token_id"] = token.token_id
                item["source_node_id"] = token.node_id
                item["proposal"] = key in {
                    "messages", "proposed_work", "hiring_requests", "decisions",
                }
                if item["proposal"]:
                    material = json.dumps({
                        "run_id": state.run_id,
                        "token_id": token.token_id,
                        "kind": key,
                        "position": position,
                        "value": value,
                    }, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                    item["proposal_id"] = "proposal-" + hashlib.sha256(material.encode()).hexdigest()
                    item["status"] = "pending"
                collected[key].append(item)
    decisions = {
        str(event.get("payload", {}).get("proposal_id")): bool(
            event.get("payload", {}).get("approved")
        )
        for event in company_events
        if event.get("kind") == "hiring_proposal_decided"
        and isinstance(event.get("payload"), Mapping)
        and event.get("payload", {}).get("proposal_id")
    }
    for item in collected["hiring_requests"]:
        proposal_id = str(item.get("proposal_id") or "")
        if proposal_id in decisions:
            item["status"] = "approved" if decisions[proposal_id] else "rejected"
    return collected


def _program_readiness(state: WorkflowRunState) -> Mapping[str, Any] | None:
    """Project live readiness from the immutable admitted program and token facts."""

    program = state.context.get("mission_program")
    if not isinstance(program, Mapping):
        return None
    successful_nodes = {
        token.node_id for token in state.tokens if token.status is TokenStatus.SUCCEEDED
    }
    unresolved_questions: list[str] = []
    blocked_workstreams: set[str] = set()
    raw_questions = program.get("clarifications", ())
    if isinstance(raw_questions, Sequence) and not isinstance(raw_questions, (str, bytes)):
        for raw in raw_questions:
            if not isinstance(raw, Mapping) or raw.get("status") != "open":
                continue
            node_id = raw.get("human_node_id")
            if isinstance(node_id, str) and node_id in successful_nodes:
                continue
            question_id = raw.get("question_id")
            if isinstance(question_id, str):
                unresolved_questions.append(question_id)
            blockers = raw.get("blocking_workstream_ids", ())
            if isinstance(blockers, Sequence) and not isinstance(blockers, (str, bytes)):
                blocked_workstreams.update(str(item) for item in blockers if str(item))

    def unresolved_plans(key: str, id_key: str, node_key: str) -> list[str]:
        unresolved: list[str] = []
        raw_plans = program.get(key, ())
        if not isinstance(raw_plans, Sequence) or isinstance(raw_plans, (str, bytes)):
            return unresolved
        for raw in raw_plans:
            if not isinstance(raw, Mapping) or raw.get("status") == "verified":
                continue
            node_ids = raw.get(node_key, ())
            if (
                not isinstance(node_ids, Sequence)
                or isinstance(node_ids, (str, bytes))
                or not node_ids
                or not all(str(item) in successful_nodes for item in node_ids)
            ):
                identifier = raw.get(id_key)
                if isinstance(identifier, str):
                    unresolved.append(identifier)
        return unresolved

    resources = unresolved_plans("resources", "resource_id", "acquisition_node_ids")
    capabilities = unresolved_plans("capabilities", "capability_id", "expansion_node_ids")
    if state.status.value == "succeeded":
        status = "verified"
    elif unresolved_questions and not state.ready():
        status = "awaiting_human"
    elif resources or capabilities:
        status = "acquiring_resources_and_capabilities"
    else:
        status = "executing_and_verifying"
    return {
        "program_format": program.get("format"),
        "revision": program.get("revision"),
        "status": status,
        "feasibility_verdict": (
            program.get("feasibility", {}).get("verdict")
            if isinstance(program.get("feasibility"), Mapping) else None
        ),
        "unresolved_question_ids": sorted(unresolved_questions),
        "blocked_workstream_ids": sorted(blocked_workstreams),
        "outstanding_resource_ids": sorted(resources),
        "outstanding_capability_ids": sorted(capabilities),
        "independent_work_continues": bool(
            unresolved_questions and any(
                token.status in {TokenStatus.READY, TokenStatus.RUNNING}
                for token in state.tokens
            )
        ),
    }


def project_mission_control(
    *,
    lifecycle_run_id: str,
    planning_state: WorkflowRunState,
    execution_state: WorkflowRunState | None,
    definition: WorkflowDefinition,
    observation: Mapping[str, Any] | None = None,
    organization_events: Sequence[Mapping[str, Any]] = (),
    company_events: Sequence[Mapping[str, Any]] = (),
    now: datetime | None = None,
    slow_after_seconds: int = 300,
) -> Mapping[str, Any]:
    """Build a bounded, authority-derived view for managers and the CEO."""

    if not lifecycle_run_id or slow_after_seconds < 1:
        raise ValueError("lifecycle_run_id and a positive slow threshold are required")
    state = execution_state or planning_state
    if definition.workflow_id != state.workflow_id or definition.version != state.workflow_version:
        raise ValueError("mission-control definition does not match its selected graph run")
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    nodes = {node.node_id: node for node in definition.nodes}
    latest_actions = _latest_actions(observation)
    work_items: list[dict[str, Any]] = []
    signals: list[dict[str, Any]] = []

    for token in state.tokens:
        node = nodes[token.node_id]
        action = latest_actions.get(token.token_id)
        health, reason, next_checkpoint = _token_health(
            token.status,
            action,
            now=current_time,
            slow_after_seconds=slow_after_seconds,
        )
        role = node.owner_role
        manager_id = _manager_for(role)
        summary = token.output.get("summary")
        item = {
            "work_id": token.token_id,
            "node_id": token.node_id,
            "kind": node.kind.value,
            "objective": node.purpose,
            "owner_role": role,
            "owner_id": None if role is None else f"agent:{role}",
            "manager_id": manager_id,
            "status": token.status.value,
            "health": health,
            "iteration": token.iteration,
            "attempt": token.attempt,
            "summary": summary if isinstance(summary, str) else None,
            "evidence_ids": list(token.evidence_ids),
            "wait_correlation_id": token.wait_correlation_id,
            "wait_reason": token.wait_reason,
            "last_error": token.last_error,
            "queue_status": None if action is None else action.get("status"),
            "queue_attempts": None if action is None else action.get("attempts"),
            "last_progress_at": None if action is None else _iso(
                action.get("completed_at") or action.get("created_at")
            ),
            "next_infrastructure_checkpoint_at": next_checkpoint,
            "diagnostic_reason": reason,
        }
        work_items.append(item)
        if health in {
            "waiting_for_human", "failed", "attention_required", "recovering",
            "slow_but_owned", "dispatch_delayed", "retry_scheduled",
        }:
            severity = "info"
            if health in {"failed", "attention_required", "dispatch_delayed"}:
                severity = "critical"
            elif health in {"recovering", "slow_but_owned"}:
                severity = "warning"
            signals.append({
                "signal": health,
                "severity": severity,
                "work_id": token.token_id,
                "owner_id": item["owner_id"],
                "recipient_id": manager_id,
                "reason": reason or token.wait_reason,
                "recommended_action": {
                    "waiting_for_human": "Track the correlated response without blocking unrelated work.",
                    "slow_but_owned": "Ask the owner for a durable progress checkpoint; do not terminate healthy work.",
                    "recovering": "Allow lease reclamation, then verify that the next attempt resumes safely.",
                    "retry_scheduled": "Observe the scheduled retry and escalate only after repeated non-progress.",
                    "dispatch_delayed": "Inspect worker capacity and queue health.",
                    "failed": "Diagnose the evidence and decide repair, reassignment, or escalation.",
                    "attention_required": "Reconcile the conflicting durable state before continuing.",
                }[health],
            })

    role_nodes: dict[str, list[str]] = {}
    for node in definition.nodes:
        if node.owner_role:
            role_nodes.setdefault(node.owner_role, []).append(node.node_id)
    team = [{
        "agent_id": f"agent:{role}",
        "role": role,
        "manager_id": _manager_for(role),
        "assigned_node_ids": sorted(node_ids),
        "mission_scoped": role not in _ROLE_MANAGERS,
    } for role, node_ids in sorted(role_nodes.items())]

    complete = sum(token.status is TokenStatus.SUCCEEDED for token in state.tokens)
    live = sum(token.status not in _TERMINAL_TOKEN for token in state.tokens)
    failed = sum(token.status is TokenStatus.FAILED for token in state.tokens)
    waiting = sum(token.status is TokenStatus.WAITING for token in state.tokens)
    denominator = complete + live + failed
    materialized_ratio = 0.0 if denominator == 0 else complete / denominator
    if state.status.value == "succeeded":
        materialized_ratio = 1.0

    critical = any(signal["severity"] == "critical" for signal in signals)
    warning = any(signal["severity"] == "warning" for signal in signals)
    if state.status.value in {"succeeded", "failed", "cancelled"}:
        health = state.status.value
    elif waiting and not any(token.status in {TokenStatus.READY, TokenStatus.RUNNING} for token in state.tokens):
        health = "waiting"
    elif critical:
        health = "attention_required"
    elif warning:
        health = "degraded"
    else:
        health = "healthy"

    organization = _organization_actions(state, company_events)
    communications = list(organization["messages"])
    for item in work_items:
        if item["status"] == "waiting":
            communications.append({
                "kind": "request",
                "audience": "human",
                "recipient_ids": ["human:ceo"],
                "body": item["wait_reason"],
                "correlation_id": item["wait_correlation_id"],
                "source_token_id": item["work_id"],
                "proposal": False,
                "delivery": "durable_wait_notification",
            })

    durable_risks = [{
        "description": str(event.get("payload", {}).get("description") or ""),
        "actor_id": event.get("actor_id"),
        "stream_version": event.get("stream_version"),
        "source": "organization_ledger",
    } for event in organization_events if event.get("kind") in {
        "risk_raised", "incident_raised", "escalation_raised",
    }]

    return {
        "format": "agent-os.mission-control.v1",
        "lifecycle_run_id": lifecycle_run_id,
        "planning_run_id": planning_state.run_id,
        "execution_run_id": None if execution_state is None else execution_state.run_id,
        "selected_run_id": state.run_id,
        "workflow_id": state.workflow_id,
        "workflow_version": state.workflow_version,
        "state_version": state.version,
        "status": state.status.value,
        "health": health,
        "progress": {
            "materialized_completion_ratio": materialized_ratio,
            "observed_work_items": len(state.tokens),
            "completed": complete,
            "live": live,
            "waiting": waiting,
            "failed": failed,
            "note": "Ratio covers materialized work only; adaptive branches may create more work.",
        },
        "program": state.context.get("mission_program"),
        "readiness": _program_readiness(state),
        "last_graph_progress_at": None if observation is None else _iso(observation.get("updated_at")),
        "team": team,
        "work_items": work_items,
        "management_signals": signals,
        "communications": communications,
        "proposed_work": organization["proposed_work"],
        "hiring_requests": organization["hiring_requests"],
        "decisions": organization["decisions"],
        "risks": [*durable_risks, *organization["risks"]],
        "observations": organization["observations"],
        "next_actions": organization["next_actions"],
    }
