import datetime
import math
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import notifications
import push
import responder
import watchdog


class _Cursor:
    def __init__(self, state, rows=(), fail_schema=False):
        self.state = state
        self.rows = list(rows)
        self.fail_schema = fail_schema

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, query, params=None):
        sql = str(query)
        self.state["sql"].append((sql, params))
        if self.fail_schema and ("CREATE TABLE" in sql or "ALTER TABLE" in sql):
            raise RuntimeError("simulated DDL lock timeout")

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _Connection:
    def __init__(self, state, rows=(), fail_schema=False):
        self.state = state
        self.rows = rows
        self.fail_schema = fail_schema

    def __enter__(self):
        assert not self.state["transaction_open"]
        self.state["transaction_open"] = True
        self.state["connections"] += 1
        return self

    def __exit__(self, *_):
        self.state["transaction_open"] = False
        return False

    def cursor(self):
        return _Cursor(self.state, self.rows, self.fail_schema)


def _state():
    return {"transaction_open": False, "connections": 0, "sql": []}


def test_watchdog_closes_database_transactions_before_callbacks(monkeypatch, tmp_path):
    """Responder, model/RCA, and HTTP paging must never inherit an open watchdog transaction."""
    state = _state()
    monkeypatch.setattr(watchdog, "connection", lambda *a, **k: _Connection(state))
    monkeypatch.setattr(watchdog, "_db_reachable", lambda: True)
    monkeypatch.setattr(watchdog, "_ensure", lambda: None)
    monkeypatch.setattr(watchdog, "beat", lambda *a, **k: None)
    monkeypatch.setattr(watchdog, "check", lambda: [
        {"sig": "novel:test", "level": "crit", "msg": "novel test failure"}])
    monkeypatch.setattr(watchdog, "_DB_DOWN_MARK", tmp_path / "absent")

    def outside_transaction(*_args, **_kwargs):
        assert state["transaction_open"] is False

    monkeypatch.setattr(watchdog.responder, "remediate", lambda *a, **k: outside_transaction())
    monkeypatch.setattr(watchdog.responder, "classify", lambda *_: "unknown")
    monkeypatch.setattr(
        watchdog.incident, "investigate",
        lambda *a, **k: outside_transaction() or {"summary": "bounded diagnosis"})
    monkeypatch.setattr(watchdog.notify, "send", lambda *a, **k: outside_transaction() or True)

    out = watchdog.tick()
    assert out["paged"] == ["novel test failure"]
    assert state["transaction_open"] is False
    assert state["connections"] == 2  # one known-alert read, one isolated alert write


def test_watchdog_failed_recovery_page_remains_retryable(monkeypatch, tmp_path):
    state = _state()
    last_sent = datetime.datetime.now(datetime.timezone.utc)
    monkeypatch.setattr(
        watchdog, "connection", lambda *a, **k: _Connection(state, [("old:incident", last_sent, 0)]))
    monkeypatch.setattr(watchdog, "_db_reachable", lambda: True)
    monkeypatch.setattr(watchdog, "_ensure", lambda: None)
    monkeypatch.setattr(watchdog, "beat", lambda *a, **k: None)
    monkeypatch.setattr(watchdog, "check", lambda: [])
    monkeypatch.setattr(watchdog, "_DB_DOWN_MARK", tmp_path / "absent")
    monkeypatch.setattr(watchdog.notify, "send", lambda *a, **k: False)

    out = watchdog.tick(auto_heal=False)
    sql = "\n".join(query for query, _ in state["sql"])
    assert out["recovery_delivery_failed"] == ["old:incident"]
    assert "delivery_status='recovery_failed'" in sql
    assert "DELETE FROM watchdog_alerts" not in sql


def test_watchdog_surfaces_responder_exception_instead_of_crashing(monkeypatch, tmp_path):
    state = _state()
    monkeypatch.setattr(watchdog, "connection", lambda *a, **k: _Connection(state))
    monkeypatch.setattr(watchdog, "_db_reachable", lambda: True)
    monkeypatch.setattr(watchdog, "_ensure", lambda: None)
    monkeypatch.setattr(watchdog, "beat", lambda *a, **k: None)
    monkeypatch.setattr(watchdog, "check", lambda: [
        {"sig": "daemon:test", "level": "crit", "msg": "test process is DOWN"}])
    monkeypatch.setattr(watchdog, "_DB_DOWN_MARK", tmp_path / "absent")
    monkeypatch.setattr(
        watchdog.responder, "remediate",
        lambda *_: (_ for _ in ()).throw(RuntimeError("subprocess control failed")))
    sent = []
    monkeypatch.setattr(watchdog.notify, "send", lambda message, **_k: sent.append(message) or True)
    out = watchdog.tick()
    assert out["paged"] == ["test process is DOWN"]
    assert any("auto-fix FAILED" in message and "responder failed safely" in message for message in sent)


def test_watchdog_host_probe_failure_is_degraded_not_false_green(monkeypatch):
    monkeypatch.setattr(
        watchdog.subprocess, "run",
        lambda *_a, **_k: (_ for _ in ()).throw(subprocess.TimeoutExpired("ps", 10)))
    issues = watchdog._host_pressure()
    assert issues[0]["sig"] == "watchdog:host-pressure-read"
    assert issues[0]["level"] == "warn"


def test_runtime_schema_ensure_is_cached_bounded_and_retries_after_lock_timeout(monkeypatch):
    for module in (watchdog, notifications, push):
        state = _state()
        failures = [True, False]

        def connect(*_args, **_kwargs):
            return _Connection(state, fail_schema=failures.pop(0) if failures else False)

        monkeypatch.setattr(module, "connection", connect)
        monkeypatch.setattr(module, "_ensured", False)
        try:
            module._ensure()
            assert False, f"{module.__name__} must surface a simulated schema lock timeout"
        except RuntimeError as exc:
            assert "DDL lock timeout" in str(exc)
        assert module._ensured is False
        module._ensure()
        module._ensure()
        assert module._ensured is True
        assert state["connections"] == 2  # failed try + successful retry; cached call does no DB work
        first_ddl = next(i for i, (sql, _) in enumerate(state["sql"])
                         if "CREATE TABLE" in sql or "ALTER TABLE" in sql)
        before_ddl = "\n".join(sql for sql, _ in state["sql"][:first_ddl])
        assert "lock_timeout" in before_ddl and "statement_timeout" in before_ddl


def test_notification_retry_stalled_transport_stays_inside_budget_without_preclaiming_tail(monkeypatch):
    clock = [0.0]
    due = [(n, f"tenant-{n}", f"title-{n}", "body", "urgent") for n in range(50)]
    claimed = []
    recorded = []

    monkeypatch.setattr(notifications, "_ensure", lambda: None)
    monkeypatch.setattr(notifications.time, "monotonic", lambda: clock[0])

    def claim(notification_id=None):
        row = due.pop(0) if due else None
        if row:
            claimed.append(row[0])
        return row

    def stalled(*_args, **_kwargs):
        clock[0] += notifications.PUSH_TIMEOUT_S
        return {"sent": False, "reason": "transport timeout"}

    monkeypatch.setattr(notifications, "_claim_pending", claim)
    monkeypatch.setattr(notifications, "_push_now", stalled)
    monkeypatch.setattr(
        notifications, "_delivery",
        lambda nid, *_a, **_k: recorded.append(nid))

    out = notifications.retry_pending(limit=50, budget_s=75)
    assert 0 < out["attempted"] < 50
    assert out == {"attempted": len(claimed), "accepted": 0}
    assert claimed == recorded
    assert len(due) == 50 - out["attempted"]  # the never-attempted tail was never leased/pre-delayed
    assert clock[0] <= 75 < 120


def test_notification_claim_is_one_row_skip_locked_with_short_crash_lease(monkeypatch):
    state = _state()
    row = (77, "tenant", "title", "body", "standard")
    monkeypatch.setattr(notifications, "connection", lambda *a, **k: _Connection(state, [row]))
    assert notifications._claim_pending() == row
    sql = "\n".join(query for query, _ in state["sql"])
    assert "FOR UPDATE OF nd SKIP LOCKED LIMIT 1" in sql
    assert "interval '1 second'" in sql
    updates = [(query, params) for query, params in state["sql"]
               if "UPDATE notification_deliveries" in query]
    assert len(updates) == 1 and updates[0][1][-1] == 77
    assert notifications.RETRY_CLAIM_LEASE_S <= 60


def test_responder_subprocesses_are_finite_and_daemons_use_exact_recovery(monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="0", stderr="")

    monkeypatch.setattr(responder.subprocess, "run", run)
    monkeypatch.setattr(responder, "_audit", lambda *a, **k: None)
    responder.restart_container("postgres")
    responder.take_snapshot()
    assert calls
    assert all(math.isfinite(call[1]["timeout"]) and call[1]["timeout"] > 0 for call in calls)

    recovered = []
    monkeypatch.setattr(responder.service_recovery, "ensure", lambda name: (
        recovered.append(("ensure", name)) or {"state": "healthy"}))
    monkeypatch.setattr(responder.service_recovery, "replace", lambda name: (
        recovered.append(("replace", name)) or {"state": "healthy"}))
    assert responder.restart_daemon("api")["ok"] is True
    assert responder.restart_daemon("ticker", replace=True)["ok"] is True
    assert recovered == [("ensure", "api"), ("replace", "ticker")]
    source = Path(responder.__file__).read_text()
    assert "pgrep" not in source and "subprocess.Popen" not in source


def test_responder_disk_cleanup_verifies_postcondition(monkeypatch, tmp_path):
    monkeypatch.setattr(responder, "ROOT", tmp_path)
    monkeypatch.setattr(responder, "_audit", lambda *a, **k: None)
    monkeypatch.setitem(sys.modules, "objstore", SimpleNamespace(gc=lambda: 0))
    monkeypatch.setitem(sys.modules, "retention", SimpleNamespace(sweep=lambda: None))
    monkeypatch.setattr(
        responder.shutil, "disk_usage", lambda *_: SimpleNamespace(total=100, used=90, free=10))
    result = responder.free_disk()
    assert result["ok"] is False
    assert any("disk remains 90.0% used" in failure for failure in result["failures"])


def test_watchdog_shell_has_bounded_process_group_tick_without_pipeline():
    script = (Path(__file__).resolve().parents[1] / "scripts" / "watchdog.sh").read_text()
    tick_line = next(line for line in script.splitlines() if "run_child timeout --signal=TERM" in line)
    assert "--kill-after=" in tick_line
    assert "|" not in tick_line
    assert "TICK_TIMEOUT_S <= 90" in script
    assert "INTERVAL >= 10" in script
    assert "^[[1-9][0-9]*$" not in script  # guard against accidentally quoting the regex as a literal


def test_push_schema_lookup_contention_is_a_retryable_transport_failure(monkeypatch):
    monkeypatch.setattr(push, "_topic_of", lambda *_: (_ for _ in ()).throw(RuntimeError("DDL lock timeout")))
    result = push.send("tenant", "title", "body")
    assert result["sent"] is False
    assert "lookup unavailable" in result["reason"]
