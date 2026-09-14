from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import inspect
import os
import subprocess
import sys
import threading


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import factory
import process_assurance
import scheduler
import customagents


class _Cursor:
    def __init__(self, candidates):
        self.candidates = candidates
        self.rows = []
        self.rowcount = 0
        self.sql = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, args=()):
        normalized = " ".join(str(sql).split())
        self.sql.append((normalized, args))
        self.rowcount = 0
        if "SELECT cursor_generation_ts,cursor_run_id" in normalized:
            self.rows = [(None, None)]
        elif "WITH incomplete AS" in normalized:
            assert "LIMIT %s" in normalized
            assert args[-1] <= factory.RESUME_CANDIDATE_PAGE == 100
            self.rows = list(self.candidates)[: args[-1]]
        elif "UPDATE factory_resume_sweep_state" in normalized:
            self.rows = []
            self.rowcount = 1
        else:
            raise AssertionError(f"unexpected SQL: {normalized}")

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self._cursor


def test_factory_candidate_page_bounds_process_probes_at_100(monkeypatch):
    now = datetime.now(timezone.utc) - timedelta(hours=1)
    candidates = [(f"build-product-{index:03d}", now + timedelta(microseconds=index))
                  for index in range(140)]
    cursor = _Cursor(candidates)
    own_id = process_assurance.ProcessIdentity(os.getpid(), 10, "boot-test")
    own = process_assurance.ProcessSnapshot(
        own_id, os.getppid(), os.getpgrp(), os.getuid(), "pytest")
    probes = []

    monkeypatch.setattr(factory, "_ensure_resume_claims", lambda: None)
    monkeypatch.setattr(factory, "connection", lambda: _Connection(cursor))
    monkeypatch.setattr(factory.process_assurance, "scan_snapshots", lambda: {os.getpid(): own})
    monkeypatch.setattr(factory.process_assurance, "read_snapshot", lambda _pid: own)
    monkeypatch.setattr(factory, "_detect_kind", lambda _repo: "lib")
    monkeypatch.setattr(
        factory, "_build_process_alive",
        lambda product, snapshots=None, scan_reliable=None: probes.append(product) or False)

    found = factory.find_incomplete_builds(max_age_min=20, candidate_limit=10_000)
    assert len(found) == len(probes) == 100
    assert probes == [f"product-{index:03d}" for index in range(100)]
    assert "subprocess.run" not in inspect.getsource(factory._build_process_alive)


def test_factory_candidate_cursor_rotates_to_the_unprobed_tail(monkeypatch):
    base = datetime(2026, 8, 16, tzinfo=timezone.utc)
    candidates = [(f"build-tail-{index}", base + timedelta(seconds=index)) for index in range(5)]

    class FairCursor(_Cursor):
        cursor = (None, None)

        def execute(self, sql, args=()):
            normalized = " ".join(str(sql).split())
            self.sql.append((normalized, args))
            self.rowcount = 0
            if "SELECT cursor_generation_ts,cursor_run_id" in normalized:
                self.rows = [self.cursor]
            elif "WITH incomplete AS" in normalized:
                generation, run_id = self.cursor
                after = [row for row in candidates if generation is None or row[1:] > (generation,) or
                         (row[1] == generation and row[0] > run_id)]
                before = [row for row in candidates if row not in after]
                self.rows = (after + before)[: args[-1]]
            elif "UPDATE factory_resume_sweep_state" in normalized:
                self.cursor = (args[0], args[1])
                self.rows, self.rowcount = [], 1
            else:
                raise AssertionError(f"unexpected SQL: {normalized}")

    cursor = FairCursor(candidates)
    own_id = process_assurance.ProcessIdentity(os.getpid(), 11, "boot-fair")
    own = process_assurance.ProcessSnapshot(own_id, os.getppid(), os.getpgrp(), os.getuid(), "pytest")
    monkeypatch.setattr(factory, "_ensure_resume_claims", lambda: None)
    monkeypatch.setattr(factory, "connection", lambda: _Connection(cursor))
    monkeypatch.setattr(factory.process_assurance, "scan_snapshots", lambda: {os.getpid(): own})
    monkeypatch.setattr(factory.process_assurance, "read_snapshot", lambda _pid: own)
    monkeypatch.setattr(factory, "_build_process_alive", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(factory, "_detect_kind", lambda _repo: "lib")

    pages = [factory.find_incomplete_builds(candidate_limit=2) for _ in range(3)]
    assert [[name for name, _kind in page] for page in pages] == [
        ["tail-0", "tail-1"], ["tail-2", "tail-3"], ["tail-4", "tail-0"]]


def test_factory_process_match_requires_exact_trusted_argv_identity():
    identity = process_assurance.ProcessIdentity(90, 12, "boot-argv")
    trusted = process_assurance.ProcessSnapshot(
        identity, 1, 90, os.getuid(),
        f"{sys.executable} {factory.SCRIPTS / 'factory.py'} build product-a  lib")
    shell_lookalike = process_assurance.ProcessSnapshot(
        identity, 1, 90, os.getuid(),
        f"/bin/sh -c {factory.SCRIPTS / 'factory.py'} build product-a")
    wrong_script = process_assurance.ProcessSnapshot(
        identity, 1, 90, os.getuid(),
        f"{sys.executable} /tmp/factory.py build product-a")
    assert factory._factory_build_product(trusted) == "product-a"
    assert factory._factory_build_product(shell_lookalike) is None
    assert factory._factory_build_product(wrong_script) is None


def test_two_factory_sweepers_launch_one_exact_registered_child(monkeypatch):
    claim_source = inspect.getsource(factory._claim_resume_candidate)
    generation = datetime(2026, 8, 16, tzinfo=timezone.utc)
    barrier = threading.Barrier(2)
    claim_lock = threading.Lock()
    claimed = []
    launches = []
    registered = []
    identity = process_assurance.ProcessIdentity(4242, 77, "boot-factory")
    snapshot = process_assurance.ProcessSnapshot(
        identity, os.getpid(), 4242, os.getuid(),
        f"{sys.executable} {factory.SCRIPTS / 'factory.py'} build exact-product lib")

    def claim(*_args):
        barrier.wait(timeout=3)
        with claim_lock:
            if claimed:
                return None
            claimed.append("frc-one")
            return "frc-one"

    class Proc:
        pid = identity.pid

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

    def popen(*_args, **_kwargs):
        launches.append(_kwargs["env"]["AOS_FACTORY_RESUME_CLAIM_TOKEN"])
        return Proc()

    monkeypatch.setattr(factory, "_reconcile_resume_claims", lambda: 0)
    monkeypatch.setattr(factory, "_find_incomplete_build_candidates",
                        lambda *_args, **_kwargs: [("exact-product", "lib", generation)])
    monkeypatch.setattr(factory, "_claim_resume_candidate", claim)
    monkeypatch.setattr(factory.subprocess, "Popen", popen)
    monkeypatch.setattr(factory.process_assurance, "read_snapshot", lambda _pid: snapshot)
    monkeypatch.setattr(factory, "_register_resume_child",
                        lambda token, ident: registered.append((token, ident.token())))
    monkeypatch.setattr(factory.audit, "append", lambda **_kwargs: None)
    monkeypatch.setitem(sys.modules, "notify", SimpleNamespace(send=lambda *_args, **_kwargs: None))

    results = []
    threads = [threading.Thread(target=lambda: results.append(factory.resume_incomplete_builds()))
               for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert all(not thread.is_alive() for thread in threads)
    assert launches == ["frc-one"]
    assert registered == [("frc-one", identity.token())]
    assert sum(len(result["resumed"]) for result in results) == 1
    assert "pg_advisory_xact_lock" in claim_source and "ON CONFLICT (product)" in claim_source
    main_source = inspect.getsource(factory._main)
    assert main_source.index("_await_resume_child_registration") < main_source.index("build_product")


def test_scheduler_terminal_failure_after_child_marker_never_reinvokes_occurrence(monkeypatch):
    token = "sc-occurrence-once"
    due_calls = 0
    markers = []
    child_envs = []
    degraded = []

    def claim_due(_names=None):
        nonlocal due_calls
        due_calls += 1
        return [("marker-job", f"{scheduler.VENV_PY} -c pass", token)] if due_calls == 1 else []

    def run_bounded(argv, timeout, *, env=None):
        markers.append(env["AOS_SCHEDULER_OCCURRENCE_ID"])
        child_envs.append(env)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    stop = threading.Event()
    lost = threading.Event()
    thread = SimpleNamespace(join=lambda timeout=None: None)
    monkeypatch.setattr(scheduler, "bootstrap", lambda: 0)
    monkeypatch.setattr(scheduler, "_claim_due", claim_due)
    monkeypatch.setattr(scheduler, "_mark_execution_started", lambda seen: seen == token or None)
    monkeypatch.setattr(scheduler, "_start_claim_heartbeat", lambda _token: (stop, lost, thread))
    monkeypatch.setattr(scheduler, "_run_bounded", run_bounded)
    monkeypatch.setattr(scheduler, "_persist_terminal_once",
                        lambda _outcome: (_ for _ in ()).throw(RuntimeError("commit ack lost")))
    monkeypatch.setattr(scheduler, "_terminal_persistence_state", lambda _outcome: "unproved")
    monkeypatch.setattr(scheduler, "_persist_degraded_once",
                        lambda outcome, reason: degraded.append((outcome["claim_token"], reason)))
    monkeypatch.setattr(scheduler.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(scheduler.audit, "append", lambda **_kwargs: None)

    assert scheduler.tick(["marker-job"]) == 0
    assert scheduler.tick(["marker-job"]) == 0
    assert markers == [token]
    assert degraded == [(token, "commit ack lost")]
    assert child_envs[0]["AOS_SCHEDULER_CLAIM_TOKEN"] == token


def test_started_stale_scheduler_occurrence_is_degraded_without_early_replay():
    class Cursor:
        rowcount = 0

        def __init__(self):
            self.rows = []
            self.sql = []

        def execute(self, sql, args=()):
            normalized = " ".join(str(sql).split())
            self.sql.append((normalized, args))
            if normalized.startswith("SELECT claim_token,name,execution_started_at"):
                self.rows = [("sc-started", "side-effect-job", datetime.now(timezone.utc))]
                self.rowcount = 1
            else:
                self.rows = []
                self.rowcount = 1

        def fetchall(self):
            rows, self.rows = self.rows, []
            return rows

    cursor = Cursor()
    stale = scheduler._reconcile_stale_claims_cur(cursor)
    assert stale[0][0] == "sc-started"
    statements = [sql for sql, _args in cursor.sql]
    assert any("SET status=%s,finished_at=now()" in sql for sql in statements)
    schedule_updates = [sql for sql in statements if sql.startswith("UPDATE schedules")]
    assert len(schedule_updates) == 1
    assert "failure_count=failure_count+1" in schedule_updates[0]
    assert "next_run" not in schedule_updates[0]
    assert any("occurrence_id" in sql for sql in statements)


def test_scheduler_rejects_hostile_external_python_paths():
    assert scheduler._safe_argv("python -c pass") is None
    assert scheduler._safe_argv("python3 -c pass") is None
    assert scheduler._safe_argv("/tmp/python -c pass") is None
    assert scheduler._safe_argv("/tmp/python3.99 -c pass") is None
    assert scheduler._safe_argv(f"{sys.executable} -c pass") == [sys.executable, "-c", "pass"]


def test_scheduler_sizes_custom_agent_timeout_from_exact_entrypoint_not_name():
    custom_agent = [scheduler.VENV_PY, str(scheduler.SCRIPTS / "customagents.py"),
                    "run", "tenant-one", "76"]
    assert scheduler._job_timeout("ca-76", custom_agent) == scheduler.CUSTOM_AGENT_JOB_TIMEOUT
    # Legacy rows use ``run <id>`` and must receive the same safe migration behavior.
    assert scheduler._job_timeout("ca-38", custom_agent[:3] + ["38"]) == \
        scheduler.CUSTOM_AGENT_JOB_TIMEOUT

    ordinary = [scheduler.VENV_PY, str(scheduler.SCRIPTS / "tasksweep.py"), "run"]
    assert scheduler._job_timeout("ca-looks-like-one", ordinary) == scheduler.JOB_TIMEOUT
    assert scheduler._job_timeout("ordinary", custom_agent[:-3] + ["customagents.py", "run", "76"]) == \
        scheduler.JOB_TIMEOUT


def test_custom_agent_cli_propagates_work_outcome_to_scheduler():
    assert customagents._cli_exit_code({"status": "ok", "rc": 0}) == 0
    assert customagents._cli_exit_code({"skipped": "disabled"}) == 0
    assert customagents._cli_exit_code({"status": "failed", "rc": 1}) == 1
    assert customagents._cli_exit_code({"error": "not found"}) == 1


def test_migration_72_canonically_wires_occurrence_and_exact_resume_identity():
    migration = (ROOT / "postgres" / "initdb" /
                 "72-scheduler-factory-exactness.sql").read_text()
    for required in ("execution_started_at", "heartbeat_at", "occurrence_id",
                     "scheduler_runs_occurrence_uidx", "factory_resume_claims",
                     "worker_start_ticks", "worker_boot_id",
                     "factory_resume_one_active_product_idx",
                     "factory_resume_sweep_state", "REVOKE ALL"):
        assert required in migration
