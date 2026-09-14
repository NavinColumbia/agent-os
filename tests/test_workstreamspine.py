import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import workstreamspine as spine


def test_identities_are_stable_and_namespaced():
    first = spine.identities("controller:42", "tenant-a")
    second = spine.identities("controller:42", "tenant-a")
    other = spine.identities("controller:43", "tenant-a")
    assert first == second
    assert first != other
    assert first != spine.identities("controller:42", "tenant-b")
    assert first["contract_id"].startswith("wc-live-")
    assert first["objective_id"].startswith("so-live-")
    assert first["review_id"].startswith("ar-live-")


def test_progress_does_not_invent_an_unseeded_commitment(monkeypatch):
    monkeypatch.setattr(spine, "_one", lambda *args: None)
    called = []
    monkeypatch.setattr(spine.workcontracts, "update", lambda *args, **kwargs: called.append(args))
    out = spine.record_progress("tenant", "work", "phase_changed", {"phase": "PLAN"},
                                actor="controller")
    assert out["recorded"] is False
    assert out["reason"] == "workstream_not_seeded"
    assert called == []


def test_selftest_workstream_is_durably_marked_out_of_production_management(monkeypatch):
    created = []
    monkeypatch.setenv("AOS_SELFTEST", "1")
    monkeypatch.setattr(spine.workcontracts, "ensure", lambda: True)
    monkeypatch.setattr(spine.objectiveportfolio, "ensure", lambda: True)
    monkeypatch.setattr(spine.workcontracts, "create",
                        lambda *args, **kwargs: created.append((args, kwargs)))

    def existing(_tenant, query, _args):
        if "FROM work_contracts" in query:
            return None
        if "FROM objective_portfolios" in query:
            return (1,)
        if "FROM strategic_objectives" in query:
            return ("active", 1)
        return None

    monkeypatch.setattr(spine, "_one", existing)
    spine.ensure_workstream(
        "tenant-test", "controller:7", "deliver", {"qa": "pass"},
        accountable_owner="builder", manager_owner="controller")
    assert created[0][1]["constraints"] == {"execution_scope": "test"}


def test_green_assurance_uses_three_independent_acceptance_criteria(monkeypatch):
    ids = spine.identities("controller:7", "tenant")
    state = {"review_status": None, "evidence": False, "objective_state": "active"}
    calls = []

    def fake_one(_tenant, query, _args):
        if "FROM work_contracts" in query:
            return ("ship", {"qa": "pass"}, "build-team", "controller")
        if "FROM assurance_reviews" in query:
            return (state["review_status"],) if state["review_status"] else None
        if "FROM assurance_evidence" in query:
            return (1,) if state["evidence"] else None
        if "state,version FROM strategic_objectives" in query:
            return (state["objective_state"], 1)
        if "state FROM strategic_objectives" in query:
            return (state["objective_state"],)
        return None

    monkeypatch.setattr(spine, "_one", fake_one)
    monkeypatch.setattr(spine.assurance_learning, "ensure", lambda: True)

    def open_review(*args, **kwargs):
        state["review_status"] = "awaiting_evidence"
        calls.append(("open", args, kwargs))

    def add_evidence(*args, **kwargs):
        state["evidence"] = True
        state["review_status"] = "ready"
        calls.append(("evidence", args, kwargs))

    def decide(*args, **kwargs):
        state["review_status"] = args[3]
        calls.append(("decide", args, kwargs))

    def change_state(*args, **kwargs):
        state["objective_state"] = args[2]
        calls.append(("objective", args, kwargs))

    monkeypatch.setattr(spine.assurance_learning, "open_review", open_review)
    monkeypatch.setattr(spine.assurance_learning, "add_evidence", add_evidence)
    monkeypatch.setattr(spine.assurance_learning, "decide", decide)
    monkeypatch.setattr(spine.objectiveportfolio, "change_state", change_state)

    out = spine.record_assurance(
        "tenant", "controller:7",
        {"qa_ok": True, "stories": 3, "blocking_open": 0, "verdict": "green"})

    assert out["review_id"] == ids["review_id"]
    assert out["verdict"] == "accepted"
    assert all(out["criteria"].values())
    decision = next(c for c in calls if c[0] == "decide")
    assert decision[1][2] == "qa-gate-auditor"
    assert decision[1][3] == "accepted"
    assert state["objective_state"] == "achieved"
