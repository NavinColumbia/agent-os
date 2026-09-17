"""Translate a structured model turn into governed organization facts.

Model output is a proposal, never authority.  This module validates/routs what
it can against the current organization, rejects hallucinated recipients, and
turns consequential actions into proposals/approval requests.  The resulting
events have deterministic IDs so replay cannot duplicate work or messages.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Any, Mapping, Sequence

from agent_os.application.ports import OrganizationEventReceipt, OrganizationLedger
from agent_os.domain.organization import Audience, Message, MessageKind, Organization
from agent_os.domain.organization_events import OrganizationEvent, OrganizationEventKind


@dataclass(frozen=True)
class AgentTurnContext:
    tenant_id: str
    run_id: str
    actor_id: str
    command_id: str
    occurred_at: str
    starting_stream_version: int

    def __post_init__(self) -> None:
        if not all((self.tenant_id, self.run_id, self.actor_id, self.command_id, self.occurred_at)):
            raise ValueError("agent turn context requires tenant, run, actor, command, and timestamp")
        if self.starting_stream_version < 0:
            raise ValueError("starting stream version cannot be negative")


@dataclass(frozen=True)
class AgentTurnPlan:
    events: tuple[OrganizationEvent, ...]
    rejected_actions: tuple[Mapping[str, Any], ...]


def _event_id(command_id: str, position: int, kind: OrganizationEventKind) -> str:
    material = f"agent-os:organization-event:v1:{command_id}:{position}:{kind.value}"
    return "orgevt-" + hashlib.sha256(material.encode()).hexdigest()


def _items(output: Mapping[str, Any], key: str) -> tuple[Mapping[str, Any], ...]:
    raw = output.get(key, ())
    if not isinstance(raw, (list, tuple)) or any(not isinstance(item, Mapping) for item in raw):
        raise ValueError(f"agent turn {key} must be a list of objects")
    return tuple(raw)


def plan_agent_turn(
    context: AgentTurnContext,
    output: Mapping[str, Any],
    *,
    organization: Organization | None = None,
    history: Sequence[Mapping[str, Any]] = (),
) -> AgentTurnPlan:
    """Create an ordered event batch; external effects remain unexecuted."""

    summary = str(output.get("summary") or "").strip()
    disposition = str(output.get("disposition") or "").strip()
    progress = output.get("progress_percent")
    if not summary or disposition not in {
        "continue", "delegate", "wait_for_agent", "wait_for_human", "complete", "fail",
    }:
        raise ValueError("agent turn requires a summary and valid disposition")
    if not isinstance(progress, int) or isinstance(progress, bool) or not 0 <= progress <= 100:
        raise ValueError("agent turn progress_percent must be an integer from 0 to 100")
    evidence_ids = output.get("evidence_ids", ())
    if not isinstance(evidence_ids, (list, tuple)) or any(not str(item).strip() for item in evidence_ids):
        raise ValueError("agent turn evidence_ids must be a list of nonempty IDs")
    if disposition == "complete" and not evidence_ids:
        raise ValueError("agent completion requires evidence")

    drafts: list[tuple[OrganizationEventKind, Mapping[str, Any], str | None]] = [(
        OrganizationEventKind.AGENT_TURN_RECORDED,
        {
            "summary": summary,
            "disposition": disposition,
            "progress_percent": progress,
            "observations": list(output.get("observations", ())),
            "next_actions": list(output.get("next_actions", ())),
        },
        None,
    )]
    rejected: list[Mapping[str, Any]] = []
    pending_requests: dict[str, str] = {}
    answered_requests: set[str] = set()
    prior_decisions: dict[str, tuple[str, str]] = {}
    for prior in history:
        payload = prior.get("payload", {})
        if not isinstance(payload, Mapping):
            continue
        correlation = prior.get("correlation_id")
        if prior.get("kind") == OrganizationEventKind.MESSAGE_SENT.value and correlation:
            if payload.get("kind") == MessageKind.REQUEST.value and payload.get("requires_response"):
                pending_requests[str(correlation)] = str(prior.get("actor_id") or "")
            elif payload.get("kind") == MessageKind.RESPONSE.value:
                answered_requests.add(str(correlation))
        if prior.get("kind") in {
            OrganizationEventKind.DECISION_PROPOSED.value,
            OrganizationEventKind.DECISION_RECORDED.value,
        }:
            intent = str(payload.get("intent") or "").strip()
            choice = str(payload.get("chosen_option") or "").strip()
            if intent and choice:
                prior_decisions[intent] = (choice, str(prior.get("event_id") or ""))

    if evidence_ids:
        drafts.append((
            OrganizationEventKind.EVIDENCE_RECORDED,
            {"evidence_ids": list(evidence_ids), "summary": summary},
            None,
        ))
    drafts.append((
        OrganizationEventKind.WORK_PROGRESS_REPORTED,
        {"progress_percent": progress, "summary": summary, "disposition": disposition},
        None,
    ))

    risks = output.get("risks", ())
    if not isinstance(risks, (list, tuple)):
        raise ValueError("agent turn risks must be a list")
    for risk in risks:
        if str(risk).strip():
            drafts.append((OrganizationEventKind.RISK_RAISED, {"description": str(risk)}, None))

    for position, raw in enumerate(_items(output, "messages")):
        try:
            message = Message(
                message_id=f"proposal:{context.command_id}:{position}",
                conversation_id=str(raw.get("conversation_id") or context.run_id),
                sender_id=context.actor_id,
                recipient_ids=tuple(str(item) for item in raw.get("recipient_ids", ())),
                audience=Audience(str(raw["audience"])),
                kind=MessageKind(str(raw["kind"])),
                subject=str(raw["subject"]),
                body=str(raw["body"]),
                created_at=context.occurred_at,
                related_work_id=(
                    None if raw.get("related_work_id") is None
                    else str(raw["related_work_id"])
                ),
                requires_response=bool(raw.get("requires_response", False)),
                correlation_id=raw.get("correlation_id"),
            )
            routed = message.recipient_ids if organization is None else organization.route(message)
            if message.kind is MessageKind.REQUEST and message.requires_response:
                if (
                    message.correlation_id in pending_requests
                    and message.correlation_id not in answered_requests
                ):
                    raise ValueError("request correlation is already awaiting a response")
            if message.kind is MessageKind.RESPONSE:
                if not message.correlation_id:
                    raise ValueError("a response requires the request correlation_id")
                requester = pending_requests.get(message.correlation_id)
                if requester is None or message.correlation_id in answered_requests:
                    raise ValueError("response does not match one pending request")
                if requester and requester not in routed:
                    raise ValueError("response must route back to the original requester")
        except (KeyError, TypeError, ValueError) as exc:
            rejected.append({"action": "message", "position": position, "reason": str(exc)})
            continue
        drafts.append((OrganizationEventKind.MESSAGE_SENT, {
            "audience": message.audience.value,
            "kind": message.kind.value,
            "recipient_ids": list(routed),
            "subject": message.subject,
            "body": message.body,
            "requires_response": message.requires_response,
            "related_work_id": message.related_work_id,
            "protocol_version": "agent-os.communication.v1",
        }, message.correlation_id))
        if message.requires_response:
            drafts.append((OrganizationEventKind.PREREQUISITE_IDENTIFIED, {
                "kind": "human_response",
                "question": message.body,
                "requested_from": list(routed),
                "blocking": disposition == "wait_for_human",
            }, message.correlation_id))

    for raw in _items(output, "proposed_work"):
        drafts.append((OrganizationEventKind.WORK_PROPOSED, dict(raw), None))

    for raw in _items(output, "hiring_requests"):
        proposal_payload = {
            **dict(raw),
            "proposed_by": context.actor_id,
            # Creating employment/vendor obligations is consequential even if
            # the requesting agent has staffing authority.
            "needs_human_approval": True,
        }
        drafts.append((OrganizationEventKind.ORGANIZATION_CHANGE_PROPOSED, proposal_payload, None))
        drafts.append((OrganizationEventKind.APPROVAL_REQUESTED, {
            "action": "hire_or_engage",
            "proposal": proposal_payload,
            "reason": "staffing creates external authority, cost, or legal obligations",
        }, None))

    for raw in _items(output, "decisions"):
        decision = dict(raw)
        intent = str(decision.get("intent") or "").strip()
        choice = str(decision.get("chosen_option") or "").strip()
        conflict = prior_decisions.get(intent)
        if conflict is not None and conflict[0] != choice:
            decision["conflicts_with_event_id"] = conflict[1]
            decision["prior_chosen_option"] = conflict[0]
            decision["needs_human_approval"] = True
            drafts.append((OrganizationEventKind.RISK_RAISED, {
                "description": "A proposed decision conflicts with an earlier durable decision",
                "intent": intent,
                "prior_chosen_option": conflict[0],
                "proposed_chosen_option": choice,
                "conflicts_with_event_id": conflict[1],
            }, None))
        drafts.append((OrganizationEventKind.DECISION_PROPOSED, decision, None))
        needs_approval = (
            bool(decision.get("needs_human_approval"))
            or not bool(decision.get("reversible", False))
            or float(decision.get("confidence", 0)) < 0.65
        )
        if needs_approval:
            drafts.append((OrganizationEventKind.APPROVAL_REQUESTED, {
                "action": "approve_decision",
                "decision": decision,
                "reason": "explicit, irreversible, or low-confidence decision",
            }, None))

    if rejected:
        drafts.append((OrganizationEventKind.RISK_RAISED, {
            "description": "One or more proposed actions failed organization/policy validation",
            "rejected_actions": list(rejected),
        }, None))

    events = tuple(
        OrganizationEvent(
            event_id=_event_id(context.command_id, position, kind),
            tenant_id=context.tenant_id,
            run_id=context.run_id,
            actor_id=context.actor_id,
            kind=kind,
            expected_version=context.starting_stream_version + position,
            occurred_at=context.occurred_at,
            payload=payload,
            causation_id=context.command_id,
            correlation_id=correlation_id,
        )
        for position, (kind, payload, correlation_id) in enumerate(drafts)
    )
    return AgentTurnPlan(events, tuple(rejected))


def commit_agent_turn(
    ledger: OrganizationLedger,
    plan: AgentTurnPlan,
) -> tuple[OrganizationEventReceipt, ...]:
    """Append the plan in order; deterministic event IDs make replay safe."""

    return ledger.append_organization_events(plan.events)
