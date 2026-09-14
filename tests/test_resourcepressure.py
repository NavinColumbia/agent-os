import sys
import io
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "orchestra"))
import resourcepressure


def test_browser_admission_is_zero_below_reserved_memory_or_cpu():
    assert resourcepressure.browser_capacity(2047, 12, reserve_mb=2048) == 0
    assert resourcepressure.browser_capacity(16000, 0, reserve_mb=2048) == 0
    assert resourcepressure.browser_capacity(16000, 12, override="0") == 0


def test_browser_capacity_is_bounded_by_every_dimension():
    assert resourcepressure.browser_capacity(
        4096, 12, reserve_mb=2048, mb_per_session=550,
        cores_per_session=0.8, auto_ceiling=2) == 2
    assert resourcepressure.browser_capacity(
        4096, 1, reserve_mb=2048, mb_per_session=550,
        cores_per_session=0.8, auto_ceiling=8) == 1
    # An override may raise the configured ceiling, but can never manufacture
    # RAM/CPU capacity or bypass the emergency reserve.
    assert resourcepressure.browser_capacity(
        2047, 12, reserve_mb=2048, auto_ceiling=2, override=64) == 0
    assert resourcepressure.browser_capacity(
        4096, 1, reserve_mb=2048, mb_per_session=550,
        cores_per_session=0.8, auto_ceiling=2, override=64) == 1


def test_weighted_host_admission_respects_live_floor_and_combined_ledger():
    allowed, reason = resourcepressure.weighted_host_admission(
        reserved_memory_mb=1000, reserved_cpu_millis=1000,
        request_memory_mb=550, request_cpu_millis=800,
        memory_envelope_mb=1600, cpu_envelope_millis=2000,
        available_memory_mb=5000, emergency_floor_mb=4096)
    assert (allowed, reason) == (True, "admitted")
    allowed, reason = resourcepressure.weighted_host_admission(
        reserved_memory_mb=1550, reserved_cpu_millis=1800,
        request_memory_mb=384, request_cpu_millis=1000,
        memory_envelope_mb=1600, cpu_envelope_millis=2000,
        available_memory_mb=5000, emergency_floor_mb=4096)
    assert allowed is False and reason in {"memory-envelope", "cpu-envelope"}
    allowed, reason = resourcepressure.weighted_host_admission(
        reserved_memory_mb=0, reserved_cpu_millis=0,
        request_memory_mb=550, request_cpu_millis=800,
        memory_envelope_mb=999999, cpu_envelope_millis=999999,
        available_memory_mb=4500, emergency_floor_mb=4096)
    assert (allowed, reason) == (False, "emergency-memory-floor")
    allowed, reason = resourcepressure.weighted_host_admission(
        reserved_memory_mb=0, reserved_cpu_millis=0,
        request_memory_mb=550, request_cpu_millis=800,
        memory_envelope_mb=999999, cpu_envelope_millis=999999,
        available_memory_mb=10000, emergency_floor_mb=4096,
        available_cpu_millis=2500, emergency_cpu_floor_millis=2000)
    assert (allowed, reason) == (False, "emergency-cpu-floor")


def test_weighted_host_decision_converges_under_concurrent_admission():
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    ledger = {"memory": 0, "cpu": 0}
    results = []

    def contender():
        barrier.wait()
        with lock:  # mirrors the DB advisory-xact serialization
            ok, _ = resourcepressure.weighted_host_admission(
                reserved_memory_mb=ledger["memory"], reserved_cpu_millis=ledger["cpu"],
                request_memory_mb=1000, request_cpu_millis=1000,
                memory_envelope_mb=1500, cpu_envelope_millis=1500,
                available_memory_mb=10000, emergency_floor_mb=4096)
            if ok:
                ledger["memory"] += 1000
                ledger["cpu"] += 1000
            results.append(ok)

    threads = [threading.Thread(target=contender) for _ in range(2)]
    for thread in threads: thread.start()
    for thread in threads: thread.join(timeout=1)
    assert sorted(results) == [False, True]


def test_runtime_workers_are_clamped_and_invalid_requests_fail_closed():
    assert resourcepressure.runtime_worker_limit(
        1000, cpu_count=12, db_pool_max=16, hard_ceiling=8) == 8
    assert resourcepressure.runtime_worker_limit(
        8, cpu_count=12, db_pool_max=4, hard_ceiling=8) == 2
    with pytest.raises(ValueError, match="positive"):
        resourcepressure.runtime_worker_limit(0, cpu_count=12, db_pool_max=16)


def test_qa_tool_threads_never_exceed_browser_capacity():
    assert resourcepressure.tool_worker_capacity(100, 2) == 2
    assert resourcepressure.tool_worker_capacity(2, 0) == 0
    assert resourcepressure.tool_worker_capacity("invalid", 2) == 0


def test_combined_agent_capacity_is_host_sized_and_can_close():
    assert resourcepressure.agent_process_capacity(16000, 12) == 8
    assert resourcepressure.agent_process_capacity(4095, 12) == 0
    assert resourcepressure.agent_process_capacity(16000, 12, override=2) == 2


def test_queue_aging_eventually_serves_old_low_priority_work():
    tasks = [
        {"id": 1, "priority": 9, "created_at": 0},
        {"id": 2, "priority": 1, "created_at": 2399},
    ]
    ordered = resourcepressure.fair_order(tasks, now=2400, aging_seconds=300)
    assert [x["id"] for x in ordered] == [1, 2]


def test_pressure_snapshot_never_hides_oversubscription():
    p = resourcepressure.pressure_snapshot(
        browsers_active=3, browser_capacity=2, encoders_active=1, encoder_capacity=1,
        db_in_use=14, db_capacity=16, workers_active=8, worker_capacity=8, queued=12)
    assert p["resources"]["browser"]["oversubscribed_by"] == 1
    assert p["resources"]["encoder"]["saturated"] is True
    assert p["queued"] == 12
    assert p["admit_new"] is False


def test_live_browser_gate_uses_zero_capacity_instead_of_forced_one(monkeypatch):
    import browser_gate
    monkeypatch.delenv("AOS_BROWSER_GLOBAL_MAX", raising=False)
    monkeypatch.setattr("builtins.open", lambda *a, **k: io.StringIO("MemAvailable: 1024 kB\n"))
    monkeypatch.setattr(browser_gate.os, "cpu_count", lambda: 12)
    assert browser_gate._auto_cap() == 0
    monkeypatch.setattr(browser_gate, "GLOBAL_MAX", 2)
    monkeypatch.setattr(browser_gate, "_auto_cap", lambda: 0)
    monkeypatch.setattr(browser_gate.claude_gate, "_ensure",
                        lambda *a, **k: pytest.fail("zero pressure capacity must not touch DB admission"))
    assert browser_gate.acquire("offline-pressure-test", wait_s=0) is None


def test_claude_context_does_not_launch_without_global_capacity(monkeypatch):
    import claude_gate
    monkeypatch.setattr(claude_gate, "FAIL_OPEN", False)
    monkeypatch.setattr(claude_gate, "acquire", lambda *a, **k: None)
    with pytest.raises(claude_gate.CapacityUnavailable):
        with claude_gate.slot("saturation-test", wait_s=0):
            pytest.fail("work must not start without a global provider slot")


def test_generic_agent_slot_uses_dynamic_capacity_and_fenced_lease(monkeypatch):
    import contextlib
    import claude_gate
    seen = {}

    @contextlib.contextmanager
    def fake_slot(holder, **kwargs):
        seen.update({"holder": holder, **kwargs})
        yield claude_gate.SlotLease(1, "token")

    monkeypatch.setattr(claude_gate, "agent_capacity", lambda: 3)
    monkeypatch.setattr(claude_gate, "slot", fake_slot)
    resource = claude_gate.HostResourceLease("host-1", "resource-token")
    monkeypatch.setattr(resource, "start_heartbeat", lambda **kwargs: resource)
    monkeypatch.setattr(resource, "stop_heartbeat", lambda: None)
    monkeypatch.setattr(claude_gate, "acquire_host_resource", lambda *a, **k: resource)
    monkeypatch.setattr(claude_gate, "release_host_resource", lambda lease: None)
    with claude_gate.agent_slot("codex:qa:123", wait_s=4, lease_s=99) as lease:
        assert int(lease) == 1 and lease.owner_token == "token"
    assert seen["table"] == "agent_slots"
    assert seen["max_slots"] == 3 and seen["grow"] is True


def test_expired_slot_owner_cannot_release_a_reclaimed_generation(monkeypatch):
    import claude_gate
    statements = []

    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def execute(self, sql, params=None): statements.append((sql, params))

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def cursor(self): return Cursor()
        def commit(self): pass

    monkeypatch.setattr(claude_gate.psycopg, "connect", lambda *a, **k: Conn())
    claude_gate.release(claude_gate.SlotLease(4, "old-generation"), table="agent_slots")
    sql, params = statements[0]
    assert "owner_token=%s" in sql
    assert params == (4, "old-generation")


def test_legacy_bare_release_cannot_clear_a_tokened_generation(monkeypatch):
    import claude_gate
    statements = []

    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def execute(self, sql, params=None): statements.append((sql, params))
    class Conn:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def cursor(self): return Cursor()
        def commit(self): pass

    monkeypatch.setattr(claude_gate.psycopg, "connect", lambda *a, **k: Conn())
    claude_gate.release(4, table="agent_slots")
    assert "owner_token IS NULL" in statements[0][0]


def test_admission_connection_has_socket_statement_and_lock_deadlines(monkeypatch):
    import claude_gate
    seen = {}
    sentinel = object()

    def connect(db, **kwargs):
        seen.update(kwargs)
        return sentinel

    monkeypatch.setattr(claude_gate.psycopg, "connect", connect)
    assert claude_gate._db_connect(time.monotonic() + 2) is sentinel
    assert 1 <= seen["connect_timeout"] <= 2
    assert "statement_timeout=" in seen["options"]
    assert "lock_timeout=" in seen["options"]


def test_acquire_initializes_deadline_before_schema_bootstrap(monkeypatch):
    import claude_gate
    observed = []

    def ensure(*args, **kwargs):
        observed.append(kwargs.get("deadline"))
        raise TimeoutError("bounded bootstrap")

    monkeypatch.setattr(claude_gate, "_ensure", ensure)
    started = time.monotonic()
    assert claude_gate.acquire("offline", wait_s=0, max_slots=1) is None
    assert observed and observed[0] > started


def test_host_resource_generation_is_token_fenced(monkeypatch):
    import claude_gate
    statements = []

    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def execute(self, sql, params=None): statements.append((sql, params))
    class Conn:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def cursor(self): return Cursor()
        def commit(self): pass

    monkeypatch.setattr(claude_gate, "_db_connect", lambda *a, **k: Conn())
    lease = claude_gate.HostResourceLease("lease-a", "old-token")
    claude_gate.release_host_resource(lease)
    assert "lease_id=%s AND owner_token=%s" in statements[0][0]
    assert statements[0][1] == ("lease-a", "old-token")


def test_host_resource_ledger_serializes_sum_and_insert_under_one_fence():
    import inspect
    import claude_gate
    source = inspect.getsource(claude_gate.acquire_host_resource)
    assert "pg_try_advisory_xact_lock" in source
    assert source.index("sum(memory_mb)") < source.index("INSERT INTO")
    assert "owner_token" in source


def test_browser_pressure_drop_reclaims_old_cap_before_host_admission(monkeypatch):
    import browser_gate
    import claude_gate
    caps = iter([2, 2, 1])
    provider_caps = []
    provider_releases = []
    leases = [claude_gate.SlotLease(1, "old-cap"), claude_gate.SlotLease(1, "new-cap")]
    resource = claude_gate.HostResourceLease("resource", "token")

    monkeypatch.setattr(browser_gate, "current_capacity", lambda: next(caps))
    monkeypatch.setattr(browser_gate, "_LOCAL_SEM", threading.BoundedSemaphore(64))
    monkeypatch.setattr(browser_gate, "_LOCAL_HELD", 0)
    monkeypatch.setattr(browser_gate, "_reclaim_dead_qa_slots", lambda: 0)
    def acquire(*args, **kwargs):
        provider_caps.append(kwargs["max_slots"])
        return leases.pop(0)
    monkeypatch.setattr(claude_gate, "acquire", acquire)
    monkeypatch.setattr(claude_gate, "release", lambda lease, **kwargs: provider_releases.append(lease))
    monkeypatch.setattr(claude_gate, "acquire_host_resource", lambda *a, **k: resource)
    monkeypatch.setattr(claude_gate, "renew", lambda *a, **k: True)
    monkeypatch.setattr(claude_gate, "renew_host_resource", lambda *a, **k: True)
    token = browser_gate.acquire("qa:pressure-drop", wait_s=0)
    try:
        assert token is not None
        assert provider_caps == [2, 1]
        assert int(provider_releases[0]) == 1
    finally:
        browser_gate.release(token)


def test_browser_host_admission_error_releases_partial_provider_lease(monkeypatch):
    import browser_gate
    import claude_gate
    provider = claude_gate.SlotLease(1, "provider-token")
    released = []
    monkeypatch.setattr(browser_gate, "current_capacity", lambda: 1)
    monkeypatch.setattr(browser_gate, "_LOCAL_SEM", threading.BoundedSemaphore(64))
    monkeypatch.setattr(browser_gate, "_LOCAL_HELD", 0)
    monkeypatch.setattr(browser_gate, "_reclaim_dead_qa_slots", lambda: 0)
    monkeypatch.setattr(claude_gate, "acquire", lambda *a, **k: provider)
    monkeypatch.setattr(claude_gate, "acquire_host_resource",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ledger down")))
    monkeypatch.setattr(claude_gate, "release", lambda lease, **kwargs: released.append(lease))
    assert browser_gate.acquire("qa:no-partial", wait_s=0) is None
    assert released == [provider]
    assert browser_gate._LOCAL_HELD == 0


def test_browser_local_pressure_queues_until_the_live_slot_releases(monkeypatch):
    import browser_gate
    import claude_gate
    import threading
    import time

    provider = claude_gate.SlotLease(1, "queued-provider")
    resource = claude_gate.HostResourceLease("queued-resource", "queued-token")
    sem = threading.BoundedSemaphore(64)
    assert sem.acquire(timeout=0)
    monkeypatch.setattr(browser_gate, "_LOCAL_SEM", sem)
    monkeypatch.setattr(browser_gate, "_LOCAL_HELD", 1)
    monkeypatch.setattr(browser_gate, "current_capacity", lambda: 1)
    monkeypatch.setattr(browser_gate, "_reclaim_dead_qa_slots", lambda: 0)
    monkeypatch.setattr(claude_gate, "acquire", lambda *a, **k: provider)
    monkeypatch.setattr(claude_gate, "release", lambda *a, **k: None)
    monkeypatch.setattr(claude_gate, "acquire_host_resource", lambda *a, **k: resource)
    monkeypatch.setattr(claude_gate, "release_host_resource", lambda *a, **k: None)
    monkeypatch.setattr(claude_gate, "renew", lambda *a, **k: True)
    monkeypatch.setattr(claude_gate, "renew_host_resource", lambda *a, **k: True)
    result = []
    waiter = threading.Thread(
        target=lambda: result.append(browser_gate.acquire("qa:queued", wait_s=1)), daemon=True)
    waiter.start()
    time.sleep(0.05)
    assert not result, "a saturated local pressure cap must queue, not fail immediately"
    browser_gate._release_local_capacity()
    waiter.join(timeout=1)
    assert result and result[0] is not None
    browser_gate.release(result[0])


def test_agent_heartbeat_start_error_releases_partial_host_lease(monkeypatch):
    import contextlib
    import claude_gate
    provider = claude_gate.SlotLease(1, "provider-token")
    released = []

    @contextlib.contextmanager
    def admitted(*args, **kwargs):
        yield provider

    resource = claude_gate.HostResourceLease("resource-id", "resource-token")
    monkeypatch.setattr(resource, "start_heartbeat",
                        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("thread unavailable")))
    monkeypatch.setattr(resource, "stop_heartbeat", lambda: None)
    monkeypatch.setattr(claude_gate, "agent_capacity", lambda: 1)
    monkeypatch.setattr(claude_gate, "slot", admitted)
    monkeypatch.setattr(claude_gate, "acquire_host_resource", lambda *a, **k: resource)
    monkeypatch.setattr(claude_gate, "release_host_resource", released.append)
    with pytest.raises(RuntimeError, match="thread unavailable"):
        with claude_gate.agent_slot("agent:no-partial", wait_s=0):
            pass
    assert released == [resource]


def test_factory_codex_subprocess_is_inside_generic_agent_admission(monkeypatch, tmp_path):
    import contextlib
    import types
    import claude_gate
    import factory
    monkeypatch.delenv("AOS_DISABLE_EXTERNAL_MODEL_EXEC", raising=False)
    events = []

    @contextlib.contextmanager
    def admitted(holder, **kwargs):
        events.append(("admit", holder, kwargs))
        try:
            yield 1
        finally:
            events.append(("release", holder))

    def run(*args, **kwargs):
        events.append(("run", args[0][0], args[0], kwargs))
        return types.SimpleNamespace(returncode=0, stdout='{"type":"turn.completed","usage":{}}\n')

    monkeypatch.setattr(claude_gate, "agent_slot", admitted)
    monkeypatch.setattr(claude_gate, "run_fenced", run)
    monkeypatch.setattr(factory, "_govern_writes", lambda *a, **k: None)
    result = factory._run_once_codex("qa", tmp_path, "offline prompt", 30, {},
                                     model="gpt-5.6-luna", reasoning_effort="low")
    assert [e[0] for e in events] == ["admit", "run", "release"]
    assert events[0][1].startswith("codex:v2:") and events[0][1].endswith(":qa")
    assert events[1][2][events[1][2].index("-m") + 1] == "gpt-5.6-luna"
    effort_arg = events[1][2][events[1][2].index("-c") + 1]
    assert effort_arg == 'model_reasoning_effort="low"'
    assert events[1][2][-1] == "-"
    assert "offline prompt" not in events[1][2]
    assert events[1][3]["input"] == "offline prompt"
    assert events[1][3]["stdin"] is subprocess.PIPE
    assert "capture_output" not in events[1][3]
    assert hasattr(events[1][3]["stdout"], "write") and hasattr(events[1][3]["stderr"], "write")
    assert result[-1] == "gpt-5.6-luna"


def test_heartbeat_marks_generation_lost_when_token_renewal_fails():
    import claude_gate
    lease = claude_gate.SlotLease(2, "generation-a")
    lease.start_heartbeat(table="agent_slots", lease_s=0.01,
                          renew_fn=lambda *a, **k: False)
    assert lease.lost.wait(1), "a failed/token-mismatched renewal must fence the generation"
    lease.stop_heartbeat()


def test_fenced_child_is_terminated_and_output_rejected_on_lease_loss(monkeypatch):
    import claude_gate

    class Proc:
        returncode = None
        terminated = killed = False
        def communicate(self, input=None, timeout=None):
            raise claude_gate.subprocess.TimeoutExpired(["agent"], timeout)
        def terminate(self): self.terminated = True; self.returncode = -15
        def kill(self): self.killed = True; self.returncode = -9
        def wait(self, timeout=None): return self.returncode
        def poll(self): return self.returncode

    proc = Proc()
    lease = claude_gate.SlotLease(1, "old")
    lease.lost.set()
    monkeypatch.setattr(claude_gate.subprocess, "Popen", lambda *a, **k: proc)
    with pytest.raises(claude_gate.LeaseLost):
        claude_gate.run_fenced(["agent"], lease=lease, timeout=10)
    assert proc.terminated is True


def test_fenced_cleanup_is_bounded_when_escaped_pipe_holder_never_drains(monkeypatch):
    import claude_gate

    class Proc:
        returncode = None
        stdout = stderr = stdin = None
        def communicate(self, input=None, timeout=None):
            raise claude_gate.subprocess.TimeoutExpired(["agent"], timeout)
        def terminate(self): pass
        def kill(self): pass
        def wait(self, timeout=None):
            raise claude_gate.subprocess.TimeoutExpired(["agent"], timeout)
        def poll(self): return self.returncode

    cleaned = []
    monkeypatch.setattr(claude_gate, "PROCESS_TERM_GRACE_S", 0.01)
    monkeypatch.setattr(claude_gate, "PROCESS_DRAIN_GRACE_S", 0.01)
    monkeypatch.setattr(claude_gate.subprocess, "Popen", lambda *a, **k: Proc())
    started = time.monotonic()
    with pytest.raises(claude_gate.subprocess.TimeoutExpired):
        claude_gate.run_fenced(["agent"], lease=None, timeout=0.01, poll_s=0.001,
                               on_terminate=lambda proc: cleaned.append(proc))
    assert time.monotonic() - started < 0.5
    assert len(cleaned) == 1


def test_fenced_regular_file_exit_race_is_completion_not_false_timeout(monkeypatch):
    """Codex writes JSONL/stderr to files, so no inherited PIPE exists to justify a timeout after root exit."""
    import claude_gate

    class Proc:
        returncode = None
        stdout = stderr = stdin = None

        def communicate(self, input=None, timeout=None):
            self.returncode = 0                    # exits just after the short communicate wait expired
            raise claude_gate.subprocess.TimeoutExpired(["agent"], timeout)

        def poll(self):
            return self.returncode

        def terminate(self):
            raise AssertionError("completed regular-file child must not be terminated")

        def kill(self):
            raise AssertionError("completed regular-file child must not be killed")

    monkeypatch.setattr(claude_gate.subprocess, "Popen", lambda *a, **k: Proc())
    result = claude_gate.run_fenced(["agent"], lease=None, timeout=90, poll_s=0.2,
                                    stdout=object(), stderr=object())

    assert result.returncode == 0


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="double-fork is Linux-specific")
def test_fenced_timeout_does_not_hang_on_double_forked_pipe_holder(monkeypatch, tmp_path):
    import claude_gate
    child_file = tmp_path / "escaped-child.pid"
    code = (
        "import os,time,pathlib\n"
        "child=os.fork()\n"
        "if child==0:\n"
        " os.setsid(); time.sleep(30)\n"
        "else:\n"
        " pathlib.Path(os.environ['CHILD_FILE']).write_text(str(child)); os._exit(0)\n"
    )
    escaped = []

    def exact_cleanup(_proc):
        deadline = time.monotonic() + 0.5
        while not child_file.exists() and time.monotonic() < deadline:
            time.sleep(0.005)
        if child_file.exists():
            pid = int(child_file.read_text())
            escaped.append(pid)
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    monkeypatch.setattr(claude_gate, "PROCESS_TERM_GRACE_S", 0.05)
    monkeypatch.setattr(claude_gate, "PROCESS_DRAIN_GRACE_S", 0.05)
    started = time.monotonic()
    try:
        with pytest.raises(claude_gate.subprocess.TimeoutExpired):
            claude_gate.run_fenced(
                [sys.executable, "-c", code], lease=None, timeout=0.05, poll_s=0.005,
                capture_output=True, env={**os.environ, "CHILD_FILE": str(child_file)},
                on_terminate=exact_cleanup)
        assert time.monotonic() - started < 1.0
        assert escaped
    finally:
        for pid in escaped:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_fenced_spawn_registration_failure_cleans_child_once(monkeypatch):
    import claude_gate

    class Proc:
        returncode = None
        stdout = stderr = stdin = None
        killed = False
        def communicate(self, input=None, timeout=None): self.returncode = -9; return None, None
        def terminate(self): self.returncode = -15
        def kill(self): self.killed = True; self.returncode = -9
        def wait(self, timeout=None): return self.returncode
        def poll(self): return self.returncode

    proc = Proc()
    cleaned = []
    monkeypatch.setattr(claude_gate.subprocess, "Popen", lambda *a, **k: proc)
    with pytest.raises(RuntimeError, match="registry unavailable"):
        claude_gate.run_fenced(
            ["agent"], lease=None,
            on_spawn=lambda child: (_ for _ in ()).throw(RuntimeError("registry unavailable")),
            on_terminate=lambda child: cleaned.append(child))
    assert proc.killed is True and cleaned == [proc]


def test_fenced_cleanup_callback_cannot_defeat_timeout(monkeypatch):
    import claude_gate
    blocker = threading.Event()

    class Proc:
        returncode = None
        stdout = stderr = stdin = None
        def communicate(self, input=None, timeout=None): self.returncode = -9; return None, None
        def terminate(self): self.returncode = -15
        def kill(self): self.returncode = -9
        def wait(self, timeout=None): return self.returncode
        def poll(self): return self.returncode

    monkeypatch.setattr(claude_gate, "PROCESS_CLEANUP_GRACE_S", 0.01)
    monkeypatch.setattr(claude_gate.subprocess, "Popen", lambda *a, **k: Proc())
    lease = claude_gate.SlotLease(1, "lost")
    lease.lost.set()
    started = time.monotonic()
    try:
        with pytest.raises(claude_gate.LeaseLost):
            claude_gate.run_fenced(["agent"], lease=lease,
                                   on_terminate=lambda proc: blocker.wait(10))
        assert time.monotonic() - started < 0.2
    finally:
        blocker.set()


def test_browser_holder_identity_detects_pid_reuse(monkeypatch):
    import browser_gate
    import process_assurance
    expected = process_assurance.ProcessIdentity(42, 100, "boot-a")
    holder = "qa:v2:boot-a:42:100:story"
    assert browser_gate._holder_identity(holder) == expected
    reused = process_assurance.ProcessSnapshot(
        process_assurance.ProcessIdentity(42, 101, "boot-a"), 1, 42, 1000, "node bridge")
    assert not process_assurance.same_process(expected, reused)


def test_weighted_host_holder_and_orphan_reclaim_are_generation_fenced(monkeypatch):
    import claude_gate
    import process_assurance

    identity = process_assurance.ProcessIdentity(42, 100, "boot-a")
    snapshot = process_assurance.ProcessSnapshot(identity, 1, 42, 1000, "worker")
    monkeypatch.setattr(claude_gate.process_assurance, "read_snapshot", lambda pid: snapshot)
    monkeypatch.setattr(claude_gate.os, "getpid", lambda: 42)
    holder = claude_gate.process_holder("codex", "qa-security")
    assert holder == "codex:v2:boot-a:42:100:qa-security"
    assert claude_gate.holder_identity(holder) == identity
    assert claude_gate.holder_identity("codex:qa-security:42") is None

    seen = []

    class Cursor:
        rowcount = 0

        def execute(self, sql, params=None):
            seen.append((sql, params))
            self.rowcount = 1 if "DELETE" in sql else 0

        def fetchall(self):
            return [("lease-dead", holder, "token-dead"),
                    ("lease-legacy", "codex:qa-security:99", "token-legacy")]

    monkeypatch.setattr(claude_gate.process_assurance, "read_snapshot", lambda _pid: None)
    cur = Cursor()
    assert claude_gate._reclaim_dead_host_resources(cur) == 1
    deletes = [(sql, params) for sql, params in seen if "DELETE" in sql]
    assert len(deletes) == 1
    assert "lease_id=%s AND owner_token=%s AND holder=%s" in deletes[0][0]
    assert deletes[0][1] == ("lease-dead", "token-dead", holder)


def test_browser_orphan_reclaim_cannot_clear_replacement_generation(monkeypatch):
    import browser_gate
    seen = []
    holder = "qa:v2:boot-a:42:100:story"

    class Cursor:
        rowcount = 0
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def execute(self, sql, params=None):
            seen.append((sql, params))
            self.rowcount = 1 if "UPDATE" in sql else 0
        def fetchall(self): return [(1, holder, "observed-generation")]
    class Conn:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def cursor(self): return Cursor()
        def commit(self): pass

    monkeypatch.setattr(browser_gate.claude_gate, "_db_connect", lambda: Conn())
    monkeypatch.setattr(browser_gate.process_assurance, "read_snapshot", lambda pid: None)
    assert browser_gate._reclaim_dead_qa_slots() == 1
    update, params = next((sql, params) for sql, params in seen if "UPDATE" in sql)
    assert "owner_token IS NOT DISTINCT FROM %s" in update
    assert params == (1, "observed-generation", holder)
    assert "lease_until=NULL" in update and "owner_token=NULL" in update


def test_factory_claude_path_preserves_owned_child_inside_generic_slot():
    import inspect
    import factory
    source = inspect.getsource(factory._run_once)
    assert "claude_gate.agent_slot" in source
    assert "clauded.run_owned" in source
    assert source.index("claude_gate.agent_slot") < source.index("clauded.run_owned")
    codex = inspect.getsource(factory._run_once_codex)
    assert "clauded.run_owned" in codex
    assert "claude_gate.run_fenced" not in codex


def test_direct_database_fallback_has_a_hard_process_limit(monkeypatch):
    import dbpool
    sem = threading.BoundedSemaphore(1)
    assert sem.acquire(blocking=False)
    monkeypatch.setattr(dbpool, "_direct_sem", sem)
    monkeypatch.setattr(dbpool, "_DIRECT_MAX", 1)
    monkeypatch.setattr(dbpool, "_DIRECT_WAIT_S", 0)
    monkeypatch.setattr(dbpool, "_get_pool", lambda: None)
    monkeypatch.setattr(dbpool.psycopg, "connect",
                        lambda *a, **k: pytest.fail("must not connect past direct limit"))
    try:
        with pytest.raises(dbpool.DatabaseCapacityError):
            with dbpool.connection():
                pass
    finally:
        sem.release()


def test_dispatcher_claim_query_contains_aging_and_skip_locked(monkeypatch):
    import dispatcher
    seen = []

    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def execute(self, query, params=None): seen.append((query, params))
        def fetchall(self): return []

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def cursor(self): return Cursor()

    monkeypatch.setattr(dispatcher, "_conn", lambda tenant_id=None: Conn())
    assert dispatcher._pull(2) == []
    sql = seen[0][0]
    assert "EXTRACT(EPOCH" in sql and "created_at" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert seen[0][1]["aging"] == dispatcher.PRIORITY_AGING_S


def test_dispatcher_singleton_uses_kernel_lifetime_lock_before_pidfile():
    source = (SCRIPTS / "dispatcher.sh").read_text()
    assert 'flock -n 9' in source
    assert source.index('flock -n 9') < source.index('echo $$ > "$PIDFILE"')
    assert 'kill -0 "$(' not in source


def test_dispatcher_claim_batch_and_fleet_have_hard_caps():
    import inspect
    import dispatcher
    assert dispatcher.MAX_PER_TICK <= dispatcher._batch_ceiling
    source = inspect.getsource(dispatcher.fleet)
    assert "resourcepressure.runtime_worker_limit" in source


def test_zero_qa_capacity_fails_incomplete_without_launching_tool(monkeypatch):
    import jobrunner
    emitted = []

    class Store:
        def emit_once(self, *args):
            emitted.append(args)
            return {"id": 1}

    monkeypatch.setattr(jobrunner, "_QA_TOOL_CAP", 0)
    job = {"run_id": 991100, "tenant": "pressure-test", "actor_id": 7,
           "tool": "qa_explore", "args": {"story": {"id": "S1"}}}
    called = []
    jid = jobrunner.dispatch(job, Store(), run_tool=lambda *a: called.append(a), sync=True)
    try:
        assert called == []
        payload = emitted[0][5]
        assert payload["status"] == "failed"
        assert payload["result"]["stop_reason"] == "capacity-wait-checkpoint"
        assert payload["result"]["checkpoint_required"] is True
        assert payload["result"]["capacity_wait"] is True
    finally:
        with jobrunner._LOCK:
            jobrunner._JOBS.pop(jid, None)


def test_queued_qa_admission_observes_cancellation_promptly():
    import jobrunner
    sem = threading.BoundedSemaphore(1)
    assert sem.acquire(blocking=False)
    jid = "pressure-cancel"
    event = threading.Event()
    job = {"run_id": 1, "tenant": "t", "tool": "qa_explore"}
    with jobrunner._LOCK:
        jobrunner._JOBS[jid] = {"state": "running", "cancel_event": event, "job": job}
    timer = threading.Timer(0.05, event.set)
    timer.start()
    started = time.monotonic()
    try:
        with jobrunner._Admission(sem, jid, job) as admitted:
            assert admitted is False
        assert time.monotonic() - started < 0.5
    finally:
        timer.cancel()
        sem.release()
        with jobrunner._LOCK:
            jobrunner._JOBS.pop(jid, None)


def test_ffmpeg_does_not_start_without_shared_media_capacity(monkeypatch, tmp_path):
    sys.path.insert(0, str(SCRIPTS / "qa"))
    import artifacts
    import browser_gate
    source = tmp_path / "evidence.webm"
    source.write_bytes(b"video")
    monkeypatch.setattr(artifacts.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(browser_gate, "acquire", lambda *a, **k: None)
    monkeypatch.setattr(artifacts, "_run_media_owned",
                        lambda *a, **k: pytest.fail("ffmpeg must not launch without media capacity"))
    output = tmp_path / "evidence.mp4"
    assert artifacts.webm_to_mp4(source, output) is None
    assert artifacts.webm_to_mp4_result(source, output) == {
        "status": "deferred", "reason": "media capacity unavailable"}


def test_media_admission_releases_exactly_once(monkeypatch):
    sys.path.insert(0, str(SCRIPTS / "qa"))
    import artifacts
    import browser_gate
    released = []
    acquired = []
    token = {"kind": "test", "db_sid": 7}
    monkeypatch.setattr(browser_gate, "acquire",
                        lambda *a, **k: acquired.append((a, k)) or token)
    monkeypatch.setattr(browser_gate, "release", released.append)
    with artifacts._media_admission("offline") as admitted:
        assert admitted is token
    assert released == [token]
    assert acquired[0][1]["resource_kind"] == "media"


def test_media_child_runs_under_admission_fence_and_exact_registry(monkeypatch):
    sys.path.insert(0, str(SCRIPTS / "qa"))
    import artifacts
    import browser_gate
    import clauded
    token = {"kind": "local", "db_sid": object()}
    seen = {}
    monkeypatch.setattr(browser_gate, "fencing_lease", lambda admission: admission["db_sid"])
    monkeypatch.setattr(clauded, "run_owned",
                        lambda args, **kwargs: seen.update({"args": args, **kwargs}) or object())
    artifacts._run_media_owned(["ffmpeg", "-i", "in", "out.mp4"], token, timeout=12)
    assert seen["lease"] is token["db_sid"]
    assert seen["owner"].startswith("qa-media:")
    assert seen["check"] is True and seen["timeout"] == 12


def test_browser_bridge_fences_exact_tree_immediately_on_host_lease_loss(monkeypatch):
    sys.path.insert(0, str(SCRIPTS / "qa"))
    import browser_gate
    import clauded
    import qa_explorer
    lease = type("Lease", (), {"lost": threading.Event()})()
    lease.lost.set()

    class Proc:
        pid = 4321
        stdin = object()
        def poll(self): return None

    bridge = object.__new__(qa_explorer.BrowserBridge)
    bridge.proc = Proc()
    bridge._gate_slot = {"db_sid": lease}
    bridge._ownership_record = {"pid": 4321}
    exact = []
    signals = []
    monkeypatch.setattr(clauded, "_signal_registered_descendants", exact.append)
    monkeypatch.setattr(bridge, "_terminate_process_tree", signals.append)
    result = bridge._send({"cmd": "state"})
    assert result["ok"] is False and "lease lost" in result["error"]
    assert exact == [bridge._ownership_record]
    assert signals == [qa_explorer.signal.SIGTERM]
