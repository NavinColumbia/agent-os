"""Execute lifecycle commands through an agent runtime and durable org ledger."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from typing import Any, Callable, Mapping

from agent_os.application.agent_turn import AgentTurnContext, commit_agent_turn, plan_agent_turn
from agent_os.application.lifecycle import CommandEnvelope
from agent_os.application.ports import ArtifactStore, AgentRuntime, CommandExecutor, OrganizationLedger
from agent_os.domain.lifecycle import CommandKind
from agent_os.domain.organization import Organization
from agent_os.infrastructure.proposed_artifacts import persist_and_validate_artifacts


_ROLES: dict[CommandKind, str] = {
    CommandKind.START_RESEARCH: "research-lead",
    CommandKind.START_SPECIFICATION: "product-architect",
    CommandKind.START_BUILD: "engineering-manager",
    CommandKind.START_VERIFICATION: "quality-manager",
    CommandKind.START_REPAIR: "repair-lead",
    CommandKind.START_RELEASE: "release-manager",
    CommandKind.RESUME_PHASE: "mission-manager",
}

_COMPLETION_EVENTS: dict[CommandKind, str] = {
    CommandKind.START_RESEARCH: "research_completed",
    CommandKind.START_SPECIFICATION: "specification_approved",
    CommandKind.START_BUILD: "build_completed",
    CommandKind.START_VERIFICATION: "verification_passed",
    CommandKind.START_REPAIR: "repair_completed",
    CommandKind.START_RELEASE: "release_completed",
}

AGENT_COMMAND_KINDS = frozenset(_ROLES)


def _organization_context(organization: Organization | None) -> Mapping[str, Any] | None:
    if organization is None:
        return None
    return {
        "organization": {
            "id": organization.organization_id,
            "name": organization.name,
        },
        "teams": [{
            "id": team.team_id,
            "name": team.name,
            "purpose": team.purpose,
            "manager_id": team.manager_id,
        } for team in organization.teams.values()],
        "agents": [{
            "id": agent.agent_id,
            "role": agent.role,
            "team_id": agent.team_id,
            "manager_id": agent.manager_id,
            "capabilities": sorted(agent.capabilities),
            "tool_grants": sorted(agent.tool_grants),
            "hiring_authority": agent.hiring_authority,
            "spending_limit_cents": agent.spending_limit_cents,
            "status": agent.status.value,
        } for agent in organization.agents.values()],
        "humans": [{
            "id": human.participant_id,
            "display_name": human.display_name,
            "team_id": human.team_id,
            "responsibilities": list(human.responsibilities),
            "response_sla_seconds": human.response_sla_seconds,
            "active": human.active,
        } for human in organization.humans.values()],
        "services": [{
            "id": service.participant_id,
            "name": service.name,
            "capabilities": sorted(service.capabilities),
            "owner_id": service.owner_id,
            "active": service.active,
        } for service in organization.services.values()],
    }


def _followup_event(item: CommandEnvelope, output: Mapping[str, Any]) -> Mapping[str, Any] | None:
    disposition = str(output.get("disposition") or "")
    summary = str(output.get("summary") or "agent operation failed")
    evidence = [str(value) for value in output.get("evidence_ids", ())]
    payload: dict[str, Any]
    if disposition == "complete":
        kind = _COMPLETION_EVENTS.get(item.command.kind)
        if kind is None:
            return None
        payload = {"evidence_ids": evidence, "summary": summary}
        if kind == "research_completed":
            payload["report_id"] = evidence[0]
        elif kind == "specification_approved":
            payload["specification_id"] = evidence[0]
        elif kind in {"build_completed", "repair_completed"}:
            payload["artifact_revision"] = evidence[0]
        elif kind == "release_completed":
            payload["deployment_id"] = evidence[0]
    elif disposition == "wait_for_human":
        messages = output.get("messages", ())
        request = next((
            message for message in messages
            if isinstance(message, Mapping) and message.get("requires_response")
        ), None)
        if request is None:
            raise ValueError("wait_for_human requires a correlated response message")
        kind = "wait_requested"
        payload = {
            "wait_kind": "human",
            "correlation_id": str(request.get("correlation_id") or ""),
            "reason": str(request.get("body") or summary),
            "resume_command": item.command.kind.value,
        }
    elif disposition == "fail":
        kind = "operation_failed"
        payload = {
            "operation": item.command.kind.value,
            "reason": summary,
            "recoverable": True,
            "retryable": False,
        }
    else:
        # Continue/delegate/wait-for-agent is progress inside the organization
        # graph. It must not falsely advance the customer-visible phase.
        return None
    return {
        "event_id": "command-result-" + hashlib.sha256(item.command_id.encode()).hexdigest(),
        "kind": kind,
        "expected_version": item.aggregate_version,
        "payload": payload,
    }


def _output_from_recorded_events(events: list[Mapping[str, Any]]) -> Mapping[str, Any]:
    turn = next(event for event in events if event.get("kind") == "agent_turn_recorded")
    evidence = next((
        event for event in events if event.get("kind") == "evidence_recorded"
    ), None)
    messages = [{
        **dict(event.get("payload", {})),
        "correlation_id": event.get("correlation_id"),
    } for event in events if event.get("kind") == "message_sent"]
    return {
        **dict(turn.get("payload", {})),
        "evidence_ids": [] if evidence is None else list(evidence["payload"].get("evidence_ids", ())),
        "messages": messages,
    }


class DurableAgentCommandExecutor(CommandExecutor):
    """Keeps a model turn auditable without granting the model direct authority."""

    def __init__(
        self,
        *,
        runtime: AgentRuntime,
        ledger: OrganizationLedger,
        organization_loader: Callable[[str, str], Organization | None] | None = None,
        artifact_store: ArtifactStore | None = None,
        clock: Callable[[], datetime] | None = None,
        max_turn_budget_cents: int = 100,
    ) -> None:
        if max_turn_budget_cents < 1:
            raise ValueError("max_turn_budget_cents must be positive")
        self._runtime = runtime
        self._ledger = ledger
        self._organization_loader = organization_loader
        self._artifact_store = artifact_store
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._max_turn_budget_cents = max_turn_budget_cents

    @staticmethod
    def supports(kind: CommandKind) -> bool:
        return kind in AGENT_COMMAND_KINDS

    def execute(self, envelope: Mapping[str, Any]) -> Mapping[str, Any]:
        item = CommandEnvelope.from_dict(envelope)
        role = _ROLES.get(item.command.kind)
        if role is None:
            raise ValueError(f"command {item.command.kind.value} is not an agent role command")
        prompt = str(
            item.command.payload.get("prompt")
            or item.command.payload.get("objective")
            or f"Advance mission phase by executing {item.command.kind.value}."
        )
        history = self._ledger.load_organization_events(item.organization_id, item.run_id)
        caused = [event for event in history if event.get("causation_id") == item.command_id]
        if caused:
            # The durable turn already committed; a prior worker died before it
            # acknowledged the outbox. Never pay for or side-effect a second
            # model turn merely to reconstruct that acknowledgement.
            return {
                "agent_turn": {"replayed": True, "idempotency_key": item.command_id},
                "organization_event_versions": [int(event["stream_version"]) for event in caused],
                "rejected_actions": [],
                "lifecycle_event": _followup_event(item, _output_from_recorded_events(caused)),
            }
        version = 0 if not history else int(history[-1]["stream_version"])
        occurred_at = self._clock().isoformat()
        organization = None
        if self._organization_loader is not None:
            organization = self._organization_loader(item.organization_id, item.run_id)
        requested_budget = item.command.payload.get("budget_cents", self._max_turn_budget_cents)
        if isinstance(requested_budget, bool):
            requested_budget = self._max_turn_budget_cents
        try:
            turn_budget = int(requested_budget)
        except (TypeError, ValueError):
            turn_budget = self._max_turn_budget_cents
        turn_budget = min(self._max_turn_budget_cents, max(1, turn_budget))
        result = self._runtime.run_agent(
            organization_id=item.organization_id,
            run_id=item.run_id,
            role=role,
            prompt=prompt,
            idempotency_key=item.command_id,
            budget_cents=turn_budget,
            context=_organization_context(organization),
        )
        raw_output = result.get("output")
        if not isinstance(raw_output, Mapping):
            raise ValueError("agent runtime result must contain structured output")
        allowed_evidence = {
            str(evidence_id)
            for event in history
            if event.get("kind") == "evidence_recorded"
            for evidence_id in event.get("payload", {}).get("evidence_ids", ())
        }
        output = persist_and_validate_artifacts(
            store=self._artifact_store,
            organization_id=item.organization_id,
            idempotency_key=item.command_id,
            output=raw_output,
            allowed_evidence_ids=allowed_evidence,
        )
        recorded_result = {**dict(result), "output": dict(output)}
        context = AgentTurnContext(
            tenant_id=item.organization_id,
            run_id=item.run_id,
            actor_id=f"agent:{role}",
            command_id=item.command_id,
            occurred_at=occurred_at,
            starting_stream_version=version,
        )
        plan = plan_agent_turn(context, output, organization=organization)
        receipts = commit_agent_turn(self._ledger, plan)
        return {
            "agent_turn": recorded_result,
            "organization_event_versions": [receipt.stream_version for receipt in receipts],
            "rejected_actions": list(plan.rejected_actions),
            "lifecycle_event": _followup_event(item, output),
        }
