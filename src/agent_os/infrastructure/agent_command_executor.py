"""Execute lifecycle commands through an agent runtime and durable org ledger."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from agent_os.application.agent_turn import AgentTurnContext, commit_agent_turn, plan_agent_turn
from agent_os.application.lifecycle import CommandEnvelope
from agent_os.application.ports import AgentRuntime, CommandExecutor, OrganizationLedger
from agent_os.domain.lifecycle import CommandKind
from agent_os.domain.organization import Organization


_ROLES: dict[CommandKind, str] = {
    CommandKind.START_RESEARCH: "research-lead",
    CommandKind.START_SPECIFICATION: "product-architect",
    CommandKind.START_BUILD: "engineering-manager",
    CommandKind.START_VERIFICATION: "quality-manager",
    CommandKind.START_REPAIR: "repair-lead",
    CommandKind.START_RELEASE: "release-manager",
    CommandKind.RESUME_PHASE: "mission-manager",
}


class DurableAgentCommandExecutor(CommandExecutor):
    """Keeps a model turn auditable without granting the model direct authority."""

    def __init__(
        self,
        *,
        runtime: AgentRuntime,
        ledger: OrganizationLedger,
        organization_loader: Callable[[str, str], Organization | None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._runtime = runtime
        self._ledger = ledger
        self._organization_loader = organization_loader
        self._clock = clock or (lambda: datetime.now(timezone.utc))

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
            }
        result = self._runtime.run_agent(
            organization_id=item.organization_id,
            run_id=item.run_id,
            role=role,
            prompt=prompt,
            idempotency_key=item.command_id,
        )
        output = result.get("output")
        if not isinstance(output, Mapping):
            raise ValueError("agent runtime result must contain structured output")
        version = 0 if not history else int(history[-1]["stream_version"])
        occurred_at = self._clock().isoformat()
        organization = None
        if self._organization_loader is not None:
            organization = self._organization_loader(item.organization_id, item.run_id)
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
            "agent_turn": dict(result),
            "organization_event_versions": [receipt.stream_version for receipt in receipts],
            "rejected_actions": list(plan.rejected_actions),
        }
