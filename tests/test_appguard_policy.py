from pathlib import Path
import sys
import uuid


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def test_first_pause_persists_the_envelope_that_fired(monkeypatch, tmp_path):
    import appguard

    calls = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def execute(self, sql, params=None):
            calls.append((sql, params))

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def cursor(self):
            return Cursor()

    product = "first-pause-product"
    (tmp_path / product).mkdir()
    monkeypatch.setattr(appguard, "PRODUCTS", tmp_path)
    monkeypatch.setattr(appguard, "_tenant_for", lambda _app: "tenant-a")
    monkeypatch.setattr(appguard, "_conn", lambda _tenant=None: Conn())
    monkeypatch.setattr(appguard.audit, "append", lambda **_kwargs: (1, None))
    monkeypatch.setattr(appguard.notify, "send", lambda *_args, **_kwargs: True)

    appguard.pause(product, "spend $500 >= cap $500",
                   policy={"spend_cap": 500, "loss_limit": 100})

    sql, params = calls[0]
    assert "spend_cap, loss_limit" in sql
    assert params == (product, 500.0, 100.0, "spend $500 >= cap $500")
    assert (tmp_path / product / "PAUSED.html").is_file()


def test_pause_marker_is_not_rewritten_when_unchanged(monkeypatch, tmp_path):
    import appguard

    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def execute(self, *_args, **_kwargs): return None

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def cursor(self): return Cursor()

    product = "stable-marker-product"
    root = tmp_path / product
    root.mkdir()
    monkeypatch.setattr(appguard, "PRODUCTS", tmp_path)
    monkeypatch.setattr(appguard, "_tenant_for", lambda _app: "tenant-a")
    monkeypatch.setattr(appguard, "_conn", lambda _tenant=None: Conn())
    monkeypatch.setattr(appguard.audit, "append", lambda **_kwargs: (1, None))
    monkeypatch.setattr(appguard.notify, "send", lambda *_args, **_kwargs: True)

    policy = {"spend_cap": 500, "loss_limit": 100}
    appguard.pause(product, "threshold", policy=policy)
    marker = root / "PAUSED.html"
    first_mtime = marker.stat().st_mtime_ns
    appguard.pause(product, "threshold", policy=policy)
    assert marker.stat().st_mtime_ns == first_mtime


def test_standalone_qa_process_context_carries_product_guard_into_worker_threads(monkeypatch):
    import appguard
    import factory

    monkeypatch.setattr(factory, "_QA_CTX", {"product": "guarded-product", "tenant": None})
    monkeypatch.setattr(factory.governance, "load_manifest", lambda _role: {})
    monkeypatch.setattr(factory.audit, "append", lambda **_kwargs: (1, None))
    monkeypatch.setattr(factory.killswitch, "is_halted", lambda _scope: {"halted": False})
    monkeypatch.setattr(appguard, "blocks", lambda product: "spend boundary" if product == "guarded-product" else None)
    monkeypatch.setattr(factory, "BUDGET_USD", 0)

    blocked = factory._chat_gates("controller", "qa-security", "/tmp/repo", "inspect")

    assert blocked["out"] == "app circuit-breaker"
    assert "guarded-product" in blocked["blocker"]


def test_qa_spend_gate_opens_one_fixed_typed_authority_request(monkeypatch):
    import appguard
    import loopcontroller as lc

    captured, updates, reports = {}, [], []
    monkeypatch.setattr(appguard, "_policy", lambda _product: {
        "status": "paused", "spend_cap": 500, "loss_limit": 100})
    monkeypatch.setattr(appguard, "economics", lambda _product: {"spend": 509.97})
    monkeypatch.setattr(appguard, "blocks", lambda _product: "spend boundary")
    monkeypatch.setattr(lc, "_agentic_lifecycle_decision",
                        lambda *args, **kwargs: captured.update(state=args[3], default=kwargs["default"]) or {
                            "status": "human_wait", "action": "request_human", "boundary": "spend"})
    monkeypatch.setattr(lc, "_job_clear", lambda thread_id: updates.append((thread_id, "clear")))
    monkeypatch.setattr(lc, "_set", lambda thread_id, **values: updates.append((thread_id, values)))
    monkeypatch.setattr(lc, "_report", lambda *args, **kwargs: reports.append((args, kwargs)))
    monkeypatch.setattr(lc.audit, "append", lambda **_kwargs: (1, None))

    assert lc._qa_spend_gate(2787, {
        "thread_id": 2787, "tenant_id": "tenant-a", "product": "dog-app"}) is False
    assert captured["state"]["requested_cap_usd"] == 650.0
    assert captured["state"]["amount_usd"] == 150.0
    assert "tracked model-cost estimate" in captured["state"]["question"]
    assert "Authorize $140.03 more" in captured["state"]["question"]
    assert captured["default"]["boundary"] == "spend"
    assert (2787, {"awaiting": "user_feedback"}) in updates
    assert reports[0][0][3]["requested_cap_usd"] == 650.0


def test_answered_qa_spend_authority_applies_exact_cap_then_resumes(monkeypatch):
    import appguard
    import killswitch
    import loopcontroller as lc

    calls = []
    monkeypatch.setattr(appguard, "_policy", lambda _product: {"loss_limit": 100})
    monkeypatch.setattr(appguard, "set_policy",
                        lambda product, cap=None, loss=None: calls.append(("set", product, cap, loss)))
    monkeypatch.setattr(appguard, "resume", lambda product: calls.append(("resume", product)))
    monkeypatch.setattr(killswitch, "is_halted", lambda scope: {"halted": True})
    monkeypatch.setattr(killswitch, "resume", lambda scope: calls.append(("kill-resume", scope)))
    monkeypatch.setattr(lc, "_set", lambda thread_id, **values: calls.append(("state", thread_id, values)))
    monkeypatch.setattr(lc, "advance", lambda thread_id: calls.append(("advance", thread_id)))
    monkeypatch.setattr(lc.audit, "append", lambda **_kwargs: (1, None))

    handled = lc._apply_qa_budget_answer({
        "id": 91, "tenant_id": "tenant-a", "thread_id": 2787,
        "decision_type": "qa_budget_extension",
        "state": {"product": "dog-app", "requested_cap_usd": 650},
        "outcome": {"action": "proceed", "human_answer": "authorize up to $650"},
    }, {"product": "dog-app"})

    assert handled is True
    assert calls == [("set", "dog-app", 650.0, 100), ("resume", "dog-app"),
                     ("kill-resume", "dog-app"), ("kill-resume", "thread-2787"),
                     ("state", 2787, {"awaiting": None}), ("advance", 2787)]


def test_answered_qa_spend_authority_clears_real_exact_stop_scopes(monkeypatch):
    import appguard
    import killswitch
    import loopcontroller as lc
    from dbpool import connection

    suffix = uuid.uuid4().hex
    product = f"qa-budget-resume-{suffix}"
    thread_id = 980000000 + int(suffix[:6], 16)
    monkeypatch.setattr(lc, "_set", lambda *_a, **_k: None)
    monkeypatch.setattr(lc, "advance", lambda *_a, **_k: None)
    monkeypatch.setattr(lc.audit, "append", lambda **_kwargs: (1, None))
    try:
        appguard.set_policy(product, cap=500, loss=100)
        with connection() as c, c.cursor() as cur:
            cur.execute("UPDATE app_policies SET status='paused',reason='test boundary' WHERE app=%s",
                        (product,))
            cur.execute("INSERT INTO kill_switch(scope,reason,set_by) VALUES (%s,'test','test')",
                        (product,))
            cur.execute("INSERT INTO kill_switch(scope,reason,set_by) VALUES (%s,'test','test')",
                        (f"thread-{thread_id}",))

        handled = lc._apply_qa_budget_answer({
            "id": 92, "tenant_id": "tenant-a", "thread_id": thread_id,
            "decision_type": "qa_budget_extension",
            "state": {"product": product, "requested_cap_usd": 650},
            "outcome": {"action": "proceed", "human_answer": "authorize $650"},
        }, {"product": product})

        assert handled is True
        policy = appguard._policy(product)
        assert policy["status"] == "active" and policy["spend_cap"] == 650
        assert not killswitch.is_halted(product)["halted"]
        assert not killswitch.is_halted(f"thread-{thread_id}")["halted"]
    finally:
        with connection() as c, c.cursor() as cur:
            cur.execute("DELETE FROM kill_switch WHERE scope=ANY(%s)",
                        ([product, f"thread-{thread_id}"],))
            cur.execute("DELETE FROM app_policies WHERE app=%s", (product,))


def test_global_app_policy_never_uses_tenant_app_role(monkeypatch):
    import appguard
    from dbpool import connection

    product = f"global-policy-{uuid.uuid4().hex}"
    calls = []
    real_conn = appguard._conn
    monkeypatch.setattr(appguard, "_tenant_for", lambda _app: "tenant-must-not-own-global-policy")
    monkeypatch.setattr(appguard, "_conn", lambda tenant_id=None: (
        calls.append(tenant_id) or real_conn(tenant_id)))
    try:
        appguard.set_policy(product, cap=321, loss=45)
        policy = appguard._policy(product)
        assert policy["spend_cap"] == 321 and policy["loss_limit"] == 45
        assert calls and all(tenant_id is None for tenant_id in calls)
    finally:
        with connection() as c, c.cursor() as cur:
            cur.execute("DELETE FROM app_policies WHERE app=%s", (product,))


def test_guard_isolates_one_app_failure_and_continues_tail(monkeypatch):
    import appguard

    monkeypatch.setattr(appguard, "_guard_rows", lambda: [
        ("broken", 10, 2, "active", None, 12, 100),
        ("healthy", 10, 2, "active", None, 0, 0),
    ])
    monkeypatch.setattr(appguard, "pause", lambda app, *_a, **_k: (
        (_ for _ in ()).throw(RuntimeError("fixture pause failed")) if app == "broken" else None))
    result = appguard.guard()
    assert result["evaluated"] == 2 and result["failed"] == 1
    assert result["errors"][0]["app"] == "broken"


def test_atomic_reservation_decision_counts_concurrent_inflight_headroom():
    import appguard

    assert appguard._reservation_allowed(640, 4, 6, 650, "active") is True
    assert appguard._reservation_allowed(640, 5, 6, 650, "active") is False
    assert appguard._reservation_allowed(100, 0, 1, 650, "paused") is False


def test_factory_uses_small_bounded_envelopes_only_for_isolated_qa_calls():
    import factory

    assert factory._model_call_reserve_usd("qa-security", "gpt-5.6-sol", "judge") == 1.5
    assert factory._model_call_reserve_usd(
        "reviewer", "gpt-5.6-sol", "SEALED EVIDENCE CAPSULE") == 1.5
    assert factory._model_call_reserve_usd("dev-fixer", "gpt-5.6-sol", "edit repo") == 10.0
    assert factory._model_call_reserve_usd("qa-security", "gpt-5.6-luna", "decide") == 0.25


def test_warm_api_call_reserves_then_settles_actual_cost(monkeypatch):
    import factory

    calls = []
    monkeypatch.setattr(factory, "_reserve_model_call", lambda role, model, prompt: (
        calls.append(("reserve", role, model, prompt)) or {"ok": True, "token": "r-1"}))
    monkeypatch.setattr(factory, "_api_once", lambda prompt, model, key, timeout: (
        calls.append(("provider", prompt, model, key, timeout)) or
        (0, "done", 0.42, 11, 7, model)))
    monkeypatch.setattr(factory, "_settle_model_call", lambda reservation, cost, **kwargs: (
        calls.append(("settle", reservation["token"], cost, kwargs))))

    result = factory._reserved_api_once("assistant", "hello", "fast-model", "key", 30)

    assert result[:3] == (0, "done", 0.42)
    assert calls == [
        ("reserve", "assistant", "fast-model", "hello"),
        ("provider", "hello", "fast-model", "key", 30),
        ("settle", "r-1", 0.42, {}),
    ]


def test_streaming_call_does_not_touch_provider_when_reservation_is_denied(monkeypatch):
    import factory

    monkeypatch.setattr(factory, "_reserve_model_call", lambda *_args: {
        "ok": False, "token": None, "reason": "headroom unavailable"})
    monkeypatch.setattr(factory, "_stream_api", lambda *_args, **_kwargs: (
        (_ for _ in ()).throw(AssertionError("provider must not start"))))

    result = factory._reserved_stream_api(
        "assistant", "hello", "fast-model", lambda _text: None, "key", 30)

    assert result[0] == factory._SPEND_RESERVATION_DENIED_RC
    assert "headroom unavailable" in result[1]
