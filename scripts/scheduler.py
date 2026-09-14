#!/usr/bin/env python3
"""scheduler.py — recurring autonomous jobs (ADR 0004): periodic data ingestion, retention sweeps,
monitors. register() a job + an interval; tick() runs whatever is due, audits each run, advances
next_run. Drive tick() from cron/systemd-timer/a loop. Combined with connectors.py this gives
"live data feeding from any source on a cadence".

    scheduler.py register <name> <interval_s> '<command>'
    scheduler.py bootstrap        # (idempotently) register the default recovery/sweep jobs
    scheduler.py tick             # run all due jobs (also self-heals missing defaults)
    scheduler.py enable <name> | disable <name> | deregister <name>
    scheduler.py list
    scheduler.py selftest
Run with the agent-os venv python.
"""
import os
import signal
import shlex
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent                      # agent-os root; relative job commands resolve against it
VENV_PY = str(ROOT / ".venv" / "bin" / "python")
sys.path.insert(0, str(SCRIPTS))
import audit  # noqa: E402
import process_assurance  # noqa: E402
from dbpool import connection  # noqa: E402

# Per-job wall-clock bound. A job that exceeds this is killed and its turn skipped — it must NOT
# wedge the whole tick (see tick() isolation). Exposed as a module constant so the selftest can
# shrink it without sleeping for two minutes.
JOB_TIMEOUT = 120
TERM_GRACE_S = 2.0
KILL_DRAIN_GRACE_S = 1.0
TERMINAL_PERSIST_ATTEMPTS = 3
TERMINAL_PERSIST_BACKOFF_S = 0.05
STALE_CLAIM_PAGE = 100


def _int_env(name, default, *, minimum, maximum=None):
    """Read a bounded integer without letting one malformed service env kill autonomy."""
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = int(default)
    value = max(int(minimum), value)
    return min(int(maximum), value) if maximum is not None else value


MAX_PARALLEL = _int_env("AOS_SCHEDULER_MAX_PARALLEL", 4, minimum=1, maximum=8)
SNAPSHOT_JOB_TIMEOUT = _int_env("AOS_SNAPSHOT_JOB_TIMEOUT_S", 1800, minimum=300, maximum=7200)
# A standing tenant agent owns its own workload-sized per-attempt limits, retries, provider failover,
# and progress traces.  The scheduler's old generic 120-second wrapper killed healthy eight-minute
# research calls before their *first* attempt could finish, then retried the same weekly occurrence.
# Keep a finite outer failsafe, but leave enough room for the largest normal estimate plus its governed
# retries/failover.  The claim heartbeat below keeps the occurrence leased throughout this longer run.
CUSTOM_AGENT_JOB_TIMEOUT = _int_env(
    "AOS_CUSTOM_AGENT_JOB_TIMEOUT_S", 5400, minimum=900, maximum=21600)
RETRY_BASE_S = _int_env("AOS_SCHEDULER_RETRY_BASE_S", 30, minimum=5, maximum=3600)
CLAIM_LEASE_S = _int_env("AOS_SCHEDULER_CLAIM_LEASE_S", 180,
                         minimum=JOB_TIMEOUT + 30, maximum=3600)
FAIRNESS_MAX_LAG_S = _int_env("AOS_SCHEDULER_FAIRNESS_MAX_LAG_S", 300,
                              minimum=60, maximum=86400)
_RECOVERY_PRIORITY = {
    "controller-resume": 0, "controller-sla": 1, "claude-reap": 2,
    "notification-delivery": 3, "management-control": 4, "resume-sweep": 5,
    "tasksweep": 6, "reap-orphans": 7,
}


def _is_custom_agent_argv(argv):
    """Recognise only the repository-owned custom-agent entry point, never a lookalike job name."""
    if not argv or len(argv) < 3 or argv[2] != "run":
        return False
    try:
        return Path(argv[1]).resolve() == (SCRIPTS / "customagents.py").resolve()
    except (OSError, TypeError, ValueError):
        return False


def _job_timeout(name, argv=None):
    """Return a bounded ceiling sized to the proven workload rather than the schedule's label."""
    if name == "encrypted-snapshot":
        return SNAPSHOT_JOB_TIMEOUT
    if _is_custom_agent_argv(argv):
        return CUSTOM_AGENT_JOB_TIMEOUT
    return JOB_TIMEOUT


def _capture_exact_tree(root_pid):
    """Capture the scheduler-owned process generation and descendants leaf-first."""
    root = process_assurance.read_snapshot(root_pid)
    if root is None:
        return []
    snapshots = process_assurance.scan_snapshots()
    return process_assurance.cleanup_plan(root.identity, snapshots) + [root.identity]


def _signal_exact(identities, sig):
    """Signal only still-matching process generations from an owned cleanup plan."""
    signaled = []
    for expected in identities:
        if not process_assurance.same_process(
                expected, process_assurance.read_snapshot(expected.pid)):
            continue
        try:
            os.kill(expected.pid, sig)
            signaled.append(expected.pid)
        except (ProcessLookupError, PermissionError):
            pass
    return signaled


def _exact_survivors(identities):
    """Return only captured generations that are still running now."""
    return [expected for expected in identities
            if process_assurance.same_process(
                expected, process_assurance.read_snapshot(expected.pid))]


def _signal_job_tree(proc, sig, captured):
    """Clean exact descendants, then the process group created solely for this job."""
    # Capture again before each escalation so a child spawned after the first
    # snapshot is not missed. Descendants that called setsid remain provable by
    # ancestry and are signalled exactly; the owned process group closes the
    # scan-to-signal race for ordinary descendants.
    current = _capture_exact_tree(proc.pid)
    merged = {item.token(): item for item in [*captured, *current]}
    _signal_exact(list(merged.values()), sig)
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _close_capture_pipes(proc):
    """Close local read ends so an escaped pipe-holder cannot pin cleanup."""
    for pipe in (getattr(proc, "stdout", None), getattr(proc, "stderr", None)):
        try:
            if pipe is not None:
                pipe.close()
        except Exception:
            pass


def _run_bounded(argv, timeout, *, env=None):
    """Run a job in its own process group and finitely reap/drain it on timeout."""
    proc = subprocess.Popen(argv, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True, env=env)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as expired:
        captured = _capture_exact_tree(proc.pid)
        _signal_job_tree(proc, signal.SIGTERM, captured)
        try:
            stdout, stderr = proc.communicate(timeout=TERM_GRACE_S)
            # EOF only proves every writer closed its capture pipe. A detached
            # descendant can ignore TERM after redirecting output, so revalidate
            # the exact captured generations and kill any survivor before return.
            survivors = _exact_survivors(captured)
            if survivors:
                _signal_exact(survivors, signal.SIGKILL)
        except subprocess.TimeoutExpired:
            _signal_job_tree(proc, signal.SIGKILL, captured)
            try:
                stdout, stderr = proc.communicate(timeout=KILL_DRAIN_GRACE_S)
            except subprocess.TimeoutExpired:
                # A double-forked/reparented descendant can retain the write end
                # after escaping both ancestry and PGID ownership. We cannot
                # safely signal an unprovable PID, but we can close our read ends
                # and reap the exact root with a finite wait.
                _close_capture_pipes(proc)
                try:
                    proc.wait(timeout=KILL_DRAIN_GRACE_S)
                except subprocess.TimeoutExpired:
                    pass
                stdout = stderr = b""
        expired.stdout, expired.stderr = stdout, stderr
        raise expired
    return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)

# #8/#14: the recovery + sweep jobs that MUST exist for the platform to self-heal. ticker.sh only
# calls `scheduler.py tick`, so nothing else registers these; tick() bootstraps them idempotently
# (ON CONFLICT DO NOTHING — operator overrides of command/interval are preserved). Without this the
# schedules table can come up empty and stalled loops, interrupted builds, expired task leases and
# orphan processes are never recovered.
DEFAULT_SCHEDULES = [
    ("controller-resume", f"{VENV_PY} {SCRIPTS / 'loopcontroller.py'} resume", 600),
    # User-facing SLA: surface "taking longer than usual" on jobs that overran their ETA promptly (tight
    # cadence), well before the 30-min reaper. `controller-resume` also calls it as a 10-min backstop.
    ("controller-sla",    f"{VENV_PY} {SCRIPTS / 'loopcontroller.py'} watchdog", 120),
    ("resume-sweep",      f"{VENV_PY} {SCRIPTS / 'factory.py'} resume-sweep", 600),
    ("tasksweep",         f"{VENV_PY} {SCRIPTS / 'tasksweep.py'} run", 600),
    ("reap-orphans",      f"{VENV_PY} {SCRIPTS / 'reap.py'} run", 600),
    # Close the loop on the findings board. Dogfood/QA write findings into task_board; before this nothing
    # ever read them back out — 27 open, ZERO triaged after being raised, avg age 35 days, 3 of them
    # CRITICAL. This escalates an overdue critical/high to the CEO (cooldown-guarded, once per item) and
    # feeds the founder digest. It NEVER auto-closes: a finding closes only on evidence the bug no longer
    # reproduces, because closing on a heuristic would manufacture the false-green the board exists to catch.
    # NOT "findings-sweep" — that name is taken by findings.py's own governed sweep (owner re-routing +
    # evidence-gated resolution). This is the CEO-facing triage view over the board mirror: rank by
    # severity, escalate anything overdue, feed the digest. It never auto-closes; closing is findings.py's
    # job and requires a passing verification record.
    ("findings-triage",   f"{VENV_PY} {SCRIPTS / 'findings_sweep.py'} sweep", 21600),
    # F12: kill hung/orphaned `claude` agent calls (a dead parent orphans its claude child, which then holds
    # subscription capacity forever → new calls throttle+hang). Tight cadence; jobd also reaps every tick, this
    # is the always-on backstop for when jobd itself is down.
    ("claude-reap",       f"{VENV_PY} {SCRIPTS / 'clauded.py'} reap", 120),
    # Standing acceptance/dogfood pass (DAILY): a rotating demanding-user persona DRIVES the live
    # console through the qa explorer on the real journeys and FILES findings (blockers alert at once).
    # `cron` detaches the real run so JOB_TIMEOUT can't guillotine a long browser pass.
    ("acceptance-dogfood", f"{VENV_PY} {SCRIPTS / 'dogfood.py'} cron", 86400),
    # CEO chief-of-staff brief (REBUILD-PLAN B1): DAILY per-tenant briefing composed from each company's
    # real state, delivered into their console notifications — the "your executive team briefs you" moment.
    # Frequent bounded batches, each tenant idempotently receives at most one brief per UTC day. A former
    # once-daily 200-tenant model loop timed out and retried the same prefix forever.
    ("chiefofstaff-daily", f"{VENV_PY} {SCRIPTS / 'chiefofstaff.py'} push-daily", 300),
    # CEO requirements keeper: DAILY re-refine of the standing requirements from the vision + live system
    # state, so the spec (goals, quality bar, the UP-FRONT prerequisites) keeps self-sharpening without the
    # CEO ever restating it. One model call; fail-soft (a bad refine never wipes a prior good spec).
    ("vision-refine",     f"{VENV_PY} {SCRIPTS / 'visionkeeper.py'} refine meta", 86400),
    # Proactive briefing: PUSH each tenant's actionable items (blockers, decisions, AI questions) to their
    # phone/feed so the CEO is briefed BEFORE they wonder — deduped so a standing item reminds, never spams.
    ("proactive-comms",   f"{VENV_PY} {SCRIPTS / 'proactivecomms.py'} sweep-all", 300),
    # Durable retry for external notification transports. This is a real process, not a daemon thread that
    # disappears when a short-lived producer exits; accepted/unavailable/failed stays observable in SQL.
    ("notification-delivery", f"{VENV_PY} {SCRIPTS / 'notifications.py'} retry", 60),
    # Owned monitoring alerts must not stop at "an agent was assigned". If a critical/high agent_alert ages
    # past its SLA, page upward and audit the breach; this is the management loop for the alert owner.
    ("alerts-sla",        f"{VENV_PY} {SCRIPTS / 'alerts.py'} sweep", 300),
    # Handoff accountability: if agent A asks agent B for work and B never acts, route the dropped ball into
    # the owned alert fabric and page a compact summary. This keeps inter-agent coordination from becoming
    # a write-only conversation log.
    ("accountability-sweep", f"{VENV_PY} {SCRIPTS / 'accountability.py'} sweep", 900),
    # Independent duty manager: state changes wake cases immediately, while this cadence reviews silence,
    # overdue internal questions, dropped handoffs, stale actors, and broken scheduler mechanisms. The sweep
    # claims each durable case with SKIP LOCKED + a lease, so a cadence tick can never overlap a line-manager
    # review. A timer triggers agentic judgement; it never stops or parks healthy work.
    ("management-control", f"{VENV_PY} {SCRIPTS / 'management.py'} sweep", 60),
    # Predictive budget warning: tell tenant CEOs before projected burn hits the hard quota wall. The
    # forecast module was otherwise only useful if the CEO happened to open the screen in time.
    ("forecast-sweep",   f"{VENV_PY} {SCRIPTS / 'forecast.py'} sweep", 3600),
    # Daily encrypted disaster-recovery artifact. The snapshot implementation validates pg_dump, includes
    # product source, prunes retention, and strictly copies to AOSNAP_OFFSITE_DIR when configured.
    ("encrypted-snapshot", f"{VENV_PY} {ROOT / 'platform' / 'snapshot.py'} export", 86400),
]

# These are useful deliberate verification jobs, but they launch real browser/model work and therefore must
# never become live merely because scheduler bootstrap ran on a fresh machine.
DEFAULT_DISABLED_SCHEDULES = {"acceptance-dogfood"}

# Historical default names that should not keep running beside their replacement. Delete only when the
# stored command still points at the same script, so an operator-created job with the old name is preserved.
RETIRED_DEFAULT_SCHEDULES = [
    ("budget-forecast-sweep", "forecast.py"),
    ("devserve-keepalive", "devserve.py"),
]


def _conn():
    return connection()


def _ensure():
    with _conn() as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS schedules (
            name TEXT PRIMARY KEY,
            command TEXT NOT NULL,
            interval_s INTEGER NOT NULL,
            enabled BOOLEAN NOT NULL DEFAULT true,
            last_run TIMESTAMPTZ,
            next_run TIMESTAMPTZ NOT NULL DEFAULT now())""")
        cur.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS failure_count INTEGER NOT NULL DEFAULT 0")
        cur.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS last_error TEXT")
        cur.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS manual_only BOOLEAN NOT NULL DEFAULT false")
        cur.execute("""CREATE TABLE IF NOT EXISTS scheduler_runs (
            id BIGSERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            decision TEXT NOT NULL,
            rc INT,
            duration_ms INT NOT NULL DEFAULT 0,
            detail TEXT,
            at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        cur.execute("ALTER TABLE scheduler_runs ADD COLUMN IF NOT EXISTS occurrence_id TEXT")
        cur.execute("CREATE INDEX IF NOT EXISTS scheduler_runs_name_at_idx ON scheduler_runs (name, at DESC)")
        cur.execute("""CREATE UNIQUE INDEX IF NOT EXISTS scheduler_runs_occurrence_uidx
                       ON scheduler_runs(occurrence_id) WHERE occurrence_id IS NOT NULL""")
        cur.execute("""CREATE TABLE IF NOT EXISTS scheduler_claims (
            claim_token TEXT PRIMARY KEY,
            name TEXT NOT NULL REFERENCES schedules(name) ON DELETE CASCADE,
            command TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            claimed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            lease_until TIMESTAMPTZ NOT NULL,
            finished_at TIMESTAMPTZ, rc INT, detail TEXT)""")
        cur.execute("ALTER TABLE scheduler_claims ADD COLUMN IF NOT EXISTS execution_started_at TIMESTAMPTZ")
        cur.execute("ALTER TABLE scheduler_claims ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ")
        cur.execute("""CREATE UNIQUE INDEX IF NOT EXISTS scheduler_one_running_claim_idx
                       ON scheduler_claims (name) WHERE status='running'""")
        cur.execute("""CREATE INDEX IF NOT EXISTS scheduler_claims_lease_idx
                       ON scheduler_claims (lease_until) WHERE status='running'""")


def _record_run(name, decision, rc=None, duration_ms=0, detail="", *, occurrence_id=None):
    with _conn() as c, c.cursor() as cur:
        cur.execute("""INSERT INTO scheduler_runs
                         (name, decision, rc, duration_ms, detail, occurrence_id)
                       VALUES (%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (occurrence_id) WHERE occurrence_id IS NOT NULL DO NOTHING""",
                    (name, decision, rc, int(duration_ms or 0), (detail or "")[:500], occurrence_id))


def _retry_delay_s(failure_count, interval_s, base_s=RETRY_BASE_S):
    """Bounded exponential retry: retry recovery jobs promptly, never faster than base or later than cadence."""
    failures = max(1, int(failure_count or 1))
    interval = max(1, int(interval_s or 1))
    return min(interval, int(base_s) * (2 ** min(failures - 1, 6)))


def _record_schedule_outcome_cur(cur, name, decision, detail=""):
    """Apply one occurrence outcome using the caller's transaction."""
    if decision == "executed":
        cur.execute("UPDATE schedules SET failure_count=0,last_error=NULL WHERE name=%s", (name,))
        if cur.rowcount != 1:
            raise RuntimeError(f"schedule {name!r} vanished during terminalization")
        return
    cur.execute("SELECT failure_count,interval_s FROM schedules WHERE name=%s FOR UPDATE", (name,))
    row = cur.fetchone()
    if not row:
        raise RuntimeError(f"schedule {name!r} vanished during terminalization")
    failures = int(row[0] or 0) + 1
    delay = _retry_delay_s(failures, row[1])
    cur.execute("""UPDATE schedules
                      SET failure_count=%s,last_error=%s,
                          next_run=now()+(%s || ' seconds')::interval
                    WHERE name=%s""", (failures, (detail or "")[:500], str(delay), name))
    if cur.rowcount != 1:
        raise RuntimeError(f"schedule {name!r} changed during terminalization")


def _record_schedule_outcome(name, decision, detail=""):
    """Reset failure state on success; otherwise shorten next_run to a bounded retry with backoff."""
    with _conn() as c, c.cursor() as cur:
        _record_schedule_outcome_cur(cur, name, decision, detail)


def _record_degraded_schedule_cur(cur, name, detail):
    """Expose terminal uncertainty without pulling the next occurrence forward.

    The cadence was already advanced at claim time. Retrying early solely because
    the terminal ack was uncertain would repeat child side effects under a fresh
    token, defeating occurrence exactness.
    """
    cur.execute("""UPDATE schedules
                      SET failure_count=failure_count+1,last_error=%s
                    WHERE name=%s""", ((detail or "")[:500], name))
    if cur.rowcount != 1:
        raise RuntimeError(f"schedule {name!r} vanished during degraded terminalization")


def _mark_execution_started(claim_token):
    """Durably cross the no-return boundary before any child side effect."""
    with _conn() as c, c.cursor() as cur:
        cur.execute("""UPDATE scheduler_claims
                          SET execution_started_at=COALESCE(execution_started_at,now()),
                              heartbeat_at=now(),
                              lease_until=now()+(%s*interval '1 second')
                        WHERE claim_token=%s AND status='running'""",
                    (CLAIM_LEASE_S, claim_token))
        if cur.rowcount != 1:
            raise RuntimeError(f"scheduler occurrence {claim_token} is not owned")


def _renew_claim(claim_token):
    with _conn() as c, c.cursor() as cur:
        cur.execute("""UPDATE scheduler_claims
                          SET heartbeat_at=now(),lease_until=now()+(%s*interval '1 second')
                        WHERE claim_token=%s AND status='running'
                          AND execution_started_at IS NOT NULL""",
                    (CLAIM_LEASE_S, claim_token))
        if cur.rowcount != 1:
            raise RuntimeError(f"scheduler occurrence {claim_token} lost its lease")


def _start_claim_heartbeat(claim_token):
    """Renew long executions; return (stop, lost, thread)."""
    stop, lost = threading.Event(), threading.Event()
    interval = max(1.0, min(30.0, CLAIM_LEASE_S / 3.0))

    def beat():
        while not stop.wait(interval):
            try:
                _renew_claim(claim_token)
            except Exception:
                lost.set()
                return

    thread = threading.Thread(target=beat, name=f"scheduler-heartbeat:{claim_token[:16]}", daemon=True)
    thread.start()
    return stop, lost, thread


def _execute_due_job(name, command, claim_token=None):
    """Execute one already-claimed schedule. Pure process work; DB/audit bookkeeping stays in the caller."""
    decision, rc, detail = "executed", None, ""
    started = time.monotonic()
    argv = _safe_argv(command)
    if argv is None:
        decision = "rejected"
        detail = "command failed scheduler allowlist validation"
    else:
        stop = lost = thread = None
        try:
            if claim_token:
                _mark_execution_started(claim_token)
                stop, lost, thread = _start_claim_heartbeat(claim_token)
            child_env = {**os.environ,
                         "AOS_SCHEDULER_CLAIM_TOKEN": str(claim_token or ""),
                         "AOS_SCHEDULER_OCCURRENCE_ID": str(claim_token or ""),
                         "AOS_SCHEDULER_NAME": str(name)}
            res = _run_bounded(argv, _job_timeout(name, argv), env=child_env)
            rc = res.returncode
            detail = ((res.stderr or res.stdout or b"")[:500]).decode("utf-8", errors="replace")
            if rc != 0:
                decision = "nonzero"
        except subprocess.TimeoutExpired as e:
            decision = "timeout"
            detail = str(e)[:500]
        except Exception as e:
            decision = "error"
            detail = str(e)[:500]
        finally:
            if stop is not None:
                stop.set()
            if thread is not None:
                thread.join(timeout=1)
            if lost is not None and lost.is_set():
                detail = (f"claim heartbeat lost; {detail}" if detail else "claim heartbeat lost")[:500]
    return {"name": name, "decision": decision, "rc": rc, "detail": detail,
            "claim_token": claim_token,
            "duration_ms": int((time.monotonic() - started) * 1000)}


def _claim_batch(rows, max_parallel=MAX_PARALLEL, *, now=None):
    """Pure priority/cap policy with a bounded-starvation override.

    Recovery work wins while schedules are near their promised cadence.  Once any
    job is more than ``FAIRNESS_MAX_LAG_S`` overdue, oldest-due wins until the
    backlog clears.  The old fixed priority sort could permanently starve every
    non-recovery duty because the short-cadence recovery arrival rate exceeded the
    two-slot tick capacity.

    Production rows are ``(name, command, next_run, interval_s)``.  Two-column
    rows remain supported for pure callers/tests and retain priority ordering.
    """
    now = now or datetime.now(timezone.utc)

    def key(indexed):
        index, row = indexed
        name = row[0]
        next_run = row[2] if len(row) > 2 else None
        if next_run is not None:
            if next_run.tzinfo is None:
                next_run = next_run.replace(tzinfo=timezone.utc)
            overdue_s = max(0.0, (now - next_run).total_seconds())
            if overdue_s >= FAIRNESS_MAX_LAG_S:
                return (0, next_run, index)
        # Stable recovery preference for work that has not breached its cadence.
        return (1, _RECOVERY_PRIORITY.get(name, 100), name, index)

    ordered = [row for _index, row in sorted(enumerate(rows), key=key)]
    return ordered[:max(0, int(max_parallel))]


def _reconcile_stale_claims_cur(cur):
    """Converge a bounded stale page without replaying a started occurrence.

    An unstarted claim is safe to expire and make due immediately. Once
    ``execution_started_at`` is durable, however, the child may have committed
    an irreversible side effect. Losing its terminal ack degrades that occurrence;
    it never licenses replay of the same occurrence.
    """
    cur.execute("""SELECT claim_token,name,execution_started_at
                     FROM scheduler_claims
                    WHERE status='running' AND lease_until<=now()
                    ORDER BY lease_until,claim_token
                    LIMIT %s FOR UPDATE SKIP LOCKED""", (STALE_CLAIM_PAGE,))
    stale = cur.fetchall()
    for token, name, execution_started_at in stale:
        if execution_started_at is None:
            decision, claim_status = "expired", "expired"
            detail = "execution owner vanished before child start; occurrence safely recoverable"
        else:
            decision, claim_status = "degraded", "error"
            detail = ("terminal persistence was not proved after child start; "
                      "occurrence fenced from replay")
        cur.execute("""UPDATE scheduler_claims
                          SET status=%s,finished_at=now(),detail=%s
                        WHERE claim_token=%s AND status='running'""",
                    (claim_status, detail, token))
        if cur.rowcount != 1:
            raise RuntimeError(f"stale scheduler claim {token} lost its token fence")
        if execution_started_at is None:
            cur.execute("""UPDATE schedules
                              SET failure_count=failure_count+1,last_error=%s,
                                  next_run=LEAST(next_run,now())
                            WHERE name=%s""", (detail, name))
            if cur.rowcount != 1:
                raise RuntimeError(f"schedule {name!r} vanished during stale recovery")
        else:
            _record_degraded_schedule_cur(cur, name, detail)
        cur.execute("""INSERT INTO scheduler_runs
                         (name,decision,rc,duration_ms,detail,occurrence_id)
                       VALUES (%s,%s,NULL,0,%s,%s)
                       ON CONFLICT (occurrence_id) WHERE occurrence_id IS NOT NULL DO NOTHING""",
                    (name, decision, detail, token))
    return stale


def _claim_due(only_names=None):
    """Recover expired executions, then durably lease due rows in one short transaction."""
    only_names = list(only_names or [])
    with _conn() as c, c.cursor() as cur:
        _reconcile_stale_claims_cur(cur)
        if only_names:
            cur.execute("""SELECT name,command,next_run,interval_s FROM schedules
                           WHERE enabled AND next_run<=now() AND name=ANY(%s)
                           ORDER BY next_run FOR UPDATE SKIP LOCKED""", (only_names,))
        else:
            cur.execute("""SELECT name,command,next_run,interval_s FROM schedules
                           WHERE enabled AND NOT manual_only AND next_run<=now()
                           ORDER BY next_run FOR UPDATE SKIP LOCKED""")
        due_rows = cur.fetchall()
        claimed = []
        # Never lease work that is merely waiting in an in-process executor queue: its lease could expire
        # before it starts and a second ticker would legitimately recover it, creating a duplicate execution.
        for name, command, _next_run, _interval_s in _claim_batch(due_rows):
            token = f"sc-{uuid.uuid4().hex}"
            cur.execute("""INSERT INTO scheduler_claims
                (claim_token,name,command,lease_until,heartbeat_at)
                VALUES (%s,%s,%s,now()+(%s*interval '1 second'),now())
                ON CONFLICT (name) WHERE status='running' DO NOTHING RETURNING claim_token""",
                (token, name, command, CLAIM_LEASE_S))
            if not cur.fetchone():
                continue
            cur.execute("""UPDATE schedules SET last_run=now(),
                           next_run=now()+(interval_s*interval '1 second') WHERE name=%s""", (name,))
            claimed.append((name, command, token))
        return claimed


def _finish_claim(outcome):
    token = outcome.get("claim_token")
    if not token:
        return
    with _conn() as c, c.cursor() as cur:
        cur.execute("""UPDATE scheduler_claims SET status=%s,finished_at=now(),rc=%s,detail=%s
                       WHERE claim_token=%s AND status='running'""",
                    (outcome["decision"], outcome.get("rc"),
                     (outcome.get("detail") or "")[:500], token))
        if cur.rowcount != 1:
            raise RuntimeError(f"scheduler occurrence {token} is not running or no longer owned")


def _claim_terminal_status(decision):
    return decision if decision in {"executed", "nonzero", "timeout", "error", "rejected", "expired"} \
        else "error"


def _persist_terminal_once(outcome):
    """Commit claim terminality, cadence outcome, and telemetry atomically."""
    token = outcome.get("claim_token")
    if not token:
        raise RuntimeError("scheduler outcome has no occurrence token")
    name, decision = outcome["name"], outcome["decision"]
    rc = outcome.get("rc")
    detail = (outcome.get("detail") or "")[:500]
    duration_ms = int(outcome.get("duration_ms") or 0)
    claim_status = _claim_terminal_status(decision)
    with _conn() as c, c.cursor() as cur:
        cur.execute("""UPDATE scheduler_claims
                          SET status=%s,finished_at=now(),rc=%s,detail=%s,heartbeat_at=now()
                        WHERE claim_token=%s AND status='running'""",
                    (claim_status, rc, detail, token))
        if cur.rowcount != 1:
            raise RuntimeError(f"scheduler occurrence {token} failed its terminal token fence")
        _record_schedule_outcome_cur(cur, name, decision, detail)
        cur.execute("""INSERT INTO scheduler_runs
                         (name,decision,rc,duration_ms,detail,occurrence_id)
                       VALUES (%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (occurrence_id) WHERE occurrence_id IS NOT NULL DO NOTHING""",
                    (name, decision, rc, duration_ms, detail, token))
        if cur.rowcount != 1:
            raise RuntimeError(f"scheduler occurrence {token} telemetry already exists unexpectedly")


def _terminal_persistence_state(outcome):
    """Prove an ambiguous commit from occurrence-keyed durable state."""
    token = outcome.get("claim_token")
    if not token:
        return "unproved"
    expected = _claim_terminal_status(outcome.get("decision"))
    with _conn() as c, c.cursor() as cur:
        cur.execute("""SELECT c.status,r.decision
                         FROM scheduler_claims c
                         JOIN scheduler_runs r ON r.occurrence_id=c.claim_token
                        WHERE c.claim_token=%s""", (token,))
        row = cur.fetchone()
    if row == (expected, outcome.get("decision")):
        return "persisted"
    if row == ("error", "degraded"):
        return "degraded"
    return "unproved"


def _persist_degraded_once(outcome, reason):
    """Durably fence an unprovable post-start occurrence from side-effect replay."""
    token, name = outcome.get("claim_token"), outcome["name"]
    if not token:
        raise RuntimeError("scheduler degraded outcome has no occurrence token")
    detail = f"DEGRADED terminal persistence unproved: {reason}"[:500]
    with _conn() as c, c.cursor() as cur:
        cur.execute("""UPDATE scheduler_claims
                          SET status='error',finished_at=now(),rc=%s,detail=%s,heartbeat_at=now()
                        WHERE claim_token=%s AND status='running'""",
                    (outcome.get("rc"), detail, token))
        if cur.rowcount != 1:
            raise RuntimeError(f"scheduler occurrence {token} failed its degraded token fence")
        _record_degraded_schedule_cur(cur, name, detail)
        cur.execute("""INSERT INTO scheduler_runs
                         (name,decision,rc,duration_ms,detail,occurrence_id)
                       VALUES (%s,'degraded',%s,%s,%s,%s)
                       ON CONFLICT (occurrence_id) WHERE occurrence_id IS NOT NULL DO NOTHING""",
                    (name, outcome.get("rc"), int(outcome.get("duration_ms") or 0), detail, token))
        if cur.rowcount != 1:
            raise RuntimeError(f"scheduler occurrence {token} degraded telemetry conflicts")


def _persist_terminal_outcome(outcome, attempts=TERMINAL_PERSIST_ATTEMPTS):
    """Boundedly retry and prove terminal convergence; never invokes the child."""
    last_error = "unknown terminal persistence failure"
    tries = max(1, min(5, int(attempts)))
    for attempt in range(tries):
        try:
            _persist_terminal_once(outcome)
            return "persisted"
        except Exception as exc:
            last_error = str(exc)[:300]
            try:
                state = _terminal_persistence_state(outcome)
                if state != "unproved":
                    return state
            except Exception as proof_exc:
                last_error = f"{last_error}; proof failed: {str(proof_exc)[:180]}"
            if attempt + 1 < tries:
                time.sleep(TERMINAL_PERSIST_BACKOFF_S * (attempt + 1))
    # A separate minimal transaction makes the operational state honest when the
    # full desired terminal outcome cannot be established. It is also token-fenced
    # and occurrence-keyed, so retry ambiguity cannot duplicate telemetry.
    for attempt in range(tries):
        try:
            _persist_degraded_once(outcome, last_error)
            return "degraded"
        except Exception as exc:
            last_error = str(exc)[:300]
            try:
                state = _terminal_persistence_state(outcome)
                if state != "unproved":
                    return state
            except Exception:
                pass
            if attempt + 1 < tries:
                time.sleep(TERMINAL_PERSIST_BACKOFF_S * (attempt + 1))
    outcome["persistence_error"] = last_error
    return "unproved"


def _finalize_outcome(outcome):
    """Durably terminalize one occurrence as soon as its child exits.

    Do not wait for sibling futures.  Otherwise a fast job remains recorded as
    ``running`` behind a slow 120-second sibling; if the scheduler process dies in
    that window, lease recovery legitimately repeats side effects that already
    happened.
    """
    name, decision, rc = outcome["name"], outcome["decision"], outcome.get("rc")
    detail, duration_ms = outcome.get("detail") or "", outcome.get("duration_ms") or 0
    persistence = _persist_terminal_outcome(outcome)
    try:
        audit.append(actor="scheduler", action="RunJob", resource=name,
                     decision=decision if persistence == "persisted" else "degraded",
                     payload={"rc": rc, "duration_ms": duration_ms,
                              "occurrence_id": outcome.get("claim_token"),
                              "persistence": persistence,
                              "persistence_error": outcome.get("persistence_error")})
    except Exception:
        # The occurrence transaction is the source of truth. Audit transport must
        # not turn a committed terminal occurrence into an executor failure.
        pass
    return decision == "executed" and persistence == "persisted"


def register(name, command, interval_s, *, manual_only=False):
    # first run is ONE INTERVAL out, not immediately — so registering e.g. a weekly eval doesn't fire
    # a fleet of builds the instant it's set up.
    _ensure()
    with _conn() as c, c.cursor() as cur:
        cur.execute("""INSERT INTO schedules (name, command, interval_s, manual_only, next_run)
                       VALUES (%s,%s,%s,%s, now() + (%s||' seconds')::interval)
                       ON CONFLICT (name) DO UPDATE SET command=EXCLUDED.command,
                         interval_s=EXCLUDED.interval_s,manual_only=EXCLUDED.manual_only""",
                    (name, command, interval_s, bool(manual_only), interval_s))


def deregister(name):
    """#12: drop a schedule so it stops firing (e.g. when its custom agent is deleted). Returns True
    if a row was removed. Idempotent — removing a non-existent schedule is a no-op."""
    _ensure()
    with _conn() as c, c.cursor() as cur:
        cur.execute("DELETE FROM schedules WHERE name=%s", (name,))
        removed = cur.rowcount > 0
    audit.append(actor="scheduler", action="Deregister", resource=name,
                 decision="removed" if removed else "absent", payload={})
    return removed


# aliases so callers probing for any reasonable de-register verb find one (customagents.delete does)
unregister = deregister
remove = deregister


def set_enabled(name, enabled):
    """#12: pause/resume a schedule WITHOUT deleting it (e.g. when a custom agent is toggled off).
    A disabled schedule is skipped by tick() but keeps its row + next_run. Returns True if updated."""
    _ensure()
    with _conn() as c, c.cursor() as cur:
        cur.execute("UPDATE schedules SET enabled=%s WHERE name=%s", (bool(enabled), name))
        updated = cur.rowcount > 0
    audit.append(actor="scheduler", action="SetEnabled", resource=name,
                 decision="enabled" if enabled else "disabled",
                 payload={"found": updated})
    return updated


def bootstrap():
    """Idempotently register the default recovery/sweep schedules (#8, #14). Uses ON CONFLICT DO
    NOTHING so it never clobbers an operator's customised command/interval and never resets a
    running job's next_run. Returns the number of schedules newly created."""
    _ensure()
    created = 0
    retired = 0
    with _conn() as c, c.cursor() as cur:
        for name, command, interval_s in DEFAULT_SCHEDULES:
            cur.execute("""INSERT INTO schedules (name, command, interval_s, enabled, next_run)
                           VALUES (%s,%s,%s,%s, now() + (%s||' seconds')::interval)
                           ON CONFLICT (name) DO NOTHING""",
                        (name, command, interval_s, name not in DEFAULT_DISABLED_SCHEDULES, interval_s))
            created += cur.rowcount
        # Safe migration of the historical default only. Preserve operator-custom commands/cadences, but do
        # not leave an installed default on the known starvation-prone once-daily 200-model-call behavior.
        cos_command = f"{VENV_PY} {SCRIPTS / 'chiefofstaff.py'} push-daily"
        cur.execute("""UPDATE schedules SET interval_s=300,next_run=LEAST(next_run,now()+interval '5 minutes')
                       WHERE name='chiefofstaff-daily' AND command=%s AND interval_s=86400""",
                    (cos_command,))
        for name, script_name in RETIRED_DEFAULT_SCHEDULES:
            cur.execute("DELETE FROM schedules WHERE name=%s AND command LIKE %s", (name, f"%/{script_name} %"))
            retired += cur.rowcount
    if created:
        audit.append(actor="scheduler", action="Bootstrap", resource="defaults",
                     decision="registered", payload={"created": created})
    if retired:
        audit.append(actor="scheduler", action="Bootstrap", resource="retired-defaults",
                     decision="removed", payload={"removed": retired})
    return created


def _safe_argv(command):
    """Parse a stored command into an argv list and validate it — we run jobs WITHOUT a shell, so we
    must (a) tokenise ourselves and (b) refuse anything that isn't a legitimate agent-os job.

    FAIL-CLOSED: returns None for anything we can't prove is safe (unparseable, empty, or an
    executable that is neither the venv python interpreter nor a file living under the agent-os root).
    A None here means tick() will NOT execute the command — a corrupted/injected schedules row can no
    longer get a shell. We still advance next_run for it so one bad row can't wedge the loop."""
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    if not argv:
        return None
    prog = argv[0]
    candidate = Path(os.path.abspath(prog if os.path.isabs(prog) else str(ROOT / prog)))
    # Interpreter trust is path-based, never basename-based. A hostile executable
    # called `python` in PATH or `/tmp/python3` is not equivalent to the repository
    # venv/current process interpreter merely because its filename looks familiar.
    trusted_interpreters = {
        Path(os.path.abspath(VENV_PY)),
        Path(os.path.abspath(sys.executable)),
    }
    if candidate in trusted_interpreters:
        return argv
    if candidate.name == "python" or candidate.name == "python3" or candidate.name.startswith("python3."):
        return None
    # otherwise the program must resolve to a file inside the agent-os tree
    try:
        candidate.resolve().relative_to(ROOT.resolve())
        return argv if candidate.is_file() else None
    except (OSError, ValueError):
        return None


def tick(only_names=None):
    """Run all due jobs; return how many actually executed.

    #15: each job is isolated. A job that raises, times out, or fails validation must NOT abort the
    tick or starve the other due jobs, and must NOT pin next_run (a 'poison' job that never advances
    would block forever). So every job is wrapped, TimeoutExpired is caught, and next_run is advanced
    for EVERY due job no matter the outcome. We also self-heal the default schedules first so a fresh
    install starts recovering immediately even though ticker.sh only ever calls `tick`."""
    bootstrap()
    ran = 0
    # CLAIM phase — a SHORT transaction: grab all due jobs (FOR UPDATE SKIP LOCKED so concurrent tickers
    # don't double-claim) and advance next_run+last_run immediately, then commit. We do NOT run any job
    # inside this transaction: running subprocesses inside the open txn pinned it idle-in-transaction AND
    # held the row locks for the whole tick (a real hang risk per hang-resilience). Advancing next_run at
    # claim time also keeps the 'slow/poison job never pins the loop' guarantee.
    due = _claim_due(only_names)
    # Recovery and delivery control-plane work always enters the bounded executor first. A slow housekeeping
    # job may use the other slot, but cannot sit ahead of controller continuation merely because it was older.
    due.sort(key=lambda row: (_RECOVERY_PRIORITY.get(row[0], 100), row[0]))
    # RUN phase — no open transaction or row locks. Execute a small bounded number concurrently so a poison
    # 120-second job cannot delay controller recovery, SLA, and communication jobs queued behind it by N×120s.
    # The subprocess cap remains the primary resource guard; this merely isolates independent schedules.
    if due:
        with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(due)),
                                thread_name_prefix="aos-scheduler") as pool:
            futures = {pool.submit(_execute_due_job, name, command, token): (name, token)
                       for name, command, token in due}
            for future in as_completed(futures):
                name, token = futures[future]
                try:
                    outcome = future.result()
                except Exception as e:                       # defensive: helper already catches job failures
                    outcome = {"name": name, "decision": "error", "rc": None,
                               "detail": str(e)[:500], "duration_ms": 0,
                               "claim_token": token}
                if _finalize_outcome(outcome):
                    ran += 1
    return ran


def _selftest():
    names = ["selftest-job", "selftest-good", "selftest-poison", "selftest-denied", "selftest-dis",
             "selftest-expired",
             "budget-forecast-sweep"]
    marker = Path("/tmp/scheduler-selftest-marker")
    marker.unlink(missing_ok=True)
    global JOB_TIMEOUT
    saved_timeout = JOB_TIMEOUT
    try:
        assert "acceptance-dogfood" in DEFAULT_DISABLED_SCHEDULES
        _ensure()
        with _conn() as c, c.cursor() as cur:
            cur.execute("DELETE FROM schedules WHERE name = ANY(%s)", (names,))
            cur.execute("DELETE FROM scheduler_runs WHERE name = ANY(%s)", (names,))
        # ---- (1) basic: a due job runs once, advances next_run, is not re-run immediately ----
        register("selftest-job", "true" and f"{VENV_PY} -c pass", interval_s=3600, manual_only=True)
        with _conn() as c, c.cursor() as cur:   # force due (register defers first run)
            cur.execute("UPDATE schedules SET next_run=now() WHERE name='selftest-job'")
        n1 = tick(["selftest-job"])                          # runs only this proof's due job
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT last_run IS NOT NULL, next_run > now() FROM schedules WHERE name='selftest-job'")
            ran_recorded, scheduled_future = cur.fetchone()
            cur.execute("SELECT decision, rc IS NOT NULL, duration_ms >= 0 FROM scheduler_runs WHERE name='selftest-job' ORDER BY id DESC LIMIT 1")
            run_decision, run_rc_recorded, run_duration_recorded = cur.fetchone()
        basic_ok = n1 >= 1 and ran_recorded and scheduled_future
        telemetry_ok = run_decision == "executed" and run_rc_recorded and run_duration_recorded

        # ---- restart recovery: a claim whose owner vanished is expired and the occurrence runs again ----
        register("selftest-expired", f"{VENV_PY} -c pass", interval_s=3600, manual_only=True)
        with _conn() as c, c.cursor() as cur:
            cur.execute("""INSERT INTO scheduler_claims
                (claim_token,name,command,lease_until) VALUES
                ('selftest-expired-old','selftest-expired',%s,now()-interval '1 second')""",
                (f"{VENV_PY} -c pass",))
        tick(["selftest-expired"])
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT status FROM scheduler_claims WHERE claim_token='selftest-expired-old'")
            old_expired = cur.fetchone()[0] == "expired"
            cur.execute("""SELECT status FROM scheduler_claims WHERE name='selftest-expired'
                           ORDER BY claimed_at DESC,claim_token DESC LIMIT 1""")
            recovered_claim = cur.fetchone()[0] == "executed"
        claim_recovery_ok = old_expired and recovered_claim

        # ---- (15) isolation: a poison (timeout) job next to a good one — good still runs, BOTH advance ----
        JOB_TIMEOUT = 1
        register("selftest-good", f"{VENV_PY} -c pass", interval_s=3600, manual_only=True)
        register("selftest-poison", f"{VENV_PY} -c \"import time; time.sleep(30)\"", interval_s=3600,
                 manual_only=True)
        # ---- (15) fail-closed: a non-agent-os command is REJECTED, never executed ----
        register("selftest-denied", f"/usr/bin/touch {marker}", interval_s=3600, manual_only=True)
        with _conn() as c, c.cursor() as cur:
            cur.execute("UPDATE schedules SET next_run=now() WHERE name IN "
                        "('selftest-good','selftest-poison','selftest-denied')")
        tick(["selftest-good", "selftest-poison", "selftest-denied"])  # isolated from real due jobs
        # Claims are intentionally capped to jobs that can start immediately. Drain the third due row on
        # the next tick rather than leasing it while it waits behind the two-worker executor.
        tick(["selftest-good", "selftest-poison", "selftest-denied"])
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT name, next_run > now() FROM schedules WHERE name IN "
                        "('selftest-good','selftest-poison','selftest-denied')")
            adv = dict(cur.fetchall())
            cur.execute("""SELECT name, decision FROM scheduler_runs
                           WHERE name IN ('selftest-good','selftest-poison','selftest-denied')
                           ORDER BY id""")
            run_decisions = dict(cur.fetchall())
        isolation_ok = adv.get("selftest-good") and adv.get("selftest-poison") and adv.get("selftest-denied")
        denied_ok = not marker.exists()                      # rejected command never touched the FS
        telemetry_ok = telemetry_ok and run_decisions.get("selftest-good") == "executed" \
            and run_decisions.get("selftest-poison") == "timeout" \
            and run_decisions.get("selftest-denied") == "rejected"
        JOB_TIMEOUT = saved_timeout

        # ---- (12) set_enabled disables (kept, not deleted); deregister removes ----
        register("selftest-dis", f"{VENV_PY} -c pass", interval_s=3600, manual_only=True)
        set_enabled("selftest-dis", False)
        with _conn() as c, c.cursor() as cur:
            cur.execute("UPDATE schedules SET next_run=now() WHERE name='selftest-dis'")
        tick(["selftest-dis"])
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT enabled, last_run IS NULL FROM schedules WHERE name='selftest-dis'")
            still_disabled, never_ran = cur.fetchone()
        disable_ok = (not still_disabled) and never_ran      # disabled row kept AND skipped by tick

        removed = deregister("selftest-dis")
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT count(*) FROM schedules WHERE name='selftest-dis'")
            gone = cur.fetchone()[0] == 0
        deregister_ok = removed and gone

        # ---- (8/14) bootstrap registered the default recovery/sweep jobs ----
        with _conn() as c, c.cursor() as cur:
            cur.execute("""INSERT INTO schedules (name, command, interval_s, next_run)
                           VALUES ('budget-forecast-sweep', %s, 3600, now())""",
                        (f"{VENV_PY} {SCRIPTS / 'forecast.py'} sweep",))
        bootstrap()
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT count(*) FROM schedules WHERE name = ANY(%s)",
                        ([n for n, _, _ in DEFAULT_SCHEDULES],))
            defaults_present = cur.fetchone()[0] == len(DEFAULT_SCHEDULES)
            cur.execute("SELECT count(*) FROM schedules WHERE name='budget-forecast-sweep'")
            retired_removed = cur.fetchone()[0] == 0

        ok = (basic_ok and telemetry_ok and claim_recovery_ok and isolation_ok and denied_ok and disable_ok and deregister_ok
              and defaults_present and retired_removed)
        print(f"basic(run+advance)={basic_ok} isolation(poison-doesnt-block)={isolation_ok} "
              f"fail-closed(rejected-not-run)={denied_ok} set_enabled+skip={disable_ok} "
              f"deregister={deregister_ok} run-telemetry={telemetry_ok} defaults-bootstrapped={defaults_present} "
              f"expired-claim-recovered={claim_recovery_ok} retired-defaults-cleaned={retired_removed}")
        print("PASS: scheduler runs due jobs once, isolates poison jobs, fail-closed on bad "
              "commands, supports enable/deregister, bootstraps recovery jobs, cleans retired defaults, audited ✅"
              if ok else "FAIL")
    finally:
        JOB_TIMEOUT = saved_timeout
        marker.unlink(missing_ok=True)
        with _conn() as c, c.cursor() as cur:
            cur.execute("DELETE FROM schedules WHERE name = ANY(%s)", (names,))
            cur.execute("DELETE FROM scheduler_runs WHERE name = ANY(%s)", (names,))
    sys.exit(0 if ok else 1)


def _main(a):
    if a and a[0] == "register":
        register(a[1], a[3], int(a[2])); print(f"registered '{a[1]}' every {a[2]}s")
    elif a and a[0] == "bootstrap":
        print(f"bootstrapped {bootstrap()} default schedule(s)")
    elif a and a[0] == "tick":
        print(f"ran {tick()} due job(s)")
    elif a and a[0] in ("enable", "disable") and len(a) > 1:
        found = set_enabled(a[1], a[0] == "enable")
        print(f"{'enabled' if a[0] == 'enable' else 'disabled'} '{a[1]}'" if found else f"no such schedule '{a[1]}'")
    elif a and a[0] == "deregister" and len(a) > 1:
        print(f"deregistered '{a[1]}'" if deregister(a[1]) else f"no such schedule '{a[1]}'")
    elif a and a[0] == "list":
        _ensure()
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT name, interval_s, enabled, last_run, next_run FROM schedules ORDER BY name")
            for r in cur.fetchall():
                print(r)
    elif a and a[0] in ("selftest", "test"):
        _selftest()
    else:
        sys.exit("usage: scheduler.py register|bootstrap|tick|enable|disable|deregister|list|selftest ...")


if __name__ == "__main__":
    _main(sys.argv[1:])
