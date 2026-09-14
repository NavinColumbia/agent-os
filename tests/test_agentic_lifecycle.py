"""Deterministic adoption tests for lifecycle decisions (no external workers)."""
import contextlib
import json
import sys
import types
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import loopcontroller as lc
from dbpool import connection


def _fixture(label, phase, **fields):
    lc._ensure()
    tid = f"agentic-life-{label}-{uuid.uuid4().hex}"
    thread_id = 997000000 + int(uuid.uuid4().hex[:6], 16)
    with connection() as c, c.cursor() as cur:
        cur.execute("""INSERT INTO controller_state
            (thread_id,tenant_id,org_id,phase,awaiting,plan,product,updated_at,execution_scope)
            VALUES (%s,%s,1,%s,%s,%s,%s,now(),'test')""",
            (thread_id, tid, phase, fields.get("awaiting"),
             json.dumps(fields["plan"]) if fields.get("plan") is not None else None,
             fields.get("product")))
    return tid, thread_id


def _cleanup(thread_id):
    with connection() as c, c.cursor() as cur:
        cur.execute("DELETE FROM controller_jobs WHERE thread_id=%s", (thread_id,))
        cur.execute("DELETE FROM controller_state WHERE thread_id=%s", (thread_id,))


def _quiet(monkeypatch):
    monkeypatch.setattr(lc, "_report", lambda *_a, **_k: None)
    monkeypatch.setattr(lc, "_ping", lambda *_a, **_k: None)
    monkeypatch.setattr(lc.audit, "append", lambda **_k: None)
    monkeypatch.setattr(lc, "_job_clear", lambda *_a, **_k: None)


def test_options_completion_is_decided_internally_and_remains_runnable(monkeypatch):
    tid, thread_id = _fixture("options", "RESEARCH")
    _quiet(monkeypatch)
    decisions = []
    recursive = []
    fake_research = types.ModuleType("research")
    fake_research.select = lambda *_a: {"option_id": "b", "title": "B"}
    monkeypatch.setitem(sys.modules, "research", fake_research)
    monkeypatch.setattr(lc, "_research_report_for_run", lambda *_a: {"report": "evidence"})

    def decide(*args, **kwargs):
        decisions.append((args, kwargs))
        return {"status": "resolved", "action": "select", "selection": "b",
                "confidence": 0.9, "decided_by": "product-owner"}

    monkeypatch.setattr(lc, "_agentic_lifecycle_decision", decide)
    original = lc.advance

    def wrapped(tid_arg, job_result=None):
        if job_result is None:
            recursive.append(tid_arg)
            return None
        return original(tid_arg, job_result)

    monkeypatch.setattr(lc, "advance", wrapped)
    try:
        wrapped(thread_id, {"run_id": 444, "options": [
            {"id": "a", "title": "A"}, {"id": "b", "title": "B", "recommended": True}]})
        state = lc._st(thread_id)
        assert len(decisions) == 1 and recursive == [thread_id]
        assert state["phase"] == "DEEP_DESIGN" and state["awaiting"] is None
        assert state["chosen_option"]["option_id"] == "b"
    finally:
        _cleanup(thread_id)


def test_plan_review_advances_without_approval_gate(monkeypatch):
    plan = {"name": "app", "platform": "web", "kind": "web", "plan": ["build"]}
    tid, thread_id = _fixture("plan", "DEEP_DESIGN", plan=plan)
    _quiet(monkeypatch)
    advanced = []
    monkeypatch.setattr(lc, "_agentic_lifecycle_decision", lambda *_a, **_k: {
        "status": "resolved", "action": "proceed", "confidence": 0.91,
        "decided_by": "product-manager"})
    monkeypatch.setattr(lc, "_advance_owned", lambda value, *_a, **_k: advanced.append(value))
    try:
        out = lc._agentic_plan_review(tid, thread_id, lc._st(thread_id))
        state = lc._st(thread_id)
        assert out["advanced"] is True and advanced == [thread_id]
        assert state["phase"] == "PLAN_APPROVAL" and state["awaiting"] is None
    finally:
        _cleanup(thread_id)


def test_typed_plan_boundary_is_the_only_human_gate(monkeypatch):
    plan = {"name": "app", "platform": "web", "kind": "web"}
    tid, thread_id = _fixture("boundary", "DEEP_DESIGN", plan=plan)
    _quiet(monkeypatch)
    monkeypatch.setattr(lc, "_agentic_lifecycle_decision", lambda *_a, **_k: {
        "status": "human_wait", "action": "request_human", "boundary": "legal",
        "decided_by": "product-manager"})
    try:
        out = lc._agentic_plan_review(tid, thread_id, lc._st(thread_id))
        assert out["awaiting"] == "user_feedback"
        assert lc._st(thread_id)["awaiting"] == "user_feedback"
    finally:
        _cleanup(thread_id)


def test_prototype_review_proceeds_without_silent_gate(monkeypatch):
    plan = {"name": "app", "platform": "web", "kind": "web"}
    tid, thread_id = _fixture("prototype", "PROTOTYPE", plan=plan, product="app")
    _quiet(monkeypatch)
    recursive = []
    monkeypatch.setattr(lc, "_agentic_lifecycle_decision", lambda *_a, **_k: {
        "status": "resolved", "action": "proceed", "confidence": 0.93,
        "decided_by": "product-manager"})
    original = lc.advance

    def wrapped(tid_arg, job_result=None):
        if job_result is None:
            recursive.append(tid_arg)
            return None
        return original(tid_arg, job_result)

    monkeypatch.setattr(lc, "advance", wrapped)
    try:
        wrapped(thread_id, {"screens": 3, "surfaces": ["cockpit", "external"]})
        state = lc._st(thread_id)
        assert recursive == [thread_id]
        assert state["phase"] == "IMPLEMENT" and state["awaiting"] is None
    finally:
        _cleanup(thread_id)


def test_exhausted_build_retry_is_managed_internally(monkeypatch):
    plan = {"name": "app", "platform": "web", "kind": "web"}
    tid, thread_id = _fixture("build-retry", "IMPLEMENT", plan=plan, product="app")
    _quiet(monkeypatch)
    advanced = []
    fake_registry = types.ModuleType("productregistry")
    fake_registry.attempt = lambda *_a: lc.MAX_BUILD_RETRY + 1
    monkeypatch.setitem(sys.modules, "productregistry", fake_registry)
    monkeypatch.setattr(lc, "_spend_usd", lambda *_a: 0.0)
    monkeypatch.setattr(lc, "_agentic_lifecycle_decision", lambda *_a, **_k: {
        "status": "resolved", "action": "reassign", "confidence": 0.9,
        "decided_by": "senior-engineering-manager"})
    monkeypatch.setattr(lc, "advance", lambda value, *_a, **_k: advanced.append(value))
    try:
        lc._autoloop_build(thread_id, tid, "app", reason="verification failed")
        state = lc._st(thread_id)
        assert advanced == [thread_id]
        assert state["phase"] == "IMPLEMENT" and state["awaiting"] is None
    finally:
        _cleanup(thread_id)


def test_build_retry_parks_only_for_typed_authority_result(monkeypatch):
    tid, thread_id = _fixture("build-boundary", "IMPLEMENT", product="app")
    _quiet(monkeypatch)
    fake_registry = types.ModuleType("productregistry")
    fake_registry.attempt = lambda *_a: lc.MAX_BUILD_RETRY + 1
    monkeypatch.setitem(sys.modules, "productregistry", fake_registry)
    monkeypatch.setattr(lc, "_spend_usd", lambda *_a: lc.BUILD_BUDGET_USD + 1)
    monkeypatch.setattr(lc, "_product_spend_policy", lambda *_a: {
        "cap": lc.BUILD_BUDGET_USD, "status": "active", "reason": None})
    monkeypatch.setattr(lc, "_agentic_lifecycle_decision", lambda *_a, **_k: {
        "status": "human_wait", "action": "request_human", "boundary": "spend"})
    monkeypatch.setattr(lc, "advance", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("human boundary must not advance")))
    try:
        lc._autoloop_build(thread_id, tid, "app", reason="verification failed")
        assert lc._st(thread_id)["awaiting"] == "user_feedback"
    finally:
        _cleanup(thread_id)


def test_build_retry_honors_the_persisted_product_cap(monkeypatch):
    tid, thread_id = _fixture("build-raised-cap", "IMPLEMENT", product="app")
    _quiet(monkeypatch)
    fake_registry = types.ModuleType("productregistry")
    fake_registry.attempt = lambda *_a: lc.MAX_BUILD_RETRY + 1
    monkeypatch.setitem(sys.modules, "productregistry", fake_registry)
    monkeypatch.setattr(lc, "_spend_usd", lambda *_a: 1084.45)
    monkeypatch.setattr(lc, "_product_spend_policy", lambda *_a: {
        "cap": 10000.0, "status": "active", "reason": None})
    decisions, advanced = [], []

    def decide(*args, **kwargs):
        decisions.append((args, kwargs))
        return {"status": "resolved", "action": "reassign", "confidence": 0.9,
                "decided_by": "senior-product-director"}

    monkeypatch.setattr(lc, "_agentic_lifecycle_decision", decide)
    monkeypatch.setattr(lc, "advance", lambda value, *_a, **_k: advanced.append(value))
    try:
        lc._autoloop_build(thread_id, tid, "app", reason="45 findings remain")
        args, _kwargs = decisions[0]
        assert args[2] == "build_recovery"
        assert args[3]["budget_usd"] == 10000.0
        assert args[3]["amount_usd"] == 0.0
        assert advanced == [thread_id]
        assert lc._st(thread_id)["awaiting"] is None
    finally:
        _cleanup(thread_id)


def test_build_budget_request_names_actual_spend_cap_and_requested_cap(monkeypatch):
    tid, thread_id = _fixture("build-cap-question", "IMPLEMENT", product="app")
    _quiet(monkeypatch)
    fake_registry = types.ModuleType("productregistry")
    fake_registry.attempt = lambda *_a: lc.MAX_BUILD_RETRY + 1
    monkeypatch.setitem(sys.modules, "productregistry", fake_registry)
    monkeypatch.setattr(lc, "_spend_usd", lambda *_a: 1084.45)
    monkeypatch.setattr(lc, "_product_spend_policy", lambda *_a: {
        "cap": 650.0, "status": "active", "reason": None})
    decisions = []

    def decide(*args, **kwargs):
        decisions.append((args, kwargs))
        return {"status": "human_wait", "action": "request_human", "boundary": "spend"}

    monkeypatch.setattr(lc, "_agentic_lifecycle_decision", decide)
    try:
        lc._autoloop_build(thread_id, tid, "app", reason="release is not clean")
        args, _kwargs = decisions[0]
        state = args[3]
        assert args[2] == "build_budget_extension"
        assert state["requested_cap_usd"] == 1200.0
        assert state["amount_usd"] == 115.55
        assert "$1084.45" in state["question"]
        assert "$650.00" in state["question"]
        assert "$1200.00" in state["question"]
        assert lc._st(thread_id)["awaiting"] == "user_feedback"
    finally:
        _cleanup(thread_id)


def test_approved_build_budget_is_applied_before_resume(monkeypatch):
    tid, thread_id = _fixture(
        "build-cap-apply", "IMPLEMENT", product="app", awaiting="user_feedback")
    _quiet(monkeypatch)
    calls, advanced = [], []
    fake_appguard = types.ModuleType("appguard")
    fake_appguard._policy = lambda *_a: {"spend_cap": 650.0, "loss_limit": 100.0}
    fake_appguard.set_policy = lambda app, **kw: calls.append(("policy", app, kw))
    fake_appguard.resume = lambda app: calls.append(("resume", app))
    fake_killswitch = types.ModuleType("killswitch")
    fake_killswitch.is_halted = lambda *_a: {"halted": False}
    fake_killswitch.resume = lambda scope: calls.append(("kill-resume", scope))
    monkeypatch.setitem(sys.modules, "appguard", fake_appguard)
    monkeypatch.setitem(sys.modules, "killswitch", fake_killswitch)
    monkeypatch.setattr(lc, "_spend_usd", lambda *_a: 700.0)
    monkeypatch.setattr(lc, "advance", lambda value, *_a, **_k: advanced.append(value))
    item = {
        "id": 7,
        "decision_type": "build_budget_extension",
        "tenant_id": tid,
        "thread_id": thread_id,
        "state": {"product": "app", "requested_cap_usd": 900.0},
        "outcome": {"action": "proceed", "human_answer": "approved"},
    }
    try:
        assert lc._apply_build_budget_answer(item, lc._st(thread_id)) is True
        assert ("policy", "app", {"cap": 900.0, "loss": 100.0}) in calls
        assert ("resume", "app") in calls
        assert lc._st(thread_id)["awaiting"] is None
        assert advanced == [thread_id]
    finally:
        _cleanup(thread_id)


def test_answered_agentic_gate_wakes_durable_phase(monkeypatch):
    plan = {"name": "app", "platform": "web", "kind": "web"}
    tid, thread_id = _fixture("answer-resume", "DEEP_DESIGN", awaiting="user_feedback", plan=plan)
    _quiet(monkeypatch)
    fake = types.ModuleType("decisionchain")
    fake.reconcile_human_answers = lambda *_a, **_k: [{
        "id": 1, "tenant_id": tid, "thread_id": thread_id, "decision_type": "plan_acceptance",
        "outcome": {"action": "proceed", "human_answer": "approved"}}]
    fake.needs_application = lambda *_a, **_k: True
    fake.mark_applied = lambda *_a, **_k: True
    monkeypatch.setitem(sys.modules, "decisionchain", fake)
    advanced = []

    @contextlib.contextmanager
    def owned(_thread_id):
        yield True

    monkeypatch.setattr(lc, "thread_drive_lock", owned)
    monkeypatch.setattr(lc, "advance", lambda value, *_a, **_k: advanced.append(value))
    try:
        assert lc._resume_agentic_answers(execution_scope="test") == 1
        assert advanced == [thread_id]
        assert lc._st(thread_id)["awaiting"] is None
    finally:
        _cleanup(thread_id)


def test_answered_gate_is_not_acknowledged_before_controller_side_effect(monkeypatch):
    """A crash during application leaves the resolved answer replayable on the next sweep."""
    plan = {"name": "app", "platform": "web", "kind": "web"}
    tid, thread_id = _fixture("answer-crash", "DEEP_DESIGN", awaiting="user_feedback", plan=plan)
    _quiet(monkeypatch)
    fake = types.ModuleType("decisionchain")
    item = {"id": 22, "tenant_id": tid, "thread_id": thread_id,
            "decision_type": "plan_acceptance",
            "outcome": {"action": "proceed", "human_answer": "approved"}}
    fake.reconcile_human_answers = lambda *_a, **_k: [item]
    fake.needs_application = lambda *_a, **_k: True
    acknowledged = []
    fake.mark_applied = lambda *_a, **_k: acknowledged.append(True) or True
    monkeypatch.setitem(sys.modules, "decisionchain", fake)

    @contextlib.contextmanager
    def owned(_thread_id):
        yield True

    monkeypatch.setattr(lc, "thread_drive_lock", owned)
    attempts = {"count": 0}

    def advance(_thread_id, *_a, **_k):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("crash before application acknowledgement")

    monkeypatch.setattr(lc, "advance", advance)
    try:
        with pytest.raises(RuntimeError):
            lc._resume_agentic_answers(execution_scope="test")
        assert acknowledged == []
        assert lc._resume_agentic_answers(execution_scope="test") == 1
        assert attempts["count"] == 2 and acknowledged == [True]
    finally:
        _cleanup(thread_id)
