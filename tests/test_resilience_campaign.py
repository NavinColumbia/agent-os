from __future__ import annotations

import pytest

from agent_os.domain.resilience import (
    FaultCampaign,
    FaultKind,
    FaultObservation,
    FaultScenario,
    evaluate_fault_campaign,
)


def campaign(*, production_shaped: bool = False) -> FaultCampaign:
    return FaultCampaign(
        campaign_id="release-resilience",
        revision=1,
        environment="local-compose" if not production_shaped else "staging",
        production_shaped=production_shaped,
        scenarios=tuple(FaultScenario(
            scenario_id=f"scenario-{kind.value}",
            fault_kind=kind,
            recovery_deadline_seconds=60,
        ) for kind in FaultKind),
    )


def observations() -> list[FaultObservation]:
    return [FaultObservation(
        observation_id=f"observation-{kind.value}",
        campaign_id="release-resilience",
        campaign_revision=1,
        scenario_id=f"scenario-{kind.value}",
        fault_kind=kind,
        injection_succeeded=True,
        containment_succeeded=True,
        recovery_succeeded=True,
        recovery_seconds=10,
        data_loss_records=0,
        duplicate_effects=0,
        cross_tenant_exposure=False,
        audit_complete=True,
        fallback_engaged=True,
        evidence_ids=(f"artifact-{kind.value}",),
    ) for kind in FaultKind]


def test_local_campaign_can_pass_without_masquerading_as_production_evidence():
    report = evaluate_fault_campaign(campaign(), observations())
    assert report.passed is True
    assert report.production_release_evidence is False
    assert len(report.scenarios) == 6
    assert len(report.campaign_sha256) == 64
    assert len(report.observation_set_sha256) == 64


def test_production_shaped_campaign_is_release_evidence_only_when_every_bound_passes():
    healthy = evaluate_fault_campaign(campaign(production_shaped=True), observations())
    assert healthy.production_release_evidence is True
    broken = observations()
    row = broken[0]
    broken[0] = FaultObservation(**{
        **row.__dict__, "recovery_seconds": 61, "duplicate_effects": 1,
        "audit_complete": False,
    })
    report = evaluate_fault_campaign(campaign(production_shaped=True), broken)
    assert report.passed is False
    assert report.production_release_evidence is False
    assert report.scenarios[0].reasons == (
        "recovery exceeded its admitted deadline",
        "duplicate effects exceeded their admitted maximum",
        "fault, diagnosis, and recovery are not fully auditable",
    )


def test_campaign_rejects_missing_or_relabelled_fault_observations():
    with pytest.raises(ValueError, match="matrix"):
        evaluate_fault_campaign(campaign(), observations()[:-1])
    changed = observations()
    changed[0] = FaultObservation(**{
        **changed[0].__dict__, "fault_kind": FaultKind.PROCESS_DEATH,
    })
    with pytest.raises(ValueError, match="changed the admitted fault kind"):
        evaluate_fault_campaign(campaign(), changed)
