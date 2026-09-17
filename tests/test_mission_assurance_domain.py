from datetime import datetime, timedelta, timezone
import base64
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_os.application.assurance import (
    AssuranceKernel,
    PolicyDecision,
    validate_delegation,
)
from agent_os.domain.mission_model import (
    AssuranceDisposition,
    AuthorityGrant,
    EffectRequest,
    EffectRisk,
    EvidenceKind,
    EvidenceRef,
    Hazard,
    HazardSeverity,
    MissionSpec,
    SafeMode,
)
from agent_os.infrastructure.authzen_policy import LocalAuthZenPolicy, baseline_effect_policy


NOW = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)


def _iso(delta: timedelta = timedelta()) -> str:
    return (NOW + delta).isoformat()


def mission() -> MissionSpec:
    return MissionSpec(
        mission_id="mission-1",
        tenant_id="tenant-a",
        objective="Ship a verified customer application",
        principal_id="human:ceo",
        accountable_owner_id="human:ceo",
        budget_limit_cents=10_000,
        success_measures=("accepted release is independently fetched",),
        prohibited_effects=("credential.export",),
    )


def grant(**changes) -> AuthorityGrant:
    values = dict(
        grant_id="grant-1",
        tenant_id="tenant-a",
        mission_id="mission-1",
        principal_id="human:ceo",
        delegate_id="agent:release",
        allowed_effects=("deploy.preview",),
        allowed_resources=("preview:mission-1",),
        budget_limit_cents=2_000,
        valid_from=_iso(timedelta(hours=-1)),
        expires_at=_iso(timedelta(hours=1)),
        delegation_chain=("human:ceo", "agent:release"),
    )
    values.update(changes)
    return AuthorityGrant(**values)


def effect(**changes) -> EffectRequest:
    values = dict(
        effect_id="effect-1",
        tenant_id="tenant-a",
        mission_id="mission-1",
        actor_id="agent:release",
        authority_grant_id="grant-1",
        action="deploy.preview",
        resource="preview:mission-1",
        risk=EffectRisk.REVERSIBLE,
        estimated_cost_cents=250,
        reversible=True,
        idempotency_key="publish-v1",
        requested_at=_iso(),
        input_sha256="a" * 64,
    )
    values.update(changes)
    return EffectRequest(**values)


def test_evidence_is_reference_only_and_personal_data_has_retention():
    with pytest.raises(ValueError, match="references"):
        EvidenceRef(
            "e-1", "mission-1", EvidenceKind.TEST, "inline:secret", "a" * 64,
            "agent:qa", _iso(), _iso(),
        )
    with pytest.raises(ValueError, match="retention"):
        EvidenceRef(
            "e-1", "mission-1", EvidenceKind.TEST, "artifact:e-1", "a" * 64,
            "agent:qa", _iso(), _iso(), contains_personal_data=True,
        )


def test_delegation_can_only_attenuate_authority():
    parent = grant()
    child = AuthorityGrant(
        grant_id="grant-child",
        tenant_id="tenant-a",
        mission_id="mission-1",
        principal_id="human:ceo",
        delegate_id="agent:publisher",
        allowed_effects=("deploy.preview",),
        allowed_resources=("preview:mission-1",),
        budget_limit_cents=500,
        valid_from=_iso(timedelta(minutes=-30)),
        expires_at=_iso(timedelta(minutes=30)),
        delegation_chain=("human:ceo", "agent:release", "agent:publisher"),
        parent_grant_id="grant-1",
    )
    validate_delegation(child, parent)
    with pytest.raises(ValueError, match="widen effect"):
        validate_delegation(
            AuthorityGrant.from_dict({**child.to_dict(), "allowed_effects": ["*"]}),
            parent,
        )


def test_kernel_binds_authority_policy_hazards_and_budget_to_the_effect():
    kernel = AssuranceKernel(baseline_effect_policy())
    accepted = kernel.decide(
        mission=mission(), effect=effect(), authority=grant(), hazards=(),
        available_budget_cents=1_000, now=NOW,
    )
    assert accepted.disposition is AssuranceDisposition.ALLOWED

    denied = kernel.decide(
        mission=mission(), effect=effect(estimated_cost_cents=1_001), authority=grant(),
        hazards=(), available_budget_cents=1_000, now=NOW,
    )
    assert denied.disposition is AssuranceDisposition.DENIED
    assert any("uncommitted budget" in reason for reason in denied.reasons)

    critical = Hazard(
        hazard_id="hazard-prod-loss",
        mission_id="mission-1",
        description="Production state could be destroyed",
        unacceptable_loss="Irrecoverable customer data loss",
        severity=HazardSeverity.CRITICAL,
        safety_constraints=("verified backup must exist",),
        unsafe_control_actions=("delete production state without a verified backup",),
        fallback_mode=SafeMode.FREEZE,
        requires_human_release=True,
    )
    held = kernel.decide(
        mission=mission(),
        effect=effect(hazard_ids=(critical.hazard_id,)),
        authority=grant(), hazards=(critical,), available_budget_cents=1_000, now=NOW,
    )
    assert held.disposition is AssuranceDisposition.HUMAN_REQUIRED
    assert held.safe_mode is SafeMode.FREEZE


def test_human_release_is_bound_to_exact_input_and_retained_evidence():
    kernel = AssuranceKernel(baseline_effect_policy())
    request = effect(requires_human_approval=True)
    mismatched = grant(
        human_approved=True,
        approval_binding_sha256="b" * 64,
        approval_evidence_ids=("artifact:approval",),
    )
    held = kernel.decide(
        mission=mission(), effect=request, authority=mismatched, hazards=(),
        available_budget_cents=1_000, now=NOW,
    )
    assert held.disposition is AssuranceDisposition.HUMAN_REQUIRED

    exact = AuthorityGrant.from_dict({
        **mismatched.to_dict(), "approval_binding_sha256": request.input_sha256,
    })
    released = kernel.decide(
        mission=mission(), effect=request, authority=exact, hazards=(),
        available_budget_cents=1_000, now=NOW,
    )
    assert released.disposition is AssuranceDisposition.ALLOWED
    assert "retain effect-bound human approval receipt" in released.obligations


def test_policy_outage_fails_closed():
    class BrokenPolicy:
        def evaluate(self, **_kwargs) -> PolicyDecision:
            raise ConnectionError("offline")

    decision = AssuranceKernel(BrokenPolicy()).decide(
        mission=mission(), effect=effect(), authority=grant(), hazards=(),
        available_budget_cents=1_000, now=NOW,
    )
    assert decision.disposition is AssuranceDisposition.DENIED
    assert decision.policy_version == "policy-unavailable"


def test_local_policy_bundle_is_signed_time_bounded_and_tamper_evident():
    private = Ed25519PrivateKey.generate()
    payload = {
        "version": "tenant-policy-7",
        "issued_at": _iso(timedelta(minutes=-1)),
        "expires_at": _iso(timedelta(minutes=30)),
        "default_allowed": False,
        "rules": [{
            "rule_id": "allow-preview", "actions": ["deploy.preview"],
            "resources": ["preview:*"], "allowed": True,
        }],
    }
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    signature = base64.urlsafe_b64encode(private.sign(encoded)).decode().rstrip("=")
    policy = LocalAuthZenPolicy.from_signed_bundle(
        {"payload": payload, "signature": signature},
        public_key=private.public_key(), now=NOW,
    )
    assert policy.evaluate(
        subject={"id": "agent:release"}, action={"name": "deploy.preview", "risk": "reversible"},
        resource={"id": "preview:mission-1", "tenant_id": "tenant-a"},
        context={"mission_id": "mission-1"},
    ).allowed is True
    with pytest.raises(ValueError, match="signature"):
        LocalAuthZenPolicy.from_signed_bundle(
            {"payload": {**payload, "default_allowed": True}, "signature": signature},
            public_key=private.public_key(), now=NOW,
        )
