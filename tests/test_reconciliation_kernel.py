from agent_os.application.reconciliation import (
    ReconciliationOperation,
    ReconciliationOperationKind,
    ReconciliationProposal,
    admit_reconciliation,
)


def proposal(**changes) -> ReconciliationProposal:
    values = dict(
        proposal_id="proposal-1",
        tenant_id="tenant-a",
        mission_id="mission-1",
        reconciler_id="reconciler:delivery",
        observation_ids=("observation-1",),
        expected_revision=3,
        operations=(ReconciliationOperation(
            ReconciliationOperationKind.PROPOSE_EFFECT,
            "effect-1",
            {"action": "deploy.preview"},
        ),),
        rationale="The verified artifact is ready to publish",
        confidence=0.9,
        evidence_ids=("evidence-1",),
    )
    values.update(changes)
    return ReconciliationProposal(**values)


def test_reconciler_proposes_but_cannot_bypass_effect_assurance():
    result = admit_reconciliation(
        proposal(), tenant_id="tenant-a", mission_id="mission-1", current_revision=3,
        known_observation_ids=frozenset({"observation-1"}),
        known_evidence_ids=frozenset({"evidence-1"}),
    )
    assert result.admitted is True
    assert "route proposed effect through the assurance kernel" in result.obligations


def test_stale_or_unsupported_reconciliation_is_rejected():
    result = admit_reconciliation(
        proposal(), tenant_id="tenant-a", mission_id="mission-1", current_revision=4,
        known_observation_ids=frozenset(), known_evidence_ids=frozenset(),
    )
    assert result.admitted is False
    assert any("stale" in reason for reason in result.reasons)
    assert any("unknown observation" in reason for reason in result.reasons)


def test_low_confidence_material_change_requires_human_review():
    material = ReconciliationOperation(
        ReconciliationOperationKind.UPDATE_COMMITMENT,
        "commitment-1",
        {"deadline": "2026-10-01"},
        material=True,
    )
    result = admit_reconciliation(
        proposal(operations=(material,), confidence=0.2),
        tenant_id="tenant-a", mission_id="mission-1", current_revision=3,
        known_observation_ids=frozenset({"observation-1"}),
        known_evidence_ids=frozenset({"evidence-1"}),
    )
    assert result.admitted is True
    assert result.requires_human is True
