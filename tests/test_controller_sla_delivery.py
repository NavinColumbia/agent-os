import inspect
import sys
import types
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import loopcontroller as mod  # noqa: E402
import orchestrator  # noqa: E402


def _row(thread_id=7):
    return (thread_id, "t-acme", "IMPLEMENT", "build", 2,
            datetime(2026, 8, 16, 8, 0, tzinfo=timezone.utc), 10 * 60)


def test_failed_notification_releases_claim_and_never_marks_warned(monkeypatch):
    monkeypatch.setattr(mod, "_ensure", lambda: None)
    monkeypatch.setattr(mod, "_claim_sla_warnings", lambda _limit, *_a, **_k: ("tok", [_row()]))
    finishes = []
    monkeypatch.setattr(mod, "_finish_sla_warning",
                        lambda tid, token, accepted, new_eta=None:
                        finishes.append((tid, token, accepted, new_eta)) or 1)
    monkeypatch.setattr(mod, "_report", lambda *_args, **_kwargs:
                        (_ for _ in ()).throw(AssertionError("chat must follow durable notification")))
    fake = types.ModuleType("notifications")
    fake.send = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("transport DB busy"))
    monkeypatch.setitem(sys.modules, "notifications", fake)

    result = mod.sla_watchdog(limit=1)
    assert result == {"warned": 0, "failed": 1, "claimed": 1, "batch_limit": 1}
    assert finishes == [(7, "tok", False, None)]


def test_accepted_notification_uses_stable_context_and_idempotent_chat(monkeypatch):
    monkeypatch.setattr(mod, "_ensure", lambda: None)
    monkeypatch.setattr(mod, "_claim_sla_warnings", lambda _limit, *_a, **_k: ("tok-2", [_row(9)]))
    finishes = []
    monkeypatch.setattr(mod, "_finish_sla_warning",
                        lambda tid, token, accepted, new_eta=None:
                        finishes.append((tid, token, accepted, new_eta)) or 1)
    reports, sends = [], []
    monkeypatch.setattr(mod, "_report", lambda *args, **kwargs: reports.append((args, kwargs)) or 1)
    fake = types.ModuleType("notifications")
    fake.send = lambda *args, **kwargs: sends.append((args, kwargs)) or {"id": 81, "duplicate": False}
    monkeypatch.setitem(sys.modules, "notifications", fake)
    monkeypatch.setattr(mod.audit, "append", lambda **_kwargs: None)

    result = mod.sla_watchdog(limit=1)
    assert result["warned"] == 1 and result["failed"] == 0
    key = sends[0][1]["context_key"]
    assert key.startswith("controller-sla:9:2026-08-16T08:00:00+00:00:")
    assert reports[0][0][3]["context_key"] == key
    assert finishes == [(9, "tok-2", True, 12)]


def test_sla_claim_is_bounded_skip_locked_and_generation_fenced():
    claim = inspect.getsource(mod._claim_sla_warnings).lower()
    finish = inspect.getsource(mod._finish_sla_warning).lower()
    assert "for update skip locked" in claim and "limit %s" in claim
    assert "job_sla_claim_token" in claim and "job_sla_claim_token=%s" in finish
    assert "phase <> 'testqa'" in claim


def test_chat_post_has_durable_context_dedupe_and_bounded_lock():
    source = inspect.getsource(orchestrator.post).lower()
    assert "meta->>'context_key'" in source
    assert "pg_advisory_xact_lock" in source
    assert "lock_timeout='1s'" in source and "statement_timeout='3s'" in source
