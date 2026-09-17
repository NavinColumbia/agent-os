"""Canonical intent, provenance, authority, and effect contracts.

The workflow runtime answers *how* durable work is executed.  This module owns
the framework-neutral facts that explain *why* work exists, which effects are
authorized, and what evidence supports a claim.  The records deliberately do
not contain raw evidence payloads or credentials; those belong in retention-
controlled artifact and secret stores.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from fnmatch import fnmatchcase
import hashlib
import json
from typing import Any, Mapping


def _required(value: str, label: str, *, maximum: int = 4_000) -> str:
    value = value.strip()
    if not value or len(value) > maximum:
        raise ValueError(f"{label} must contain 1 to {maximum} characters")
    return value


def _utc(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def canonical_fingerprint(raw: Mapping[str, Any]) -> str:
    """Return a stable digest for JSON-safe authority and evidence records."""

    try:
        encoded = json.dumps(
            raw, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("mission records must contain JSON-compatible primitives") from exc
    return hashlib.sha256(encoded.encode()).hexdigest()


class ClaimStatus(str, Enum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    DISPUTED = "disputed"
    SUPERSEDED = "superseded"
    INVALIDATED = "invalidated"


class EvidenceKind(str, Enum):
    OBSERVATION = "observation"
    TEST = "test"
    ARTIFACT = "artifact"
    APPROVAL = "approval"
    DECISION = "decision"
    EXTERNAL_SOURCE = "external_source"


class HazardSeverity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class EffectRisk(str, Enum):
    READ_ONLY = "read_only"
    REVERSIBLE = "reversible"
    CONSEQUENTIAL = "consequential"
    IRREVERSIBLE = "irreversible"


class SafeMode(str, Enum):
    READ_ONLY = "read_only"
    DRAFT_ONLY = "draft_only"
    FREEZE = "freeze"
    ROLLBACK = "rollback"
    HUMAN_REVIEW = "human_review"
    BASELINE = "baseline"


class AssuranceDisposition(str, Enum):
    ALLOWED = "allowed"
    RESTRICTED = "restricted"
    HUMAN_REQUIRED = "human_required"
    DENIED = "denied"


@dataclass(frozen=True)
class MissionSpec:
    mission_id: str
    tenant_id: str
    objective: str
    principal_id: str
    accountable_owner_id: str
    budget_limit_cents: int
    success_measures: tuple[str, ...]
    constraints: tuple[str, ...] = ()
    prohibited_effects: tuple[str, ...] = ()
    risk_tier: str = "moderate"
    human_involvement_mode: str = "balanced"
    daily_interrupt_limit: int = 8
    revision: int = 1
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    revised_at: str | None = None

    def __post_init__(self) -> None:
        for value, label, maximum in (
            (self.mission_id, "mission_id", 256),
            (self.tenant_id, "tenant_id", 128),
            (self.objective, "objective", 50_000),
            (self.principal_id, "principal_id", 256),
            (self.accountable_owner_id, "accountable_owner_id", 256),
        ):
            _required(value, label, maximum=maximum)
        if self.budget_limit_cents < 0 or self.revision < 1:
            raise ValueError("mission budget cannot be negative and revision must be positive")
        if not self.success_measures or any(not item.strip() for item in self.success_measures):
            raise ValueError("mission requires explicit success measures")
        if self.risk_tier not in {"low", "moderate", "high", "critical"}:
            raise ValueError("mission risk_tier must be low, moderate, high, or critical")
        if self.human_involvement_mode not in {"autonomous", "balanced", "collaborative"}:
            raise ValueError("human involvement mode is invalid")
        if not 0 <= self.daily_interrupt_limit <= 100:
            raise ValueError("daily interruption limit must be between zero and 100")
        created = _utc(self.created_at, "created_at")
        revised = created if self.revised_at is None else _utc(self.revised_at, "revised_at")
        if revised < created:
            raise ValueError("mission revised_at cannot precede created_at")

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "tenant_id": self.tenant_id,
            "objective": self.objective,
            "principal_id": self.principal_id,
            "accountable_owner_id": self.accountable_owner_id,
            "budget_limit_cents": self.budget_limit_cents,
            "success_measures": list(self.success_measures),
            "constraints": list(self.constraints),
            "prohibited_effects": list(self.prohibited_effects),
            "risk_tier": self.risk_tier,
            "human_involvement_mode": self.human_involvement_mode,
            "daily_interrupt_limit": self.daily_interrupt_limit,
            "revision": self.revision,
            "created_at": self.created_at,
            "revised_at": self.created_at if self.revised_at is None else self.revised_at,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "MissionSpec":
        return cls(
            mission_id=str(raw["mission_id"]),
            tenant_id=str(raw["tenant_id"]),
            objective=str(raw["objective"]),
            principal_id=str(raw["principal_id"]),
            accountable_owner_id=str(raw["accountable_owner_id"]),
            budget_limit_cents=int(raw["budget_limit_cents"]),
            success_measures=tuple(str(item) for item in raw.get("success_measures", ())),
            constraints=tuple(str(item) for item in raw.get("constraints", ())),
            prohibited_effects=tuple(str(item) for item in raw.get("prohibited_effects", ())),
            risk_tier=str(raw.get("risk_tier", "moderate")),
            human_involvement_mode=str(raw.get("human_involvement_mode", "balanced")),
            daily_interrupt_limit=int(raw.get("daily_interrupt_limit", 8)),
            revision=int(raw.get("revision", 1)),
            created_at=str(raw["created_at"]),
            revised_at=str(raw.get("revised_at") or raw["created_at"]),
        )


@dataclass(frozen=True)
class Claim:
    claim_id: str
    mission_id: str
    statement: str
    status: ClaimStatus
    asserted_by: str
    valid_from: str
    recorded_at: str
    evidence_ids: tuple[str, ...] = ()
    depends_on_claim_ids: tuple[str, ...] = ()
    valid_to: str | None = None
    supersedes_claim_id: str | None = None

    def __post_init__(self) -> None:
        for value, label in (
            (self.claim_id, "claim_id"), (self.mission_id, "mission_id"),
            (self.statement, "statement"), (self.asserted_by, "asserted_by"),
        ):
            _required(value, label, maximum=8_000 if label == "statement" else 256)
        start = _utc(self.valid_from, "valid_from")
        _utc(self.recorded_at, "recorded_at")
        if self.valid_to is not None and _utc(self.valid_to, "valid_to") <= start:
            raise ValueError("claim valid_to must be after valid_from")
        if self.claim_id in self.depends_on_claim_ids:
            raise ValueError("claim cannot depend on itself")

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "mission_id": self.mission_id,
            "statement": self.statement,
            "status": self.status.value,
            "asserted_by": self.asserted_by,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "recorded_at": self.recorded_at,
            "evidence_ids": list(self.evidence_ids),
            "depends_on_claim_ids": list(self.depends_on_claim_ids),
            "supersedes_claim_id": self.supersedes_claim_id,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Claim":
        return cls(
            claim_id=str(raw["claim_id"]), mission_id=str(raw["mission_id"]),
            statement=str(raw["statement"]), status=ClaimStatus(str(raw["status"])),
            asserted_by=str(raw["asserted_by"]), valid_from=str(raw["valid_from"]),
            valid_to=None if raw.get("valid_to") is None else str(raw["valid_to"]),
            recorded_at=str(raw["recorded_at"]),
            evidence_ids=tuple(str(item) for item in raw.get("evidence_ids", ())),
            depends_on_claim_ids=tuple(
                str(item) for item in raw.get("depends_on_claim_ids", ())
            ),
            supersedes_claim_id=(
                None if raw.get("supersedes_claim_id") is None
                else str(raw["supersedes_claim_id"])
            ),
        )


@dataclass(frozen=True)
class EvidenceRef:
    evidence_id: str
    mission_id: str
    kind: EvidenceKind
    artifact_ref: str
    sha256: str
    produced_by: str
    observed_at: str
    recorded_at: str
    media_type: str = "application/octet-stream"
    contains_personal_data: bool = False
    retention_until: str | None = None
    source_uri: str | None = None

    def __post_init__(self) -> None:
        for value, label in (
            (self.evidence_id, "evidence_id"), (self.mission_id, "mission_id"),
            (self.artifact_ref, "artifact_ref"), (self.produced_by, "produced_by"),
            (self.media_type, "media_type"),
        ):
            _required(value, label, maximum=2_000 if label == "artifact_ref" else 256)
        if self.artifact_ref.startswith(("data:", "inline:")):
            raise ValueError("evidence ledger accepts references, not inline payloads")
        if len(self.sha256) != 64 or any(ch not in "0123456789abcdef" for ch in self.sha256):
            raise ValueError("evidence sha256 must be a lowercase hexadecimal digest")
        _utc(self.observed_at, "observed_at")
        recorded = _utc(self.recorded_at, "recorded_at")
        if self.contains_personal_data and self.retention_until is None:
            raise ValueError("personal-data evidence requires an explicit retention deadline")
        if self.retention_until is not None and _utc(
            self.retention_until, "retention_until"
        ) <= recorded:
            raise ValueError("evidence retention deadline must be after it was recorded")

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "mission_id": self.mission_id,
            "kind": self.kind.value,
            "artifact_ref": self.artifact_ref,
            "sha256": self.sha256,
            "produced_by": self.produced_by,
            "observed_at": self.observed_at,
            "recorded_at": self.recorded_at,
            "media_type": self.media_type,
            "contains_personal_data": self.contains_personal_data,
            "retention_until": self.retention_until,
            "source_uri": self.source_uri,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "EvidenceRef":
        return cls(
            evidence_id=str(raw["evidence_id"]), mission_id=str(raw["mission_id"]),
            kind=EvidenceKind(str(raw["kind"])), artifact_ref=str(raw["artifact_ref"]),
            sha256=str(raw["sha256"]), produced_by=str(raw["produced_by"]),
            observed_at=str(raw["observed_at"]), recorded_at=str(raw["recorded_at"]),
            media_type=str(raw.get("media_type", "application/octet-stream")),
            contains_personal_data=bool(raw.get("contains_personal_data", False)),
            retention_until=(
                None if raw.get("retention_until") is None else str(raw["retention_until"])
            ),
            source_uri=None if raw.get("source_uri") is None else str(raw["source_uri"]),
        )


@dataclass(frozen=True)
class Hazard:
    hazard_id: str
    mission_id: str
    description: str
    unacceptable_loss: str
    severity: HazardSeverity
    safety_constraints: tuple[str, ...]
    unsafe_control_actions: tuple[str, ...]
    fallback_mode: SafeMode
    requires_human_release: bool = False

    def __post_init__(self) -> None:
        for value, label in (
            (self.hazard_id, "hazard_id"), (self.mission_id, "mission_id"),
            (self.description, "description"), (self.unacceptable_loss, "unacceptable_loss"),
        ):
            _required(value, label, maximum=4_000)
        if not self.safety_constraints or not self.unsafe_control_actions:
            raise ValueError("hazards require safety constraints and unsafe control actions")

    def to_dict(self) -> dict[str, Any]:
        return {
            "hazard_id": self.hazard_id, "mission_id": self.mission_id,
            "description": self.description, "unacceptable_loss": self.unacceptable_loss,
            "severity": self.severity.value,
            "safety_constraints": list(self.safety_constraints),
            "unsafe_control_actions": list(self.unsafe_control_actions),
            "fallback_mode": self.fallback_mode.value,
            "requires_human_release": self.requires_human_release,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Hazard":
        return cls(
            hazard_id=str(raw["hazard_id"]), mission_id=str(raw["mission_id"]),
            description=str(raw["description"]),
            unacceptable_loss=str(raw["unacceptable_loss"]),
            severity=HazardSeverity(str(raw["severity"])),
            safety_constraints=tuple(str(item) for item in raw.get("safety_constraints", ())),
            unsafe_control_actions=tuple(
                str(item) for item in raw.get("unsafe_control_actions", ())
            ),
            fallback_mode=SafeMode(str(raw["fallback_mode"])),
            requires_human_release=bool(raw.get("requires_human_release", False)),
        )


@dataclass(frozen=True)
class AuthorityGrant:
    grant_id: str
    tenant_id: str
    mission_id: str
    principal_id: str
    delegate_id: str
    allowed_effects: tuple[str, ...]
    allowed_resources: tuple[str, ...]
    budget_limit_cents: int
    valid_from: str
    expires_at: str
    delegation_chain: tuple[str, ...]
    mission_revision: int = 1
    parent_grant_id: str | None = None
    human_approved: bool = False
    approval_binding_sha256: str | None = None
    approval_evidence_ids: tuple[str, ...] = ()
    policy_version: str = "builtin-v1"

    def __post_init__(self) -> None:
        for value, label in (
            (self.grant_id, "grant_id"), (self.tenant_id, "tenant_id"),
            (self.mission_id, "mission_id"), (self.principal_id, "principal_id"),
            (self.delegate_id, "delegate_id"), (self.policy_version, "policy_version"),
        ):
            _required(value, label, maximum=256)
        if not self.allowed_effects or not self.allowed_resources:
            raise ValueError("authority requires explicit effect and resource scopes")
        if self.budget_limit_cents < 0:
            raise ValueError("authority budget cannot be negative")
        if self.mission_revision < 1:
            raise ValueError("authority mission revision must be positive")
        if _utc(self.expires_at, "expires_at") <= _utc(self.valid_from, "valid_from"):
            raise ValueError("authority expiry must be after its start")
        if not self.delegation_chain or self.delegation_chain[0] != self.principal_id:
            raise ValueError("delegation chain must begin with the principal")
        if self.delegation_chain[-1] != self.delegate_id:
            raise ValueError("delegation chain must end with the delegate")
        if len(set(self.delegation_chain)) != len(self.delegation_chain):
            raise ValueError("delegation chain cannot contain a cycle")
        if self.human_approved:
            if (
                self.approval_binding_sha256 is None
                or len(self.approval_binding_sha256) != 64
                or any(
                    ch not in "0123456789abcdef"
                    for ch in self.approval_binding_sha256
                )
                or not self.approval_evidence_ids
            ):
                raise ValueError(
                    "human-approved authority requires an effect-bound digest and evidence"
                )
        elif self.approval_binding_sha256 is not None or self.approval_evidence_ids:
            raise ValueError("approval binding is valid only for human-approved authority")

    def permits(self, action: str, resource: str) -> bool:
        return any(fnmatchcase(action, scope) for scope in self.allowed_effects) and any(
            fnmatchcase(resource, scope) for scope in self.allowed_resources
        )

    def active_at(self, instant: datetime) -> bool:
        instant = instant.astimezone(timezone.utc)
        return _utc(self.valid_from, "valid_from") <= instant < _utc(
            self.expires_at, "expires_at"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "grant_id": self.grant_id, "tenant_id": self.tenant_id,
            "mission_id": self.mission_id, "principal_id": self.principal_id,
            "delegate_id": self.delegate_id, "allowed_effects": list(self.allowed_effects),
            "allowed_resources": list(self.allowed_resources),
            "budget_limit_cents": self.budget_limit_cents,
            "valid_from": self.valid_from, "expires_at": self.expires_at,
            "delegation_chain": list(self.delegation_chain),
            "mission_revision": self.mission_revision,
            "parent_grant_id": self.parent_grant_id,
            "human_approved": self.human_approved,
            "approval_binding_sha256": self.approval_binding_sha256,
            "approval_evidence_ids": list(self.approval_evidence_ids),
            "policy_version": self.policy_version,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AuthorityGrant":
        return cls(
            grant_id=str(raw["grant_id"]), tenant_id=str(raw["tenant_id"]),
            mission_id=str(raw["mission_id"]), principal_id=str(raw["principal_id"]),
            delegate_id=str(raw["delegate_id"]),
            allowed_effects=tuple(str(item) for item in raw.get("allowed_effects", ())),
            allowed_resources=tuple(str(item) for item in raw.get("allowed_resources", ())),
            budget_limit_cents=int(raw["budget_limit_cents"]),
            valid_from=str(raw["valid_from"]), expires_at=str(raw["expires_at"]),
            delegation_chain=tuple(str(item) for item in raw.get("delegation_chain", ())),
            mission_revision=int(raw.get("mission_revision", 1)),
            parent_grant_id=(
                None if raw.get("parent_grant_id") is None else str(raw["parent_grant_id"])
            ),
            human_approved=bool(raw.get("human_approved", False)),
            approval_binding_sha256=(
                None if raw.get("approval_binding_sha256") is None
                else str(raw["approval_binding_sha256"])
            ),
            approval_evidence_ids=tuple(
                str(item) for item in raw.get("approval_evidence_ids", ())
            ),
            policy_version=str(raw.get("policy_version", "builtin-v1")),
        )


@dataclass(frozen=True)
class EffectRequest:
    effect_id: str
    tenant_id: str
    mission_id: str
    actor_id: str
    authority_grant_id: str
    action: str
    resource: str
    risk: EffectRisk
    estimated_cost_cents: int
    reversible: bool
    idempotency_key: str
    requested_at: str
    input_sha256: str
    hazard_ids: tuple[str, ...] = ()
    purpose: str = ""
    requires_human_approval: bool = False

    def __post_init__(self) -> None:
        for value, label, maximum in (
            (self.effect_id, "effect_id", 256), (self.tenant_id, "tenant_id", 128),
            (self.mission_id, "mission_id", 256), (self.actor_id, "actor_id", 256),
            (self.authority_grant_id, "authority_grant_id", 256),
            (self.action, "action", 256), (self.resource, "resource", 2_000),
            (self.idempotency_key, "idempotency_key", 256),
        ):
            _required(value, label, maximum=maximum)
        if self.estimated_cost_cents < 0:
            raise ValueError("effect cost cannot be negative")
        if self.risk is EffectRisk.IRREVERSIBLE and self.reversible:
            raise ValueError("irreversible effects cannot be marked reversible")
        if len(self.input_sha256) != 64 or any(
            ch not in "0123456789abcdef" for ch in self.input_sha256
        ):
            raise ValueError("effect input_sha256 must be a lowercase hexadecimal digest")
        _utc(self.requested_at, "requested_at")

    def to_dict(self) -> dict[str, Any]:
        return {
            "effect_id": self.effect_id, "tenant_id": self.tenant_id,
            "mission_id": self.mission_id, "actor_id": self.actor_id,
            "authority_grant_id": self.authority_grant_id,
            "action": self.action, "resource": self.resource, "risk": self.risk.value,
            "estimated_cost_cents": self.estimated_cost_cents,
            "reversible": self.reversible, "idempotency_key": self.idempotency_key,
            "requested_at": self.requested_at, "input_sha256": self.input_sha256,
            "hazard_ids": list(self.hazard_ids), "purpose": self.purpose,
            "requires_human_approval": self.requires_human_approval,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "EffectRequest":
        return cls(
            effect_id=str(raw["effect_id"]), tenant_id=str(raw["tenant_id"]),
            mission_id=str(raw["mission_id"]), actor_id=str(raw["actor_id"]),
            authority_grant_id=str(raw["authority_grant_id"]), action=str(raw["action"]),
            resource=str(raw["resource"]), risk=EffectRisk(str(raw["risk"])),
            estimated_cost_cents=int(raw["estimated_cost_cents"]),
            reversible=bool(raw["reversible"]), idempotency_key=str(raw["idempotency_key"]),
            requested_at=str(raw["requested_at"]), input_sha256=str(raw["input_sha256"]),
            hazard_ids=tuple(str(item) for item in raw.get("hazard_ids", ())),
            purpose=str(raw.get("purpose", "")),
            requires_human_approval=bool(raw.get("requires_human_approval", False)),
        )


@dataclass(frozen=True)
class AssuranceDecision:
    decision_id: str
    effect_id: str
    disposition: AssuranceDisposition
    safe_mode: SafeMode | None
    reasons: tuple[str, ...]
    obligations: tuple[str, ...]
    policy_version: str
    authority_grant_id: str
    decided_at: str
    reservation_id: str | None = None

    def __post_init__(self) -> None:
        for value, label in (
            (self.decision_id, "decision_id"), (self.effect_id, "effect_id"),
            (self.policy_version, "policy_version"),
            (self.authority_grant_id, "authority_grant_id"),
        ):
            _required(value, label, maximum=256)
        if not self.reasons:
            raise ValueError("assurance decisions require reasons")
        if self.disposition is AssuranceDisposition.ALLOWED and self.safe_mode is not None:
            raise ValueError("fully allowed effects do not use a safe fallback mode")
        if self.disposition is not AssuranceDisposition.ALLOWED and self.safe_mode is None:
            raise ValueError("non-allowed effects require a safe fallback mode")
        _utc(self.decided_at, "decided_at")

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id, "effect_id": self.effect_id,
            "disposition": self.disposition.value,
            "safe_mode": None if self.safe_mode is None else self.safe_mode.value,
            "reasons": list(self.reasons), "obligations": list(self.obligations),
            "policy_version": self.policy_version,
            "authority_grant_id": self.authority_grant_id,
            "decided_at": self.decided_at, "reservation_id": self.reservation_id,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AssuranceDecision":
        return cls(
            decision_id=str(raw["decision_id"]), effect_id=str(raw["effect_id"]),
            disposition=AssuranceDisposition(str(raw["disposition"])),
            safe_mode=(None if raw.get("safe_mode") is None else SafeMode(str(raw["safe_mode"]))),
            reasons=tuple(str(item) for item in raw.get("reasons", ())),
            obligations=tuple(str(item) for item in raw.get("obligations", ())),
            policy_version=str(raw["policy_version"]),
            authority_grant_id=str(raw["authority_grant_id"]),
            decided_at=str(raw["decided_at"]),
            reservation_id=(
                None if raw.get("reservation_id") is None else str(raw["reservation_id"])
            ),
        )
