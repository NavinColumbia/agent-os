import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import authority
import decisionchain
from dbpool import connection


def _tenant(label):
    return f"decision-chain-{label}-{uuid.uuid4().hex}"


def _cleanup(tid):
    with connection() as c, c.cursor() as cur:
        cur.execute("""DELETE FROM agentic_decision_reviews WHERE tenant_id=%s""", (tid,))
        cur.execute("""DELETE FROM agentic_decisions WHERE tenant_id=%s""", (tid,))
        cur.execute("""DELETE FROM authority_reviews WHERE tenant_id=%s""", (tid,))
        cur.execute("""DELETE FROM authority_decisions WHERE tenant_id=%s""", (tid,))
        cur.execute("""DELETE FROM authority_envelopes WHERE tenant_id=%s""", (tid,))


def test_confident_worker_makes_reversible_decision_locally(monkeypatch):
    tid = _tenant("local")
    monkeypatch.setattr(decisionchain.audit, "append", lambda **_kw: None)
    try:
        out = decisionchain.decide(
            tid, 11, "option:11", "option_selection", {"candidates": ["a", "b"]},
            correlation_id="local:" + tid,
            decide_fn=lambda role, _kind, _state: {
                "action": "select", "selection": "b", "confidence": 0.94,
                "rationale": "best reversible fit", "boundary": "none"})
        assert out["status"] == "resolved" and out["selection"] == "b"
        assert out["decided_by"] == "product-owner" and out["tier"] == 0
    finally:
        _cleanup(tid)


def test_low_confidence_escalates_internally_then_manager_decides(monkeypatch):
    tid = _tenant("escalate")
    calls = []
    monkeypatch.setattr(decisionchain.audit, "append", lambda **_kw: None)

    def decide(role, _kind, _state):
        calls.append(role)
        if role == "product-owner":
            return {"action": "select", "selection": "a", "confidence": 0.2,
                    "rationale": "uncertain", "boundary": "none"}
        return {"action": "experiment", "selection": "b", "confidence": 0.88,
                "rationale": "manager bounded the uncertainty", "boundary": "none"}

    try:
        out = decisionchain.decide(tid, 12, "option:12", "option_selection", {},
                                   correlation_id="escalate:" + tid, decide_fn=decide)
        assert calls == ["product-owner", "product-manager"]
        assert out["status"] == "resolved" and out["decided_by"] == "product-manager"
        assert out["selection"] == "b"
    finally:
        _cleanup(tid)


def test_only_typed_validated_boundary_pages_human(monkeypatch):
    tid = _tenant("boundary")
    pages = []
    monkeypatch.setattr(decisionchain.audit, "append", lambda **_kw: None)

    def decide(_role, _kind, _state):
        return {"action": "request_human", "confidence": 0.99, "boundary": "credential",
                "question": "Connect the deployment credential", "rationale": "credential is absent"}

    def page(tenant, question, **kwargs):
        pages.append((tenant, question, kwargs))
        return {"request_id": 778899}

    try:
        out = decisionchain.decide(tid, 13, "deploy:13", "prototype_acceptance", {},
                                   correlation_id="boundary:" + tid, decide_fn=decide,
                                   request_human=page)
        assert out["status"] == "human_wait" and out["boundary"] == "credential"
        assert out["decided_by"] == "product-manager" and len(pages) == 1
    finally:
        _cleanup(tid)


def test_crash_after_review_reuses_committed_tier(monkeypatch):
    tid = _tenant("crash")
    calls = []
    crashed = {"done": False}
    monkeypatch.setattr(decisionchain.audit, "append", lambda **_kw: None)

    def decide(role, _kind, _state):
        calls.append(role)
        if role == "product-owner":
            return {"action": "proceed", "confidence": 0.1,
                    "rationale": "needs review", "boundary": "none"}
        return {"action": "proceed", "confidence": 0.91,
                "rationale": "reviewed", "boundary": "none"}

    def crash(tier, _review):
        if tier == 0 and not crashed["done"]:
            crashed["done"] = True
            raise RuntimeError("process died after durable review")

    corr = "crash:" + tid
    try:
        with pytest.raises(RuntimeError):
            decisionchain.decide(tid, 14, "plan:14", "plan_acceptance", {},
                                 correlation_id=corr, decide_fn=decide, after_review=crash)
        out = decisionchain.decide(tid, 14, "plan:14", "plan_acceptance", {},
                                   correlation_id=corr, decide_fn=decide)
        again = decisionchain.decide(tid, 14, "plan:14", "plan_acceptance", {},
                                     correlation_id=corr, decide_fn=lambda *_: pytest.fail("replayed actor"))
        assert calls == ["product-owner", "product-manager"]
        assert out["status"] == again["status"] == "resolved"
        assert again["duplicate"] is True
    finally:
        _cleanup(tid)


def test_uncertainty_exhaustion_uses_reversible_fallback_not_silent_gate(monkeypatch):
    tid = _tenant("fallback")
    pages = []
    monkeypatch.setattr(decisionchain.audit, "append", lambda **_kw: None)
    try:
        out = decisionchain.decide(
            tid, 15, "prototype:15", "prototype_acceptance", {},
            correlation_id="fallback:" + tid,
            decide_fn=lambda *_: {"action": "continue", "confidence": 0.05,
                                  "rationale": "uncertain", "boundary": "none"},
            default={"action": "experiment", "selection": "bounded-prototype",
                     "rationale": "ship a reversible internal experiment"},
            request_human=lambda *a, **k: pages.append((a, k)))
        assert out["status"] == "resolved" and out["fallback"] is True
        assert out["action"] == "experiment" and pages == []
    finally:
        _cleanup(tid)


def test_business_boundary_reaches_human_only_after_senior_review(monkeypatch):
    tid = _tenant("business")
    pages = []
    roles = []
    monkeypatch.setattr(decisionchain.audit, "append", lambda **_kw: None)

    def decide(role, _kind, _state):
        roles.append(role)
        return {"action": "request_human", "confidence": 0.95, "boundary": "business",
                "risk": "high", "reversible": True,
                "question": "Choose which regulated market to enter",
                "rationale": "the objective does not reserve this market choice"}

    def page(tenant, question, **kwargs):
        pages.append((tenant, question, kwargs))
        return {"request_id": 889900}

    try:
        out = decisionchain.decide(tid, 16, "market:16", "market_selection", {},
                                   correlation_id="business:" + tid, decide_fn=decide,
                                   request_human=page)
        assert roles == list(decisionchain.ROLES)
        assert out["status"] == "human_wait" and out["boundary"] == "business"
        assert out["decided_by"] == "senior-product-director" and len(pages) == 1
    finally:
        _cleanup(tid)


def test_answered_authority_request_resumes_once(monkeypatch):
    tid = _tenant("answer")
    request_id = 990000 + int(uuid.uuid4().hex[:5], 16)
    monkeypatch.setattr(decisionchain.audit, "append", lambda **_kw: None)

    def decide(_role, _kind, _state):
        return {"action": "request_human", "confidence": 0.99, "boundary": "credential",
                "question": "Connect credential", "rationale": "credential missing"}

    def page(_tenant, _question, **_kwargs):
        with connection() as c, c.cursor() as cur:
            cur.execute("""INSERT INTO agent_requests
                (id,tenant_id,thread_id,kind,question,status,correlation_id)
                VALUES (%s,%s,17,'credential','Connect credential','open',%s)""",
                (request_id, tid, "answer:" + tid))
        return {"request_id": request_id}

    try:
        out = decisionchain.decide(tid, 17, "deploy:17", "prototype_acceptance", {},
                                   correlation_id="answer-decision:" + tid, decide_fn=decide,
                                   request_human=page)
        assert out["status"] == "human_wait"
        with connection() as c, c.cursor() as cur:
            cur.execute("""UPDATE agent_requests SET status='answered',answer='connected',answered_at=now()
                           WHERE id=%s""", (request_id,))
        resumed = decisionchain.reconcile_human_answers(
            answer_fn=lambda *_: {"action": "proceed", "rationale": "credential connected"})
        assert len(resumed) == 1 and resumed[0]["outcome"]["action"] == "proceed"
        replay = decisionchain.reconcile_human_answers(answer_fn=lambda *_: pytest.fail("replayed interpreter"))
        assert len(replay) == 1 and replay[0]["id"] == resumed[0]["id"]
        assert decisionchain.needs_application(resumed[0]["id"], tid)
        assert decisionchain.mark_applied(resumed[0]["id"], tid)
        assert not decisionchain.needs_application(resumed[0]["id"], tid)
        assert decisionchain.reconcile_human_answers(answer_fn=lambda *_: pytest.fail("replayed")) == []
    finally:
        with connection() as c, c.cursor() as cur:
            cur.execute("DELETE FROM agent_requests WHERE tenant_id=%s", (tid,))
        _cleanup(tid)


def test_explicit_fixed_cap_answer_is_applied_without_model_reinterpretation(monkeypatch):
    import factory

    monkeypatch.setattr(factory, "agent", lambda *_a, **_k: pytest.fail("explicit human answer used a model"))
    state = {"requested_cap_usd": 650, "amount_usd": 150, "campaign_spent_usd": 519.56}
    assert decisionchain._interpret_human("Authorize the cap?", "Approve $650 tracked cap", state)[
        "action"] == "proceed"
    assert decisionchain._interpret_human("Authorize the cap?", "approve $130.44 more", state)[
        "action"] == "proceed"
    assert decisionchain._interpret_human("Authorize the cap?", "Decline", state)[
        "action"] == "cancel"
    assert decisionchain._interpret_human("Authorize the cap?", "approve USD 700", state)[
        "action"] == "revise"


def test_senior_fallback_spend_boundary_still_opens_correlated_request(monkeypatch):
    tid = _tenant("fallback-spend")
    pages = []
    monkeypatch.setattr(decisionchain.audit, "append", lambda **_kw: None)
    try:
        out = decisionchain.decide(
            tid, 18, "budget:18", "build_budget_extension",
            {"amount_usd": 30, "campaign_spent_usd": 90},
            correlation_id="fallback-spend:" + tid,
            decide_fn=lambda *_: {"action": "retry", "confidence": 0.01,
                                  "rationale": "uncertain", "boundary": "none"},
            default={"action": "request_human", "boundary": "spend", "confidence": 1,
                     "rationale": "additional spend exceeds standing authority"},
            request_human=lambda tenant, question, **kwargs: pages.append(
                (tenant, question, kwargs)) or {"request_id": 991122})
        assert out["status"] == "human_wait" and out["boundary"] == "spend"
        assert len(pages) == 1 and out["authority_decision_id"] is not None
    finally:
        _cleanup(tid)


def test_spend_reviewer_cannot_rewrite_durable_ledger_facts(monkeypatch):
    tid = _tenant("immutable-spend-facts")
    pages = []
    monkeypatch.setattr(decisionchain.audit, "append", lambda **_kw: None)

    def decide(role, *_args):
        if role == "product-owner":
            return {"action": "continue", "confidence": 0.01, "boundary": "none",
                    "rationale": "manager review needed"}
        return {"action": "request_human", "confidence": 0.99, "boundary": "spend",
                "question": "Approve a misleading amount?", "amount_usd": 1,
                "campaign_spent_usd": 0, "rationale": "cap needs approval"}

    try:
        out = decisionchain.decide(
            tid, 19, "budget:19", "qa_budget_extension",
            {"product": "dog-app", "question": "Approve cap $500 to $650?",
             "amount_usd": 150, "campaign_spent_usd": 519.56,
             "budget_usd": 500, "requested_cap_usd": 650},
            correlation_id="immutable-spend:" + tid, authority_kind="spend",
            decide_fn=decide,
            request_human=lambda tenant, question, **kwargs: pages.append(
                (tenant, question, kwargs)) or {"request_id": 991123})
        assert out["status"] == "human_wait"
        assert pages[0][1] == "Approve cap $500 to $650?"
        with connection() as c, c.cursor() as cur:
            cur.execute("SELECT proposal FROM authority_decisions WHERE id=%s",
                        (out["authority_decision_id"],))
            proposal = cur.fetchone()[0]
        assert proposal["amount_usd"] == 150
        assert proposal["campaign_spent_usd"] == 519.56
        assert proposal["budget_usd"] == 500
        assert proposal["requested_cap_usd"] == 650
        assert proposal["product"] == "dog-app"
    finally:
        _cleanup(tid)
