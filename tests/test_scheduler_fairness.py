from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import threading
import time
import uuid

import psycopg


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import scheduler
from aoscfg import DB


def test_overdue_duty_eventually_outranks_continuously_due_recovery_work():
    now = datetime(2026, 8, 16, tzinfo=timezone.utc)
    rows = [
        ("controller-resume", "recover", now - timedelta(seconds=10), 600),
        ("controller-sla", "sla", now - timedelta(seconds=20), 120),
        ("proactive-comms", "brief", now - timedelta(seconds=601), 300),
    ]
    selected = scheduler._claim_batch(rows, 2, now=now)
    assert [row[0] for row in selected] == ["proactive-comms", "controller-resume"]


def test_fairness_selection_is_oldest_first_after_lag_boundary():
    now = datetime(2026, 8, 16, tzinfo=timezone.utc)
    rows = [
        ("forecast-sweep", "forecast", now - timedelta(seconds=700), 3600),
        ("snapshot-backup", "backup", now - timedelta(seconds=900), 86400),
        ("management-control", "manage", now - timedelta(seconds=800), 60),
    ]
    assert [row[0] for row in scheduler._claim_batch(rows, 2, now=now)] == [
        "snapshot-backup", "management-control"]


def test_legacy_rows_retain_recovery_priority_and_batch_is_hard_capped():
    rows = [("housekeeping", "h"), ("tasksweep", "t"),
            ("controller-resume", "r"), ("management-control", "m")]
    assert scheduler._claim_batch(rows, 2) == [
        ("controller-resume", "r"), ("management-control", "m")]
    assert scheduler._claim_batch(rows, 0) == []


def test_default_parallelism_covers_the_short_cadence_arrival_rate():
    # Per minute: notification + management each contribute one occurrence;
    # controller-SLA + agent-reap each contribute half. Four bounded slots leave
    # one slot of headroom for less-frequent duties instead of building backlog.
    assert scheduler.MAX_PARALLEL >= 4


def test_invalid_or_extreme_scheduler_limits_fail_to_safe_bounded_defaults(monkeypatch):
    monkeypatch.setenv("BROKEN_SCHED_INT", "definitely-not-an-integer")
    assert scheduler._int_env("BROKEN_SCHED_INT", 4, minimum=1, maximum=8) == 4
    monkeypatch.setenv("BROKEN_SCHED_INT", "999999")
    assert scheduler._int_env("BROKEN_SCHED_INT", 4, minimum=1, maximum=8) == 8
    monkeypatch.setenv("BROKEN_SCHED_INT", "-10")
    assert scheduler._int_env("BROKEN_SCHED_INT", 4, minimum=1, maximum=8) == 1


def test_fast_sibling_is_terminalized_while_slow_sibling_is_still_running(monkeypatch):
    suffix = uuid.uuid4().hex[:10]
    fast, slow = f"sched-fast-{suffix}", f"sched-slow-{suffix}"
    fast_exited = threading.Event()
    release_slow = threading.Event()

    def execute(name, _command, claim_token=None):
        if name == slow:
            assert release_slow.wait(5)
        else:
            fast_exited.set()
        return {"name": name, "decision": "executed", "rc": 0, "detail": "",
                "duration_ms": 1, "claim_token": claim_token}

    scheduler._ensure()
    monkeypatch.setattr(scheduler, "_execute_due_job", execute)
    try:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            for name in (fast, slow):
                cur.execute("""INSERT INTO schedules
                                  (name,command,interval_s,enabled,manual_only,next_run)
                               VALUES (%s,%s,3600,true,true,now())""",
                            (name, f"{scheduler.VENV_PY} -c pass"))
        result = []
        driver = threading.Thread(target=lambda: result.append(scheduler.tick([fast, slow])))
        driver.start()
        assert fast_exited.wait(3)
        deadline = time.monotonic() + 3
        states = {}
        while time.monotonic() < deadline:
            with psycopg.connect(DB) as c, c.cursor() as cur:
                cur.execute("""SELECT name,status FROM scheduler_claims
                               WHERE name=ANY(%s) ORDER BY name""", ([fast, slow],))
                states = dict(cur.fetchall())
            if states.get(fast) == "executed":
                break
            time.sleep(0.02)
        assert states.get(fast) == "executed"
        assert states.get(slow) == "running"
        release_slow.set()
        driver.join(5)
        assert not driver.is_alive() and result == [2]
    finally:
        release_slow.set()
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM scheduler_runs WHERE name=ANY(%s)", ([fast, slow],))
            cur.execute("DELETE FROM schedules WHERE name=ANY(%s)", ([fast, slow],))
