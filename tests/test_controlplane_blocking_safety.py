"""Deterministic regressions for bounded control-plane ownership and cleanup.

Database tests use unique advisory keys and rows, then remove every fixture. They
never drive a live controller, scheduler, or QA run.
"""
from __future__ import annotations

import contextlib
import hashlib
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import sql


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import loopcontroller as lc
import management
import scheduler
from aoscfg import DB
from dbpool import connection


def _controller_fixture(label):
    return (990_000_000 + int(uuid.uuid4().hex[:6], 16),
            f"blocking-{label}-{uuid.uuid4().hex}")


def _cleanup_controller(thread_id):
    with connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM controller_jobs WHERE thread_id=%s", (thread_id,))
        cur.execute("DELETE FROM controller_state WHERE thread_id=%s", (thread_id,))


def test_controller_ensure_ddl_lock_wait_is_bounded_and_retryable(monkeypatch):
    """A schema lock cannot pin startup, and the clean retry completes all DDL."""
    schema = f"controller_lock_{uuid.uuid4().hex[:12]}"
    saved_ensured = lc._CONTROLLER_ENSURED
    blocker = None

    @contextlib.contextmanager
    def isolated_connection(*_args, **_kwargs):
        with psycopg.connect(DB, options=f"-c search_path={schema}") as conn:
            yield conn

    try:
        with psycopg.connect(DB, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        monkeypatch.setattr(lc, "_conn", isolated_connection)
        monkeypatch.setattr(lc, "CONTROLLER_LOCK_TIMEOUT_MS", 50)
        monkeypatch.setattr(lc, "CONTROLLER_STATEMENT_TIMEOUT_MS", 500)
        lc._CONTROLLER_ENSURED = False
        lc._ensure()
        lc._CONTROLLER_ENSURED = False

        blocker = psycopg.connect(DB, options=f"-c search_path={schema}")
        with blocker.cursor() as cur:
            cur.execute("LOCK TABLE controller_state IN ACCESS SHARE MODE")

        started = time.monotonic()
        with pytest.raises(lc.ControllerDatabaseBusy) as refused:
            lc._ensure()
        assert time.monotonic() - started < 1.0
        assert refused.value.retryable is True
        assert lc._CONTROLLER_ENSURED is False

        blocker.rollback()
        blocker.close()
        blocker = None
        lc._ensure()
        assert lc._CONTROLLER_ENSURED is True
    finally:
        if blocker is not None:
            blocker.rollback()
            blocker.close()
        lc._CONTROLLER_ENSURED = saved_ensured
        with psycopg.connect(DB, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def test_controller_dispatch_lock_timeout_has_no_partial_state_then_converges_once(monkeypatch):
    """A contended dispatch remains runnable; one later attempt creates one owner."""
    lc._ensure()
    thread_id, tenant = _controller_fixture("dispatch")
    lock_key = 880_000_000 + int(uuid.uuid4().hex[:6], 16)
    blocker = psycopg.connect(DB, autocommit=True)
    try:
        with connection() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO controller_state
                              (thread_id,tenant_id,org_id,phase,awaiting,updated_at,execution_scope)
                           VALUES (%s,%s,1,'OPTIONS',NULL,now(),'test')""", (thread_id, tenant))
        monkeypatch.setattr(lc, "_DISPATCH_GLOBAL_LOCK", lock_key)
        monkeypatch.setattr(lc, "CONTROLLER_LOCK_TIMEOUT_MS", 50)
        monkeypatch.setattr(lc, "CONTROLLER_STATEMENT_TIMEOUT_MS", 500)
        monkeypatch.setattr(lc, "_MAX_ACTIVE_CONTROLLER_JOBS", 1_000_000)
        launches = []
        monkeypatch.setattr(lc, "_spawn_parked_worker",
                            lambda tid, kind, jid: launches.append((tid, kind, jid)) or True)
        with blocker.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (lock_key,))

        started = time.monotonic()
        assert lc._dispatch(thread_id, "design", fn=lambda: {}, eta_min=1) is None
        assert time.monotonic() - started < 1.0
        with connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT awaiting FROM controller_state WHERE thread_id=%s", (thread_id,))
            assert cur.fetchone()[0] is None
            cur.execute("SELECT count(*) FROM controller_jobs WHERE thread_id=%s", (thread_id,))
            assert cur.fetchone()[0] == 0

        with blocker.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))
        job_id = lc._dispatch(thread_id, "design", fn=lambda: {}, eta_min=1)
        assert job_id is not None and launches == [(thread_id, "design", job_id)]
        with connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT awaiting FROM controller_state WHERE thread_id=%s", (thread_id,))
            assert cur.fetchone()[0] == "fleet"
            cur.execute("SELECT count(*) FROM controller_jobs WHERE thread_id=%s", (thread_id,))
            assert cur.fetchone()[0] == 1
    finally:
        blocker.close()
        _cleanup_controller(thread_id)


def test_thread_drive_lock_backend_is_never_idle_in_transaction():
    """Session ownership may span external work without retaining a transaction."""
    thread_id, _tenant = _controller_fixture("drive")
    with lc.thread_drive_lock(thread_id) as owned:
        assert owned is True
        with psycopg.connect(DB) as conn, conn.cursor() as cur:
            cur.execute("""SELECT activity.state
                             FROM pg_locks AS held
                             JOIN pg_stat_activity AS activity ON activity.pid=held.pid
                            WHERE held.locktype='advisory' AND held.granted
                              AND held.classid=%s AND held.objid=%s
                              AND held.pid <> pg_backend_pid()""",
                        (lc._DRIVE_LOCK_NS, thread_id))
            states = [row[0] for row in cur.fetchall()]
        assert states and "idle in transaction" not in states


class _ClosablePipe:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _EscapedPipeHolderProc:
    """A killed root whose inherited pipes never reach EOF."""

    pid = 424242
    returncode = None

    def __init__(self, argv):
        self.argv = argv
        self.stdout = _ClosablePipe()
        self.stderr = _ClosablePipe()
        self.communicate_timeouts = []
        self.wait_timeouts = []

    def communicate(self, timeout=None):
        self.communicate_timeouts.append(timeout)
        raise subprocess.TimeoutExpired(self.argv, timeout)

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        raise subprocess.TimeoutExpired(self.argv, timeout)


def test_scheduler_timeout_cleanup_has_only_finite_pipe_drains(monkeypatch):
    """Even an escaped inherited pipe holder cannot create an unbounded drain."""
    made = []

    def fake_popen(argv, **_kwargs):
        proc = _EscapedPipeHolderProc(argv)
        made.append(proc)
        return proc

    signals = []
    monkeypatch.setattr(scheduler.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(scheduler, "_capture_exact_tree", lambda _pid: [])
    monkeypatch.setattr(scheduler, "_signal_job_tree",
                        lambda proc, sig, captured: signals.append((proc.pid, sig, captured)))
    monkeypatch.setattr(scheduler, "TERM_GRACE_S", 0.02)
    monkeypatch.setattr(scheduler, "KILL_DRAIN_GRACE_S", 0.01)

    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        scheduler._run_bounded([sys.executable, "-c", "pass"], 0.03)
    assert time.monotonic() - started < 0.5
    proc = made[0]
    assert proc.communicate_timeouts == [0.03, 0.02, 0.01]
    assert proc.wait_timeouts == [0.01]
    assert proc.stdout.closed and proc.stderr.closed
    assert [item[1] for item in signals] == [signal.SIGTERM, signal.SIGKILL]


def test_scheduler_kills_exact_survivor_even_when_capture_pipes_reach_eof(monkeypatch):
    """A TERM-ignoring child that redirects its pipes is not mistaken for clean exit."""
    child_generation = object()

    class Proc:
        pid = 424243
        returncode = -signal.SIGTERM
        stdout = _ClosablePipe()
        stderr = _ClosablePipe()

        def __init__(self):
            self.calls = 0

        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("fixture", timeout)
            return b"", b""

    proc = Proc()
    tree_signals = []
    exact_signals = []
    monkeypatch.setattr(scheduler.subprocess, "Popen", lambda *_a, **_k: proc)
    monkeypatch.setattr(scheduler, "_capture_exact_tree", lambda _pid: [child_generation])
    monkeypatch.setattr(scheduler, "_signal_job_tree",
                        lambda _proc, sig, captured: tree_signals.append((sig, captured)))
    monkeypatch.setattr(scheduler, "_exact_survivors", lambda captured: list(captured))
    monkeypatch.setattr(scheduler, "_signal_exact",
                        lambda captured, sig: exact_signals.append((sig, captured)))

    with pytest.raises(subprocess.TimeoutExpired):
        scheduler._run_bounded(["fixture"], 0.01)
    assert tree_signals == [(signal.SIGTERM, [child_generation])]
    assert exact_signals == [(signal.SIGKILL, [child_generation])]


def test_management_signal_contention_retries_to_exactly_one_case_and_event(monkeypatch):
    management._ensure()
    dedupe = f"same-key-{uuid.uuid4().hex}"
    tenant = f"management-contention-{uuid.uuid4().hex}"
    case_id = f"mc-{hashlib.sha256(dedupe.encode()).hexdigest()[:20]}"
    blocker = psycopg.connect(DB)
    release = None
    try:
        with blocker.cursor() as cur:
            cur.execute("""INSERT INTO management_cases
                              (case_id,dedupe_key,tenant_id,subject,trigger,state,state_fingerprint)
                           VALUES (%s,%s,%s,'fixture','fixture','{}'::jsonb,%s)""",
                        (case_id, dedupe, tenant, management._fingerprint({})))
        monkeypatch.setattr(management, "MANAGEMENT_DB_LOCK_TIMEOUT_MS", 40)
        monkeypatch.setattr(management, "MANAGEMENT_DB_STATEMENT_TIMEOUT_MS", 500)
        monkeypatch.setattr(management, "MANAGEMENT_SIGNAL_ATTEMPTS", 4)
        release = threading.Timer(0.09, blocker.commit)
        release.start()

        result = management.signal(dedupe, "contended case", "worker_state_changed",
                                   {"step": 1}, tenant_id=tenant, progress=True)
        release.join(1)
        assert result["case_id"] == case_id
        with connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM management_cases WHERE dedupe_key=%s", (dedupe,))
            assert cur.fetchone()[0] == 1
            cur.execute("SELECT progress_seq FROM management_cases WHERE case_id=%s", (case_id,))
            assert cur.fetchone()[0] == 1
            cur.execute("SELECT count(*) FROM management_events WHERE case_id=%s", (case_id,))
            assert cur.fetchone()[0] == 1
    finally:
        if release is not None:
            release.cancel()
            release.join(1)
        try:
            blocker.rollback()
        finally:
            blocker.close()
        with connection() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM management_events WHERE case_id=%s", (case_id,))
            cur.execute("DELETE FROM management_cases WHERE case_id=%s", (case_id,))


def test_dispatch_launch_failure_never_runs_in_process_after_terminalization(monkeypatch):
    lc._ensure()
    thread_id, tenant = _controller_fixture("launch")
    ran = threading.Event()
    try:
        with connection() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO controller_state
                              (thread_id,tenant_id,org_id,phase,awaiting,updated_at,execution_scope)
                           VALUES (%s,%s,1,'OPTIONS',NULL,now(),'test')""", (thread_id, tenant))
        monkeypatch.setattr(lc, "_MAX_ACTIVE_CONTROLLER_JOBS", 1_000_000)
        monkeypatch.setattr(lc, "_spawn_parked_worker", lambda *_args: False)
        job_id = lc._dispatch(thread_id, "design", fn=lambda: ran.set(), eta_min=1)
        assert job_id is not None
        assert not ran.wait(0.1)
        with connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT status,result FROM controller_jobs WHERE id=%s", (job_id,))
            status, result = cur.fetchone()
        assert status == "failed"
        assert result["crashed"] is True and result["status"] == "failed"
    finally:
        _cleanup_controller(thread_id)
