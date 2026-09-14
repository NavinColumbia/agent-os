import sys
import types
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import authority
import agent_request


def test_reversible_internal_work_never_becomes_human_gate():
    confident = authority.evaluate("internal_recovery", {"confidence": 0.99})
    uncertain = authority.evaluate("internal_recovery", {"confidence": 0.2})
    assert confident["disposition"] == "delegated"
    assert uncertain["disposition"] == "manager_review"
    assert confident["authority_gap"] == uncertain["authority_gap"] == "none"


@pytest.mark.parametrize("kind", ["credential", "legal", "irreversible"])
def test_intrinsic_authority_boundary_requires_human(kind):
    out = authority.evaluate(kind, {"confidence": 1.0, "reason": "specific boundary"})
    assert out["disposition"] == "human_required"
    assert out["authority_gap"] == kind


def test_spend_uses_standing_per_action_and_campaign_envelope():
    env = authority.normalize_envelope({"spend": {"per_action_usd": 20, "campaign_usd": 50}})
    assert authority.evaluate("spend", {"amount_usd": 12, "campaign_spent_usd": 30}, env)[
        "disposition"] == "delegated"
    over = authority.evaluate("spend", {"amount_usd": 12, "campaign_spent_usd": 45}, env)
    assert over["disposition"] == "human_required"
    assert over["authority_gap"] == "spend"


def test_reversible_business_judgment_is_delegated_but_high_risk_routes_to_manager():
    assert authority.evaluate("business", {"risk": "medium", "reversible": True})[
        "disposition"] == "delegated"
    high = authority.evaluate("business", {"risk": "high", "reversible": True})
    assert high["disposition"] == "manager_review"
    assert high["authority_gap"] == "none"
    ceo = authority.evaluate("business", {
        "risk": "high", "reversible": True, "management_exhausted": True,
        "requires_ceo_business_judgment": True,
    })
    assert ceo["disposition"] == "human_required"
    assert ceo["authority_gap"] == "business"


def test_envelope_cannot_pretend_intrinsic_human_authority_exists():
    with pytest.raises(ValueError, match="intrinsic human boundaries"):
        authority.normalize_envelope({"delegated_kinds": ["internal_recovery", "legal"]})


def _clean(tid):
    with authority.connection() as c, c.cursor() as cur:
        cur.execute("DELETE FROM authority_reviews WHERE tenant_id=%s", (tid,))
        cur.execute("DELETE FROM authority_decisions WHERE tenant_id=%s", (tid,))
        cur.execute("DELETE FROM authority_envelopes WHERE tenant_id=%s", (tid,))


def test_durable_internal_review_is_state_triggered_and_manager_resolved(monkeypatch):
    tid = f"authority-test-{uuid.uuid4().hex}"
    monkeypatch.setattr(authority.audit, "append", lambda **kw: (0, "test"))
    try:
        routed = authority.open_decision(
            tid, "qa:story-7", "internal_recovery", {"confidence": 0.1},
            correlation_id=f"qa:story-7:{tid}", review_after_s=3600)
        assert routed["status"] == "manager_review"
        assert routed["agent_request_id"] is None

        changed = authority.state_changed(routed["id"], tid, {"test": "timed out"})
        assert changed["review_due"] is True
        due = {r["id"]: r for r in authority.due_reviews()}
        assert due[routed["id"]]["kind"] == "internal_recovery"

        resolved = authority.record_review(
            routed["id"], tid, "qa-manager", "reassign", "fresh worker has the needed package",
            trigger="state_change")
        assert resolved["status"] == "resolved"
        assert authority.decision(routed["id"], tid)["status"] == "resolved"
    finally:
        _clean(tid)


def test_human_gate_is_idempotently_correlated_and_answer_reconciles(monkeypatch):
    tid = f"authority-test-{uuid.uuid4().hex}"
    corr = f"missing-credential:{tid}"
    calls = []

    def request(t, question, **kwargs):
        calls.append((t, question, kwargs))
        return {"request_id": 984321, "status": "open"}

    monkeypatch.setattr(authority.audit, "append", lambda **kw: (0, "test"))
    try:
        first = authority.open_decision(
            tid, "deploy:payments", "credential",
            {"question": "Connect the payment provider credential", "reason": "credential absent"},
            correlation_id=corr, request_human=request)
        second = authority.open_decision(
            tid, "deploy:payments", "credential", correlation_id=corr, request_human=request)
        assert first["status"] == "human_required"
        assert first["agent_request_id"] == 984321
        assert second["duplicate"] is True
        assert len(calls) == 1

        fake = types.ModuleType("agent_request")
        fake.is_answered = lambda rid, tenant_id=None: rid == 984321 and tenant_id == tid
        monkeypatch.setitem(sys.modules, "agent_request", fake)
        assert authority.reconcile_human_answers() == [first["id"]]
        assert authority.decision(first["id"], tid)["status"] == "resolved"
    finally:
        _clean(tid)


def test_agent_request_correlation_prevents_duplicate_ceo_page(monkeypatch):
    tid = f"authority-test-{uuid.uuid4().hex}"
    sent = []
    fake_notifications = types.ModuleType("notifications")
    fake_notifications.send = lambda *args, **kwargs: sent.append((args, kwargs)) or {"accepted": ["in_app"]}
    monkeypatch.setitem(sys.modules, "notifications", fake_notifications)
    monkeypatch.setattr(agent_request.audit, "append", lambda **kw: (0, "test"))
    try:
        first = agent_request.ask(tid, "Approve the external action?", kind="decision",
                                  correlation_id="authority:deploy-1")
        second = agent_request.ask(tid, "Approve the external action?", kind="decision",
                                   correlation_id="authority:deploy-1")
        assert first["request_id"] == second["request_id"]
        assert first["duplicate"] is False and second["duplicate"] is True
        assert len(sent) == 1
    finally:
        with authority.connection() as c, c.cursor() as cur:
            cur.execute("DELETE FROM agent_requests WHERE tenant_id=%s", (tid,))
