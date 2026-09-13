"""Durable facts emitted by the adaptive AI organization.

The product lifecycle is deliberately coarse.  This stream records the much
richer reality inside it: work, messages, decisions, staffing, approvals,
reviews, evidence, incidents, and human interaction.  Events are immutable and
tenant/run scoped so an agent cannot rewrite history or smuggle a customer ID
through an untrusted payload.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
from typing import Any, Mapping


class OrganizationEventKind(str, Enum):
    PROGRAM_ADMITTED = "program_admitted"
    MISSION_CHARTERED = "mission_chartered"
    RESOURCE_INVENTORY_UPDATED = "resource_inventory_updated"
    PREREQUISITE_IDENTIFIED = "prerequisite_identified"
    PREREQUISITE_RESOLVED = "prerequisite_resolved"
    TEAM_CHANGED = "team_changed"
    AGENT_CHANGED = "agent_changed"
    HUMAN_CHANGED = "human_changed"
    SERVICE_CHANGED = "service_changed"
    COPILOT_GRANT_CHANGED = "copilot_grant_changed"
    WORK_PROPOSED = "work_proposed"
    WORK_ASSIGNED = "work_assigned"
    WORK_PROGRESS_REPORTED = "work_progress_reported"
    WORK_WAITING = "work_waiting"
    WORK_REVIEWED = "work_reviewed"
    WORK_COMPLETED = "work_completed"
    MESSAGE_SENT = "message_sent"
    DECISION_PROPOSED = "decision_proposed"
    DECISION_RECORDED = "decision_recorded"
    ORGANIZATION_CHANGE_PROPOSED = "organization_change_proposed"
    ORGANIZATION_CHANGE_APPLIED = "organization_change_applied"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RESOLVED = "approval_resolved"
    EVIDENCE_RECORDED = "evidence_recorded"
    RISK_RAISED = "risk_raised"
    INCIDENT_RAISED = "incident_raised"
    ESCALATION_RAISED = "escalation_raised"
    AGENT_TURN_RECORDED = "agent_turn_recorded"


@dataclass(frozen=True)
class OrganizationEvent:
    event_id: str
    tenant_id: str
    run_id: str
    actor_id: str
    kind: OrganizationEventKind
    expected_version: int
    occurred_at: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    causation_id: str | None = None
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        if not all((self.event_id.strip(), self.tenant_id.strip(), self.run_id.strip(),
                    self.actor_id.strip(), self.occurred_at.strip())):
            raise ValueError("organization event identity, actor, and timestamp are required")
        if self.expected_version < 0:
            raise ValueError("organization event expected_version cannot be negative")
        # Canonicalization here fails before an adapter or database transaction.
        organization_event_fingerprint(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "tenant_id": self.tenant_id,
            "run_id": self.run_id,
            "actor_id": self.actor_id,
            "kind": self.kind.value,
            "expected_version": self.expected_version,
            "occurred_at": self.occurred_at,
            "payload": dict(self.payload),
            "causation_id": self.causation_id,
            "correlation_id": self.correlation_id,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "OrganizationEvent":
        payload = raw.get("payload", {})
        if not isinstance(payload, Mapping):
            raise ValueError("organization event payload must be an object")
        return cls(
            event_id=str(raw["event_id"]),
            tenant_id=str(raw["tenant_id"]),
            run_id=str(raw["run_id"]),
            actor_id=str(raw["actor_id"]),
            kind=OrganizationEventKind(str(raw["kind"])),
            expected_version=int(raw["expected_version"]),
            occurred_at=str(raw["occurred_at"]),
            payload=dict(payload),
            causation_id=raw.get("causation_id"),
            correlation_id=raw.get("correlation_id"),
        )


def organization_event_fingerprint(event: OrganizationEvent) -> str:
    try:
        encoded = json.dumps(
            event.to_dict() if hasattr(event, "to_dict") else event,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("organization event payload must contain JSON-compatible primitives") from exc
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
