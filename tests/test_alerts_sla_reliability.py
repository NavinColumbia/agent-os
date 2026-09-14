import sys
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import alerts as mod  # noqa: E402


def _due(n=3):
    return [{"alert_id": i, "severity": "critical", "age_min": 99, "owner": "sre",
             "body": "stuck", "source": "test", "target_role": "sre"} for i in range(n)]


def test_failed_page_releases_claim_without_stamping_escalated(monkeypatch):
    monkeypatch.setattr(mod, "_ensure", lambda: None)
    monkeypatch.setattr(mod, "_claim_due", lambda **_kwargs: ("tok", _due()))
    finished = []
    monkeypatch.setattr(mod, "_finish_escalation",
                        lambda token, ids, accepted: finished.append((token, ids, accepted)) or len(ids))
    monkeypatch.setattr(mod.audit, "append", lambda **_kwargs: None)
    result = mod.sweep(notify_fn=lambda _text: False)
    assert result["attempted"] == 3 and result["escalated"] == 0
    assert finished == [("tok", [0, 1, 2], False)]


def test_accepted_page_stamps_only_exact_claim(monkeypatch):
    monkeypatch.setattr(mod, "_ensure", lambda: None)
    monkeypatch.setattr(mod, "_claim_due", lambda **_kwargs: ("generation-7", _due(2)))
    finished = []
    monkeypatch.setattr(mod, "_finish_escalation",
                        lambda token, ids, accepted: finished.append((token, ids, accepted)) or len(ids))
    monkeypatch.setattr(mod.audit, "append", lambda **_kwargs: None)
    result = mod.sweep(notify_fn=lambda _text: True)
    assert result["escalated"] == 2 and result["finalized"] == 2
    assert finished == [("generation-7", [0, 1], True)]


def test_claim_query_is_bounded_and_skip_locked():
    # The database-specific query is intentionally kept in one helper; these source invariants protect the
    # operational contract without creating rows in the shared live alert table.
    import inspect
    source = inspect.getsource(mod._claim_due).lower()
    assert "for update skip locked" in source
    assert "limit %s" in source
    assert "escalation_claim_token" in source
    assert "execution_scope=%s" in source


def test_selftest_alert_scope_never_enters_production_sla(monkeypatch):
    monkeypatch.setenv("AOS_SELFTEST", "1")
    assert mod._execution_scope() == "test"
    assert mod._execution_scope("production") == "production"
