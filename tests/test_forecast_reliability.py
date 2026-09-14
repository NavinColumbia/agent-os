import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import forecast as mod  # noqa: E402


class _StateCursor:
    def __init__(self, state):
        self.state = state
        self.rowcount = 0
        self._one = None

    def execute(self, query, args=()):
        normalized = " ".join(query.split()).lower()
        self.rowcount = 0
        if normalized.startswith("select 1 from budget_alert_state"):
            self._one = (1,) if (args[0], args[1]) in self.state else None
        elif normalized.startswith("insert into budget_alert_state"):
            key = (args[0], args[1])
            if key not in self.state:
                self.state.add(key)
                self.rowcount = 1
        elif normalized.startswith("delete from budget_alert_state"):
            self.state.discard((args[0], args[1]))

    def fetchone(self):
        return self._one

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _StateConn:
    def __init__(self, state):
        self.state = state

    def cursor(self):
        return _StateCursor(self.state)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_alert_send_is_outside_transaction_and_crash_retry_is_idempotent(monkeypatch):
    state = set()
    in_tx = {"value": False}

    @contextmanager
    def tenant_connection(_tid):
        in_tx["value"] = True
        try:
            yield _StateConn(state)
        finally:
            in_tx["value"] = False

    calls = []

    def send(*_args, **kwargs):
        assert not in_tx["value"]
        calls.append(kwargs["context_key"])
        return {"id": 1, "duplicate": len(calls) > 1}

    monkeypatch.setattr(mod, "_ensure", lambda: None)
    monkeypatch.setattr(mod, "tenant_connection", tenant_connection)
    monkeypatch.setattr(mod, "forecast", lambda _tid: {
        "pct_of_quota_projected": 120, "plan": "free", "eta_days_to_quota": 2, "level": "over"
    })
    monkeypatch.setattr(
        mod,
        "_alert_context",
        lambda threshold: f"budget-forecast:2026-08:{int(threshold)}",
    )
    monkeypatch.setattr(mod.notifications, "send", send)
    monkeypatch.setattr(mod.audit, "append", lambda **_kwargs: None)

    first = mod.check_alerts("t-a")
    second = mod.check_alerts("t-a")
    assert first["alerted"] == [80, 100]
    assert second["alerted"] == []
    assert calls == ["budget-forecast:2026-08:80", "budget-forecast:2026-08:100"]


def test_monthly_context_is_stable_and_threshold_specific():
    at = datetime(2026, 8, 31, 23, 0, tzinfo=timezone.utc)
    assert mod._alert_context(80, at) == "budget-forecast:2026-08:80"
    assert mod._alert_context(100, at) == "budget-forecast:2026-08:100"


def test_sweep_processes_only_claimed_bounded_page(monkeypatch):
    monkeypatch.setattr(mod, "_claim_sweep_page", lambda limit: [f"t-{n}" for n in range(limit)])
    seen = []
    monkeypatch.setattr(mod, "check_alerts", lambda tid: seen.append(tid) or {
        "pct": 0, "level": "ok", "alerted": []
    })
    result = mod.sweep(limit=7)
    assert result["checked"] == 7
    assert seen == [f"t-{n}" for n in range(7)]
