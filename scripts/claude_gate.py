#!/usr/bin/env python3
"""claude_gate.py — a CROSS-PROCESS concurrency gate for `claude` CLI calls (the deeper F12 fix).

The problem the per-process semaphore couldn't solve: factory's `_AGENT_SEM` caps concurrent agent calls to 8
WITHIN one python process — but agent-os runs many processes at once (build driver, QA run, jobd, the console
server, ad-hoc scripts), each with its OWN semaphore. So the true concurrency against the single Claude
subscription is 8 × (number of processes) — dozens of simultaneous calls, which throttles the subscription and
makes calls slow/hang. There is no shared limiter.

This is that shared limiter: ONE global pool of N slots in Postgres. Every `claude` invocation (factory._run_once)
must hold a slot for its duration, so TOTAL concurrent claude calls across the whole box is capped at N, no
matter how many processes are running. Lease-based: a slot whose holder died (crash / the G1 orphan problem) is
reclaimed after LEASE_S, so a dead process can't permanently hold a slot. Admission fails closed by default:
provider overload or a DB outage may delay work, but cannot create an unbounded subprocess stampede.

    with claude_gate.slot("build:1234"):     # blocks until a global slot is free (or wait budget elapses)
        subprocess.run(["claude", ...])
    claude_gate.py status | selftest
"""
import contextlib
import math
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import psycopg  # noqa: E402
import trace as _trace  # noqa: E402
import process_assurance  # noqa: E402
import resourcepressure  # noqa: E402

DB = _trace.DB


def _env_int(name, default, minimum=0):
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return max(minimum, int(default))


def _env_float(name, default, minimum=0.01):
    try:
        return max(float(minimum), float(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return max(float(minimum), float(default))


def _auto_global_max():
    """Total concurrent `claude -p` calls allowed across the box. Env override wins; else AUTO-SIZE.

    The binding constraint is NOT this box. A `claude -p` process is an API client — the heavy compute is
    server-side, so locally it costs little more than RAM. What actually saturates is the PROVIDER: on a
    subscription, sustained heavy calls get rate-limited, fail over to Codex, and degrade QA coverage. So the
    ceiling below is a provider-capacity estimate, deliberately far under what the hardware could drive.

    NOTE: this returns ~8, NOT the 'cores*3, floor 12' an earlier version of this docstring described — that
    text outlived the code and overstated the real limit by ~4.5x on a 12-core box. Anything reasoning about
    headroom should read GLOBAL_MAX, not this prose. Ops override: AOS_CLAUDE_GLOBAL_MAX (raise it with an API
    key or higher tier that tolerates more concurrency; lower it if a plan still hits rate limits)."""
    env = os.environ.get("AOS_CLAUDE_GLOBAL_MAX")
    if env:
        try:
            return max(1, int(env))
        except ValueError:
            pass
    # Empirical for Anthropic/Claude subscription mode: sustained heavy calls (QA's long browser-exploration
    # prompts, many workers for hours) at high concurrency hit the subscription's rate limit. Keep the
    # Claude-specific default conservative; raise AOS_CLAUDE_GLOBAL_MAX only with an API key / higher-tier
    # plan that tolerates more. Codex-primary runs bypass this gate and use the factory's Codex path.
    try:
        cores = os.cpu_count() or 4
    except Exception:
        cores = 4
    return max(6, min(8, cores))


GLOBAL_MAX = _auto_global_max()                                    # total concurrent claude calls across the box
LEASE_S = _env_int("AOS_CLAUDE_LEASE_S", 1200, 1)
WAIT_S = _env_int("AOS_CLAUDE_WAIT_S", 900, 0)
FAIL_OPEN = os.environ.get("AOS_CLAUDE_FAIL_OPEN", "0").strip().lower() in {"1", "true", "yes", "on"}
AGENT_TABLE = "agent_slots"
AGENT_RESERVE_MB = _env_int("AOS_AGENT_RESERVE_MB", 4096, 0)
AGENT_MB = _env_int("AOS_AGENT_MB_PER_PROCESS", 1200, 1)
AGENT_CEILING = _env_int("AOS_AGENT_AUTO_CEILING", 8, 0)
AGENT_CPU_MILLIS = _env_int("AOS_AGENT_CPU_MILLIS", 1000, 1)

# Every DB operation in the admission path is bounded independently of the caller's
# capacity-wait budget.  A wedged socket or DDL/advisory-lock waiter must never pin a
# controller thread before its deadline has even been initialized.
DB_CONNECT_TIMEOUT_S = _env_int("AOS_ADMISSION_DB_CONNECT_TIMEOUT_S", 3, 1)
DB_OPERATION_TIMEOUT_S = _env_int("AOS_ADMISSION_DB_OPERATION_TIMEOUT_S", 5, 1)
DB_LOCK_TIMEOUT_MS = _env_int("AOS_ADMISSION_DB_LOCK_TIMEOUT_MS", 1000, 1)
DB_STATEMENT_TIMEOUT_MS = _env_int("AOS_ADMISSION_DB_STATEMENT_TIMEOUT_MS", 4000, 1)
PROCESS_TERM_GRACE_S = _env_float("AOS_FENCED_TERM_GRACE_S", 2)
PROCESS_DRAIN_GRACE_S = _env_float("AOS_FENCED_DRAIN_GRACE_S", 1)
PROCESS_CLEANUP_GRACE_S = _env_float("AOS_FENCED_CLEANUP_GRACE_S", 2)

# One weighted host ledger covers model clients, Chromium and ffmpeg. Resource-specific
# provider/browser caps still apply; this is the missing cross-resource envelope that
# prevents each independent gate from spending the same apparent free RAM/CPU.
HOST_RESOURCE_TABLE = "host_resource_leases"
HOST_RESOURCE_LEASE_S = _env_int("AOS_HOST_RESOURCE_LEASE_S", 1200, 1)
HOST_EMERGENCY_FLOOR_MB = _env_int("AOS_HOST_EMERGENCY_FLOOR_MB", 4096, 0)
HOST_EMERGENCY_CPU_MILLIS = _env_int("AOS_HOST_EMERGENCY_CPU_MILLIS", 2000, 0)
HOST_MEMORY_ENVELOPE_MB = _env_int("AOS_HOST_MEMORY_ENVELOPE_MB", 0, 0)
HOST_CPU_ENVELOPE_MILLIS = _env_int("AOS_HOST_CPU_ENVELOPE_MILLIS", 0, 0)
HOST_ADVISORY_KEY = "aos:host-resource-admission:v1"
_ENSURE_LOCK = threading.Lock()
_ENSURED_TABLES = set()
_HOST_SCHEMA_READY = False


def process_holder(kind, label="") -> str:
    """Return a boot/PID/start-generation-bound holder for durable capacity rows."""
    prefix = "".join(ch for ch in str(kind).lower() if ch.isalnum() or ch in "_-")[:24] or "process"
    snap = process_assurance.read_snapshot(os.getpid())
    if snap is None:
        return f"{prefix}:legacy-pid={os.getpid()}:{str(label)[:100]}"
    ident = snap.identity
    return f"{prefix}:v2:{ident.boot_id}:{ident.pid}:{ident.start_ticks}:{str(label)[:100]}"


def holder_identity(holder):
    """Parse only the exact v2 holder format; legacy bare-PID rows expire naturally."""
    try:
        _kind, version, boot_id, pid, start_ticks, _label = str(holder).split(":", 5)
        if version == "v2":
            return process_assurance.ProcessIdentity(int(pid), int(start_ticks), boot_id)
    except (TypeError, ValueError):
        pass
    return None


def _reclaim_dead_host_resources(cur) -> int:
    """Delete only leases whose exact process generation is provably gone."""
    cur.execute(f"SELECT lease_id, holder, owner_token FROM {HOST_RESOURCE_TABLE} "
                "WHERE lease_until>now()")
    reclaimed = 0
    for lease_id, holder, owner_token in cur.fetchall():
        identity = holder_identity(holder)
        if identity is None or process_assurance.same_process(
                identity, process_assurance.read_snapshot(identity.pid)):
            continue
        cur.execute(f"DELETE FROM {HOST_RESOURCE_TABLE} "
                    "WHERE lease_id=%s AND owner_token=%s AND holder=%s",
                    (lease_id, owner_token, holder))
        reclaimed += max(0, int(cur.rowcount or 0))
    return reclaimed


class CapacityUnavailable(RuntimeError):
    """No globally-fenced provider capacity was available inside the wait budget."""


class LeaseLost(RuntimeError):
    """The durable slot generation was lost; work from its child must be discarded."""


def _operation_deadline(seconds=DB_OPERATION_TIMEOUT_S):
    return time.monotonic() + max(0.001, float(seconds))


def _db_connect(deadline=None):
    """Open one admission connection with real socket/query/lock deadlines."""
    deadline = deadline if deadline is not None else _operation_deadline()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("admission database deadline elapsed")
    connect_s = max(1, min(DB_CONNECT_TIMEOUT_S, int(math.ceil(remaining))))
    statement_ms = max(1, min(DB_STATEMENT_TIMEOUT_MS, int(remaining * 1000)))
    lock_ms = max(1, min(DB_LOCK_TIMEOUT_MS, statement_ms))
    return psycopg.connect(
        DB, connect_timeout=connect_s,
        options=f"-c statement_timeout={statement_ms} -c lock_timeout={lock_ms}")


class SlotLease(int):
    """Backward-compatible integer slot id carrying a fencing token."""
    def __new__(cls, slot_id, owner_token):
        value = int.__new__(cls, int(slot_id))
        value.owner_token = str(owner_token)
        value.lost = threading.Event()
        value._heartbeat_stop = threading.Event()
        value._heartbeat_thread = None
        value._resource_leases = []
        return value

    def attach_resource(self, resource_lease):
        """Make a weighted host lease part of this generation's fencing boundary."""
        if resource_lease is not None:
            resource_lease.lost = self.lost
            self._resource_leases.append(resource_lease)
        return self

    def start_heartbeat(self, *, table, lease_s, renew_fn=None):
        """Renew this exact generation. Any failed/token-mismatched renewal fences its child."""
        if self._heartbeat_thread is not None:
            return self
        renew_fn = renew_fn or renew
        # Long leases do not need a write every two seconds. That old cadence
        # multiplied DB pressure across the whole fleet; 30s still fences loss
        # promptly while cutting steady-state renewal traffic by ~15x.
        interval = max(0.1, min(30.0, float(lease_s) / 3.0))
        self._heartbeat_table = table
        self._heartbeat_lease_s = lease_s

        def beat():
            while not self._heartbeat_stop.wait(interval):
                if not renew_fn(self, table=table, lease_s=lease_s):
                    self.lost.set()
                    return
        self._heartbeat_thread = threading.Thread(
            target=beat, name=f"slot-heartbeat:{table}:{int(self)}", daemon=True)
        self._heartbeat_thread.start()
        return self

    def stop_heartbeat(self):
        self._heartbeat_stop.set()
        thread = self._heartbeat_thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=1)
        for resource_lease in tuple(self._resource_leases):
            resource_lease.stop_heartbeat()

    def confirm_owned(self):
        """Synchronously fence result acceptance against a completion/heartbeat scheduling race."""
        table = getattr(self, "_heartbeat_table", None)
        if table is None:
            return not self.lost.is_set()
        owned = renew(self, table=table, lease_s=self._heartbeat_lease_s)
        if not owned:
            self.lost.set()
            return False
        for resource_lease in tuple(self._resource_leases):
            if not resource_lease.confirm_owned():
                self.lost.set()
                return False
        return True


class HostResourceLease:
    """Fenced generation in the shared weighted host-resource ledger."""
    def __init__(self, lease_id, owner_token):
        self.lease_id = str(lease_id)
        self.owner_token = str(owner_token)
        self.lost = threading.Event()
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread = None
        self._lease_s = HOST_RESOURCE_LEASE_S

    def start_heartbeat(self, *, lease_s=HOST_RESOURCE_LEASE_S, lost_event=None,
                        renew_fn=None):
        if self._heartbeat_thread is not None:
            return self
        if lost_event is not None:
            self.lost = lost_event
        self._lease_s = max(1, int(lease_s))
        renew_fn = renew_fn or renew_host_resource
        interval = max(0.1, min(30.0, float(self._lease_s) / 3.0))

        def beat():
            while not self._heartbeat_stop.wait(interval):
                if not renew_fn(self, lease_s=self._lease_s):
                    self.lost.set()
                    return
        self._heartbeat_thread = threading.Thread(
            target=beat, name=f"host-resource-heartbeat:{self.lease_id[:12]}", daemon=True)
        self._heartbeat_thread.start()
        return self

    def stop_heartbeat(self):
        self._heartbeat_stop.set()
        thread = self._heartbeat_thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=1)

    def confirm_owned(self):
        owned = renew_host_resource(self, lease_s=self._lease_s)
        if not owned:
            self.lost.set()
        return owned


def agent_capacity():
    """Re-probe host pressure for every new generic agent admission."""
    try:
        available_mb = 0
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    available_mb = int(line.split()[1]) // 1024
                    break
        return resourcepressure.agent_process_capacity(
            available_mb, os.cpu_count() or 1, reserve_mb=AGENT_RESERVE_MB,
            mb_per_agent=AGENT_MB, ceiling=AGENT_CEILING,
            override=os.environ.get("AOS_AGENT_GLOBAL_MAX"))
    except Exception:
        return resourcepressure.agent_process_capacity(
            0, 0, ceiling=AGENT_CEILING,
            override=os.environ.get("AOS_AGENT_GLOBAL_MAX"))


def _ensure(table="claude_slots", n=GLOBAL_MAX, grow=False, legacy_lease_s=LEASE_S,
            deadline=None):
    outer_deadline = deadline if deadline is not None else _operation_deadline()
    deadline = min(outer_deadline, _operation_deadline())
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not _ENSURE_LOCK.acquire(timeout=remaining):
        raise TimeoutError("admission schema initialization deadline elapsed")
    try:
        schema_needed = table not in _ENSURED_TABLES
        if not schema_needed and not grow:
            return
        with _db_connect(deadline) as c, c.cursor() as cur:
            if schema_needed:
                cur.execute(f"""CREATE TABLE IF NOT EXISTS {table} (
                    slot_id INT PRIMARY KEY, holder TEXT, acquired_at TIMESTAMPTZ,
                    lease_until TIMESTAMPTZ, owner_token TEXT)""")
                cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS lease_until TIMESTAMPTZ")
                cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS owner_token TEXT")
                # Rolling upgrade: preserve leases held by old processes before token/lease_until existed.
                cur.execute(f"""UPDATE {table} SET lease_until=acquired_at+make_interval(secs=>%s)
                                 WHERE holder IS NOT NULL AND acquired_at IS NOT NULL AND lease_until IS NULL""",
                            (legacy_lease_s,))
            cur.execute(f"SELECT count(*) FROM {table}")
            if cur.fetchone()[0] == 0 or grow:
                cur.execute(f"INSERT INTO {table} (slot_id) SELECT g FROM generate_series(1,%s) g "
                            f"ON CONFLICT (slot_id) DO NOTHING", (n,))
            c.commit()
            _ENSURED_TABLES.add(table)
    finally:
        _ENSURE_LOCK.release()


def acquire(holder, wait_s=WAIT_S, table="claude_slots", lease_s=LEASE_S,
            max_slots=None, grow=False):
    """Claim a free/expired slot or return None; slot() applies the fail-closed/open policy."""
    capacity = GLOBAL_MAX if max_slots is None else max(0, int(max_slots))
    if capacity <= 0:
        return None
    # Initialize the whole-operation deadline before schema/bootstrap work. A wait_s=0
    # caller still gets one bounded DB attempt, never an unbounded connect or DDL wait.
    wait_budget = max(0.0, float(wait_s))
    deadline = time.monotonic() + max(float(DB_OPERATION_TIMEOUT_S), wait_budget)
    try:
        _ensure(table, capacity, grow=grow, legacy_lease_s=lease_s, deadline=deadline)
    except Exception:
        return None
    backoff = 0.5
    while True:
        try:
            with _db_connect(deadline) as c, c.cursor() as cur:
                # Never join an unbounded advisory-lock queue. Retrying the try-lock is
                # covered by the same monotonic admission deadline.
                cur.execute("SELECT pg_try_advisory_xact_lock(hashtext(%s))",
                            (f"agent-capacity:{table}",))
                if not bool(cur.fetchone()[0]):
                    row = None
                    c.commit()
                    raise BlockingIOError("admission advisory lock busy")
                cur.execute(f"SELECT count(*) FROM {table} WHERE holder IS NOT NULL AND lease_until>now()")
                if int(cur.fetchone()[0]) >= capacity:
                    row = None
                else:
                    token = uuid.uuid4().hex
                    cur.execute(f"""
                        UPDATE {table} SET holder=%s, acquired_at=now(),
                            lease_until=now()+make_interval(secs=>%s), owner_token=%s
                        WHERE slot_id = (
                            SELECT slot_id FROM {table}
                            WHERE slot_id<=%s
                              AND (holder IS NULL OR lease_until IS NULL OR lease_until<=now())
                            ORDER BY slot_id LIMIT 1 FOR UPDATE SKIP LOCKED)
                        RETURNING slot_id""", (str(holder)[:80], lease_s, token, capacity))
                    row = cur.fetchone()
                c.commit()
                if row:
                    return SlotLease(row[0], token)
        except BlockingIOError:
            pass
        except Exception:
            return None                          # DB trouble means no globally fenced capacity
        if wait_budget <= 0:
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None                          # bounded wait elapsed
        time.sleep(min(backoff, 5.0, remaining)); backoff *= 1.5


def release(slot_id, table="claude_slots"):
    if slot_id is None:
        return
    try:
        with _db_connect() as c, c.cursor() as cur:
            token = getattr(slot_id, "owner_token", None)
            if token:
                cur.execute(f"""UPDATE {table} SET holder=NULL,acquired_at=NULL,
                               lease_until=NULL,owner_token=NULL
                               WHERE slot_id=%s AND owner_token=%s""", (int(slot_id), token))
            else:
                # Rolling-upgrade compatibility: an old process may only release an old un-tokened row. A
                # delayed bare slot id must never clear a newer tokened generation after expiry/takeover.
                cur.execute(f"""UPDATE {table} SET holder=NULL,acquired_at=NULL,
                               lease_until=NULL,owner_token=NULL
                               WHERE slot_id=%s AND owner_token IS NULL""", (slot_id,))
            c.commit()
    except Exception:
        pass                                     # a leaked slot is reclaimed by the lease — never block on release


def renew(slot_id, table="claude_slots", lease_s=LEASE_S):
    """Extend only the caller's current fenced generation; False means ownership is no longer provable."""
    token = getattr(slot_id, "owner_token", None)
    if not token:
        return False
    try:
        with _db_connect() as c, c.cursor() as cur:
            cur.execute(f"""UPDATE {table}
                            SET lease_until=now()+make_interval(secs=>%s)
                            WHERE slot_id=%s AND owner_token=%s AND lease_until>now()
                            RETURNING slot_id""", (max(1, int(lease_s)), int(slot_id), token))
            owned = cur.fetchone() is not None
            c.commit()
            return owned
    except Exception:
        return False


def _host_snapshot():
    """Return total/available RAM, CPUs and one-minute load; unknown is fail-closed."""
    total_mb = available_mb = 0
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    total_mb = int(line.split()[1]) // 1024
                elif line.startswith("MemAvailable:"):
                    available_mb = int(line.split()[1]) // 1024
    except Exception:
        return 0, 0, 0, 0.0
    try:
        cores = max(0, int(os.cpu_count() or 0))
    except Exception:
        cores = 0
    try:
        with open("/proc/loadavg") as fh:
            load_one = max(0.0, float(fh.read().split()[0]))
    except Exception:
        load_one = float(cores)  # no observable CPU runway => fail closed below
    return total_mb, available_mb, cores, load_one


def host_resource_envelope():
    """Configured static envelope plus current live availability facts."""
    total_mb, available_mb, cores, load_one = _host_snapshot()
    derived_memory = max(0, total_mb - HOST_EMERGENCY_FLOOR_MB)
    memory_mb = HOST_MEMORY_ENVELOPE_MB or derived_memory
    cpu_millis = HOST_CPU_ENVELOPE_MILLIS or (cores * 1000)
    available_cpu_millis = max(0, int((cores - load_one) * 1000))
    return {
        "memory_mb": max(0, int(memory_mb)),
        "cpu_millis": max(0, int(cpu_millis)),
        "available_memory_mb": max(0, int(available_mb)),
        "available_cpu_millis": available_cpu_millis,
        "emergency_floor_mb": HOST_EMERGENCY_FLOOR_MB,
        "emergency_cpu_floor_millis": HOST_EMERGENCY_CPU_MILLIS,
    }


def _ensure_host_resources(deadline=None):
    global _HOST_SCHEMA_READY
    outer_deadline = deadline if deadline is not None else _operation_deadline()
    deadline = min(outer_deadline, _operation_deadline())
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not _ENSURE_LOCK.acquire(timeout=remaining):
        raise TimeoutError("host-resource schema initialization deadline elapsed")
    try:
        if _HOST_SCHEMA_READY:
            return
        with _db_connect(deadline) as c, c.cursor() as cur:
            cur.execute(f"""CREATE TABLE IF NOT EXISTS {HOST_RESOURCE_TABLE} (
                lease_id TEXT PRIMARY KEY,
                holder TEXT NOT NULL,
                resource_kind TEXT NOT NULL,
                memory_mb INT NOT NULL CHECK (memory_mb >= 0),
                cpu_millis INT NOT NULL CHECK (cpu_millis >= 0),
                acquired_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                lease_until TIMESTAMPTZ NOT NULL,
                owner_token TEXT NOT NULL)""")
            cur.execute(f"CREATE INDEX IF NOT EXISTS {HOST_RESOURCE_TABLE}_lease_until_idx "
                        f"ON {HOST_RESOURCE_TABLE}(lease_until)")
            c.commit()
            _HOST_SCHEMA_READY = True
    finally:
        _ENSURE_LOCK.release()


def acquire_host_resource(holder, *, resource_kind, memory_mb, cpu_millis,
                          wait_s=WAIT_S, lease_s=HOST_RESOURCE_LEASE_S):
    """Atomically reserve weighted host capacity, or fail closed by the deadline."""
    try:
        memory_mb = max(0, int(memory_mb))
        cpu_millis = max(0, int(cpu_millis))
        wait_s = max(0.0, float(wait_s))
    except (TypeError, ValueError):
        return None
    deadline = time.monotonic() + max(float(DB_OPERATION_TIMEOUT_S), wait_s)
    try:
        _ensure_host_resources(deadline)
    except Exception:
        return None
    backoff = 0.2
    while True:
        try:
            envelope = host_resource_envelope()
            with _db_connect(deadline) as c, c.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_xact_lock(hashtext(%s))", (HOST_ADVISORY_KEY,))
                if not bool(cur.fetchone()[0]):
                    c.commit()
                    raise BlockingIOError("host-resource advisory lock busy")
                cur.execute(f"DELETE FROM {HOST_RESOURCE_TABLE} WHERE lease_until<=now()")
                _reclaim_dead_host_resources(cur)
                cur.execute(f"SELECT COALESCE(sum(memory_mb),0), COALESCE(sum(cpu_millis),0) "
                            f"FROM {HOST_RESOURCE_TABLE} WHERE lease_until>now()")
                reserved_memory, reserved_cpu = cur.fetchone()
                allowed, _reason = resourcepressure.weighted_host_admission(
                    reserved_memory_mb=reserved_memory,
                    reserved_cpu_millis=reserved_cpu,
                    request_memory_mb=memory_mb,
                    request_cpu_millis=cpu_millis,
                    memory_envelope_mb=envelope["memory_mb"],
                    cpu_envelope_millis=envelope["cpu_millis"],
                    available_memory_mb=envelope["available_memory_mb"],
                    emergency_floor_mb=envelope["emergency_floor_mb"],
                    available_cpu_millis=envelope["available_cpu_millis"],
                    emergency_cpu_floor_millis=envelope["emergency_cpu_floor_millis"],
                )
                if allowed:
                    lease_id = uuid.uuid4().hex
                    token = uuid.uuid4().hex
                    cur.execute(f"""INSERT INTO {HOST_RESOURCE_TABLE}
                        (lease_id, holder, resource_kind, memory_mb, cpu_millis,
                         acquired_at, lease_until, owner_token)
                        VALUES (%s,%s,%s,%s,%s,now(),
                                now()+make_interval(secs=>%s),%s)""",
                        (lease_id, str(holder)[:160], str(resource_kind)[:40], memory_mb,
                         cpu_millis, max(1, int(lease_s)), token))
                    c.commit()
                    return HostResourceLease(lease_id, token)
                c.commit()
        except BlockingIOError:
            pass
        except Exception:
            return None
        if wait_s <= 0:
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(backoff, 2.0, remaining))
        backoff = min(2.0, backoff * 1.5)


def renew_host_resource(lease, *, lease_s=HOST_RESOURCE_LEASE_S):
    if lease is None or not getattr(lease, "owner_token", None):
        return False
    try:
        with _db_connect() as c, c.cursor() as cur:
            cur.execute(f"""UPDATE {HOST_RESOURCE_TABLE}
                SET lease_until=now()+make_interval(secs=>%s)
                WHERE lease_id=%s AND owner_token=%s AND lease_until>now()
                RETURNING lease_id""",
                (max(1, int(lease_s)), lease.lease_id, lease.owner_token))
            owned = cur.fetchone() is not None
            c.commit()
            return owned
    except Exception:
        return False


def release_host_resource(lease):
    if lease is None or not getattr(lease, "owner_token", None):
        return
    try:
        with _db_connect() as c, c.cursor() as cur:
            cur.execute(f"DELETE FROM {HOST_RESOURCE_TABLE} WHERE lease_id=%s AND owner_token=%s",
                        (lease.lease_id, lease.owner_token))
            c.commit()
    except Exception:
        # Expiry reclaims a release whose cleanup connection cannot be established.
        pass


def run_fenced(args, *, lease, timeout=None, poll_s=0.2, on_spawn=None,
               on_terminate=None, **kwargs):
    """subprocess.run-compatible execution that terminates and rejects a child after lease loss."""
    input_data = kwargs.pop("input", None)
    if input_data is not None:
        kwargs.setdefault("stdin", subprocess.PIPE)
    check = bool(kwargs.pop("check", False))
    if kwargs.pop("capture_output", False):
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    proc = subprocess.Popen(args, **kwargs)
    started = time.monotonic()
    termination_prepared = False

    def prepare_termination():
        nonlocal termination_prepared
        if termination_prepared:
            return
        termination_prepared = True
        if on_terminate is not None:
            finished = threading.Event()

            def cleanup():
                try:
                    on_terminate(proc)
                except Exception:
                    pass
                finally:
                    finished.set()

            threading.Thread(target=cleanup, name=f"fenced-cleanup:{getattr(proc, 'pid', '?')}",
                             daemon=True).start()
            # Cleanup gets first chance while ancestry is intact, but a broken
            # registry/filesystem callback cannot defeat the subprocess deadline.
            finished.wait(max(0.01, PROCESS_CLEANUP_GRACE_S))

    def close_pipe(name):
        pipe = getattr(proc, name, None)
        if pipe is not None:
            try:
                pipe.close()
            except Exception:
                pass

    def bounded_terminate(*, terminate_first=True):
        """Clean exact descendants and reap/drain without trusting inherited pipes to close."""
        prepare_termination()
        if proc.poll() is None:
            try:
                (proc.terminate if terminate_first else proc.kill)()
            except Exception:
                pass
            try:
                proc.wait(timeout=max(0.01, PROCESS_TERM_GRACE_S))
            except (subprocess.TimeoutExpired, TimeoutError):
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=max(0.01, PROCESS_TERM_GRACE_S))
                except (subprocess.TimeoutExpired, TimeoutError):
                    pass
            except Exception:
                pass
        try:
            return proc.communicate(timeout=max(0.01, PROCESS_DRAIN_GRACE_S))
        except (subprocess.TimeoutExpired, TimeoutError):
            # A double-forked/escaped descendant can keep an inherited stdout/stderr
            # fd open after the registered tree was signalled. Closing our read ends
            # is the final bounded ownership boundary; never call communicate() again.
            close_pipe("stdin"); close_pipe("stdout"); close_pipe("stderr")
            return None, None
        except Exception:
            close_pipe("stdin"); close_pipe("stdout"); close_pipe("stderr")
            return None, None

    try:
        if on_spawn is not None:
            try:
                on_spawn(proc)
            except Exception:
                bounded_terminate(terminate_first=False)
                raise
        while True:
            if lease is not None and lease.lost.is_set():
                bounded_terminate()
                raise LeaseLost(f"slot {int(lease)} ownership lost; child output fenced")
            remaining = None if timeout is None else timeout - (time.monotonic() - started)
            if remaining is not None and remaining <= 0:
                stdout, stderr = bounded_terminate(terminate_first=False)
                raise subprocess.TimeoutExpired(args, timeout, output=stdout, stderr=stderr)
            try:
                stdout, stderr = proc.communicate(
                    input=input_data, timeout=min(poll_s, remaining) if remaining is not None else poll_s)
                if lease is not None and not lease.confirm_owned():
                    # The root may already have exited while an escaped descendant remains.
                    # The exact registered descendant cleanup still owns that tree.
                    bounded_terminate()
                    raise LeaseLost(f"slot {int(lease)} ownership lost; child output fenced")
                completed = subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)
                if check:
                    completed.check_returncode()
                return completed
            except subprocess.TimeoutExpired:
                input_data = None  # communicate may only send input once
                if proc.poll() is not None:
                    # There are two materially different races here:
                    #
                    # 1) capture_output=True: communicate timed out because an escaped/reparented descendant
                    #    still owns a PIPE after the registered root exited. Reap/fence immediately.
                    # 2) stdout/stderr are regular files (the Codex JSONL path): communicate's short wait
                    #    expired, then the root exited before poll(). There is no pipe left to drain, so this
                    #    is a successful completion—not a fake "timeout after 90s" after ~10 seconds.
                    if proc.stdout is None and proc.stderr is None:
                        if lease is not None and not lease.confirm_owned():
                            bounded_terminate()
                            raise LeaseLost(f"slot {int(lease)} ownership lost; child output fenced")
                        completed = subprocess.CompletedProcess(args, proc.returncode, None, None)
                        if check:
                            completed.check_returncode()
                        return completed
                    stdout, stderr = bounded_terminate(terminate_first=False)
                    raise subprocess.TimeoutExpired(args, timeout, output=stdout, stderr=stderr)
    finally:
        if proc.poll() is None:
            bounded_terminate(terminate_first=False)


@contextlib.contextmanager
def slot(holder, wait_s=WAIT_S, table="claude_slots", lease_s=LEASE_S,
         max_slots=None, grow=False):
    """Hold a global Claude slot, failing closed unless an operator explicitly opts out."""
    sid = acquire(holder, wait_s=wait_s, table=table, lease_s=lease_s,
                  max_slots=max_slots, grow=grow)
    if sid is None and not FAIL_OPEN:
        raise CapacityUnavailable(f"Claude capacity unavailable for {holder!r}")
    if sid is not None:
        sid.start_heartbeat(table=table, lease_s=lease_s)
    try:
        yield sid
    finally:
        if sid is not None:
            sid.stop_heartbeat()
        release(sid, table=table)


@contextlib.contextmanager
def agent_slot(holder, wait_s=WAIT_S, lease_s=LEASE_S):
    """Provider slot plus weighted host reservation for every Claude/Codex child."""
    resource_lease = None
    with slot(holder, wait_s=wait_s, table=AGENT_TABLE, lease_s=lease_s,
              max_slots=agent_capacity(), grow=True) as sid:
        # A provider fail-open override must never bypass the host emergency floor.
        # Also require a provider generation so result acceptance remains fenced.
        if sid is None:
            raise CapacityUnavailable(f"fenced agent capacity unavailable for {holder!r}")
        resource_lease = acquire_host_resource(
            holder, resource_kind="agent", memory_mb=AGENT_MB,
            cpu_millis=AGENT_CPU_MILLIS, wait_s=wait_s, lease_s=lease_s)
        if resource_lease is None:
            raise CapacityUnavailable(f"host resource capacity unavailable for {holder!r}")
        try:
            if not sid.confirm_owned():
                raise LeaseLost(f"agent slot {int(sid)} was lost during host admission")
            sid.attach_resource(resource_lease)
            resource_lease.start_heartbeat(lease_s=lease_s, lost_event=sid.lost)
            yield sid
        finally:
            resource_lease.stop_heartbeat()
            release_host_resource(resource_lease)


def status(table="claude_slots"):
    _ensure(table)
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute(f"SELECT count(*), count(holder), "
                    f"count(*) FILTER (WHERE holder IS NOT NULL AND lease_until>now()) FROM {table}")
        total, held, live = cur.fetchone()
    return {"slots": total, "held": held, "live_held": live, "free": total - live}


def _selftest():
    import uuid
    t = f"claude_slots_test_{uuid.uuid4().hex[:8]}"
    try:
        _ensure(table=t, n=2)                     # a tiny 2-slot pool
        a = acquire("h1", wait_s=2, table=t, max_slots=2)
        b = acquire("h2", wait_s=2, table=t, max_slots=2)
        assert a and b and a != b, f"two distinct slots: {a},{b}"
        c3 = acquire("h3", wait_s=1, table=t, max_slots=2)
        assert c3 is None, f"3rd acquire on a full 2-pool must time out -> None, got {c3}"
        release(a, table=t)
        d = acquire("h4", wait_s=2, table=t, max_slots=2)
        assert d == a, f"released slot is reusable: {d} vs {a}"
        # lease reclaim: force b's slot stale -> it becomes acquirable even though 'held'
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute(f"UPDATE {t} SET lease_until=now()-interval '1 second' WHERE slot_id=%s", (b,)); c.commit()
        e = acquire("h5", wait_s=2, table=t, lease_s=60, max_slots=2)
        assert e == b, f"a lease-expired (crashed-holder) slot is reclaimed: {e} vs {b}"
        release(d, table=t); release(e, table=t)   # free the pool before the ctx-mgr check
        # context manager acquires then releases on exit
        with slot("h6", wait_s=2, table=t, max_slots=2) as sid:
            assert sid is not None, "ctx-mgr should acquire a freed slot"
        assert status(table=t)["live_held"] == 0, "ctx-mgr must release on exit"
        st = status(table=t)
        print(f"claude_gate selftest: PASS (global N-slot cap across processes; released + lease-expired slots "
              f"reclaimed; bounded wait reports full; ctx-mgr releases). final={st}")
        return 0
    finally:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {t}"); c.commit()


if __name__ == "__main__":
    import json
    a = sys.argv[1:]
    if not a or a[0] == "selftest":
        sys.exit(_selftest())
    elif a[0] == "status":
        print(json.dumps(status(), indent=2))
