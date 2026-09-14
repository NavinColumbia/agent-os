import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import resourcecommitments


def test_commitment_requires_owned_work_and_durable_authority_context():
    args = dict(description="browser fleet", expected_amount="80", reserved_amount="100",
                authority_kind="standing", authority_reference="envelope:7",
                authority_snapshot={"revision": 7, "per_action_usd": 100}, approved_by="qa-manager")
    with pytest.raises(ValueError, match="objective_id or work_contract_id"):
        resourcecommitments.normalize_commitment(**args)
    with pytest.raises(ValueError, match="authority_snapshot"):
        resourcecommitments.normalize_commitment(**{**args, "objective_id": "obj-1",
                                                      "authority_snapshot": {}})


def test_standing_authority_and_approval_are_preserved_as_facts():
    row = resourcecommitments.normalize_commitment(
        "test runners", 12, 16, work_contract_id="wc-qa", authority_kind="manager",
        authority_reference="authority-decision:44", authority_snapshot={"envelope_revision": 3},
        approval_reference="review:98", approved_by="qa-manager")
    assert row["work_contract_id"] == "wc-qa"
    assert row["authority_reference"] == "authority-decision:44"
    assert row["approval_reference"] == "review:98"
    assert row["reserved_amount"] == Decimal("16")


def test_expiry_must_be_after_authority_start():
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError, match="expires_at"):
        resourcecommitments.normalize_commitment(
            "runner", 1, 1, objective_id="obj", authority_kind="standing",
            authority_reference="env:1", authority_snapshot={"revision": 1},
            approved_by="manager", valid_from=now, expires_at=now - timedelta(seconds=1))


def test_lifecycle_requires_release_reason_and_reconciles_consumed_release():
    with pytest.raises(ValueError, match="requires a reason"):
        resourcecommitments.lifecycle_transition("committed", "released")
    released = resourcecommitments.lifecycle_transition(
        "committed", "released", actual_amount="7.25", reason="objective cancelled")
    assert released["requires_reconciliation"] is True
    with pytest.raises(ValueError, match="must be reconciled"):
        resourcecommitments.lifecycle_transition("reserved", "cancelled", actual_amount=1)


def test_terminal_commitment_cannot_be_reopened():
    with pytest.raises(ValueError, match="reconciled -> committed"):
        resourcecommitments.lifecycle_transition("reconciled", "committed")


def test_variance_is_neutral_evidence_for_manager_reconciliation():
    facts = resourcecommitments.variance_facts("80", "100", "92")
    assert facts["variance_amount"] == Decimal("12")
    assert facts["variance_ratio"] == Decimal("0.15")
    assert facts["unused_reservation"] == Decimal("8")
    zero_baseline = resourcecommitments.variance_facts(0, 5, 2)
    assert zero_baseline["variance_ratio"] is None


def test_oversubscription_is_explicit_evidence_not_a_silent_drop():
    facts = resourcecommitments.oversubscription_facts(
        [{"pool_id": "qa-minutes", "resource_kind": "capacity", "unit": "minute",
          "capacity": "100"}],
        [{"commitment_id": "a", "pool_id": "qa-minutes", "status": "committed",
          "expected_amount": 50, "reserved_amount": 70, "actual_amount": 20},
         {"commitment_id": "b", "pool_id": "qa-minutes", "status": "reserved",
          "expected_amount": 40, "reserved_amount": 45, "actual_amount": 0},
         {"commitment_id": "old", "pool_id": "qa-minutes", "status": "reconciled",
          "expected_amount": 30, "reserved_amount": 30, "actual_amount": 33}])[0]
    assert facts["reserved"] == Decimal("115")
    assert facts["available"] == Decimal("-15")
    assert facts["oversubscribed_by"] == Decimal("15")
    assert facts["oversubscribed"] is True
    assert facts["actual"] == Decimal("53")
    assert facts["live_commitment_ids"] == ["a", "b"]


def test_zero_capacity_reports_pressure_without_divide_by_zero():
    empty = resourcecommitments.oversubscription_facts(
        [{"pool_id": "gpu", "capacity": 0}], [])[0]
    assert empty["utilization"] == Decimal("0")
    pressure = resourcecommitments.oversubscription_facts(
        [{"pool_id": "gpu", "capacity": 0}],
        [{"commitment_id": "c", "pool_id": "gpu", "status": "reserved",
          "expected_amount": 1, "reserved_amount": 1, "actual_amount": 0}])[0]
    assert pressure["utilization"] is None
    assert pressure["oversubscribed_by"] == 1


def test_unknown_pool_is_rejected_in_facts_rollup():
    with pytest.raises(ValueError, match="unknown pools"):
        resourcecommitments.oversubscription_facts(
            [], [{"commitment_id": "c", "pool_id": "missing", "status": "reserved"}])
