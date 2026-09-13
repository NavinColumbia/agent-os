"""Durably project graph-agent proposals into the organization fact stream."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from agent_os.application.agent_turn import AgentTurnContext, commit_agent_turn, plan_agent_turn
from agent_os.application.command_worker import FatalCommandError
from agent_os.application.ports import CompanyDirectory, OrganizationLedger
from agent_os.domain.organization import AgentProfile, Organization
from agent_os.domain.organization_events import OrganizationEvent, OrganizationEventKind
from agent_os.domain.workflow_runtime import WorkflowAction, WorkflowActionKind


class GraphOrganizationEffectHandler:
    """Record one already-durable graph result without repeating the model call."""

    def __init__(
        self,
        ledger: OrganizationLedger,
        company_directory: CompanyDirectory | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._ledger = ledger
        self._company = company_directory
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def handlers(self):
        return {
            WorkflowActionKind.RECORD_ORGANIZATION: self.execute,
            WorkflowActionKind.RECORD_PROGRAM: self.record_program,
        }

    def _history(self, tenant_id: str, run_id: str) -> tuple[Mapping[str, Any], ...]:
        events: list[Mapping[str, Any]] = []
        after = 0
        while True:
            page = self._ledger.load_organization_events(
                tenant_id, run_id, after_version=after, limit=500,
            )
            events.extend(page)
            if len(page) < 500:
                return tuple(events)
            next_after = int(page[-1]["stream_version"])
            if next_after <= after:
                raise FatalCommandError("organization history pagination did not advance")
            after = next_after

    def execute(
        self, envelope: Mapping[str, Any], action: WorkflowAction,
    ) -> Mapping[str, Any]:
        if action.kind is not WorkflowActionKind.RECORD_ORGANIZATION:
            raise FatalCommandError("organization effect received the wrong action kind")
        tenant_id = str(envelope.get("tenant_id") or "")
        graph_run_id = str(envelope.get("run_id") or "")
        organization_run_id = str(action.payload.get("organization_run_id") or graph_run_id)
        role = str(action.payload.get("actor_role") or "mission-manager").strip()
        actions = action.payload.get("organization_actions")
        if not tenant_id or not graph_run_id or not organization_run_id or not role:
            raise FatalCommandError("organization effect is missing tenant, run, or actor identity")
        if not isinstance(actions, Mapping):
            raise FatalCommandError("organization effect payload is malformed")

        history = self._history(tenant_id, organization_run_id)
        prior = [item for item in history if item.get("causation_id") == action.action_id]
        if prior:
            return {
                "recorded": True,
                "duplicate": True,
                "graph_run_id": graph_run_id,
                "organization_run_id": organization_run_id,
                "event_versions": [int(item["stream_version"]) for item in prior],
            }

        evidence_ids = action.payload.get("evidence_ids", ())
        if not isinstance(evidence_ids, (list, tuple)):
            raise FatalCommandError("organization effect evidence must be a list")
        output = {
            "summary": str(action.payload.get("summary") or "Completed graph work."),
            "disposition": "complete",
            "progress_percent": 100,
            "evidence_ids": [str(item) for item in evidence_ids if str(item)],
            "observations": list(actions.get("observations", ())),
            "risks": list(actions.get("risks", ())),
            "messages": list(actions.get("messages", ())),
            "proposed_work": list(actions.get("proposed_work", ())),
            "hiring_requests": list(actions.get("hiring_requests", ())),
            "decisions": list(actions.get("decisions", ())),
            "next_actions": list(actions.get("next_actions", ())),
        }
        if not output["evidence_ids"]:
            raise FatalCommandError("completed graph organization effect requires evidence")
        organization = (
            None if self._company is None else self._company.get_organization(tenant_id)
        )
        actor_id = role if role.startswith("agent:") else f"agent:{role}"
        if organization is not None and actor_id not in organization.agents:
            # Workflow roles are mission-scoped by design; they need not be
            # promoted into the standing company directory merely to speak.
            # Add a read-only routing identity under the durable mission manager
            # for this turn. This grants no standing tools, spend, or hiring
            # authority and is never persisted as a company hire.
            manager = organization.agents.get("agent:mission-manager")
            if manager is None:
                raise FatalCommandError("standing organization has no mission manager")
            scoped = AgentProfile(
                agent_id=actor_id,
                role=role.removeprefix("agent:"),
                team_id=manager.team_id,
                manager_id=manager.agent_id,
            )
            organization = Organization(
                organization.tenant_id,
                organization.organization_id,
                organization.name,
                organization.teams,
                {**dict(organization.agents), actor_id: scoped},
                organization.humans,
                organization.services,
            )
        context = AgentTurnContext(
            tenant_id=tenant_id,
            run_id=organization_run_id,
            actor_id=actor_id,
            command_id=action.action_id,
            occurred_at=self._clock().isoformat(),
            starting_stream_version=(
                0 if not history else int(history[-1]["stream_version"])
            ),
        )
        try:
            plan = plan_agent_turn(context, output, organization=organization)
            receipts = commit_agent_turn(self._ledger, plan)
        except (LookupError, TypeError, ValueError) as exc:
            # This outbox action remains retryable. If a concurrent writer won,
            # the next attempt recomputes the stream version and never repeats
            # the provider/model call that produced the durable token output.
            raise RuntimeError(f"organization effect could not commit: {exc}") from exc
        return {
            "recorded": True,
            "duplicate": False,
            "graph_run_id": graph_run_id,
            "organization_run_id": organization_run_id,
            "event_versions": [receipt.stream_version for receipt in receipts],
            "rejected_actions": list(plan.rejected_actions),
        }

    def record_program(
        self, envelope: Mapping[str, Any], action: WorkflowAction,
    ) -> Mapping[str, Any]:
        if action.kind is not WorkflowActionKind.RECORD_PROGRAM:
            raise FatalCommandError("program effect received the wrong action kind")
        tenant_id = str(envelope.get("tenant_id") or "")
        graph_run_id = str(envelope.get("run_id") or "")
        organization_run_id = str(action.payload.get("organization_run_id") or graph_run_id)
        role = str(action.payload.get("actor_role") or "mission-architect").strip()
        program = action.payload.get("program")
        artifact_id = str(action.payload.get("program_artifact_id") or "")
        if (
            not tenant_id or not graph_run_id or not organization_run_id or not role
            or not artifact_id or not isinstance(program, Mapping)
        ):
            raise FatalCommandError("program effect payload is malformed")
        history = self._history(tenant_id, organization_run_id)
        prior = [item for item in history if item.get("causation_id") == action.action_id]
        if prior:
            return {
                "recorded": True,
                "duplicate": True,
                "program_revision": program.get("revision"),
                "event_version": int(prior[0]["stream_version"]),
            }
        actor_id = role if role.startswith("agent:") else f"agent:{role}"
        event = OrganizationEvent(
            event_id=f"program-admitted:{action.action_id}",
            tenant_id=tenant_id,
            run_id=organization_run_id,
            actor_id=actor_id,
            kind=OrganizationEventKind.PROGRAM_ADMITTED,
            expected_version=0 if not history else int(history[-1]["stream_version"]),
            occurred_at=self._clock().isoformat(),
            payload={
                "program": dict(program),
                "program_artifact_id": artifact_id,
                "graph_run_id": graph_run_id,
                "program_revision": program.get("revision"),
            },
            causation_id=action.action_id,
            correlation_id=graph_run_id,
        )
        try:
            receipt = self._ledger.append_organization_event(event)
        except (LookupError, TypeError, ValueError) as exc:
            raise RuntimeError(f"program effect could not commit: {exc}") from exc
        return {
            "recorded": True,
            "duplicate": receipt.duplicate,
            "program_revision": program.get("revision"),
            "event_version": receipt.stream_version,
        }
