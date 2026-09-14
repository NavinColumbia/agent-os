#!/usr/bin/env python3
"""jobrunner.py — dispatch-and-park executor for tool-worker actors (agentic-org phase 3a).

See docs/AGENTIC-QA-ORG.md. A tool-worker must NOT run a 10-30 min browser job inside a lease-bound
decide-step (the 900s event lease would reclaim it -> a duplicate browser). Instead its step calls
`dispatch(job)`: the job runs in a BACKGROUND thread (the browser lives there), and on completion writes the
outcome straight onto the org bus — a `finding` per bug + a `done` to the worker's supervisor — and flips the
parked worker actor to `done`. Every decide-step stays short.

Crash-safety (reuses the pulse plane, not a new table): each job is a pulse (`kind=tool-job`) that beats
while alive; if the process dies mid-job its pulse goes silent, and `reconcile()` re-dispatches from the
parked actor's persisted job spec — idempotent (a job already tracked in-process is skipped). The done/finding
+ pulse + watchdog machinery is reused, never duplicated.

Isolated + unit-tested: `dispatch`/`reconcile` take the store module and the tool runner injected, so the
selftest drives the full lifecycle with fakes (no runtime, no browser, no DB-required logic).
"""
import sys
import threading
from pathlib import Path
import json
import os
import time
from contextlib import nullcontext

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

import resourcepressure
from dbpool import connection as _db_connection

_JOBS = {}                        # job_id -> {"state": running|done|failed, "thread": Thread}
_LOCK = threading.Lock()
_RUN_DEADLINES = {}                  # (tenant, run_id) -> monotonic-ish epoch deadline
_RUN_PROGRESS = {}                   # (tenant, run_id) -> latest substantive subordinate progress
def _qa_tool_capacity():
    requested = os.environ.get("AOS_QA_TOOL_CONCURRENCY",
                               os.environ.get("AOS_QA_AGENTIC_WORKER_CAP", "2"))
    try:
        import browser_gate
        browser_cap = browser_gate.GLOBAL_MAX
    except Exception:
        browser_cap = 0
    return resourcepressure.tool_worker_capacity(requested, browser_cap)


_QA_TOOL_CAP = _qa_tool_capacity()
_QA_TOOL_SEM = threading.BoundedSemaphore(_QA_TOOL_CAP)
# Repository-mutating fixers are decision-coupled work. Two fixers can otherwise edit/restart the same
# product concurrently after a checkpoint resumes multiple parked actors. Keep that path serial by default;
# dedicated isolated builders may explicitly raise it.
try:
    _DEV_FIX_CAP = max(0, int(os.environ.get("AOS_DEV_FIX_CONCURRENCY", "1")))
except ValueError:
    _DEV_FIX_CAP = 0
_DEV_FIX_SEM = threading.BoundedSemaphore(_DEV_FIX_CAP)


def _has_launch_runway(job, now=None):
    """Do not start a fresh expensive worker at the end of a bounded QA shift.

    A browser launched with only a few minutes left gets cancelled mid-work and,
    for local-storage products, loses navigation state on the next process. Leave
    the durable parked actor untouched so the next shift starts it with a full
    envelope. Existing running workers are still cooperatively checkpointed at
    the hard deadline.
    """
    if job.get("tool") not in ("qa_explore", "dev_fix", "qa_review"):
        return True
    with _LOCK:
        deadline = _RUN_DEADLINES.get((job.get("tenant"), job.get("run_id")))
    if deadline is None:
        return True
    default = "600" if job.get("tool") == "qa_explore" else "300"
    minimum = max(0.0, float(os.environ.get("AOS_TOOL_MIN_LAUNCH_RUNWAY_S", default)))
    return float(deadline) - float(time.time() if now is None else now) >= minimum


class _Admission:
    """Cancellation-aware local semaphore context; queued work must not consume the whole cleanup grace."""
    def __init__(self, sem, jid, job):
        self.sem, self.jid, self.job, self.acquired = sem, jid, job, False

    def __enter__(self):
        while not self.acquired:
            with _LOCK:
                handle = _JOBS.get(self.jid) or {}
                deadline = _RUN_DEADLINES.get((self.job.get("tenant"), self.job.get("run_id")))
            cancelled = bool((handle.get("cancel_event") and handle["cancel_event"].is_set())
                             or (deadline is not None and time.time() >= deadline))
            if cancelled:
                return False
            self.acquired = self.sem.acquire(timeout=0.2)
        return True

    def __exit__(self, *_):
        if self.acquired:
            self.sem.release()


class _Admissions:
    """Acquire several cancellation-aware capacities atomically from the caller's perspective."""
    def __init__(self, sems, jid, job):
        self.items = [_Admission(sem, jid, job) for sem in sems]
        self.entered = []

    def __enter__(self):
        for item in self.items:
            if not item.__enter__():
                self.__exit__()
                return False
            self.entered.append(item)
        return True

    def __exit__(self, *_):
        for item in reversed(self.entered):
            item.__exit__()
        self.entered = []


class _FencedCancel:
    """One cancellation surface combining controller cancellation with durable lease ownership.

    Existing QA/fixer loops already check ``Event.is_set()`` at their action/attempt boundaries.  Supplying this
    object makes those same checks fencing-aware without teaching every downstream worker about PostgreSQL.
    The ownership read uses a fresh pooled transaction, so a dead DB session cannot masquerade as ownership.
    """
    def __init__(self, base_event, store, lease, tenant):
        self.base_event, self.store, self.lease, self.tenant = base_event, store, lease, tenant
        self.lost = threading.Event()

    def set(self):
        self.base_event.set()

    def is_set(self):
        if self.base_event.is_set() or self.lost.is_set():
            return True
        try:
            valid = self.store.tool_job_lease_valid(
                self.lease["lease_key"], self.lease["owner_id"],
                self.lease["fence_token"], self.tenant)
        except Exception:
            valid = False
        if not valid:                                  # side-effect boundary is deliberately fail-closed
            self.lost.set()
        return not valid


class _LeaseHeartbeat:
    """Renew a fenced lease across connection churn and cancel the worker if its generation is superseded."""
    def __init__(self, store, lease, tenant, fenced_cancel, on_renew=None):
        self.store, self.lease, self.tenant, self.cancel = store, lease, tenant, fenced_cancel
        self.on_renew = on_renew
        self.stop = threading.Event()
        self.lease_s = max(1, int(lease.get("lease_s") or 90))
        self.local_expiry = time.monotonic() + self.lease_s
        self.thread = threading.Thread(target=self._loop, daemon=True,
                                       name=f"tool-lease-{lease.get('fence_token')}")

    def _loop(self):
        cadence = max(1.0, self.lease_s / 3.0)
        wait_s = cadence
        while not self.stop.wait(wait_s):
            try:
                res = self.store.renew_tool_job_lease(
                    self.lease["lease_key"], self.lease["owner_id"],
                    self.lease["fence_token"], self.tenant, self.lease_s)
                if res.get("valid"):
                    self.local_expiry = time.monotonic() + self.lease_s
                    if self.on_renew:
                        try:
                            self.on_renew()
                        except Exception:
                            pass
                    wait_s = cadence
                    continue
                self.cancel.lost.set()                 # another generation won, or ours expired
                return
            except Exception:
                # A single broken connection is survivable.  Keep reconnecting, but never work beyond the
                # last lease duration without a confirmed renewal.
                if time.monotonic() >= self.local_expiry:
                    self.cancel.lost.set()
                    return
                wait_s = min(1.0, max(0.2, self.local_expiry - time.monotonic()))

    def __enter__(self):
        self.thread.start()
        return self.cancel

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join(timeout=2.0)


def _pulse():
    try:
        import pulse
        return pulse
    except Exception:
        return None


def _heartbeat_actor_chain(store, job):
    """Roll a live tool lease heartbeat up through the durable reporting chain.

    A supervisor waiting on a long browser/fixer is doing healthy delegated work, but its own reducer has no
    event to process until the child completes. Leaving only the separate tool pulse fresh makes the fleet view
    call that supervisor stalled and can trigger needless recovery. Refresh the exact child and bounded ancestor
    chain from the same fenced lease renewal; this is liveness only and changes no actor state or decision.
    """
    if not hasattr(store, "heartbeat") or not hasattr(store, "actor"):
        return
    tenant = job.get("tenant")
    current = job.get("actor_id")
    seen = set()
    for _depth in range(32):
        if current is None or current in seen:
            return
        seen.add(current)
        try:
            store.heartbeat(current, tenant)
            row = store.actor(current, tenant) or {}
        except Exception:
            return
        current = row.get("supervisor_id")


def _default_run_tool():
    import tools
    return tools.run_tool


def job_id_for(job) -> str:
    """A stable id per durable attempt.

    A checkpointed actor legitimately runs the same tool again, so actor+tool alone is too coarse for result
    idempotency: the final result would collide with the already-processed cancellation result. ``attempt`` is
    incremented atomically in the actor's cancellation step; crash re-dispatches within an attempt retain it.
    """
    return f"{job.get('run_id')}:{job.get('actor_id')}:{job.get('tool')}:{int(job.get('attempt') or 0)}"


def dispatch(job: dict, store, run_tool=None, sync: bool = False) -> str:
    """Start a tool job for a parked tool-worker. Returns the job id immediately (the actor is already parked
    by its decide-step). `sync=True` runs inline (tests). Idempotent: an in-process handle for this exact
    durable attempt is never re-started. A terminal handle means its result is committed to the durable bus
    and may still be waiting for the actor reducer; the next legitimate pass has a new ``attempt`` id."""
    jid = job_id_for(job)
    if not _has_launch_runway(job):
        return ""
    cancel_event = threading.Event()
    with _LOCK:
        if jid in _JOBS:
            return jid
        _JOBS[jid] = {"state": "running", "thread": None, "job": dict(job),
                      "cancel_event": cancel_event}
    run_tool = run_tool or _default_run_tool()
    if sync:
        _run(jid, job, store, run_tool)
        return jid
    t = threading.Thread(target=_run, args=(jid, job, store, run_tool), daemon=True)
    with _LOCK:
        _JOBS[jid]["thread"] = t
    t.start()
    return jid


def _run(jid, job, store, run_tool):
    """Acquire the durable cross-process job lease before doing any expensive work."""
    p = _pulse()
    if p:
        try:
            p.beat(jid, kind="tool-job", tenant_id=job.get("tenant"),
                   label=f"{job.get('tool')} · {job.get('actor_name') or job.get('actor_id')}",
                   stage="queued" if job.get("tool") in ("qa_explore", "dev_fix") else "running",
                   progress=(f"queued for {job.get('tool')}" if job.get("tool") in ("qa_explore", "dev_fix")
                             else f"running {job.get('tool')}"), expected_cadence_s=300)
        except Exception:
            pass
    # A dev-fix serializes repository mutation here, but acquires browser capacity only when its independent
    # verification actually opens a browser. BrowserBridge's durable cross-process gate is the authority for
    # that scarce resource. Reserving a QA semaphore throughout code analysis caused a completed explorer's
    # stale local permit to strand the fixer indefinitely despite the global browser pool being empty.
    sems = ([_QA_TOOL_SEM] if job.get("tool") == "qa_explore" else
            [_DEV_FIX_SEM] if job.get("tool") == "dev_fix" else [])
    unavailable = (job.get("tool") in ("qa_explore", "dev_fix") and _QA_TOOL_CAP <= 0
                   or job.get("tool") == "dev_fix" and _DEV_FIX_CAP <= 0)
    if unavailable:
        _run_owned(jid, {**job, "_admission_error":
                   "host pressure admits zero QA browser/fixer workers"}, store, run_tool, p=p)
        return
    admission = _Admissions(sems, jid, job) if sems else nullcontext(True)
    try:
        # Local admission happens before durable lease acquisition. Thus a 12-story queue creates only the two
        # admitted lease heartbeats/browsers, rather than ten unnecessary DB contenders.
        with admission as admitted:
            if not admitted:
                # _run_owned observes the cancellation/deadline and emits the durable resumable result without
                # acquiring an advisory-lock connection or starting a browser/fixer.
                _run_owned(jid, job, store, run_tool, p=p)
                return
            if p and job.get("tool") in ("qa_explore", "dev_fix"):
                try:
                    p.beat(jid, kind="tool-job", tenant_id=job.get("tenant"),
                           label=f"{job.get('tool')} · {job.get('actor_name') or job.get('actor_id')}",
                           stage="coordination", progress="capacity acquired; claiming durable fence",
                           expected_cadence_s=90)
                except Exception:
                    pass
            job_args = job.get("args") or {}
            code_context = job_args.get("code_context") or {}
            resource_key = (job_args.get("repo") or
                            (code_context.get("repo") if isinstance(code_context, dict) else None)) \
                if job.get("tool") == "dev_fix" else None
            lock_cm = (store.tool_job_lock(job.get("run_id"), job.get("tenant"), job.get("actor_id"),
                                           job.get("tool"), resource_key=resource_key)
                       if hasattr(store, "tool_job_lock") else nullcontext(True))
            with lock_cm as lease:
                if not lease:
                    with _LOCK:
                        if jid in _JOBS:
                            _JOBS[jid]["state"] = "owned_elsewhere"
                            _JOBS[jid]["retry_after"] = time.time() + 5.0
                    return
                if isinstance(lease, dict) and hasattr(store, "renew_tool_job_lease"):
                    with _LOCK:
                        base_cancel = (_JOBS.get(jid) or {}).get("cancel_event") or threading.Event()
                    fenced = _FencedCancel(base_cancel, store, lease, job.get("tenant"))
                    def _renew_pulse():
                        _heartbeat_actor_chain(store, job)
                        if p:
                            p.beat(jid, kind="tool-job", tenant_id=job.get("tenant"),
                                   label=f"{job.get('tool')} · {job.get('actor_name') or job.get('actor_id')}",
                                   stage="running", progress=f"running {job.get('tool')}",
                                   expected_cadence_s=300)
                    with _LeaseHeartbeat(store, lease, job.get("tenant"), fenced,
                                         on_renew=_renew_pulse) as lease_cancel:
                        _run_owned(jid, job, store, run_tool, p=p, cancel_event=lease_cancel)
                else:                                  # injected legacy/fake stores used by isolated tests
                    _run_owned(jid, job, store, run_tool, p=p)
    except Exception as exc:
        # Coordination failure is not permission to launch a possibly duplicate browser/fixer. Leave the durable
        # actor parked and retry acquisition shortly; its work specification remains in Postgres.
        if p:
            try:
                p.beat(jid, kind="tool-job", tenant_id=job.get("tenant"),
                       label=f"{job.get('tool')} · {job.get('actor_name') or job.get('actor_id')}",
                       stage="coordination-retry",
                       progress=f"durable fence retry: {type(exc).__name__}: {str(exc)[:180]}",
                       expected_cadence_s=30,
                       meta={"error_type": type(exc).__name__, "retry_in_s": 5})
            except Exception:
                pass
        with _LOCK:
            if jid in _JOBS:
                _JOBS[jid]["state"] = "owned_elsewhere"
                _JOBS[jid]["retry_after"] = time.time() + 5.0
                _JOBS[jid]["last_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"


def _run_owned(jid, job, store, run_tool, p=None, cancel_event=None):
    try:
        args = dict(job.get("args") or {})
        with _LOCK:
            handle = _JOBS.get(jid) or {}
            deadline = _RUN_DEADLINES.get((job.get("tenant"), job.get("run_id")))
        if cancel_event is not None:
            args["_cancel_event"] = cancel_event          # fencing must not be shadowed by persisted/injected args
        else:
            args.setdefault("_cancel_event", handle.get("cancel_event"))
        if deadline is not None:
            args.setdefault("_deadline", deadline)
            args["_deadline_provider"] = lambda: run_deadline(
                job.get("run_id"), job.get("tenant"), deadline)
        # A process/slice restart is a new durable tool generation.  The tool can use this fact to verify
        # already-applied work before repeating side effects (especially a dev fixer interrupted during its
        # post-change browser verification).
        args.setdefault("_tool_attempt", int(job.get("attempt") or 0))
        args.setdefault("_tool_resumed", bool(job.get("resumed")))
        args.setdefault("tenant", job.get("tenant"))     # authoritative tenant on the job -> the tool rebuilds
        args.setdefault("_run_id", job.get("run_id"))    # scope bridge cancellation to this durable run
        args.setdefault("org", job.get("org"))           # factory._ctx from it (billing correct in this thread)
        if job.get("_admission_error"):
            out = {"status": "failed", "findings": [],
                   "result": {"error": str(job["_admission_error"]),
                              "stop_reason": "capacity-wait-checkpoint",
                              "checkpoint_required": True,
                              "capacity_wait": True}}
        elif ((args.get("_cancel_event") is not None and args["_cancel_event"].is_set())
                or (args.get("_deadline") is not None and time.time() >= args["_deadline"])):
            out = {"status": "failed", "findings": [],
                   "result": {"error": "tool run cancelled by QA safety deadline"}}
        elif job.get("tool") == "qa_explore":
            if p:
                p.beat(jid, kind="tool-job", tenant_id=job.get("tenant"),
                       label=f"{job.get('tool')} · {job.get('actor_name') or job.get('actor_id')}",
                       stage="running", progress=f"running {job.get('tool')}", expected_cadence_s=300)
            out = run_tool(job["tool"], args)
        elif job.get("tool") == "dev_fix":
            if p:
                p.beat(jid, kind="tool-job", tenant_id=job.get("tenant"),
                       label=f"{job.get('tool')} · {job.get('actor_name') or job.get('actor_id')}",
                       stage="running", progress="running dev_fix", expected_cadence_s=300)
            # A queued fixer may have spent the remainder of the slice waiting for the repository lock.
            # Re-check after acquisition so it never begins a mutation after the run deadline.
            if ((args.get("_cancel_event") is not None and args["_cancel_event"].is_set())
                    or (args.get("_deadline") is not None and time.time() >= args["_deadline"])):
                out = {"status": "failed", "findings": [],
                       "result": {"error": "dev fix cancelled while waiting for repository mutation slot"}}
            else:
                out = run_tool(job["tool"], args)
        else:
            out = run_tool(job["tool"], args)
    except Exception as e:                                # a tool must never take the org down
        error = f"{type(e).__name__}: {e}"
        # Admission pressure is not evidence that the assigned story failed.  BrowserBridge deliberately
        # fails closed when it cannot obtain both durable browser and weighted host leases; representing that
        # bounded wait as a terminal infra failure made the coordinator hire a fresh gap-fill worker for the
        # same story.  Under a large manifest that multiplied actors and consumed recovery attempts while the
        # machine was behaving exactly as designed.  Emit a durable checkpoint instead: the worker consumes
        # this generation once, stays parked, and reconcile resumes this SAME actor when capacity is available.
        capacity_wait = (job.get("tool") in ("qa_explore", "dev_fix") and
                         any(marker in error.lower() for marker in (
                             "browser capacity exhausted", "no qa browser slot",
                             "capacity unavailable", "capacity-unavailable",
                             "host pressure admits zero")))
        out = {"status": "failed", "findings": [], "result": {"error": error}}
        if capacity_wait:
            out["result"].update({"stop_reason": "capacity-wait-checkpoint",
                                  "checkpoint_required": True,
                                  "capacity_wait": True})
    with _LOCK:
        terminal_cancel = (_JOBS.get(jid) or {}).get("terminal_cancel")
    if terminal_cancel:
        story = ((job.get("args") or {}).get("story") or {})
        out = {"status": "done", "findings": [], "result": {
            "story": story.get("id") or story.get("title"),
            "title": story.get("title") or story.get("id"),
            "stop_reason": "superseded-terminal-decision", "superseded": True,
            "reason": terminal_cancel}}
    if job.get("tool") == "qa_explore" and out.get("status") != "done":
        # Preserve story identity on infrastructure failures. Without it the coordinator omitted the failed story
        # from story_status and could produce an ALL CLEAR verdict from fewer stories than were assigned.
        out = dict(out)
        result = dict(out.get("result") or {})
        story = ((job.get("args") or {}).get("story") or {})
        result.setdefault("story", story.get("id") or story.get("title"))
        result.setdefault("title", story.get("title") or story.get("id"))
        result.setdefault("stop_reason", "infra-failed-incomplete")
        out["result"] = result
    ok = _complete(jid, job, out, store)
    with _LOCK:
        _JOBS[jid]["state"] = "done" if ok else "completion_pending"
        _JOBS[jid]["out"] = out
    if p and ok:
        p.finish(jid, status=("done" if out.get("status") == "done" else "failed"),
                 result={"findings": len(out.get("findings") or [])})


def set_run_deadline(run_id, tenant, deadline):
    """Publish a QA run's absolute deadline before it starts dispatching tool threads."""
    with _LOCK:
        _RUN_DEADLINES[(tenant, run_id)] = float(deadline)
        _RUN_PROGRESS[(tenant, run_id)] = {"at": time.time(), "signature": None,
                                           "signatures": {}, "seen_signatures": {},
                                           "phase": "starting", "details": {}}


def run_deadline(run_id, tenant, fallback=None):
    """Return the live progress-leased deadline, not a stale launch-time copy."""
    with _LOCK:
        return _RUN_DEADLINES.get((tenant, run_id), fallback)


def note_run_progress(run_id, tenant, *, signature=None, phase=None, details=None, now=None):
    """Renew a QA shift only when a subordinate records a new durable progress signature.

    This turns the old 25-minute guillotine into a management lease: healthy work keeps its worker; repeated
    identical heartbeats do not extend it forever.  Spend/kill-switch/host gates remain independent hard safety
    boundaries, and an expired lease checkpoints unfinished work rather than calling it done.
    """
    if run_id is None or tenant is None:
        return None
    persist = now is None
    now = float(time.time() if persist else now)
    try:
        lease_s = max(60, int(os.environ.get("AOS_QA_PROGRESS_LEASE_S", "900")))
    except (TypeError, ValueError):
        lease_s = 900
    key = (tenant, run_id)
    with _LOCK:
        current = _RUN_PROGRESS.get(key) or {}
        # Keep an independent signature per subordinate/story. Otherwise two stalled stories can alternate
        # identical heartbeats and renew the run forever simply because A differs from the most recent B.
        details = dict(details or {})
        entity = str(details.get("story") or details.get("actor_id") or phase or "run")
        signatures = dict(current.get("signatures") or {})
        seen_signatures = {str(name): list(values or []) for name, values in
                           dict(current.get("seen_signatures") or {}).items()}
        entity_seen = list(seen_signatures.get(entity) or [])
        # A stalled subordinate can alternate two already-tried actions (or get restarted and replay an older
        # one). Comparing only with its immediately previous signature lets A→B→A renew the lease forever.
        # Renew only for a semantic state the story has not reached during this worker generation.
        if signature is not None and signature in entity_seen:
            return _RUN_DEADLINES.get(key)
        if signature is not None:
            signatures[entity] = signature
            entity_seen.append(signature)
            seen_signatures[entity] = entity_seen[-256:]
        _RUN_PROGRESS[key] = {"at": now, "signature": signature, "phase": phase,
                              "signatures": signatures, "seen_signatures": seen_signatures,
                              "details": details}
        if key in _RUN_DEADLINES:
            _RUN_DEADLINES[key] = max(float(_RUN_DEADLINES[key]), now + lease_s)
        deadline = _RUN_DEADLINES.get(key)
    if persist:
        _persist_controller_progress(run_id, tenant, signature, phase, details)
    return deadline


def _persist_controller_progress(run_id, tenant, signature, phase, details):
    """Publish meaningful QA advancement to the outer controller's durable lease.

    The QA runtime and controller intentionally live in separate supervision layers. Keeping this best-effort
    projection in ``controller_jobs`` lets a freshly restarted jobd distinguish an old-but-advancing campaign
    from a wedged process without depending on either process's in-memory deadline map.
    """
    try:
        meta = json.dumps({"run_id": run_id, "phase": phase, **dict(details or {})}, default=str)
        with _db_connection() as c, c.cursor() as cur:
            cur.execute("""UPDATE controller_jobs cj
                              SET progress_at=now(), progress_signature=%s, progress_meta=%s::jsonb
                            WHERE cj.tenant_id=%s AND cj.phase='TESTQA' AND cj.status='running'
                              AND cj.thread_id=(
                                  SELECT NULLIF(q.memory->'context'->>'thread_id','')::bigint
                                    FROM orchestra_actors q
                                   WHERE q.run_id=%s AND q.tenant_id=%s
                                     AND q.role='qa-coordinator'
                                   ORDER BY q.actor_id LIMIT 1)
                              AND cj.id=(SELECT max(latest.id) FROM controller_jobs latest
                                         WHERE latest.thread_id=cj.thread_id
                                           AND latest.status='running')""",
                        (None if signature is None else str(signature)[:1000], meta, tenant, run_id, tenant))
            c.commit()
    except Exception:
        # Observability must never make a subordinate checkpoint fail. The process-local lease remains active,
        # and the outer heartbeat still proves liveness until the next progress event can project successfully.
        pass


def run_progress(run_id, tenant):
    with _LOCK:
        value = _RUN_PROGRESS.get((tenant, run_id))
        return dict(value) if value else None


def clear_run_deadline(run_id, tenant):
    with _LOCK:
        _RUN_DEADLINES.pop((tenant, run_id), None)
        _RUN_PROGRESS.pop((tenant, run_id), None)


def cancel_run(run_id, tenant, grace_s=20.0) -> int:
    """Cooperatively stop and reap this run's background tools before their owner process exits.

    Browser bridges live in child process groups, so returning while a daemon tool thread is still using one
    would orphan Chromium/ffmpeg.  Signal every matching job, force-close only this process's live QA bridges,
    then give their ``finally: ex.close()`` paths a bounded chance to finish.
    """
    with _LOCK:
        handles = [h for h in _JOBS.values()
                   if h.get("state") == "running"
                   and (h.get("job") or {}).get("run_id") == run_id
                   and (h.get("job") or {}).get("tenant") == tenant]
        _RUN_DEADLINES.pop((tenant, run_id), None)
        _RUN_PROGRESS.pop((tenant, run_id), None)
    for h in handles:
        ev = h.get("cancel_event")
        if ev:
            ev.set()
    try:
        import qa_explorer
        qa_explorer.close_live_bridges(run_id=run_id, tenant=tenant)
    except Exception:
        pass
    deadline = time.time() + max(0.0, float(grace_s))
    for h in handles:
        t = h.get("thread")
        if t and t.is_alive():
            t.join(timeout=max(0.0, deadline - time.time()))
    return sum(1 for h in handles if h.get("thread") and h["thread"].is_alive())


def cancel_actor_job(run_id, tenant, actor_id, *, reason="superseded by terminal decision") -> int:
    """Cooperatively retire only one exact actor's in-process tool generation.

    This is intentionally narrower than ``cancel_run``: a terminal evidence decision can make one focused
    verifier redundant while unrelated story explorers and repository fixers must keep running.  The result
    is converted to a terminal supersession receipt below so normal safety-deadline recovery cannot restart it.
    """
    with _LOCK:
        handles = [h for h in _JOBS.values()
                   if h.get("state") == "running"
                   and (h.get("job") or {}).get("run_id") == run_id
                   and (h.get("job") or {}).get("tenant") == tenant
                   and (h.get("job") or {}).get("actor_id") == actor_id]
        for handle in handles:
            handle["terminal_cancel"] = str(reason or "superseded by terminal decision")[:500]
            event = handle.get("cancel_event")
            if event:
                event.set()
    return len(handles)


def _complete(jid, job, out, store) -> bool:
    """Report the tool's outcome by emitting ONE `tool_result` event to the WORKER ITSELF. The worker's next
    decide-step turns it into finding(s)+done up to its supervisor and finishes. This is deliberate: only the
    POOL ever writes an actor row (via the decide-loop) — the job thread NEVER touches actor state, so there
    is no job-thread/pool-thread lock race on the worker's row (that race hung run_org). Fail-soft."""
    aid = job.get("actor_id")
    for attempt in range(5):
        try:
            # Do not evaluate ``store.emit`` as getattr's default when an injected store supplies emit_once only;
            # Python evaluates call arguments eagerly.  Production has both, focused adapters need only the
            # idempotent contract.
            emit_fn = getattr(store, "emit_once", None) or store.emit
            emitted = emit_fn(job.get("run_id"), job.get("tenant"), aid, aid, "tool_result",
                              {"tool": job.get("tool"), "status": out.get("status", "done"),
                               "findings": out.get("findings") or [], "result": out.get("result"),
                               "assignment": job.get("assignment")}, f"job-{jid}-result")
            if isinstance(emitted, dict) and emitted.get("error"):
                raise RuntimeError(emitted["error"])
            return True
        except Exception:
            if attempt < 4:
                time.sleep(0.05 * (2 ** attempt))
    return False


def reconcile(stale_jobs, store, run_tool=None) -> int:
    """Re-dispatch tool jobs whose pulse went silent (the process running them died). `stale_jobs` are the
    parked tool-workers' persisted job specs (from actor memory). Idempotent: a job still running in THIS
    process is skipped. Returns how many were re-dispatched."""
    n = 0
    for job in (stale_jobs or []):
        jid = job_id_for(job)
        with _LOCK:
            handle = _JOBS.get(jid)
            state = handle.get("state") if handle else None
            pending_out = handle.get("out") if state == "completion_pending" else None
            retry_external = bool(state == "owned_elsewhere"
                                  and time.time() >= float(handle.get("retry_after") or 0))
        # Any in-process handle proves the tool was already dispatched. `done` means its durable result event
        # is waiting for the actor pool; rerunning here duplicates the browser. Only retry the cheap DB handoff.
        if handle is not None:
            if state == "completion_pending" and pending_out is not None:
                ok = _complete(jid, handle.get("job") or job, pending_out, store)
                if ok:
                    with _LOCK:
                        if jid in _JOBS:
                            _JOBS[jid]["state"] = "done"
            elif retry_external:
                with _LOCK:
                    _JOBS.pop(jid, None)
                if dispatch(job, store, run_tool=run_tool):
                    n += 1
            continue
        if dispatch(job, store, run_tool=run_tool):
            n += 1
    return n


def reconcile_parked(store, run_id, tenant=None) -> int:
    """Crash-resume hook: when a run (re)starts, re-dispatch tool jobs for workers that are PARKED with a
    dispatched tool but whose job isn't running in THIS process (the process that ran them died). Reads the
    job spec from the parked actor's persisted memory. Idempotent. Call from run_org at startup."""
    try:
        acts = store.actors(run_id, tenant)
    except Exception:
        return 0
    try:
        event_rows = store.events(run_id, tenant)
        pending_results = {e.get("to_actor") for e in event_rows
                           if e.get("kind") == "tool_result" and not e.get("processed_at")}
        processed_result_corrs = {e.get("corr_id") for e in event_rows
                                  if e.get("kind") == "tool_result" and e.get("processed_at")}
    except Exception:
        # The event ledger is the authority for distinguishing a crashed tool from a tool whose committed
        # result is merely waiting to be reduced.  Failing open here can launch the same browser/fixer again
        # in the few seconds between emit_once() and the worker consuming that event. Retry on the next org
        # cycle instead; a brief pause is safer and cheaper than duplicate product actions.
        return 0
    jobs = []
    superseded = 0
    for a in (acts or []):
        ctxm = ((a.get("memory") or {}).get("context")) or {}
        if (a.get("status") == "blocked" and a.get("actor_id") not in pending_results
                and ctxm.get("tool") and ctxm.get("tool_dispatched")):
            tool_args = dict(ctxm.get("tool_args") or {})
            if ctxm.get("tool") == "dev_fix" and not tool_args.get("resume_changed_files"):
                # Compatibility recovery for a checkpoint consumed by an older runtime generation. That
                # generation stored the complete partial result on the actor but did not copy its mutation
                # receipt into tool_args. Rehydrate it here so a rolling upgrade still retains exact impact.
                actor_result = dict(a.get("result") or {})
                partial = dict(actor_result.get("partial_result") or {})
                files = [str(item) for item in (partial.get("files") or []) if item]
                if files:
                    tool_args["resume_changed_files"] = list(dict.fromkeys(files))[:200]
            if ctxm.get("tool") == "dev_fix" and not tool_args.get("resume_change_diff"):
                actor_result = dict(a.get("result") or {})
                partial = dict(actor_result.get("partial_result") or {})
                change_diff = partial.get("change_diff")
                if isinstance(change_diff, str) and change_diff.strip():
                    tool_args["resume_change_diff"] = change_diff[:14000]
            if ctxm.get("tool") == "dev_fix" and not tool_args.get("resume_state_path"):
                # Rolling-upgrade compatibility: recover the paired focused-browser checkpoint from the
                # consumed partial result when an older runtime preserved only the mutation receipt.
                actor_result = dict(a.get("result") or {})
                partial = dict(actor_result.get("partial_result") or {})
                state_path = partial.get("resume_state_path")
                coverage = [dict(item) for item in (partial.get("coverage") or [])
                            if isinstance(item, dict) and item.get("aspect")]
                if state_path and Path(state_path).is_file():
                    tool_args["resume_state_path"] = state_path
                    tool_args["resume_coverage"] = coverage
                    tool_args["resume_covered"] = [item["aspect"] for item in coverage
                                                    if item.get("covered")]
            if ctxm.get("tool") == "dev_fix" and not tool_args.get("resume_triage_finding"):
                # Compatibility for a checkpoint committed just before a rolling runtime handoff: preserve
                # the sealed residual that owns the next reviewer stage instead of launching its browser again.
                actor_result = dict(a.get("result") or {})
                partial = dict(actor_result.get("partial_result") or {})
                if isinstance(partial.get("resume_triage_finding"), dict):
                    tool_args["resume_triage_finding"] = dict(partial["resume_triage_finding"])
            if ctxm.get("tool") == "dev_fix" and not tool_args.get("resume_triage_receipt"):
                # Compatibility for a checkpoint consumed immediately before the runtime learned to copy the
                # already-completed reviewer gate into tool_args.
                actor_result = dict(a.get("result") or {})
                partial = dict(actor_result.get("partial_result") or {})
                triage = partial.get("triage")
                if (isinstance(triage, dict)
                        and triage.get("disposition") == "confirmed_defect"):
                    tool_args["resume_triage_receipt"] = dict(triage)
            attempt = int(ctxm.get("tool_attempt") or 0)
            candidate = {"run_id": run_id, "actor_id": a["actor_id"], "tool": ctxm["tool"],
                         "attempt": attempt}
            # Defensive repair for legacy/manual reopen paths: if this generation's durable result was already
            # consumed yet the actor is blocked again, it is necessarily a new attempt. Advance until the result
            # id is unused; otherwise emit_once would return the historical row and strand the actor forever.
            while f"job-{job_id_for(candidate)}-result" in processed_result_corrs:
                attempt += 1
                candidate["attempt"] = attempt
            if attempt != int(ctxm.get("tool_attempt") or 0):
                ctxm = dict(ctxm)
                ctxm["tool_attempt"] = attempt
                try:
                    store.update_actor(a["actor_id"], tenant, memory={"context": ctxm})
                except Exception:
                    pass
            job = {"run_id": run_id, "tenant": tenant, "actor_id": a["actor_id"],
                   "supervisor_id": a.get("supervisor_id"), "actor_name": a.get("name"),
                   "tool": ctxm["tool"], "args": tool_args,
                   "assignment": a.get("assignment"), "attempt": attempt,
                   # This spec came from durable parked state rather than the actor's first dispatch.
                   # Tools use it to verify already-applied side effects before repeating them.
                   "resumed": True}
            # A crash can leave a focused evidence browser parked even though its durable management case
            # reached a terminal decision moments earlier. Re-launching that browser adds no authority and can
            # spend minutes replaying a story that has already been decided. Hand the normal reducer a terminal
            # supersession receipt; it will close the child and let the coordinator consume the existing case.
            review_id = str(tool_args.get("_qa_review_id") or "").strip()
            terminal_review = None
            if ctxm.get("tool") == "qa_explore" and review_id:
                try:
                    import qareview
                    review_case = qareview.get(
                        str(tenant),
                        str(tool_args.get("_qa_review_case_id")
                            or qareview.stable_case_id(str(tenant), review_id)))
                    if (review_case.get("status") in ("resolved", "external_authority")
                            and review_case.get("disposition")):
                        terminal_review = review_case
                except Exception:
                    # A review-ledger outage cannot authorize dropping browser work. Fall back to ordinary
                    # fenced recovery and let the live tool prove the story.
                    terminal_review = None
            if terminal_review:
                story = tool_args.get("story") or {}
                out = {"status": "done", "findings": [], "result": {
                    "story": story.get("id") or story.get("title"),
                    "title": story.get("title") or story.get("id"),
                    "stop_reason": "superseded-terminal-decision", "superseded": True,
                    "reason": f"QA review {review_id} already reached "
                              f"{terminal_review.get('disposition')}",
                    "terminal_review": {
                        "case_id": terminal_review.get("case_id"), "review_id": review_id,
                        "status": terminal_review.get("status"),
                        "disposition": terminal_review.get("disposition"),
                    }}}
                if _complete(job_id_for(job), job, out, store):
                    superseded += 1
                    continue
            jobs.append(job)
    # Consuming a checkpoint/cancellation result advances ``attempt`` above from the durable processed-event
    # correlation, producing a new jid. Never discard a terminal handle for the same jid merely because an
    # actor snapshot is still blocked: that snapshot can race a just-committed result event and was the source
    # of duplicate focused browsers immediately after a successful fixer returned.
    return superseded + reconcile(jobs, store)


def active_for_run(run_id, tenant=None) -> int:
    """Number of locally-running jobs for exactly one durable run (never contaminated by another tenant/run)."""
    with _LOCK:
        return sum(1 for h in _JOBS.values()
                   if h.get("state") == "running"
                   and (h.get("job") or {}).get("run_id") == run_id
                   and (tenant is None or (h.get("job") or {}).get("tenant") == tenant))


def _selftest():
    import time

    class FakeStore:
        def __init__(self):
            self.emits, self.updates = [], []

        def emit(self, run_id, tenant, frm, to, kind, payload, corr):
            self.emits.append({"kind": kind, "to": to, "payload": payload, "corr": corr})

        def update_actor(self, aid, tenant, status=None, result=None):
            self.updates.append({"aid": aid, "status": status, "result": result})

    # a tool that finds one blocking bug -> the job emits ONE tool_result to the WORKER ITSELF (to==actor_id),
    # carrying the findings + status. It must NEVER write an actor row (no update_actor) — that's the fix for
    # the pool/job lock race.
    def fake_run_tool(name, args):
        assert name == "qa_explore"
        return {"status": "done",
                "findings": [{"kind": "bug", "title": "blank panel", "blocking": True}],
                "result": {"coverage": [], "stop_reason": "coverage-complete"}}

    st = FakeStore()
    job = {"run_id": 1, "tenant": "t1", "actor_id": 10, "supervisor_id": 5, "tool": "qa_explore",
           "assignment": "verify story US1", "args": {"story": {"id": "US1"}}}
    jid = dispatch(job, st, run_tool=fake_run_tool, sync=True)

    assert [e["kind"] for e in st.emits] == ["tool_result"], st.emits
    r = st.emits[0]
    assert r["to"] == 10, "tool_result must go to the WORKER itself, not the supervisor"
    assert r["payload"]["status"] == "done" and r["payload"]["tool"] == "qa_explore"
    assert len(r["payload"]["findings"]) == 1 and r["payload"]["findings"][0]["blocking"] is True
    assert st.updates == [], "the job thread must NOT write any actor row (avoids the lock race)"
    assert _JOBS[jid]["state"] == "done"

    # a raising tool -> a tool_result with status=failed (never leaves the worker parked forever), no crash.
    def boom(name, args):
        raise RuntimeError("browser died")
    st2 = FakeStore()
    dispatch({**job, "actor_id": 11}, st2, run_tool=boom, sync=True)
    assert [e["kind"] for e in st2.emits] == ["tool_result"] and st2.emits[0]["payload"]["status"] == "failed"

    class TransientStore(FakeStore):
        def __init__(self):
            super().__init__(); self.calls = 0
        def emit(self, *args):
            self.calls += 1
            if self.calls < 3:
                raise RuntimeError("transient DB serialization failure")
            return super().emit(*args)
    st_retry = TransientStore()
    dispatch({**job, "actor_id": 12}, st_retry, run_tool=fake_run_tool, sync=True)
    assert st_retry.calls == 3 and len(st_retry.emits) == 1

    class ErrorResultStore(FakeStore):
        def __init__(self):
            super().__init__(); self.calls = 0
        def emit(self, *args):
            self.calls += 1
            if self.calls < 3:
                return {"error": "kill-switch race"}
            return super().emit(*args)
    st_error = ErrorResultStore()
    dispatch({**job, "actor_id": 13}, st_error, run_tool=fake_run_tool, sync=True)
    assert st_error.calls == 3 and len(st_error.emits) == 1

    # A completed in-process handle is NOT re-dispatched while its durable result waits to be consumed.
    with _LOCK:
        _JOBS[job_id_for(job)]["state"] = "done"
    st3 = FakeStore()
    assert reconcile([job], st3, run_tool=fake_run_tool) == 0
    assert st3.emits == []

    # A crashed previous PROCESS has no in-memory handle, so its parked durable job is re-dispatched once.
    with _LOCK:
        _JOBS.pop(job_id_for(job), None)
    n = reconcile([job], st3, run_tool=fake_run_tool)
    assert n == 1, n
    for _ in range(50):                               # give the re-dispatched (async) job a moment
        if [e["kind"] for e in st3.emits] == ["tool_result"]:
            break
        time.sleep(0.02)
    assert [e["kind"] for e in st3.emits] == ["tool_result"], st3.emits

    # a job already running is NOT re-dispatched.
    with _LOCK:
        _JOBS[job_id_for(job)]["state"] = "running"
    assert reconcile([job], FakeStore(), run_tool=fake_run_tool) == 0

    print("jobrunner selftest: PASS (durable tool_result; completed jobs never redispatch; crash resume idempotent)")
    return 0


if __name__ == "__main__":
    sys.exit(_selftest())
