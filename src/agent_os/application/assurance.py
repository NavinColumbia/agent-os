"""Deterministic admission kernel for consequential agent effects."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from fnmatch import fnmatchcase
import hashlib
from typing import Any, Mapping, Protocol

from agent_os.domain.mission_model import (
    AssuranceDecision,
    AssuranceDisposition,
    AuthorityGrant,
    EffectRequest,
    EffectRisk,
    Hazard,
    HazardSeverity,
    MissionSpec,
    SafeMode,
)


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    policy_version: str
    reasons: tuple[str, ...] = ()
    obligations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.policy_version.strip():
            raise ValueError("policy decision requires a version")
        if not self.allowed and not self.reasons:
            raise ValueError("a denied policy decision requires a reason")


class EffectPolicyEngine(Protocol):
    """AuthZEN-shaped application authorization boundary.

    Implementations may embed Cedar, call an AuthZEN PDP, or evaluate a local
    signed policy bundle.  The assurance kernel never depends on a vendor-
    specific policy language.
    """

    def evaluate(
        self,
        *,
        subject: Mapping[str, Any],
        action: Mapping[str, Any],
        resource: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> PolicyDecision: ...


def _scope_is_attenuated(child: str, parent: str) -> bool:
    """Conservatively prove a child pattern is no broader than its parent."""

    if child == parent or parent == "*":
        return True
    if any(symbol in child for symbol in "*?["):
        return False
    return fnmatchcase(child, parent)


def validate_delegation(child: AuthorityGrant, parent: AuthorityGrant) -> None:
    """Reject privilege amplification in a delegated authority grant."""

    if child.parent_grant_id != parent.grant_id:
        raise ValueError("child authority does not reference its parent grant")
    if (child.tenant_id, child.mission_id) != (parent.tenant_id, parent.mission_id):
        raise ValueError("delegated authority cannot cross tenant or mission boundaries")
    if child.mission_revision != parent.mission_revision:
        raise ValueError("delegated authority cannot cross mission revisions")
    if child.principal_id != parent.principal_id:
        raise ValueError("delegated authority must preserve the accountable root principal")
    if child.delegation_chain[:-1] != parent.delegation_chain:
        raise ValueError("delegated authority must preserve the complete parent chain")
    if child.budget_limit_cents > parent.budget_limit_cents:
        raise ValueError("delegated authority cannot increase its budget")
    if not all(
        any(_scope_is_attenuated(scope, parent_scope) for parent_scope in parent.allowed_effects)
        for scope in child.allowed_effects
    ):
        raise ValueError("delegated authority cannot widen effect scope")
    if not all(
        any(_scope_is_attenuated(scope, parent_scope) for parent_scope in parent.allowed_resources)
        for scope in child.allowed_resources
    ):
        raise ValueError("delegated authority cannot widen resource scope")
    child_start = datetime.fromisoformat(child.valid_from.replace("Z", "+00:00"))
    child_end = datetime.fromisoformat(child.expires_at.replace("Z", "+00:00"))
    parent_start = datetime.fromisoformat(parent.valid_from.replace("Z", "+00:00"))
    parent_end = datetime.fromisoformat(parent.expires_at.replace("Z", "+00:00"))
    if child_start < parent_start or child_end > parent_end:
        raise ValueError("delegated authority cannot outlive its parent")


class AssuranceKernel:
    """Pure reference monitor for one proposed external or material effect."""

    def __init__(self, policy: EffectPolicyEngine) -> None:
        self._policy = policy

    @staticmethod
    def _decision_id(effect_id: str, disposition: AssuranceDisposition, version: str) -> str:
        digest = hashlib.sha256(
            f"agent-os:assurance:v1:{effect_id}:{disposition.value}:{version}".encode()
        ).hexdigest()
        return f"decision-{digest[:40]}"

    @staticmethod
    def _fallback(effect: EffectRequest, hazards: tuple[Hazard, ...]) -> SafeMode:
        if hazards:
            order = {
                SafeMode.FREEZE: 6,
                SafeMode.ROLLBACK: 5,
                SafeMode.HUMAN_REVIEW: 4,
                SafeMode.DRAFT_ONLY: 3,
                SafeMode.READ_ONLY: 2,
                SafeMode.BASELINE: 1,
            }
            return max((item.fallback_mode for item in hazards), key=order.__getitem__)
        if effect.risk is EffectRisk.READ_ONLY:
            return SafeMode.READ_ONLY
        if effect.reversible:
            return SafeMode.ROLLBACK
        return SafeMode.HUMAN_REVIEW

    def decide(
        self,
        *,
        mission: MissionSpec,
        effect: EffectRequest,
        authority: AuthorityGrant,
        hazards: tuple[Hazard, ...],
        available_budget_cents: int,
        available_authority_budget_cents: int | None = None,
        authority_revoked: bool = False,
        now: datetime | None = None,
    ) -> AssuranceDecision:
        instant = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        reasons: list[str] = []
        obligations: list[str] = []
        hard_denial = False
        human_required = False

        if (effect.tenant_id, effect.mission_id) != (mission.tenant_id, mission.mission_id):
            reasons.append("effect identity does not match the mission")
            hard_denial = True
        if (authority.tenant_id, authority.mission_id) != (
            mission.tenant_id, mission.mission_id,
        ):
            reasons.append("authority does not belong to this mission")
            hard_denial = True
        if authority.mission_revision != mission.revision:
            reasons.append("authority belongs to a stale mission revision")
            hard_denial = True
        if effect.authority_grant_id != authority.grant_id:
            reasons.append("effect references a different authority grant")
            hard_denial = True
        if authority_revoked:
            reasons.append("authority grant has been revoked")
            hard_denial = True
        if effect.actor_id != authority.delegate_id:
            reasons.append("effect actor is not the authorized delegate")
            hard_denial = True
        if not authority.active_at(instant):
            reasons.append("authority grant is not active at decision time")
            hard_denial = True
        if not authority.permits(effect.action, effect.resource):
            reasons.append("effect or resource is outside delegated scope")
            hard_denial = True
        if any(fnmatchcase(effect.action, pattern) for pattern in mission.prohibited_effects):
            reasons.append("effect is prohibited by the mission contract")
            hard_denial = True
        authority_available = (
            authority.budget_limit_cents
            if available_authority_budget_cents is None
            else available_authority_budget_cents
        )
        if effect.estimated_cost_cents > authority_available:
            reasons.append("effect exceeds its delegated budget")
            hard_denial = True
        if effect.estimated_cost_cents > available_budget_cents:
            reasons.append("mission has insufficient uncommitted budget")
            hard_denial = True

        hazards_by_id = {item.hazard_id: item for item in hazards}
        missing_hazards = set(effect.hazard_ids) - set(hazards_by_id)
        if missing_hazards:
            reasons.append("effect references an unknown hazard")
            hard_denial = True
        applicable = tuple(hazards_by_id[item] for item in effect.hazard_ids if item in hazards_by_id)
        if any(item.requires_human_release for item in applicable):
            human_required = True
            reasons.append("a mission hazard requires human release")
        if any(item.severity is HazardSeverity.CRITICAL for item in applicable):
            human_required = True
            reasons.append("critical-hazard effects require human release")
        if effect.risk is EffectRisk.IRREVERSIBLE:
            human_required = True
            reasons.append("irreversible effects require human release")
        elif effect.risk is EffectRisk.CONSEQUENTIAL and not effect.reversible:
            human_required = True
            reasons.append("non-reversible consequential effects require human release")
        if effect.requires_human_approval:
            human_required = True
            reasons.append("the effect contract requires human release")

        try:
            policy = self._policy.evaluate(
                subject={
                    "type": "agent", "id": effect.actor_id,
                    "principal_id": authority.principal_id,
                    "delegation_chain": list(authority.delegation_chain),
                },
                action={"name": effect.action, "risk": effect.risk.value},
                resource={"id": effect.resource, "tenant_id": effect.tenant_id},
                context={
                    "mission_id": effect.mission_id,
                    "effect_id": effect.effect_id,
                    "estimated_cost_cents": effect.estimated_cost_cents,
                    "reversible": effect.reversible,
                    "human_approved": authority.human_approved,
                },
            )
        except Exception as exc:
            policy = PolicyDecision(
                allowed=False,
                policy_version="policy-unavailable",
                reasons=(f"policy evaluation unavailable: {type(exc).__name__}",),
            )
        reasons.extend(policy.reasons)
        obligations.extend(policy.obligations)
        if not policy.allowed:
            hard_denial = True

        exact_human_authority = (
            authority.human_approved
            and effect.action in authority.allowed_effects
            and effect.resource in authority.allowed_resources
            and authority.approval_binding_sha256 == effect.input_sha256
            and bool(authority.approval_evidence_ids)
        )
        if human_required and exact_human_authority:
            obligations.append("retain effect-bound human approval receipt")
            human_required = False
            reasons.append("effect-bound human approval satisfied the release gate")

        if hard_denial:
            disposition = AssuranceDisposition.DENIED
        elif human_required:
            disposition = AssuranceDisposition.HUMAN_REQUIRED
        elif applicable and any(
            item.severity in {HazardSeverity.HIGH, HazardSeverity.CRITICAL}
            for item in applicable
        ):
            disposition = AssuranceDisposition.RESTRICTED
            obligations.extend(
                constraint for item in applicable for constraint in item.safety_constraints
            )
            reasons.append("high-severity hazard requires restricted execution")
        else:
            disposition = AssuranceDisposition.ALLOWED
            reasons.append("effect passed authority, policy, hazard, and budget checks")

        fallback = None if disposition is AssuranceDisposition.ALLOWED else self._fallback(
            effect, applicable,
        )
        return AssuranceDecision(
            decision_id=self._decision_id(effect.effect_id, disposition, policy.policy_version),
            effect_id=effect.effect_id,
            disposition=disposition,
            safe_mode=fallback,
            reasons=tuple(dict.fromkeys(reasons)),
            obligations=tuple(dict.fromkeys(obligations)),
            policy_version=policy.policy_version,
            authority_grant_id=authority.grant_id,
            decided_at=instant.isoformat(),
        )
