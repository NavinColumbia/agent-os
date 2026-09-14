import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import assurance_learning as al


def test_reviewer_is_structurally_independent_from_executor_and_manager():
    out = al.validate_independence("builder", "engineering-manager", "assurance-reviewer",
                                   {"separate_reporting_line": True})
    assert out["reviewer_id"] == "assurance-reviewer"
    with pytest.raises(ValueError, match="three distinct"):
        al.validate_independence("builder", "engineering-manager", "engineering-manager")
    with pytest.raises(ValueError, match="conflict"):
        al.validate_independence("builder", "manager", "reviewer",
                                 {"reports_to_executor": True})
    with pytest.raises(ValueError, match="independence_basis"):
        al.validate_independence("builder", "manager", "reviewer")


def test_acceptance_requires_assigned_reviewer_evidence_and_all_criteria_met():
    with pytest.raises(PermissionError):
        al.validate_verdict("ready", "accepted", actor="manager", reviewer_id="reviewer",
                            evidence_ids=["e1"], rationale="looks good",
                            criterion_results={"tests": True})
    with pytest.raises(ValueError, match="cited evidence"):
        al.validate_verdict("ready", "rejected", actor="reviewer", reviewer_id="reviewer",
                            evidence_ids=[], rationale="failure", criterion_results={"tests": False})
    with pytest.raises(ValueError, match="every criterion"):
        al.validate_verdict("ready", "accepted", actor="reviewer", reviewer_id="reviewer",
                            evidence_ids=["e1"], rationale="mixed",
                            criterion_results={"tests": True, "security": False})
    accepted = al.validate_verdict(
        "ready", "accepted", actor="reviewer", reviewer_id="reviewer",
        evidence_ids=["e1", "e1", "e2"], rationale="all observed criteria met",
        criterion_results={"tests": "pass", "security": "met"})
    assert accepted["evidence_ids"] == ["e1", "e2"]


def test_terminal_assurance_decision_cannot_be_replayed():
    with pytest.raises(ValueError, match="already accepted"):
        al.validate_verdict("accepted", "rejected", actor="reviewer", reviewer_id="reviewer",
                            evidence_ids=["e1"], rationale="late change",
                            criterion_results={"tests": False})


def test_countermeasure_owner_cannot_self_verify_and_evidence_is_required():
    with pytest.raises(PermissionError, match="cannot self-verify"):
        al.validate_corrective_verification(
            "implemented", actor="owner", verifier_id="verifier",
            implementation_evidence=["deploy-1"], verification_evidence=["probe-1"])
    with pytest.raises(ValueError, match="implementation and outcome"):
        al.validate_corrective_verification(
            "implemented", actor="verifier", verifier_id="verifier",
            implementation_evidence=["deploy-1"], verification_evidence=[])
    assert al.validate_corrective_verification(
        "implemented", actor="verifier", verifier_id="verifier",
        implementation_evidence=["deploy-1"], verification_evidence=["probe-1"])


def test_postmortem_triggers_are_state_based_and_recurrence_always_learns():
    assert al.requires_postmortem("assurance_rejection", "low") is True
    assert al.requires_postmortem("customer_impact", "critical") is True
    assert al.requires_postmortem("manual", "low") is False
    assert al.requires_postmortem("manual", "low", recurrence_of="incident-1") is True
    with pytest.raises(ValueError, match="unknown incident trigger"):
        al.requires_postmortem("worker_was_slow", "high")


def test_findings_reject_culprit_model_in_favor_of_system_conditions():
    # The pure validation occurs before ensure()/DB access.
    with pytest.raises(ValueError, match="not blame"):
        al.add_finding("t", "pm", "package preflight was absent",
                       {"culprit": "qa-7"}, ["trace://1"], created_by="facilitator")


def test_postmortem_cannot_close_before_learning_actions_are_verified():
    with pytest.raises(ValueError, match="finding"):
        al.validate_postmortem_closure(0, ["verified"])
    with pytest.raises(ValueError, match="corrective action"):
        al.validate_postmortem_closure(1, [])
    with pytest.raises(ValueError, match="independently verified"):
        al.validate_postmortem_closure(2, ["verified", "implemented"])
    assert al.validate_postmortem_closure(1, ["verified"])


def test_coaching_is_contextual_visible_expiring_and_not_a_shadow_score():
    item = al.normalize_coaching(
        "worker", "manager", "checkout incident 12", "did not record dependency failure",
        "handoff diagnosis took longer", "attach package probe output to blocked updates",
        ["trace://incident-12/event-4"], retention_days=30)
    assert item["purpose"] == "developmental"
    assert item["subject_visible"] is True
    assert item["punitive_use"] is False
    assert item["retention_days"] == 30

    for unsafe in ({"score": 82}, {"single_metric_basis": True},
                   {"compensation": {"eligible": False}}, {"hidden_from_subject": True}):
        with pytest.raises(ValueError, match="coaching|unsafe"):
            al.normalize_coaching(
                "worker", "manager", "one task", "slow test", "delay", "diagnose first",
                ["trace://1"], policy=unsafe)


def test_coaching_requires_reviewable_evidence_and_bounded_retention():
    with pytest.raises(ValueError, match="reviewable evidence"):
        al.normalize_coaching("w", "m", "ctx", "behavior", "impact", "practice", [])
    with pytest.raises(ValueError, match="between 1 and 365"):
        al.normalize_coaching("w", "m", "ctx", "behavior", "impact", "practice", ["e"],
                              retention_days=1000)
