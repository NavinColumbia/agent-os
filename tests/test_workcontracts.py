import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import workcontracts
from dbpool import connection


def test_contract_requires_objective_owner_manager_and_observable_acceptance():
    with pytest.raises(ValueError, match="objective"):
        workcontracts.normalize_contract("", {"evidence": "tests pass"}, "qa", "qa-manager")
    with pytest.raises(ValueError, match="accountable_owner"):
        workcontracts.normalize_contract("ship", {"evidence": "tests pass"}, "", "qa-manager")
    with pytest.raises(ValueError, match="acceptance_contract"):
        workcontracts.normalize_contract("ship", {}, "qa", "qa-manager")


def test_contract_preserves_agentic_staffing_terms_as_facts():
    c = workcontracts.normalize_contract(
        "prove checkout works", {"evidence": ["signed run", "browser trace"]},
        "qa-7", "qa-manager", backup_owner="qa-8", priority=1, risk="high",
        capacity_units=2.5, update_cadence_s=120,
        dependencies=[{"contract_id": "build-4", "condition": "artifact_ready"}])
    assert c["accountable_owner"] == "qa-7"
    assert c["backup_owner"] == "qa-8"
    assert c["capacity_units"] == 2.5
    assert c["dependencies"][0]["condition"] == "artifact_ready"


def test_offer_does_not_transfer_ownership_until_proposed_owner_accepts():
    rejected = workcontracts.delegation_transition(
        "offered", "rejected", actor="qa-2", proposed_owner="qa-2",
        current_revision=4, offered_revision=4)
    accepted = workcontracts.delegation_transition(
        "offered", "accepted", actor="qa-2", proposed_owner="qa-2",
        current_revision=4, offered_revision=4)
    assert rejected == {"delegation_status": "rejected", "transfer_owner": False}
    assert accepted == {"delegation_status": "accepted", "transfer_owner": True}


def test_only_recipient_can_ack_and_stale_offer_cannot_steal_ownership():
    with pytest.raises(PermissionError):
        workcontracts.delegation_transition(
            "offered", "accepted", actor="manager", proposed_owner="worker",
            current_revision=1, offered_revision=1)
    with pytest.raises(ValueError, match="ownership changed"):
        workcontracts.delegation_transition(
            "offered", "accepted", actor="worker", proposed_owner="worker",
            current_revision=2, offered_revision=1)


def test_terminal_or_duplicate_response_is_not_replayed():
    with pytest.raises(ValueError, match="already accepted"):
        workcontracts.delegation_transition(
            "accepted", "accepted", actor="worker", proposed_owner="worker",
            current_revision=2, offered_revision=1)


def test_production_attention_excludes_test_scope_and_deleted_tenants():
    tid = f"wc-scope-{uuid.uuid4().hex}"
    prod = f"wc-prod-{uuid.uuid4().hex}"
    test = f"wc-test-{uuid.uuid4().hex}"
    try:
        with connection() as c, c.cursor() as cur:
            cur.execute("""INSERT INTO tenants(tenant_id,name,api_token)
                           VALUES (%s,%s,%s)""", (tid, tid, f"aos_{uuid.uuid4().hex}"))
        for cid, scope in ((prod, "production"), (test, "test")):
            workcontracts.create(
                tid, cid, {"evidence": "done"}, "builder", "manager",
                created_by="workcontracts-scope-test", contract_id=cid,
                backup_owner="backup", update_cadence_s=30,
                constraints={"execution_scope": scope})
        with connection() as c, c.cursor() as cur:
            cur.execute("""UPDATE work_contracts SET next_checkin_at=now()-interval '1 minute'
                           WHERE contract_id=ANY(%s)""", ([prod, test],))
        attention = workcontracts.attention_evidence(tid)
        assert [item["contract_id"] for item in attention] == [prod]
        assert workcontracts.owner_load(tid)[0]["active_contracts"] == 1

        # A deleted account cannot keep generating production management work.
        with connection() as c, c.cursor() as cur:
            cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,))
        assert workcontracts.attention_evidence(tid) == []
        assert workcontracts.owner_load(tid) == []
    finally:
        with connection() as c, c.cursor() as cur:
            cur.execute("DELETE FROM work_contract_updates WHERE contract_id=ANY(%s)", ([prod, test],))
            cur.execute("DELETE FROM work_delegations WHERE contract_id=ANY(%s)", ([prod, test],))
            cur.execute("DELETE FROM work_contracts WHERE contract_id=ANY(%s)", ([prod, test],))
            cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,))
