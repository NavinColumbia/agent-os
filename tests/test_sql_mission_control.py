from datetime import datetime, timedelta, timezone

import pytest

from agent_os.application.assurance import AssuranceKernel
from agent_os.domain.mission_model import (
    AuthorityGrant,
    Claim,
    ClaimStatus,
    EffectRequest,
    EffectRisk,
    EvidenceKind,
    EvidenceRef,
    Hazard,
    HazardSeverity,
    MissionSpec,
    SafeMode,
)
from agent_os.infrastructure.authzen_policy import baseline_effect_policy
from agent_os.infrastructure.sql_mission_control import SQLMissionControl
from agent_os.infrastructure.sql_notifications import SQLNotificationStore


NOW = datetime.now(timezone.utc)


def test_effect_admission_is_atomic_with_double_entry_budget(tmp_path):
    store = SQLMissionControl(
        f"sqlite:///{tmp_path / 'mission.sqlite3'}",
        assurance_kernel=AssuranceKernel(baseline_effect_policy()),
        create_schema=True,
    )
    spec = MissionSpec(
        mission_id="mission-1", tenant_id="tenant-a", objective="Ship a product",
        principal_id="human:ceo", accountable_owner_id="human:ceo",
        budget_limit_cents=1_000, success_measures=("customer can use it",),
    )
    assert store.create_mission(spec)["duplicate"] is False
    assert store.create_mission(spec)["duplicate"] is True
    assert store.budget_summary("tenant-a", "mission-1") == {
        "authorized_cents": 1_000,
        "available_cents": 1_000,
        "reserved_cents": 0,
        "spent_cents": 0,
    }

    evidence = EvidenceRef(
        evidence_id="evidence-1", mission_id="mission-1", kind=EvidenceKind.TEST,
        artifact_ref="artifact:evidence-1", sha256="b" * 64, produced_by="agent:qa",
        observed_at=NOW.isoformat(), recorded_at=NOW.isoformat(),
    )
    assert store.add_evidence("tenant-a", evidence) is True
    assert store.add_claim("tenant-a", Claim(
        claim_id="claim-1", mission_id="mission-1", statement="Acceptance passed",
        status=ClaimStatus.ACCEPTED, asserted_by="agent:qa", valid_from=NOW.isoformat(),
        recorded_at=NOW.isoformat(), evidence_ids=("evidence-1",),
    )) is True
    assert store.add_claim("tenant-a", Claim(
        claim_id="claim-dependent", mission_id="mission-1",
        statement="The accepted release may proceed", status=ClaimStatus.ACCEPTED,
        asserted_by="agent:release", valid_from=NOW.isoformat(),
        recorded_at=(NOW + timedelta(seconds=1)).isoformat(),
        depends_on_claim_ids=("claim-1",),
    )) is True
    assert store.add_claim("tenant-a", Claim(
        claim_id="claim-invalidation", mission_id="mission-1",
        statement="The earlier acceptance evidence is no longer valid",
        status=ClaimStatus.INVALIDATED, asserted_by="agent:qa",
        valid_from=(NOW + timedelta(seconds=2)).isoformat(),
        recorded_at=(NOW + timedelta(seconds=2)).isoformat(),
        supersedes_claim_id="claim-1",
    )) is True
    hazard = Hazard(
        hazard_id="hazard-1",
        mission_id="mission-1",
        description="Private customer data could escape into public logs",
        unacceptable_loss="Customer trust and privacy",
        severity=HazardSeverity.LOW,
        safety_constraints=("Keep private content out of summaries",),
        unsafe_control_actions=("Publish raw evidence",),
        fallback_mode=SafeMode.FREEZE,
    )
    assert store.add_hazard("tenant-a", hazard) is True
    assert store.add_hazard("tenant-a", hazard) is False
    grant = AuthorityGrant(
        grant_id="grant-1", tenant_id="tenant-a", mission_id="mission-1",
        principal_id="human:ceo", delegate_id="agent:release",
        allowed_effects=("deploy.preview",), allowed_resources=("preview:mission-1",),
        budget_limit_cents=500,
        valid_from=(NOW - timedelta(minutes=1)).isoformat(),
        expires_at=(NOW + timedelta(hours=1)).isoformat(),
        delegation_chain=("human:ceo", "agent:release"),
    )
    assert store.grant_authority(grant) is True
    effect = EffectRequest(
        effect_id="effect-1", tenant_id="tenant-a", mission_id="mission-1",
        actor_id="agent:release", authority_grant_id="grant-1",
        action="deploy.preview", resource="preview:mission-1",
        risk=EffectRisk.REVERSIBLE, estimated_cost_cents=300, reversible=True,
        idempotency_key="deploy-1", requested_at=NOW.isoformat(), input_sha256="c" * 64,
    )
    admitted = store.admit_effect(effect)
    assert admitted["status"] == "admitted"
    assert admitted["decision"]["reservation_id"].startswith("reservation-")
    assert store.admit_effect(effect)["duplicate"] is True
    assert store.budget_summary("tenant-a", "mission-1") == {
        "authorized_cents": 1_000,
        "available_cents": 700,
        "reserved_cents": 300,
        "spent_cents": 0,
    }
    second = EffectRequest.from_dict({
        **effect.to_dict(),
        "effect_id": "effect-2",
        "idempotency_key": "deploy-2",
        "estimated_cost_cents": 250,
    })
    assert store.admit_effect(second)["decision"]["disposition"] == "denied"
    assert store.budget_summary("tenant-a", "mission-1")["reserved_cents"] == 300
    with pytest.raises(ValueError, match="idempotency key"):
        store.admit_effect(EffectRequest.from_dict({
            **effect.to_dict(), "effect_id": "effect-3",
        }))

    assert store.settle_effect(
        tenant_id="tenant-a", mission_id="mission-1", effect_id="effect-1",
        actual_cost_cents=225, succeeded=True,
    )["status"] == "succeeded"
    assert store.settle_effect(
        tenant_id="tenant-a", mission_id="mission-1", effect_id="effect-1",
        actual_cost_cents=225, succeeded=True,
    )["duplicate"] is True
    with pytest.raises(ValueError, match="different outcome"):
        store.settle_effect(
            tenant_id="tenant-a", mission_id="mission-1", effect_id="effect-1",
            actual_cost_cents=225, succeeded=False,
        )
    assert store.budget_summary("tenant-a", "mission-1") == {
        "authorized_cents": 1_000,
        "available_cents": 775,
        "reserved_cents": 0,
        "spent_cents": 225,
    }
    assert store.revoke_authority(
        tenant_id="tenant-a", mission_id="mission-1", grant_id="grant-1",
        revoked_by="human:ceo", reason="release window closed",
    )["duplicate"] is False
    after_revoke = EffectRequest.from_dict({
        **effect.to_dict(), "effect_id": "effect-after-revoke",
        "idempotency_key": "deploy-after-revoke", "estimated_cost_cents": 25,
    })
    revoked_decision = store.admit_effect(after_revoke)["decision"]
    assert revoked_decision["disposition"] == "denied"
    assert any("revoked" in reason for reason in revoked_decision["reasons"])
    assert store.tombstone_evidence(
        tenant_id="tenant-a", mission_id="mission-1", evidence_id="evidence-1",
        erased_by="human:ceo", reason="retention period elapsed",
    )["duplicate"] is False
    view = store.control_view("tenant-a", "mission-1")
    assert view is not None
    projected_claims = {item["claim_id"]: item for item in view["claims"]}
    assert projected_claims["claim-1"]["evidence_ids"] == ["evidence-1"]
    assert projected_claims["claim-dependent"]["effective_status"] == "invalidated_by_dependency"
    assert view["evidence"][0]["artifact_ref"] == f"erased:{'b' * 64}"
    assert view["evidence"][0]["erasure_reason"] == "retention period elapsed"
    assert any(item["status"] == "succeeded" for item in view["effects"])
    assert view["authorities"][0]["revocation_reason"] == "release window closed"
    events = SQLNotificationStore(
        f"sqlite:///{tmp_path / 'mission.sqlite3'}",
        create_schema=True,
    )
    event_page = events.list_experience_events(
        "tenant-a", audience_ids=("tenant:members",), limit=100,
    )
    assert [event["kind"] for event in event_page.events] == [
        "mission.created",
        "mission.evidence.added",
        "mission.claim.added",
        "mission.claim.added",
        "mission.claim.added",
        "mission.hazard.added",
        "mission.authority.granted",
        "mission.effect.admitted",
        "mission.effect.denied",
        "mission.effect.succeeded",
        "mission.authority.revoked",
        "mission.effect.denied",
        "mission.evidence.erased",
    ]
    assert all(event["resource_id"] == "mission-1" for event in event_page.events)
    assert all(event["projection_revision"] == 1 for event in event_page.events)
    summaries = " ".join(event["safe_summary"] for event in event_page.events)
    assert "Acceptance passed" not in summaries
    assert "Private customer data" not in summaries
    assert "release window closed" not in summaries
    assert not events.list_experience_events(
        "tenant-b", audience_ids=("tenant:members",), limit=100,
    ).events
    events.close()
    store.close()


def test_mission_revision_is_immutable_history_and_fences_old_authority(tmp_path):
    store = SQLMissionControl(
        f"sqlite:///{tmp_path / 'mission-revision.sqlite3'}",
        assurance_kernel=AssuranceKernel(baseline_effect_policy()),
        create_schema=True,
    )
    initial = MissionSpec(
        mission_id="mission-revision", tenant_id="tenant-a", objective="Build v1",
        principal_id="human:ceo", accountable_owner_id="human:ceo",
        budget_limit_cents=500, success_measures=("v1 accepted",),
    )
    store.create_mission(initial)
    old_grant = AuthorityGrant(
        grant_id="grant-old", tenant_id="tenant-a", mission_id="mission-revision",
        principal_id="human:ceo", delegate_id="agent:release",
        allowed_effects=("deploy.preview",),
        allowed_resources=("preview:mission-revision",), budget_limit_cents=500,
        valid_from=(NOW - timedelta(minutes=1)).isoformat(),
        expires_at=(NOW + timedelta(hours=1)).isoformat(),
        delegation_chain=("human:ceo", "agent:release"), mission_revision=1,
    )
    store.grant_authority(old_grant)
    revised = MissionSpec.from_dict({
        **initial.to_dict(), "objective": "Build the clarified v2",
        "success_measures": ["v2 accepted"], "budget_limit_cents": 700,
        "revision": 2,
        "revised_at": (
            datetime.fromisoformat(initial.created_at) + timedelta(seconds=1)
        ).isoformat(),
    })
    result = store.revise_mission(
        revised, expected_revision=1, revised_by="human:ceo",
        reason="customer clarified the outcome",
    )
    assert result["revision"] == 2
    assert store.revise_mission(
        revised, expected_revision=1, revised_by="human:ceo",
        reason="customer clarified the outcome",
    )["duplicate"] is True
    assert store.budget_summary("tenant-a", "mission-revision")["available_cents"] == 700

    stale = store.admit_effect(EffectRequest(
        effect_id="effect-stale", tenant_id="tenant-a", mission_id="mission-revision",
        actor_id="agent:release", authority_grant_id="grant-old",
        action="deploy.preview", resource="preview:mission-revision",
        risk=EffectRisk.REVERSIBLE, estimated_cost_cents=0, reversible=True,
        idempotency_key="stale-authority-effect", requested_at=NOW.isoformat(),
        input_sha256="e" * 64,
    ))
    assert stale["decision"]["disposition"] == "denied"
    assert any("stale mission revision" in reason for reason in stale["decision"]["reasons"])
    view = store.control_view("tenant-a", "mission-revision")
    assert [item["revision"] for item in view["mission_revisions"]] == [1, 2]
    assert view["mission_revisions"][1]["revision_reason"] == "customer clarified the outcome"
    events = SQLNotificationStore(
        f"sqlite:///{tmp_path / 'mission-revision.sqlite3'}",
        create_schema=True,
    )
    event_page = events.list_experience_events(
        "tenant-a", audience_ids=("tenant:members",), limit=100,
    )
    assert [event["kind"] for event in event_page.events[:3]] == [
        "mission.created", "mission.authority.granted", "mission.revised",
    ]
    assert event_page.events[2]["projection_revision"] == 2
    assert "customer clarified" not in event_page.events[2]["safe_summary"]
    events.close()
    store.close()
