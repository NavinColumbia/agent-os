import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import incidentcommand


def test_incident_requires_two_commanders_impact_and_sane_cadence():
    with pytest.raises(ValueError, match="different"):
        incidentcommand.normalize_incident("outage", "SEV1", "alex", "alex", {"users": 4})
    with pytest.raises(ValueError, match="impact"):
        incidentcommand.normalize_incident("outage", "SEV1", "alex", "sam", {})
    with pytest.raises(ValueError, match="cadence"):
        incidentcommand.normalize_incident(
            "outage", "SEV1", "alex", "sam", {"users": 4}, 30)


def test_containment_and_resolution_are_evidence_gated():
    with pytest.raises(ValueError, match="containment"):
        incidentcommand.lifecycle_transition("triage", "contained")
    assert incidentcommand.lifecycle_transition(
        "triage", "contained", containment_verified=True) == "contained"
    with pytest.raises(ValueError, match="recovery"):
        incidentcommand.lifecycle_transition("recovering", "resolved")
    assert incidentcommand.lifecycle_transition(
        "recovering", "resolved", recovery_verified=True) == "resolved"


def test_incident_can_regress_when_recovery_or_containment_fails():
    assert incidentcommand.lifecycle_transition("contained", "triage") == "triage"
    assert incidentcommand.lifecycle_transition("resolved", "recovering") == "recovering"
    assert incidentcommand.lifecycle_transition("closed", "recovering") == "recovering"


def test_closure_requires_learning_and_finished_prevention_work():
    with pytest.raises(ValueError, match="postmortem"):
        incidentcommand.lifecycle_transition("resolved", "closed")
    with pytest.raises(ValueError, match="corrective"):
        incidentcommand.lifecycle_transition(
            "resolved", "closed", postmortem_published=True, open_corrective_actions=1)
    assert incidentcommand.lifecycle_transition(
        "resolved", "closed", postmortem_published=True, open_corrective_actions=0) == "closed"


def test_corrective_action_separates_implementation_verification_and_closure():
    due = datetime.now(timezone.utc)
    with pytest.raises(ValueError, match="implementation"):
        incidentcommand.action_transition(
            "in_progress", "implemented", owner="builder", due_at=due)
    assert incidentcommand.action_transition(
        "in_progress", "implemented", owner="builder", due_at=due,
        implementation_evidence={"change": "abc123"}) == "implemented"
    with pytest.raises(ValueError, match="cannot independently verify"):
        incidentcommand.action_transition(
            "implemented", "verified", owner="builder", due_at=due,
            verification_evidence={"test": "passed"}, verified_by="builder")
    assert incidentcommand.action_transition(
        "implemented", "verified", owner="builder", due_at=due,
        verification_evidence={"test": "passed"}, verified_by="qa") == "verified"
    assert incidentcommand.action_transition(
        "verified", "closed", owner="builder", due_at=due) == "closed"


def test_only_assigned_responder_can_acknowledge_and_release_is_explicit():
    with pytest.raises(PermissionError):
        incidentcommand.responder_transition(
            "assigned", "acknowledged", actor="manager", responder="database-sre")
    assert incidentcommand.responder_transition(
        "assigned", "acknowledged", actor="database-sre", responder="database-sre") == "acknowledged"
    assert incidentcommand.responder_transition(
        "acknowledged", "active", actor="commander", responder="database-sre") == "active"


def test_schema_keeps_timeline_append_only_for_application_role():
    schema = (ROOT / "postgres" / "initdb" / "61-incident-command.sql").read_text()
    assert "GRANT SELECT, INSERT ON TABLE incident_timeline" in schema
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE incident_timeline" not in schema
    assert "recurrence_assessment" in schema
    assert "next_communication_at" in schema
