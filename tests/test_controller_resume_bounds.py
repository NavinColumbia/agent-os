import inspect
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import decisionchain  # noqa: E402
import loopcontroller as mod  # noqa: E402


def test_answer_reconcile_is_explicitly_bounded(monkeypatch):
    calls = []
    fake = types.ModuleType("decisionchain")
    fake.reconcile_human_answers = lambda **kwargs: calls.append(kwargs) or []
    monkeypatch.setitem(sys.modules, "decisionchain", fake)
    assert mod._resume_agentic_answers(limit=1) == 0
    assert calls == [{"limit": 1}]
    source = inspect.getsource(decisionchain.reconcile_human_answers).lower()
    assert "limit %s" in source and "min(20" in source


def test_controller_resume_queries_and_pid_reaper_are_bounded():
    source = inspect.getsource(mod.resume_stalled).lower()
    reaper = inspect.getsource(mod._reap_dead_jobs).lower()
    assert source.count("limit %s") >= 3
    assert "time.monotonic() >= deadline" in source
    assert "status not in ('running','pending')" in source
    assert "_resume_resolved_internal_management" in source
    assert "for update skip locked" in reaper
    assert reaper.count("limit %s") >= 3


def test_resume_cli_returns_nonzero_on_degraded_blindness(monkeypatch, capsys):
    monkeypatch.setattr(mod, "resume_stalled", lambda: {
        "resumed": 0, "degraded": [{"source": "db", "error": "offline"}],
        "budget_exhausted": False, "batch_limit": 20})
    try:
        mod._main(["resume"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("degraded recovery must fail the scheduler occurrence")
    assert '"degraded"' in capsys.readouterr().out


def test_controlled_runtime_handoff_resumes_without_consuming_crash_budget(monkeypatch):
    calls = []
    original = mod.advance
    monkeypatch.setattr(mod, "_st", lambda _thread: {
        "tenant_id": "tenant-a", "phase": "TESTQA", "awaiting": None})
    monkeypatch.setattr(mod, "_crash_count", lambda *_args: (
        (_ for _ in ()).throw(AssertionError("controlled handoff must not inspect crash budget"))))
    monkeypatch.setattr(mod.audit, "append", lambda **kwargs: calls.append(("audit", kwargs)))
    monkeypatch.setattr(mod, "_set", lambda thread, **values: calls.append(("set", thread, values)))
    monkeypatch.setattr(mod, "_job_clear", lambda thread: calls.append(("clear", thread)))
    monkeypatch.setattr(mod, "advance", lambda thread: calls.append(("redispatch", thread)))

    original(2787, {"controlled_handoff": True, "job_id": 10217, "reason": "new QA runtime"})

    assert ("set", 2787, {"awaiting": None}) in calls
    assert ("clear", 2787) in calls
    assert calls[-1] == ("redispatch", 2787)
