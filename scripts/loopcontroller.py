#!/usr/bin/env python3
"""loopcontroller.py — the CLOSED-LOOP CEO controller: one durable state machine per org that drives the
whole vision from the chat thread: DISCOVER -> RESEARCH -> OPTIONS -> DEEP_DESIGN -> PLAN_APPROVAL ->
PROTOTYPE -> IMPLEMENT -> TESTQA -> DELIVER.  (Distinct from controller.py, the DBOS standing build-line.)

The controller is the ONLY thing that advances phases. advance() is idempotent + event-driven (safe from a
chat turn, a fleet-completion callback, or the crash-recovery sweeper). A gate (`awaiting`) blocks
transitions until the user answers / approves / supplies credentials, or a dispatched fleet job finishes.
Every async dispatch is a durable `controller_jobs` row (not a fire-and-forget thread that dies with the
process) — resume_stalled() (run from the scheduler) recovers a killed worker and still advances.

    loopcontroller.py start <tenant> <org_id>
    loopcontroller.py say <tenant> <thread_id> "<msg>"
    loopcontroller.py choose <tenant> <thread_id> <option_id>
    loopcontroller.py state <thread_id>
    loopcontroller.py research <tenant> <org_id>         # the current research run's report markdown (+ options)
    loopcontroller.py live <thread_id>                   # live progress: phase + elapsed + ETA + status
    loopcontroller.py cancel <tenant> <thread_id> [reason]  # halt the in-flight run (kill-switch) + park it
    loopcontroller.py resume
    loopcontroller.py watchdog                           # user-facing SLA: warn on jobs that overran their ETA
    loopcontroller.py selftest
Run with the agent-os venv python.
"""
import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit         # noqa: E402
import factory       # noqa: E402
import orchestrator  # noqa: E402
import process_assurance  # noqa: E402
import qatiming      # noqa: E402
from dbpool import connection, tenant_connection  # noqa: E402

from aoscfg import ENV, DB

PHASES = ["DISCOVER", "RESEARCH", "OPTIONS", "DEEP_DESIGN", "PLAN_APPROVAL",
          "PROTOTYPE", "IMPLEMENT", "TESTQA", "DELIVER"]

# Short human labels for the LIVE-PROGRESS surface (what the controller is doing right now), keyed by the
# dispatched job kind / the gate it is parked on — so the UI shows "Researching… (2m elapsed)" not a static
# bubble, and a truthful "Waiting on you" instead of a false "done" while a durable job is still in flight.
_KIND_LABEL = {"research": "Researching directions…", "design": "Designing prototype…",
               "build": "Building…", "qa": "Testing…"}
_GATE_LABEL = {"user_feedback": "Waiting on you", "user_approval": "Waiting for your pick",
               "credentials": "Waiting on a provider connection"}

# Gate prompts that become STALE the instant their prerequisite is satisfied. By the time _llm() runs,
# say()'s consent + provider gates have BOTH passed — so these messages must be dropped from the model
# context, or the model parrots an old "please accept consent / connect a provider" back at a CEO who
# already did it (the 3.4 "re-asked for consent I already accepted" bug).
_RESOLVED_GATE_KINDS = {"consent_required", "provider_required"}

# CANONICAL per-phase ETA defaults (minutes) — the ONE source of truth for the coarse fallbacks, kept
# NUMERICALLY RECONCILED with console.py:163 `_PHASE_ETA_MIN` so the console's own fallback bubble and the
# controller's promised ETA never disagree (the #2 "~3 min then 13" bug came from two drifting constants —
# loopcontroller said 3, console said 10). Build phases (PROTOTYPE/IMPLEMENT) prefer estimate.py's
# history-backed median at dispatch and only fall back to these; RESEARCH prefers the research_runs median.
_PHASE_ETA_DEFAULT = {"RESEARCH": 10, "PROTOTYPE": 6, "IMPLEMENT": 14,
                      "TESTQA": qatiming.slice_eta_min()}

# Honest-range multipliers (mirror estimate.py's LOW_MULT/HIGH_MULT) so a promised ETA is a RANGE, not a
# false-precision point — "~10-14 min", never a bare "~3 min" that over-runs. When a job overruns we RAISE
# the point (sla_watchdog) so the range tracks reality instead of lying.
_ETA_LOW_MULT, _ETA_HIGH_MULT = 0.6, 1.6
# How long past a prior SLA warning before we WARN + PING AGAIN on a still-overrunning job (#2.6/#5 re-ping
# on continued overrun) — a CEO who left the tab gets a fresh heads-up, not one-and-done silence.
# A healthy worker has an output-independent heartbeat; repeated five-minute "still working" alerts add no
# information and previously produced 143 unread duplicates for one tenant.  Warn on the first ETA miss, then
# at most hourly while health is unchanged. State transitions (crash, cleanup failure, completion) notify
# immediately through their own paths.
_SLA_REWARN_MIN = int(os.environ.get("AOS_SLA_REWARN_MIN", "60"))

# A QA wall-clock limit is a process-safety checkpoint, not a CEO approval gate.  Keep each worker bounded so
# Chromium/model pressure is reaped predictably, but let the company hand the durable campaign to a fresh worker
# without bothering the CEO.  These campaign-level bounds are deliberately independent of the 15-minute worker
# slice: repeated lack of progress or an excessive number of hand-offs is a genuine management incident.
QA_AUTO_CHECKPOINT_MAX = int(os.environ.get("AOS_QA_AUTO_CHECKPOINT_MAX", "18"))  # <= ~6h at 20m/slice
QA_NO_PROGRESS_MAX = int(os.environ.get("AOS_QA_NO_PROGRESS_MAX", "3"))


def _bounded_ms(name, default, minimum=50, maximum=30000):
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = int(default)
    return max(int(minimum), min(int(maximum), value))


# Controller DDL and dispatch serialization are recovery paths. They may defer to
# the next jobd tick, but they may never pin that daemon behind an unbounded DB
# wait. PostgreSQL applies both settings transaction-locally, including pooled
# connections, so no timeout leaks to another borrower.
CONTROLLER_LOCK_TIMEOUT_MS = _bounded_ms("AOS_CONTROLLER_LOCK_TIMEOUT_MS", 500)
CONTROLLER_STATEMENT_TIMEOUT_MS = _bounded_ms("AOS_CONTROLLER_STATEMENT_TIMEOUT_MS", 5000)
RESUME_SWEEP_BATCH = max(1, min(100, _bounded_ms("AOS_CONTROLLER_RESUME_BATCH", 20, 1, 100)))
RESUME_SWEEP_BUDGET_S = max(5, min(90, _bounded_ms("AOS_CONTROLLER_RESUME_BUDGET_S", 45, 5, 90)))
SLA_WATCHDOG_BATCH = max(1, min(100, _bounded_ms("AOS_SLA_WATCHDOG_BATCH", 20, 1, 100)))
SLA_CLAIM_TTL_MIN = max(1, min(30, _bounded_ms("AOS_SLA_CLAIM_TTL_MIN", 5, 1, 30)))
_RETRYABLE_CONTROLLER_DB_ERRORS = (
    psycopg.errors.LockNotAvailable,
    psycopg.errors.QueryCanceled,
    psycopg.errors.DeadlockDetected,
    psycopg.errors.SerializationFailure,
)
_CONTROLLER_ENSURED = False
_CONTROLLER_ENSURE_LOCK = threading.Lock()


class ControllerDatabaseBusy(RuntimeError):
    """A bounded controller DB operation deferred without committing partial state."""

    retryable = True


def _conn(tenant_id=None):
    return tenant_connection(tenant_id) if tenant_id else connection()


def _set_controller_db_timeouts(cur, *, lock_ms=None, statement_ms=None):
    lock_ms = CONTROLLER_LOCK_TIMEOUT_MS if lock_ms is None else max(1, int(lock_ms))
    statement_ms = (CONTROLLER_STATEMENT_TIMEOUT_MS if statement_ms is None
                    else max(lock_ms, int(statement_ms)))
    cur.execute("SELECT set_config('lock_timeout', %s, true)", (f"{lock_ms}ms",))
    cur.execute("SELECT set_config('statement_timeout', %s, true)", (f"{statement_ms}ms",))


def _eta_range(mins):
    """An honest (lo, hi) minute range around a point ETA, mirroring estimate.py's multipliers. Never
    returns lo>hi or a zero floor. (mins None/<=0 -> (None, None) so callers show no range.)"""
    try:
        m = int(mins)
    except (TypeError, ValueError):
        return (None, None)
    if m <= 0:
        return (None, None)
    lo = max(1, int(round(m * _ETA_LOW_MULT)))
    hi = max(lo + 1, int(round(m * _ETA_HIGH_MULT)))
    return (lo, hi)


def _eta_phrase(mins):
    """'~10-14 min' from a point ETA (honest range), or '' when there's nothing to promise."""
    lo, hi = _eta_range(mins)
    return f"~{lo}-{hi} min" if lo else ""


def _ensure():
    global _CONTROLLER_ENSURED
    if _CONTROLLER_ENSURED:
        return
    # A second caller must not wait forever behind a bootstrap thread that is
    # itself stalled. The database statements below are bounded separately.
    ensure_wait_s = max(0.1, CONTROLLER_STATEMENT_TIMEOUT_MS / 1000.0 + 0.5)
    if not _CONTROLLER_ENSURE_LOCK.acquire(timeout=ensure_wait_s):
        raise ControllerDatabaseBusy("controller schema initialization is already in progress")
    try:
        if _CONTROLLER_ENSURED:
            return
        try:
            with _conn() as c, c.cursor() as cur:
                _set_controller_db_timeouts(cur)
                cur.execute("""CREATE TABLE IF NOT EXISTS controller_state (
                    thread_id BIGINT PRIMARY KEY, tenant_id TEXT, org_id BIGINT,
                    phase TEXT NOT NULL DEFAULT 'DISCOVER', brief JSONB, options JSONB, chosen_option JSONB,
                    plan JSONB, research_run_id BIGINT, product TEXT, awaiting TEXT,
                    execution_scope TEXT NOT NULL DEFAULT 'production',
                    updated_at TIMESTAMPTZ DEFAULT now())""")
                # LIVE-PROGRESS columns (added in-place for existing orgs): the in-flight job's kind, when it
                # started, its ETA (minutes), and a short status string the console renders live.
                cur.execute("""ALTER TABLE controller_state
                    ADD COLUMN IF NOT EXISTS job_kind TEXT,
                    ADD COLUMN IF NOT EXISTS job_started_at TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS job_eta_min INTEGER,
                    ADD COLUMN IF NOT EXISTS job_status TEXT,
                    ADD COLUMN IF NOT EXISTS job_sla_warned BOOLEAN DEFAULT false,
                    ADD COLUMN IF NOT EXISTS job_sla_warned_at TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS job_sla_claimed_at TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS job_sla_claim_token TEXT,
                    ADD COLUMN IF NOT EXISTS execution_scope TEXT NOT NULL DEFAULT 'production',
                    ADD COLUMN IF NOT EXISTS pending_intent TEXT,
                    ADD COLUMN IF NOT EXISTS qa_checkpoint_count INTEGER DEFAULT 0,
                    ADD COLUMN IF NOT EXISTS qa_last_completed INTEGER,
                    ADD COLUMN IF NOT EXISTS qa_no_progress_count INTEGER DEFAULT 0,
                    ADD COLUMN IF NOT EXISTS qa_campaign_key TEXT""")
                cur.execute("""CREATE TABLE IF NOT EXISTS controller_jobs (
                    id BIGSERIAL PRIMARY KEY, thread_id BIGINT, tenant_id TEXT, phase TEXT, kind TEXT,
                    status TEXT DEFAULT 'running', result JSONB,
                    started_at TIMESTAMPTZ DEFAULT now(), finished_at TIMESTAMPTZ)""")
                # ARCHITECTURE-OVERHAUL Step 1: an OUTPUT-INDEPENDENT liveness heartbeat. The worker ticks
                # heartbeat_at on a fixed timer, decoupled from model output.
                cur.execute("ALTER TABLE controller_jobs ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ")
                cur.execute("ALTER TABLE controller_jobs ADD COLUMN IF NOT EXISTS lease_token BIGINT DEFAULT 0")
                cur.execute("ALTER TABLE controller_jobs ADD COLUMN IF NOT EXISTS worker_pid BIGINT")
                cur.execute("ALTER TABLE controller_jobs ADD COLUMN IF NOT EXISTS worker_start_ticks BIGINT")
                cur.execute("ALTER TABLE controller_jobs ADD COLUMN IF NOT EXISTS worker_boot_id TEXT")
                # Meaningful-work lease, separate from the output-independent process heartbeat. A QA worker
                # can legitimately live for hours; its absolute age is not evidence of a runaway while new
                # story/step checkpoints are still landing. Browser workers renew this only for a new durable
                # progress signature, so a tight heartbeat loop cannot keep a wedged campaign alive forever.
                cur.execute("ALTER TABLE controller_jobs ADD COLUMN IF NOT EXISTS progress_at TIMESTAMPTZ")
                cur.execute("ALTER TABLE controller_jobs ADD COLUMN IF NOT EXISTS progress_signature TEXT")
                cur.execute("ALTER TABLE controller_jobs ADD COLUMN IF NOT EXISTS progress_meta JSONB")
                cur.execute("""ALTER TABLE controller_jobs ADD COLUMN IF NOT EXISTS execution_scope TEXT
                               NOT NULL DEFAULT 'production'""")
                cur.execute("""CREATE INDEX IF NOT EXISTS controller_state_execution_queue_idx
                               ON controller_state(execution_scope, updated_at, thread_id)
                               WHERE awaiting IS NULL AND phase <> 'DELIVER'""")
                c.commit()
            _CONTROLLER_ENSURED = True
        except _RETRYABLE_CONTROLLER_DB_ERRORS as exc:
            # The connection context rolls the whole DDL transaction back. A
            # later request/jobd tick retries from the same durable state.
            raise ControllerDatabaseBusy("controller schema lock busy; retry on the next tick") from exc
    finally:
        _CONTROLLER_ENSURE_LOCK.release()


def _st(thread_id):
    with _conn() as c, c.cursor() as cur:
        cur.execute("""SELECT thread_id, tenant_id, org_id, phase, brief, options, chosen_option, plan,
                              research_run_id, product, awaiting, pending_intent, qa_checkpoint_count,
                              qa_last_completed, qa_no_progress_count, qa_campaign_key, execution_scope
                       FROM controller_state WHERE thread_id=%s""",
                    (thread_id,))
        r = cur.fetchone()
    if not r:
        return None
    keys = ["thread_id", "tenant_id", "org_id", "phase", "brief", "options", "chosen_option", "plan",
            "research_run_id", "product", "awaiting", "pending_intent", "qa_checkpoint_count",
            "qa_last_completed", "qa_no_progress_count", "qa_campaign_key", "execution_scope"]
    return dict(zip(keys, r))


def _set(thread_id, **kw):
    if not kw:
        return
    cols, vals = [], []
    for k, v in kw.items():
        cols.append(f"{k}=%s")
        vals.append(json.dumps(v) if k in ("brief", "options", "chosen_option", "plan") and v is not None else v)
    vals.append(thread_id)
    with _conn() as c, c.cursor() as cur:
        cur.execute(f"UPDATE controller_state SET {', '.join(cols)}, updated_at=now() WHERE thread_id=%s", vals)
        c.commit()


def _job_begin(thread_id, kind, eta_min, status):
    """Stamp the LIVE-PROGRESS fields when an async job kicks off (kind/start/ETA/status) so the console
    can render a real, ticking 'Researching… (Nm elapsed, ~M min)' bubble instead of a static one."""
    with _conn() as c, c.cursor() as cur:
        cur.execute("""UPDATE controller_state SET job_kind=%s, job_started_at=now(), job_eta_min=%s,
                       job_status=%s, job_sla_warned=false, job_sla_claimed_at=NULL,
                       job_sla_claim_token=NULL, updated_at=now() WHERE thread_id=%s""",
                    (kind, eta_min, status, thread_id))
        c.commit()


def _job_progress(thread_id, status):
    """Update the live status string mid-run — only while the thread is genuinely on its fleet gate, so a
    late update can never resurrect a status on an already-finished/cancelled job (NO false 'working')."""
    with _conn() as c, c.cursor() as cur:
        cur.execute("""UPDATE controller_state SET job_status=%s, updated_at=now()
                       WHERE thread_id=%s AND awaiting='fleet'""", (status, thread_id))
        c.commit()


def _worker_progress(thread_id, jid, stage, event, detail=None):
    """Renew a worker's progress lease from a substantive, deduplicated checkpoint.

    ``heartbeat_at`` says the process is alive; ``progress_at`` says its work moved. Keeping them separate
    prevents a heartbeat timer from disguising a deadlock while allowing a healthy long build to outlive any
    nominal wall-clock estimate.
    """
    label = f"{str(stage or 'build')} · {str(event or 'progress')}"
    safe_detail = detail if isinstance(detail, (dict, list, str, int, float, bool)) else None
    meta = json.dumps({"stage": str(stage or "")[:120], "event": str(event or "")[:120],
                       "detail": safe_detail}, default=str)
    try:
        with _conn() as c, c.cursor() as cur:
            _set_controller_db_timeouts(cur)
            cur.execute("""UPDATE controller_jobs
                              SET progress_at=now(),
                                  result=COALESCE(result,'{}'::jsonb)
                                         || jsonb_build_object('progress',%s::jsonb)
                            WHERE id=%s AND thread_id=%s AND status='running'""",
                        (meta, jid, thread_id))
            changed = cur.rowcount
            if changed:
                cur.execute("""UPDATE controller_state SET job_status=%s,updated_at=now()
                                WHERE thread_id=%s AND awaiting='fleet'""", (label, thread_id))
            c.commit()
        return bool(changed)
    except Exception:
        return False


def _job_clear(thread_id):
    """Clear the LIVE-PROGRESS fields once a job leaves flight (done/failed/cancelled)."""
    with _conn() as c, c.cursor() as cur:
        cur.execute("""UPDATE controller_state SET job_kind=NULL, job_started_at=NULL, job_eta_min=NULL,
                       job_status=NULL, job_sla_warned=false, job_sla_claimed_at=NULL,
                       job_sla_claim_token=NULL, updated_at=now() WHERE thread_id=%s""",
                    (thread_id,))
        c.commit()


def _qa_checkpoint_counts(previous_completed, completed, checkpoint_count, no_progress_count):
    """Pure campaign accounting used by the TESTQA hand-off policy.

    Missing progress is treated as unknown (older workers did not return it), not as proof of a stall.  Once
    both samples exist, a non-increasing completed count is a no-progress slice; any forward movement resets
    the streak.  The hard checkpoint count remains an independent resource ceiling.
    """
    checkpoints = max(0, int(checkpoint_count or 0)) + 1
    prior_stalls = max(0, int(no_progress_count or 0))
    if completed is None or previous_completed is None:
        stalls = prior_stalls
    elif int(completed) > int(previous_completed):
        stalls = 0
    else:
        stalls = prior_stalls + 1
    return checkpoints, stalls


def _qa_campaign_checkpoint_counts(previous_campaign_key, campaign_key, previous_completed, completed,
                                   checkpoint_count, no_progress_count):
    """Scope raw progress counters to the campaign/revision that produced them."""
    changed = bool(campaign_key and campaign_key != previous_campaign_key)
    checkpoints, stalls = _qa_checkpoint_counts(
        None if changed else previous_completed, completed,
        0 if changed else checkpoint_count, 0 if changed else no_progress_count)
    return checkpoints, stalls, changed


def _qa_checkpoint_can_continue(cleanup_processes, manager):
    """A time-slice count is never, by itself, a request for CEO authority.

    Process leakage is a hard safety stop. A manager may request authority only when
    `_qa_manager_decision` validated a concrete external boundary supplied by the
    work, never because an arbitrary number of internal shift rotations elapsed.
    """
    return int(cleanup_processes or 0) == 0 and manager.get("action") != "request_new_authority"


def _qa_manager_decision(thread_id, facts):
    """Ask the QA manager what to do after an abnormal checkpoint; hard safety remains deterministic.

    Productive shift hand-offs need no new decision—the standing objective already authorizes continuing.
    Repeated no-progress is different: diagnose and choose an organizational response rather than converting a
    timer into a CEO gate.  The model may request new authority only for a genuine authority boundary.  A model
    outage fails toward an internal incident + fresh bounded recovery, never toward silently bothering the CEO.
    """
    allowed = {"continue_fresh_worker", "retry_failed_actor", "reassign_worker",
               "open_internal_incident_and_cleanup", "request_new_authority"}
    try:
        s = _st(thread_id) or {}
        product = s.get("product") or "unknown"
        repo = str(Path(factory.PRODUCTS) / product)
        prompt = (
            "You are the QA director managing a durable browser-QA campaign. Decide the next management action "
            "from evidence, like an elite human lead. A wall-clock slice is only a host-safety boundary. "
            "Reversible internal retry/reassignment/cleanup is already authorized. Ask the CEO ONLY if the next "
            "step genuinely needs new money, credentials/legal consent, an irreversible external action, or a "
            "business tradeoff outside the objective. Reply ONLY JSON: "
            '{"action":"continue_fresh_worker|retry_failed_actor|reassign_worker|'
            'open_internal_incident_and_cleanup|request_new_authority","reason":"...",'
            '"authority_gap":"none|spend|credential|legal|irreversible|business"}.\nFACTS:\n' +
            json.dumps(facts, default=str)[:4000])
        out = factory.agent("qa-director", repo, prompt, timeout=180, retries=1,
                            spawner="qa-coordinator")
        data = factory._extract_json((out or {}).get("out_full") or (out or {}).get("out") or "") or {}
        action = data.get("action")
        gap = data.get("authority_gap") or "none"
        if action in allowed:
            # A bare/model-invented request for authority is not enough.  The observed state must contain the
            # same concrete boundary; otherwise the manager keeps the issue inside the company.
            observed_boundaries = set(facts.get("authority_boundaries") or [])
            if action == "request_new_authority" and (gap not in {
                    "spend", "credential", "legal", "irreversible", "business"}
                    or gap not in observed_boundaries):
                action = "open_internal_incident_and_cleanup"
            return {"action": action, "reason": str(data.get("reason") or "")[:300],
                    "authority_gap": gap, "model_decided": True}
    except Exception as e:
        return {"action": "open_internal_incident_and_cleanup",
                "reason": f"QA manager unavailable: {str(e)[:180]}",
                "authority_gap": "none", "model_decided": False}
    return {"action": "open_internal_incident_and_cleanup",
            "reason": "QA manager returned no valid structured decision",
            "authority_gap": "none", "model_decided": False}


def _research_history_min(default=None):
    """History-backed ETA (minutes) for a research run — the research analogue of estimate.py's median-of-
    history approach, but research timing lives in `research_runs` (started_at/finished_at), not the build
    `traces` estimate.py reads. We take the MEDIAN wall-clock duration of recent COMPLETED runs so the
    promised ETA matches what we actually deliver. With no history we fall back to a REALISTIC default: a
    research run fans out ~6-8 parallel web agents + a synthesis pass, so it takes ~10-14 min in practice —
    NOT the old unrealistic 3. Always returns a positive int; never raises."""
    try:
        import statistics
        with _conn() as c, c.cursor() as cur:
            # Only completed runs with a sane positive duration count (a still-running / mis-stamped row must
            # not drag the median). Recent-first, capped, mirroring estimate.py's "median of similar past runs".
            cur.execute("""SELECT EXTRACT(EPOCH FROM (finished_at - started_at)) / 60.0
                           FROM research_runs
                           WHERE status='done' AND started_at IS NOT NULL AND finished_at IS NOT NULL
                             AND finished_at > started_at
                           ORDER BY id DESC LIMIT 20""")
            mins = [float(r[0]) for r in cur.fetchall() if r[0] and float(r[0]) > 0]
        if mins:
            return max(1, int(round(statistics.median(mins))))
    except Exception:
        pass
    return max(1, int(default if default is not None else _PHASE_ETA_DEFAULT["RESEARCH"]))


def _estimate_runtime(phase, plan=None):
    """Best-effort ETA (minutes) for an async phase. RESEARCH uses a history-backed median of recent real
    research_runs (else a realistic ~12-min default — it's a multi-agent fleet, not a 3-min call). Build/
    design phases use estimate.py's history-backed estimate for the plan's kind (a 'project' falls back to
    'service'); a prototype is only a slice of the full build. QA derives from its configured safety slice. Always
    returns a positive int — never raises."""
    plan = plan or {}
    # Realistic point-estimate defaults (minutes) — the CANONICAL, console-reconciled `_PHASE_ETA_DEFAULT`
    # (RESEARCH is a real ~6-8-agent fleet + synthesis, not a 3-min call). No more per-function magic numbers
    # that drift out of sync with console.py and lie to the CEO.
    base = _PHASE_ETA_DEFAULT
    if phase == "RESEARCH":
        return _research_history_min(default=base["RESEARCH"])
    if phase in ("PROTOTYPE", "IMPLEMENT"):
        try:
            import estimate
            kind = (plan.get("kind") or "service").lower()
            kind = kind if kind in ("lib", "web", "service") else "service"   # 'project' -> service
            charter = f"{plan.get('charter', '')} {plan.get('plan', '')}"
            mins = estimate.estimate_for_charter(charter, kind).get("minutes_estimate") or base[phase]
            if phase == "PROTOTYPE":
                mins = max(2, mins * 0.4)                                      # design is a slice of the build
            return max(1, int(round(mins)))
        except Exception:
            pass
    return base.get(phase, 5)


def _ping(tid, title, body, category="build", level="urgent"):
    """Heads-up the tenant the moment async results LAND (options / prototype / build) — not only on
    failure or final ship — so a CEO who CLOSED THE TAB still gets told.

    Two channels, both best-effort:
      * IN-APP feed (notifications.send): the ALWAYS-AVAILABLE channel — the bell/badge lights up even with
        no ntfy/email configured. We default to level='urgent' (NOT a silent/passive default) because that's
        the taxonomy level whose contract includes email + tenant push and makes the feed unmistakable.
      * PUSH fallback (push.send on a daemon thread) only when notification delivery itself fails. HONEST
        CAVEAT: on a self-host box push only actually delivers if ntfy is configured AND the tenant has a
        push topic registered (email likewise needs SMTP); otherwise the in-app feed is the reliable channel.
    Never raises."""
    sent_notification = False
    try:
        import notifications
        notifications.send(tid, category, title, (body or "")[:300], level=level)
        sent_notification = True
    except Exception:
        pass
    if sent_notification:
        return

    def _p():
        try:
            import push
            push.send(tid, title, (body or "")[:160], priority="high" if level == "urgent" else "default")
        except Exception:
            pass
    threading.Thread(target=_p, daemon=True).start()


def _authorize_qa_checkpoint_resume(state):
    """Turn an explicit user retry into authority to reopen the strongest matching QA checkpoint.

    A plain cancel must keep every actor dead. Once the same thread explicitly retries, however, starting a
    fresh org discards the exact durable continuity the cancel preserved. Choose the halted matching campaign
    with the most recorded story state (latest wins ties), mark only its cancelled actors resumable, and move
    the file locator back to that workforce. This also repairs a short-lived empty replacement run.
    """
    state = dict(state or {})
    if state.get("phase") != "TESTQA" or not state.get("product"):
        return None
    tenant, product, thread_id = state.get("tenant_id"), state.get("product"), state.get("thread_id")
    if not tenant or thread_id is None:
        return None
    marker = {"resumable_checkpoint": True, "resume_authorized_by": "explicit_user_retry",
              "resume_authorized_at": time.time()}
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""SELECT r.run_id,q.memory
                             FROM orchestra_runs r
                             JOIN orchestra_actors q ON q.run_id=r.run_id AND q.tenant_id=r.tenant_id
                            WHERE r.tenant_id=%s AND r.status='halted' AND q.role='qa-coordinator'
                              AND q.memory->'context'->>'product'=%s
                              AND q.memory->'context'->>'thread_id'=%s
                            ORDER BY (SELECT count(*) FROM jsonb_object_keys(
                                       COALESCE(q.memory->'story_status','{}'::jsonb))) DESC,
                                     r.run_id DESC
                            LIMIT 1""", (tenant, product, str(thread_id)))
            row = cur.fetchone()
            if not row:
                return None
            run_id, memory = int(row[0]), dict(row[1] or {})
            payload = json.dumps(marker)
            cur.execute("""UPDATE orchestra_runs
                              SET result=COALESCE(result,'{}'::jsonb) || %s::jsonb
                            WHERE run_id=%s AND tenant_id=%s AND status='halted'""",
                        (payload, run_id, tenant))
            cur.execute("""UPDATE orchestra_actors
                              SET result=COALESCE(result,'{}'::jsonb) || %s::jsonb,
                                  last_active=now()
                            WHERE run_id=%s AND tenant_id=%s AND status='dead'
                              AND COALESCE(result->>'cancelled','false')='true'""",
                        (payload, run_id, tenant))
            c.commit()

        # The file is a process locator, not a second authority. Repoint it atomically to the selected durable
        # org; its coordinator memory remains the source of story verdicts and revision fencing.
        context = dict(memory.get("context") or {})
        repo = context.get("repo")
        stories = list(context.get("stories") or [])
        if repo and stories:
            qa_path = str(SCRIPTS / "qa")
            if qa_path not in sys.path:
                sys.path.insert(0, qa_path)
            import campaign_checkpoint
            target_url = context.get("target_url")
            signature = campaign_checkpoint.campaign_signature(
                tenant=tenant, product=product, target_url=target_url,
                vision=context.get("vision"), repo=repo, stories=stories, thread_id=thread_id)
            document = campaign_checkpoint.checkpoint_document(
                run_id=run_id, signature=signature, tenant=tenant, product=product,
                target_url=target_url, thread_id=thread_id, stories=stories,
                story_status=memory.get("story_status"),
                batch_size=context.get("story_batch_size") or 0, status="halted")
            document["product_revision"] = memory.get("coverage_revision")
            document["revision_generation"] = int(memory.get("revision_generation") or 0)
            campaign_checkpoint.write_checkpoint(Path(repo) / "docs" / "QA-CHECKPOINT.json", document)
        audit.append(actor="loopcontroller", action="QaCheckpointResumeAuthorized",
                     resource=str(thread_id), decision=str(run_id),
                     payload={"product": product,
                              "recorded_stories": len(dict(memory.get("story_status") or {}))},
                     tenant_id=tenant)
        return run_id
    except Exception:
        return None


def _resume_halts(thread_id):
    """Lift kill switches and authorize the strongest QA checkpoint after an explicit retry.

    Cancel itself remains terminal. This function is called only after a fresh user proceed/retry decision,
    so it is the authority boundary that permits the same durable workforce—not a blank replacement—to run.
    """
    state = _st(thread_id)
    resumed_run_id = _authorize_qa_checkpoint_resume(state)
    try:
        import killswitch
        killswitch.resume(f"thread-{thread_id}")
        if state and state.get("product"):
            killswitch.resume(state["product"])
    except Exception:
        pass
    return resumed_run_id


def _research_progress(tenant_id, research_run_id):
    """Live sub-step summary for durable research runs, derived from orchestra actor rows.
    No model calls: this reads the org's real hired researchers, their assignments, statuses, and liveness."""
    if not tenant_id or not research_run_id:
        return None
    try:
        with _conn(tenant_id) as c, c.cursor() as cur:
            cur.execute("""SELECT run_id FROM orchestra_actors
                           WHERE tenant_id=%s AND memory->>'research_run_id'=%s
                           ORDER BY run_id DESC LIMIT 1""", (tenant_id, str(research_run_id)))
            row = cur.fetchone()
            if not row:
                return None
            orc = row[0]
            cur.execute("""SELECT name, role, kind, status, assignment,
                                  EXTRACT(EPOCH FROM (now()-last_active))::int
                           FROM orchestra_actors
                           WHERE tenant_id=%s AND run_id=%s
                           ORDER BY actor_id""", (tenant_id, orc))
            rows = cur.fetchall()
            cur.execute("""SELECT actor_id FROM orchestra_tool_leases
                           WHERE tenant_id=%s AND run_id=%s AND lease_until>now()""",
                        (tenant_id, orc))
            active_tool_actors = {int(item[0]) for item in cur.fetchall()}
            cur.execute("""SELECT actor_id,name,role,kind,status,assignment,
                                  EXTRACT(EPOCH FROM (now()-last_active))::int
                           FROM orchestra_actors
                           WHERE tenant_id=%s AND run_id=%s
                           ORDER BY actor_id""", (tenant_id, orc))
            actor_rows = cur.fetchall()
    except Exception:
        return None
    # Keep the legacy row shape below while retaining actor identity for exact tool-lease classification.
    workers = [(actor_id, name, role, kind, status, assignment, age)
               for actor_id, name, role, kind, status, assignment, age in actor_rows if kind == "worker"]
    if not workers:
        return None
    total = len(workers)
    done = sum(1 for r in workers if r[4] == "done")
    blocked = sum(1 for r in workers if r[4] == "blocked" and r[0] not in active_tool_actors)
    working = sum(1 for r in workers if r[4] in ("working", "parked", "idle") or
                  (r[4] == "blocked" and r[0] in active_tool_actors))
    active = next((r for r in workers if r[4] in ("working", "parked", "idle") or
                   (r[4] == "blocked" and r[0] in active_tool_actors)), None)
    if active is None:
        active = next((r for r in workers if r[4] == "blocked"), None)
    current = (active[5] if active else "") or ""
    if len(current) > 90:
        current = current[:87].rstrip() + "..."
    detail = f"{done}/{total} researchers done"
    if blocked:
        detail += f", {blocked} blocked"
    if working and done < total:
        detail += f", {working} active"
    if current:
        detail += f" · {current}"
    return {"orchestra_run_id": orc, "researchers_total": total, "researchers_done": done,
            "researchers_blocked": blocked, "researchers_active": working, "current_step": current,
            "progress_detail": detail, "subprogress_pct": int(round((done / total) * 100))}


def _qa_story_snapshot(coordinator_memory):
    """Release-facing progress from the coordinator's canonical per-story state."""
    coordinator_memory = dict(coordinator_memory or {})
    context = dict(coordinator_memory.get("context") or {})
    planned = list(dict.fromkeys(
        str(story.get("id") or story.get("title")) for story in (context.get("stories") or [])
        if isinstance(story, dict) and (story.get("id") or story.get("title"))))
    statuses = {str(key): str(value) for key, value in
                dict(coordinator_memory.get("story_status") or {}).items()}
    clean = [story for story in planned if statuses.get(story) == "clean"]
    blocking = [story for story in planned if statuses.get(story) == "blocking"]
    recorded_incomplete = [story for story in planned
                           if story in statuses and statuses.get(story) not in ("clean", "blocking")]
    missing = [story for story in planned if story not in statuses]
    incomplete = recorded_incomplete + missing
    return {"planned": planned, "statuses": statuses, "clean": clean,
            "blocking": blocking, "recorded_incomplete": recorded_incomplete,
            "missing": missing, "incomplete": incomplete}


def _qa_progress(thread_id):
    """Real TESTQA campaign progress from the durable QA org, not an ETA-shaped approximation.

    QA tool actors are intentionally `blocked` while their browser job runs off-loop, so expose them as
    outstanding rather than falsely telling the CEO the workforce is blocked. Historical explorer actors are
    attempts/sessions, not acceptance passes: release progress is the coordinator's latest per-story verdict.
    The two-browser admission cap is an upper bound on simultaneous activity.
    """
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""SELECT a.run_id,
                                  EXTRACT(EPOCH FROM (now()-r.created_at))::bigint
                           FROM orchestra_actors a JOIN orchestra_runs r USING (run_id)
                           WHERE a.role='qa-coordinator'
                             AND a.memory->'context'->>'thread_id'=%s
                             AND r.status='running'
                           ORDER BY a.run_id DESC LIMIT 1""", (str(thread_id),))
            row = cur.fetchone()
            if not row:
                return None
            run_id, campaign_elapsed_s = row
            cur.execute("""SELECT role,status,assignment,memory FROM orchestra_actors
                           WHERE run_id=%s ORDER BY actor_id""", (run_id,))
            all_rows = cur.fetchall()
    except Exception:
        return None
    rows = [(status, assignment) for role, status, assignment, _ in all_rows if role == "qa-explorer"]
    if not rows:
        return None
    total = len(rows)
    done = sum(1 for status, _ in rows if status == "done")
    outstanding = sum(1 for status, _ in rows if status not in ("done", "dead", "failed"))
    explorer_current = next((assignment for status, assignment in rows
                             if status not in ("done", "dead", "failed") and assignment), "")
    support = [(role, assignment, dict(memory or {})) for role, status, assignment, memory in all_rows
               if role not in ("qa-explorer", "qa-coordinator")
               and status not in ("done", "dead", "failed")]
    support_active = len(support)
    support_current = next((assignment for _, assignment, _ in support if assignment), "")
    support_story = ""
    for _, _, memory in support:
        support_context = dict(memory.get("context") or {})
        bug = dict(support_context.get("bug") or {})
        tool_args = dict(support_context.get("tool_args") or {})
        tool_story = tool_args.get("story") or {}
        if isinstance(tool_story, dict):
            tool_story = tool_story.get("id") or tool_story.get("title")
        support_story = str(bug.get("story") or tool_story or "").strip()
        if support_story:
            break
    coordinator_memory = next((dict(memory or {}) for role, _, _, memory in all_rows
                               if role == "qa-coordinator"), {})
    snapshot = _qa_story_snapshot(coordinator_memory)
    planned, statuses = snapshot["planned"], snapshot["statuses"]
    clean_stories = snapshot["clean"]
    blocking_stories = snapshot["blocking"]
    incomplete_stories = snapshot["incomplete"]
    recorded_incomplete = snapshot["recorded_incomplete"]
    missing_stories = snapshot["missing"]
    story_total = len(planned)
    story_clean = len(clean_stories)
    if story_total:
        if missing_stories:
            recorded = story_total - len(missing_stories)
            detail = f"{recorded}/{story_total} current-revision verdicts"
            detail += f" · {story_clean} clean"
            if blocking_stories:
                detail += f" · {len(blocking_stories)} blocking"
            if recorded_incomplete:
                detail += f" · {len(recorded_incomplete)} incomplete"
            detail += f" · {len(missing_stories)} awaiting impact/recheck"
        else:
            detail = f"{story_clean}/{story_total} stories clean"
            if blocking_stories:
                detail += f" · {len(blocking_stories)} blocking"
            if recorded_incomplete:
                detail += f" · {len(recorded_incomplete)} incomplete"
        detail += f" · {done}/{total} explorer sessions complete"
        subprogress_pct = int(round((story_clean / story_total) * 100))
    else:
        detail = f"{done}/{total} QA explorer sessions complete"
        subprogress_pct = int(round((done / total) * 100))
    if outstanding:
        detail += f" · {outstanding} browser session(s) queued/running (max 2)"
    if support_active:
        detail += (f" · repairing/verifying {support_story}" if support_story else "")
        detail += f" · {support_active} repair/verification agent(s) active"
    current = explorer_current or support_current
    return {"qa_run_id": run_id, "qa_campaign_elapsed_s": int(campaign_elapsed_s or 0),
            "qa_explorers_total": total, "qa_explorers_done": done,
            "qa_explorers_outstanding": outstanding, "qa_support_active": support_active,
            "qa_stories_total": story_total, "qa_stories_clean": story_clean,
            "qa_stories_blocking": len(blocking_stories),
            "qa_stories_incomplete": len(incomplete_stories),
            "qa_story_status": {story: statuses.get(story, "missing") for story in planned},
            "current_step": (current or "")[:100],
            "progress_detail": detail, "subprogress_pct": subprogress_pct}


def _headline_progress_pct(phase, time_pct, subprogress):
    """Use release completion—not elapsed-time saturation—for a live QA headline.

    Elapsed/ETA remains useful for detecting a slow worker slice, but it is not campaign completion.  A QA
    slice can hit 99% of its expected runtime with only a few clean stories.  Publishing that time fraction as
    the CEO progress bar is materially misleading, so TESTQA is bounded by its canonical clean-story ratio.
    """
    if phase == "TESTQA" and isinstance(subprogress, dict):
        total = int(subprogress.get("qa_stories_total") or 0)
        release_pct = subprogress.get("subprogress_pct")
        if total > 0 and isinstance(release_pct, int):
            return min(99, max(0, release_pct))
    return time_pct


def live_status(thread_id, tenant_id=None):
    """Truthful live snapshot for the console: what the controller is doing RIGHT NOW. While a durable job
    runs it reports running + elapsed + ETA + a short status (so the UI shows 'Researching… (2m elapsed)');
    on a gate it reports the real awaiting status; and it reports done ONLY when the loop has actually
    reached DELIVER and isn't awaiting anything — never a false 'done' mid-job."""
    with _conn(tenant_id) as c, c.cursor() as cur:
        cur.execute("""SELECT phase, awaiting, job_kind, job_status, job_eta_min,
                              tenant_id, research_run_id,
                              EXTRACT(EPOCH FROM (now()-job_started_at))::int
                       FROM controller_state
                       WHERE thread_id=%s AND (%s::text IS NULL OR tenant_id=%s)""",
                    (thread_id, tenant_id, tenant_id))
        r = cur.fetchone()
    if not r:
        return {"error": "no such thread"}
    phase, awaiting, jk, js, eta, tid, research_run_id, elapsed = r
    running = bool(awaiting == "fleet" and jk)
    out = {"thread_id": thread_id, "phase": phase, "awaiting": awaiting, "running": running, "done": False}
    if running:
        es = int(elapsed or 0)
        em = es // 60
        lo, hi = _eta_range(eta)
        # PROGRESS FRACTION (#2.3): a truthful time-based % of the expected ETA so the console can render a
        # moving progress bar next to the elapsed timer — the CEO can tell PROGRESS from a STALL even while 6
        # agents run in parallel. Capped at 99 while still running (never a premature 100/"done"); once we
        # overrun the ETA it pins near-full and the copy flips to "taking longer than usual".
        pct = min(99, int(round((es / (eta * 60)) * 100))) if eta else None
        overrun = bool(eta and em >= eta)
        eta_txt = (f", {'over the usual' if overrun else 'usually'} ~{lo}-{hi} min" if lo else "")
        base_status = js or _KIND_LABEL.get(jk, "Working…")
        sub = (_research_progress(tid, research_run_id) if phase == "RESEARCH" else
               _qa_progress(thread_id) if phase == "TESTQA" else None)
        out.update(job_kind=jk, elapsed_s=es, elapsed_min=em, eta_min=eta, eta_lo=lo, eta_hi=hi,
                   progress_pct=pct, overrun=overrun, status=base_status,
                   label=f"{base_status} ({em}m elapsed{eta_txt})")
        if sub:
            out.update(sub)
            out["time_progress_pct"] = pct
            out["progress_pct"] = _headline_progress_pct(phase, pct, sub)
            out["label"] += f" · {sub['progress_detail']}"
    elif awaiting in _GATE_LABEL:
        out["status"] = out["label"] = _GATE_LABEL[awaiting]
    else:
        out["done"] = bool(phase == "DELIVER" and not awaiting)
        out["status"] = out["label"] = "Done" if out["done"] else "Ready"
    return out


def _report(tid, thread_id, text, meta=None, urgent=False):
    # Defense-in-depth: control markup is never user-facing. Strip any orphan [[TAG]]/[[/TAG]] markers
    # (e.g. a dangling [[/PLAN]] left when a paired block couldn't be matched) before posting to chat.
    text = re.sub(r"\[\[/?[A-Z][A-Z0-9_]*\]\]", "", text or "").strip()
    orchestrator.post(tid, thread_id, text, meta or {})
    if urgent:
        try:
            import push
            push.send(tid, "Your controller needs you", text[:160], priority="high")
        except Exception:
            pass


def _parse_block(text, tag):
    m = re.search(rf"\[\[{tag}\]\](.*?)\[\[/{tag}\]\]", text or "", re.S | re.I)
    return m.group(1).strip() if m else None


# ── dispatch-and-park: phase work rebuildable from persisted state (overhaul Step 3) ─────────────────
# Each phase's work is reconstructed here from controller_state (product/plan/brief/run-id) instead of being
# captured in an advance()-local closure. A fresh, identity-recorded worker process rebuilds it from `kind`,
# leaving one source of truth and no closure that a driver thread could continue after cancellation.
def _phase_fn(thread_id, kind):
    if kind == "__selftest__":            # trivial, side-effect-free phase used ONLY by _park_selftest
        return lambda: {"ok": True, "parked_selftest": True}
    if kind == "__selftest_slow__":       # ~4s of "work" so the crash harness can kill the driver mid-phase

        def _slow():
            import time
            time.sleep(4)
            return {"ok": True, "parked_selftest": True}
        return _slow
    s = _st(thread_id)
    tid = s["tenant_id"]
    if kind == "research":
        q = (s["brief"] or {}).get("question", "build my product")

        def _do_research():
            import time

            import research as _r
            eng = "orchestra" if _r.orchestra_on() else "fleet"
            started = _r.start(tid, s["org_id"], thread_id, q, engine=eng)
            rid = started.get("run_id")
            if started.get("error"):
                return {"run_id": rid, "status": "failed", "error": started["error"], "options": []}
            _set(thread_id, research_run_id=rid)
            _job_progress(thread_id, "Researching directions…")
            deadline = time.time() + RUNNING_TIMEOUT_MIN * 60
            while time.time() < deadline:
                st = _r.run_state(tid, rid)
                if st["status"] in ("done", "failed"):
                    return {"run_id": rid, "status": st["status"], "options": st.get("options", [])}
                time.sleep(2)
            return {"run_id": rid, "status": "pending", "pending": True}
        return _do_research
    if kind == "design":
        plan = s["plan"] or {}
        product = (s.get("product") or f"{s['org_id'] or 'o'}-{plan.get('name', 'app')}")[:30]

        def _do_proto():
            import design_fleet
            return design_fleet.prototype(tid, str(s["org_id"]), product, plan)
        return _do_proto
    if kind == "build":
        plan = s["plan"] or {}
        product = (s.get("product") or f"{s['org_id'] or 'o'}-{plan.get('name', 'app')}")[:30]

        def _do_build():
            import frontdoor
            frontdoor._own(product, tid)
            charter = plan.get("charter", "build it")
            try:                                          # embed the agentic feature(s) the CEO described
                import agentfeatures
                frag = agentfeatures.charter_for(plan.get("agentic", ""))
                if frag:
                    charter += "\n\n" + frag
            except Exception:
                pass
            platform = plan.get("platform") or ("web" if plan.get("kind") == "web" else "lib")
            if plan.get("kind") == "project":
                import project
                # target the RIGHT stack (web app, python, …) — not always Python. platform drives the
                # architect/builder/integrator language + test runner.
                log = project.build_complex(product, charter, stack=platform)
                return {"product": product, "result": (log or {}).get("result")}
            # SCAFFOLD-THEN-IMPROVE: qualityloop only IMPROVES an existing product, so build from the charter
            # at the registered path first, THEN run the quality loop to raise it to the bar.
            import pathlib

            import productregistry as _preg
            repo = pathlib.Path(_preg.path(product))
            if not repo.exists() or not any(repo.iterdir()):
                # a UI platform (web/game-web) builds the real servable artifact via kind=web
                build_kind = "web" if platform in ("web", "game-web") else plan.get("kind", "web")
                factory.build_product(product, charter, kind=build_kind, tenant_id=tid)
            import qualityloop
            return qualityloop.run(product, bar="high")
        return _do_build
    if kind == "qa":
        product = s.get("product")
        platform = (s.get("plan") or {}).get("platform")
        return lambda: qa_gate(product, platform=platform)
    raise ValueError(f"unknown phase kind for dispatch-and-park: {kind}")


def _rebuild_ctx(thread_id):
    """Reconstruct factory._ctx from persisted state so a PARKED phase in a fresh worker process bills/gates
    with the persisted tenant, org, product, and resolved provider/key. Without this a detached worker would
    silently lose the tenant's BYO key and consent/billing context."""
    s = _st(thread_id)
    factory._ctx.tenant = s["tenant_id"]
    factory._ctx.org = s.get("org_id")
    factory._ctx.product = s.get("product")
    factory._ctx.thread_id = thread_id
    factory._ctx.run = f"controller-{thread_id}"
    factory._ctx.stage = s.get("phase")
    os.environ["AOS_CONTROLLER_THREAD_ID"] = str(thread_id)
    try:
        prov = _resolved_provider(s["tenant_id"])
        if prov:
            _apply_provider_ctx(prov)
    except Exception:
        pass
    return s


# Durable phases always run in a detached, identity-recorded worker process. The old
# AOS_DISPATCH_PARK=0 rollback launched an unkillable daemon thread: cancellation
# could terminalize its DB row while the thread kept making external side effects.
# Keep the public flag for status/tests, but make safe parked execution invariant.
_PARK = True
_DISPATCH_LOCK_NS = 841001
_DISPATCH_GLOBAL_LOCK = 841002
_MAX_ACTIVE_CONTROLLER_JOBS = max(1, int(os.environ.get("AOS_JOBD_MAX_ACTIVE_JOBS", "2")))


def _start_heartbeat(jid):
    """Output-independent liveness beat for one parked worker. Returns its cooperative stop Event."""
    stop = threading.Event()

    def _beat():
        while not stop.wait(HEARTBEAT_S):
            try:
                with _conn() as c, c.cursor() as cur:
                    _set_controller_db_timeouts(cur)
                    cur.execute("UPDATE controller_jobs SET heartbeat_at=now() WHERE id=%s AND status='running'",
                                (jid,))
                    c.commit()
            except Exception:
                pass
    threading.Thread(target=_beat, daemon=True).start()
    return stop


def _finish_job(thread_id, jid, result, status):
    """Write a parked phase worker's terminal outcome without advancing inline.

    A ``pending`` sentinel is left for ``resume_stalled`` to reconcile. The
    terminal update is fenced on ``status='running'`` so cancellation/reaping
    wins, and the durable poller is always the only component that advances.
    """
    if isinstance(result, dict) and result.get("pending"):
        with _conn() as c, c.cursor() as cur:
            _set_controller_db_timeouts(cur)
            cur.execute("UPDATE controller_jobs SET status='pending', result=%s WHERE id=%s AND status='running'",
                        (json.dumps(result), jid))
            c.commit()
        _job_progress(thread_id, "Still working — this one's taking a little longer…")
        return False
    with _conn() as c, c.cursor() as cur:
        _set_controller_db_timeouts(cur)
        cur.execute("""UPDATE controller_jobs SET status=%s, result=%s, finished_at=now()
                       WHERE id=%s AND status='running'""", (status, json.dumps(result), jid))
        changed = cur.rowcount; c.commit()
    if not changed:
        return False
    return True                                          # poller advances under the drive lock


def _spawn_parked_worker(thread_id, kind, jid):
    """Launch the phase in a DETACHED worker process (start_new_session so it survives THIS process's death
    and isn't killed by the driver's signals). Returns whether the durable worker generation launched."""
    try:
        # Keep the worker's own output. It used to go to DEVNULL, so when a parked worker died there was
        # NOTHING to diagnose from — a research run vanished at 23:20 and the only evidence left anywhere
        # was a heartbeat that stopped. A detached worker is precisely the process you cannot watch, so
        # discarding its stderr throws away the one record of why it went. Per-job file, appended, cheap.
        _log_dir = Path(os.environ.get("AOS_WORKER_LOG_DIR", Path(__file__).resolve().parents[1] / "logs" / "workers"))
        try:
            _log_dir.mkdir(parents=True, exist_ok=True)
            _log = open(_log_dir / f"job-{jid}.log", "ab", buffering=0)
        except Exception:
            _log = subprocess.DEVNULL          # never let logging failure block the dispatch
        p = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "run_job", str(thread_id), kind, str(jid)],
            start_new_session=True, stdout=_log, stderr=_log,
            cwd=str(Path(__file__).resolve().parent))
        try:                                             # record the worker pid so parked work is observable
            with _conn() as c, c.cursor() as cur:
                _set_controller_db_timeouts(cur)
                snap = process_assurance.read_snapshot(p.pid)
                cur.execute("""UPDATE controller_jobs SET worker_pid=%s,worker_start_ticks=%s,
                               worker_boot_id=%s WHERE id=%s""",
                            (p.pid, snap.identity.start_ticks if snap else None,
                             snap.identity.boot_id if snap else None, jid))
                c.commit()
        except Exception:
            pass
        return True
    except Exception as e:
        try:
            _trace("dispatch", kind, f"parked-launch failed for thread {thread_id}", str(e)[:200], -1)
        except Exception:
            pass
        return False


def _worker_eta_floor(kind):
    """Minimum truthful ETA a freshly loaded worker may inherit from an older dispatcher."""
    return qatiming.slice_eta_min() if kind == "qa" else None


def _reconcile_worker_eta(thread_id, kind):
    """Repair rolling-upgrade metadata without resetting elapsed time or touching execution ownership."""
    eta_floor = _worker_eta_floor(kind)
    if eta_floor is None:
        return False
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""UPDATE controller_state
                              SET job_eta_min=GREATEST(COALESCE(job_eta_min,0),%s),updated_at=now()
                            WHERE thread_id=%s AND phase='TESTQA' AND awaiting='fleet'
                              AND job_kind='qa' AND COALESCE(job_eta_min,0)<%s
                            RETURNING tenant_id""", (eta_floor, thread_id, eta_floor))
            row = cur.fetchone(); c.commit()
        if row:
            audit.append(actor="loopcontroller", action="RepairStaleQAEta", resource=str(thread_id),
                         decision="reconciled", payload={"eta_min": eta_floor, "source": "worker"})
        return bool(row)
    except Exception:
        # ETA display repair is observability-only; it must never prevent owned phase work from running.
        return False


def run_job(thread_id, kind, jid):
    """Parked-worker entrypoint (`loopcontroller.py run_job <thread_id> <kind> <jid>`): run ONE phase job in
    this dedicated process. Rebuild factory._ctx + the phase fn from persisted state, beat the heartbeat, run
    the work, then write its terminal result. Does NOT advance — the poller does, under the drive lock."""
    _ensure()
    snap = process_assurance.read_snapshot(os.getpid())
    if snap is None:
        return {"thread_id": thread_id, "kind": kind, "jid": jid,
                "status": "refused", "error": "worker birth identity unavailable"}
    with _conn() as c, c.cursor() as cur:
        _set_controller_db_timeouts(cur)
        cur.execute("""UPDATE controller_jobs SET worker_pid=%s,worker_start_ticks=%s,worker_boot_id=%s
                       WHERE id=%s AND thread_id=%s AND kind=%s AND status='running'""",
                    (snap.identity.pid, snap.identity.start_ticks, snap.identity.boot_id,
                     jid, thread_id, kind))
        claimed = cur.rowcount; c.commit()
    if claimed != 1:
        return {"thread_id": thread_id, "kind": kind, "jid": jid,
                "status": "refused", "error": "job is absent, terminal, or belongs to another worker"}
    _reconcile_worker_eta(thread_id, kind)
    _rebuild_ctx(thread_id)
    # Factory/project component threads share this callback. Only a new stage/event/detail signature renews
    # progress, so a repeated "still alive" message cannot masquerade as forward movement.
    progress_seen = set()
    progress_lock = threading.Lock()

    def _progress(stage, event, detail=None):
        signature = (str(stage), str(event), json.dumps(detail, sort_keys=True, default=str)[:1000])
        with progress_lock:
            if signature in progress_seen:
                return
            progress_seen.add(signature)
        _worker_progress(thread_id, jid, stage, event, detail)

    factory._ctx.progress_callback = _progress
    _progress(kind.upper(), "worker-started", {"jid": jid})
    stop = _start_heartbeat(jid)
    result, status = {}, "done"
    try:
        result = _phase_fn(thread_id, kind)() or {}
    except Exception as e:
        result, status = {"error": str(e)[:200]}, "failed"
    finally:
        stop.set()
    _finish_job(thread_id, jid, result, status)
    return {"thread_id": thread_id, "kind": kind, "jid": jid, "status": status}


def _pid_alive(pid, start_ticks=None, boot_id=None):
    if not pid:
        return None
    snap = process_assurance.read_snapshot(int(pid))
    if snap is None:
        return False
    if snap.state == "Z":
        return False
    if start_ticks is None or not boot_id:
        return None                                      # legacy bare PID exists, but ownership is unprovable
    expected = process_assurance.ProcessIdentity(int(pid), int(start_ticks), str(boot_id))
    return process_assurance.same_process(expected, snap)


def park_status():
    """Operator view of in-flight phase work — the observability that makes dispatch-and-park safe to run:
    every running/pending job with its age, heartbeat freshness, worker pid, and whether that worker process
    is actually alive. ``parked=False`` identifies a legacy/unproven row with no recorded worker identity."""
    _ensure()
    rows = []
    with _conn() as c, c.cursor() as cur:
        cur.execute("""SELECT id, thread_id, phase, kind, status, worker_pid,
                              worker_start_ticks,worker_boot_id,
                              EXTRACT(EPOCH FROM now()-started_at)::int,
                              EXTRACT(EPOCH FROM now()-COALESCE(heartbeat_at, started_at))::int
                       FROM controller_jobs WHERE status IN ('running','pending')
                       ORDER BY started_at""")
        for jid, tid, phase, kind, st, pid, start_ticks, boot_id, age, beat in cur.fetchall():
            rows.append({"job": jid, "thread": tid, "phase": phase, "kind": kind, "status": st,
                         "worker_pid": pid, "worker_start_ticks": start_ticks,
                         "worker_boot_id": boot_id, "parked": pid is not None,
                         "worker_alive": _pid_alive(pid, start_ticks, boot_id),
                         "age_s": age, "heartbeat_age_s": beat})
    return {"park_mode": _PARK, "in_flight": rows}


def _worker_tool_leases(pid):
    """Return durable long-tool leases owned by one parked worker process.

    A provider/browser child proves obvious activity, but a tool thread can briefly be child-free while it
    hashes a diff, updates a checkpoint, or transitions between model stages. Treat its fenced lease as the
    stronger quiescence signal so rolling source deployment cannot land in that unsafe gap.
    """
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""SELECT lease_key,actor_id,tool,lease_until
                             FROM orchestra_tool_leases
                            WHERE owner_id LIKE %s AND lease_until>now()
                            ORDER BY actor_id,lease_key LIMIT 20""", (f"{int(pid)}-%",))
            return [{"lease_key": row[0], "actor_id": row[1], "tool": row[2],
                     "lease_until": str(row[3])} for row in cur.fetchall()]
    except Exception:
        # Older installations may not have the lease table yet. Descendant fencing remains the compatibility
        # behavior; current installations fail closed through the durable rows above.
        return []


def _worker_orchestra_claims(pid):
    """Return actor/event step claims owned by one parked worker process.

    A coordinator decision runs inside the controller process itself, so it can be child-free and hold no
    long-tool lease while it is still applying a durable event.  Killing it in that window preserves the event
    but strands the actor-step claim for the normal crash lease (15 minutes today).  More importantly, it is not
    actually a quiescent handoff boundary.  Fence on both claim types just as strictly as on child processes and
    tool leases; the normal runtime releases them immediately after committing the step.
    """
    try:
        owner_prefix = f"{int(pid)}-%"
        with _conn() as c, c.cursor() as cur:
            cur.execute("""SELECT actor_id,step_claimed_by,step_claimed_at
                             FROM orchestra_actors
                            WHERE step_claimed_by LIKE %s
                            ORDER BY actor_id LIMIT 20""", (owner_prefix,))
            actor_steps = [{"actor_id": row[0], "claimed_by": row[1], "claimed_at": str(row[2])}
                           for row in cur.fetchall()]
            cur.execute("""SELECT id,to_actor,kind,claimed_by,claimed_at
                             FROM orchestra_events
                            WHERE claimed_by LIKE %s AND processed_at IS NULL
                            ORDER BY id LIMIT 20""", (owner_prefix,))
            events = [{"event_id": row[0], "actor_id": row[1], "kind": row[2],
                       "claimed_by": row[3], "claimed_at": str(row[4])}
                      for row in cur.fetchall()]
            return {"actor_steps": actor_steps, "events": events}
    except Exception:
        # Compatibility with installations that predate durable orchestra step claims. Descendant and tool
        # lease fencing remain available there; current installations fail closed through these rows too.
        return {"actor_steps": [], "events": []}


def controlled_handoff(jid, reason="rolling runtime upgrade"):
    """Fence and stop one exact parked worker only at a proven child-free boundary.

    This is the deploy-safe alternative to an ad-hoc ``kill``. It never interrupts a provider/browser child,
    binds authority to boot-id/PID/start-ticks, freezes the root to close the spawn race, commits a durable
    non-crash handoff marker, and then terminates only that exact generation. ``resume_stalled``/jobd reloads
    the same phase from its checkpoint using current source.
    """
    _ensure()
    with _conn() as c, c.cursor() as cur:
        cur.execute("""SELECT id,thread_id,phase,status,worker_pid,worker_start_ticks,worker_boot_id
                         FROM controller_jobs WHERE id=%s""", (int(jid),))
        row = cur.fetchone()
    if not row:
        return {"handoff": False, "reason": "job not found", "job_id": int(jid)}
    job_id, thread_id, phase, status, pid, start_ticks, boot_id = row
    if status != "running" or not pid or start_ticks is None or not boot_id:
        return {"handoff": False, "reason": "job is not an identity-bound running worker",
                "job_id": job_id, "status": status}
    identity = process_assurance.ProcessIdentity(int(pid), int(start_ticks), str(boot_id))
    snapshots = process_assurance.scan_snapshots()
    if not process_assurance.same_process(identity, snapshots.get(identity.pid)):
        return {"handoff": False, "reason": "worker identity is no longer live", "job_id": job_id}
    descendants = process_assurance.descendant_pids(identity, snapshots)
    if descendants:
        return {"handoff": False, "reason": "worker is not quiescent", "job_id": job_id,
                "active_descendants": descendants[:20]}
    active_claims = _worker_orchestra_claims(identity.pid)
    if active_claims["actor_steps"] or active_claims["events"]:
        return {"handoff": False, "reason": "worker has active durable orchestra claims",
                "job_id": job_id, "active_orchestra_claims": active_claims}
    active_leases = _worker_tool_leases(identity.pid)
    if active_leases:
        return {"handoff": False, "reason": "worker has active durable tool work", "job_id": job_id,
                "active_tool_leases": active_leases}

    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_signal = getattr(signal, "pidfd_send_signal", None)
    if pidfd_open is None or pidfd_signal is None:
        return {"handoff": False, "reason": "race-safe pidfd signaling is unavailable", "job_id": job_id}
    try:
        pidfd = pidfd_open(identity.pid, 0)
    except (OSError, ValueError) as exc:
        return {"handoff": False, "reason": f"could not bind worker pidfd: {exc}", "job_id": job_id}
    stopped = False
    try:
        if not process_assurance.same_process(identity, process_assurance.read_snapshot(identity.pid)):
            return {"handoff": False, "reason": "worker identity changed before freeze", "job_id": job_id}
        pidfd_signal(pidfd, signal.SIGSTOP)
        stopped = True
        time.sleep(0.1)
        frozen = process_assurance.scan_snapshots()
        if not process_assurance.same_process(identity, frozen.get(identity.pid)):
            return {"handoff": False, "reason": "worker exited during freeze", "job_id": job_id}
        descendants = process_assurance.descendant_pids(identity, frozen)
        if descendants:
            pidfd_signal(pidfd, signal.SIGCONT)
            stopped = False
            return {"handoff": False, "reason": "worker became busy before freeze", "job_id": job_id,
                    "active_descendants": descendants[:20]}
        active_claims = _worker_orchestra_claims(identity.pid)
        if active_claims["actor_steps"] or active_claims["events"]:
            pidfd_signal(pidfd, signal.SIGCONT)
            stopped = False
            return {"handoff": False, "reason": "worker claimed durable orchestra work before freeze",
                    "job_id": job_id, "active_orchestra_claims": active_claims}
        active_leases = _worker_tool_leases(identity.pid)
        if active_leases:
            pidfd_signal(pidfd, signal.SIGCONT)
            stopped = False
            return {"handoff": False, "reason": "worker claimed durable tool work before freeze",
                    "job_id": job_id, "active_tool_leases": active_leases}
        marker = {"error": "controlled rolling-runtime handoff", "status": "failed",
                  "controlled_handoff": True, "job_id": job_id, "reason": str(reason)[:300]}
        with _conn() as c, c.cursor() as cur:
            cur.execute("""UPDATE controller_jobs
                              SET status='failed', lease_token=lease_token+1,
                                  result=COALESCE(result,'{}'::jsonb)||%s::jsonb, finished_at=now()
                            WHERE id=%s AND status='running' AND worker_pid=%s
                              AND worker_start_ticks=%s AND worker_boot_id=%s""",
                        (json.dumps(marker), job_id, identity.pid, identity.start_ticks, identity.boot_id))
            changed = cur.rowcount
            c.commit()
        if changed != 1:
            pidfd_signal(pidfd, signal.SIGCONT)
            stopped = False
            return {"handoff": False, "reason": "job ownership changed before durable fence", "job_id": job_id}
        pidfd_signal(pidfd, signal.SIGKILL)
        stopped = False
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and process_assurance.same_process(
                identity, process_assurance.read_snapshot(identity.pid)):
            time.sleep(0.05)
        gone = not process_assurance.same_process(identity, process_assurance.read_snapshot(identity.pid))
        audit.append(actor="loopcontroller", action="ControlledRuntimeHandoff", resource=str(thread_id),
                     decision=phase, payload={"job_id": job_id, "worker": identity.token(),
                                              "worker_gone": gone, "reason": str(reason)[:300]})
        return {"handoff": True, "job_id": job_id, "thread_id": thread_id, "phase": phase,
                "worker_gone": gone, "worker": identity.token()}
    except OSError as exc:
        return {"handoff": False, "reason": f"handoff signal failed: {exc}", "job_id": job_id}
    finally:
        if stopped and process_assurance.same_process(identity, process_assurance.read_snapshot(identity.pid)):
            try:
                pidfd_signal(pidfd, signal.SIGCONT)
            except OSError:
                pass
        os.close(pidfd)


def _dispatch(thread_id, kind, fn=None, eta_min=None, kickoff=None, status=None):
    """Atomically claim a durable phase and launch its identity-recorded worker process.

    On kickoff it (a) stamps the LIVE-PROGRESS fields (kind/ETA/status) so the console shows a ticking
    bubble, and (b) optionally posts a forward-looking 'working… (~N min)' message so the CEO sees an ETA
    the instant async work starts — never a silent stall."""
    # Retained for source compatibility with older callers/tests. Captured
    # closures are intentionally never executed; the worker rebuilds by `kind`.
    del fn
    # Read the phase under the same finite statement bound as the ownership
    # transaction. In particular, an ACCESS EXCLUSIVE migration lock must not
    # pin the controller before it reaches its advisory-lock guard.
    try:
        with _conn() as c, c.cursor() as cur:
            _set_controller_db_timeouts(cur)
            cur.execute("""SELECT tenant_id,phase,plan,execution_scope FROM controller_state
                            WHERE thread_id=%s""", (thread_id,))
            state_row = cur.fetchone()
    except _RETRYABLE_CONTROLLER_DB_ERRORS:
        return None
    if not state_row:
        return None
    s = {"tenant_id": state_row[0], "phase": state_row[1], "plan": state_row[2],
         "execution_scope": state_row[3]}
    if eta_min is None:
        eta_min = _estimate_runtime(s["phase"], s.get("plan"))
    try:
        with _conn() as c, c.cursor() as cur:
            _set_controller_db_timeouts(cur)
            # Capacity is an execution invariant, not merely a jobd scheduling hint.
            # Serialize every direct/completion/recovery dispatch across processes,
            # then count the durable active rows while holding that bounded lock.
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (_DISPATCH_GLOBAL_LOCK,))
            # The outer session drive lock is the normal owner. This transaction
            # lock makes the SELECT→INSERT guard atomic even for recovery/admin
            # callers that enter here without it.
            cur.execute("SELECT pg_advisory_xact_lock(%s,%s)",
                        (_DISPATCH_LOCK_NS, int(thread_id)))
            # SINGLE-WRITER AT DISPATCH (overhaul Step 2, defense-in-depth): never create a SECOND running job for
            # a thread that already has one in flight.
            cur.execute("""SELECT id FROM controller_jobs
                           WHERE thread_id=%s AND status IN ('running','pending') LIMIT 1""", (thread_id,))
            if cur.fetchone():
                return None                          # already in flight; duplicate attempt is a no-op
            cur.execute("""SELECT count(*) FROM controller_jobs
                           WHERE status IN ('running','pending') AND execution_scope=%s""",
                        (s["execution_scope"],))
            if int(cur.fetchone()[0]) >= _MAX_ACTIVE_CONTROLLER_JOBS:
                return None                          # durable queue; jobd retries when capacity opens

            # Publish the fleet gate/progress and its owning job in ONE transaction.
            # A lock/statement timeout rolls both back, so no ownerless 'running'
            # row or fleet gate can survive a deferred dispatch.
            cur.execute("""UPDATE controller_state
                              SET awaiting='fleet',job_kind=%s,job_started_at=now(),job_eta_min=%s,
                                  job_status=%s,job_sla_warned=false,job_sla_claimed_at=NULL,
                                  job_sla_claim_token=NULL,updated_at=now()
                            WHERE thread_id=%s AND awaiting IS NULL""",
                        (kind, eta_min, status or _KIND_LABEL.get(kind, "Working…"), thread_id))
            if cur.rowcount != 1:
                return None                          # another owner changed the durable phase/gate
            cur.execute("""INSERT INTO controller_jobs
                              (thread_id, tenant_id, phase, kind, heartbeat_at, progress_at, execution_scope)
                           VALUES (%s,%s,%s,%s,now(),now(),%s) RETURNING id""",
                        (thread_id, s["tenant_id"], s["phase"], kind, s["execution_scope"]))
            jid = cur.fetchone()[0]
            c.commit()
    except _RETRYABLE_CONTROLLER_DB_ERRORS:
        # The transaction context rolls back every statement. Returning None is
        # the controller's existing retryable/queued outcome: awaiting remains
        # NULL and jobd will attempt the same durable phase on its next tick.
        return None
    # Run the phase only in a detached, identity-recorded process. A launch
    # failure is a durable crashed generation; recovery may retry it, but no
    # unkillable in-process thread is allowed to outlive a terminal DB row.
    if not _spawn_parked_worker(thread_id, kind, jid):
        _finish_job(thread_id, jid,
                    {"error": "detached worker launch failed", "status": "failed", "crashed": True},
                    "failed")
        return jid
    # Notification/storage failure must never sit between durable ownership and
    # worker launch. The phase is already safely running; the poller remains the
    # authoritative progress path if this best-effort kickoff cannot be posted.
    if kickoff:
        try:
            rng = _eta_phrase(eta_min)
            eta_txt = f" ({rng})" if rng else ""
            _report(s["tenant_id"], thread_id, kickoff + eta_txt,
                    {"kind": "working", "phase": s["phase"], "job": kind, "eta_min": eta_min})
        except Exception:
            pass
    return jid


def start(tid, org_id, *, execution_scope="production"):
    if execution_scope not in {"production", "test"}:
        raise ValueError("execution_scope must be 'production' or 'test'")
    _ensure()
    thread_id = orchestrator.start_thread(tid)
    with _conn() as c, c.cursor() as cur:
        cur.execute("""INSERT INTO controller_state
                          (thread_id, tenant_id, org_id, phase, awaiting, execution_scope)
                       VALUES (%s,%s,%s,'DISCOVER','user_feedback',%s)
                       ON CONFLICT (thread_id) DO NOTHING""",
                    (thread_id, tid, org_id, execution_scope))
        c.commit()
    try:
        import workstreamspine
        workstreamspine.ensure_workstream(
            tid, f"controller:{thread_id}",
            f"Deliver the product workstream owned by controller thread {thread_id}",
            {"grounded_qa_passed": "qa_ok must be true",
             "no_blocking_findings": "blocking_open must equal zero",
             "nonempty_test_coverage": "at least one grounded story must be exercised"},
            accountable_owner="build-team", manager_owner="controller",
            backup_owner="qa-director", created_by="loopcontroller", org_id=org_id,
            risk="high", update_cadence_s=900, execution_scope=execution_scope)
    except Exception as exc:
        # The organization spine is additive observability/acceptance state. A
        # migration outage must not prevent the CEO from opening a workstream.
        audit.append(actor="loopcontroller", action="WorkstreamSpineSeed",
                     resource=str(thread_id), decision="deferred",
                     payload={"error": str(exc)[:300]}, tenant_id=tid)
    _report(tid, thread_id, "I'm your controller. Tell me what you want to build — e.g. \"a competitor to "
                            "YouTube\" — and I'll ask a couple of questions, research it, and bring you a plan.")
    audit.append(actor="loopcontroller", action="ControllerStart", resource=str(thread_id), decision="DISCOVER",
                 payload={"org": org_id})
    return {"thread_id": thread_id, "phase": "DISCOVER"}


MAX_WORKSTREAMS = int(os.environ.get("AOS_MAX_WORKSTREAMS", "8"))   # cap concurrent loops per org


def workstreams(tid, org_id):
    """REBUILD-PLAN A2: a real company runs SEVERAL workstreams at once (build one product, iterate a v2,
    run ops) — not one at a time. Every in-flight controller thread for this org, with its live state, so
    the CEO can see + switch between concurrent workstreams. The default thread (thread_for_org) is just
    the first of these."""
    _ensure()
    with _conn() as c, c.cursor() as cur:
        cur.execute("""SELECT thread_id, phase, product, awaiting, job_kind, job_status
                       FROM controller_state WHERE tenant_id=%s AND org_id=%s ORDER BY thread_id""",
                    (tid, org_id))
        return [{"thread_id": t, "phase": ph, "product": pr, "awaiting": aw,
                 "running": aw == "fleet", "job": jk, "status": js}
                for t, ph, pr, aw, jk, js in cur.fetchall()]


def new_workstream(tid, org_id):
    """Start a NEW parallel workstream (controller thread) for the org — the company takes on another
    concurrent effort. Capped (AOS_MAX_WORKSTREAMS) so an org can't spawn unbounded controller loops."""
    if len(workstreams(tid, org_id)) >= MAX_WORKSTREAMS:
        return {"error": f"workstream limit reached ({MAX_WORKSTREAMS}) — finish or archive one first"}
    r = start(tid, org_id)
    return {"thread_id": r["thread_id"], "phase": r["phase"], "created": True}


def thread_for_org(tid, org_id):
    """The org's DEFAULT controller thread (the first workstream) — create it on first access. Additional
    concurrent workstreams are created via new_workstream() and listed by workstreams()."""
    _ensure()
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT thread_id FROM controller_state WHERE tenant_id=%s AND org_id=%s ORDER BY thread_id LIMIT 1",
                    (tid, org_id))
        r = cur.fetchone()
    if r:
        return r[0]
    return start(tid, org_id)["thread_id"]


def _decision_brief(s):
    """The work-so-far the planning phase is supposed to build on: the ORIGINAL ask, the direction the CEO
    picked, and the headline findings behind it.

    None of this reached the model before. _llm sends the system prompt, the last 12 chat turns, and
    _ctx_brief — which returns '' whenever org_id is falsy. The substance lives in controller_state
    (chosen_option, brief) and in the research report, not in the chat text, so after picking an option the
    planner saw only "Great — going with that direction." / "go ahead" and said so out loud: "I don't have
    visibility into the direction you just chose — the earlier research options and your selection didn't
    come through in my context." It then invented an unrelated product (a personal-finance app, for a
    directive about AWS cost reduction). Losing the decision between phases turns a research run the CEO
    paid for into confident fiction."""
    bits = []
    try:
        brief = s.get("brief") or {}
        q = (brief.get("question") or brief.get("vision") or "").strip() if isinstance(brief, dict) else ""
        if q:
            bits.append("ORIGINAL ASK: " + q[:400])
    except Exception:
        pass
    try:
        ch = s.get("chosen_option") or {}
        if isinstance(ch, dict) and ch.get("title"):
            bits.append("DIRECTION THE CEO CHOSE: " + str(ch["title"])
                        + ((" — " + str(ch.get("summary"))[:400]) if ch.get("summary") else ""))
    except Exception:
        pass
    rep = _research_headline(s)
    if rep:
        bits.append("WHAT THE RESEARCH FOUND (headline):\n" + rep)
    if not bits:
        return ""
    return ("\n\nWORK SO FAR — build on THIS; do not ask the CEO to restate it:\n"
            + "\n\n".join(bits))


def _research_headline(s, max_chars=1800):
    """The lead section of the run's report, if it produced one. Bounded: enough to ground the plan in what
    was actually found, small enough not to crowd the prompt."""
    try:
        rid = s.get("research_run_id")
        if not rid:
            return ""
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT report_path FROM research_runs WHERE id=%s AND status='done'", (int(rid),))
            row = cur.fetchone()
        if not row or not row[0]:
            return ""
        from pathlib import Path as _P
        txt = _P(row[0]).read_text(errors="replace")
        # take the report's own lead section rather than a blind head(): reports start with a title block
        # then '## 1. Key findings'.
        i = txt.find("## 1.")
        if i == -1:
            i = 0
        return txt[i:i + max_chars].strip()
    except Exception:
        return ""


def _ctx_brief(s):
    try:
        import orgs
        return orgs.context_brief(s["tenant_id"], s["org_id"]) if s.get("org_id") else ""
    except Exception:
        return ""


def _resolved_provider(tid):
    """A tenant has a USABLE model provider when they've connected one with a key, OR signed in via a
    SUBSCRIPTION login (runs on the host CLI's own account — no key needed). Nothing connected -> None,
    so we refuse BEFORE any spend instead of silently billing the platform default. (Mirrors the
    PLAN_APPROVAL credential check at line ~399 — applied up front so no _llm/fan-out runs un-provided.)"""
    try:
        import tenantproviders
        r = tenantproviders.resolve(tid)
    except Exception:
        return None
    return r if (r.get("key") or r.get("auth_mode") in {"subscription", "default_cli"}) else None


def _ceo_context(tid, org_id=None):
    """The CEO's STANDING vision + refined requirements (visionkeeper), folded into scoping so the controller
    works from the CEO's intent instead of pestering for requirements — and is reminded to surface human
    prerequisites UP FRONT. Fail-open and NO spend (allow_refine=False): a missing keeper yields ''."""
    try:
        import visionkeeper
        v = visionkeeper.requirements_for_controller(tid, org_id=org_id, allow_refine=False)
    except Exception:
        return ""
    sv = (v.get("standing_vision") or "").strip()
    if not sv:
        return ""
    reqs = v.get("requirements") or {}
    out = f"\n\nCEO STANDING VISION (factor this — do NOT re-ask what it already answers): {sv}"
    goals = "; ".join((reqs.get("goals") or [])[:5])
    pre = "; ".join(p.get("item", "") for p in (reqs.get("prerequisites") or [])[:6])
    if goals:
        out += f"\nKnown goals: {goals}"
    if pre:
        out += f"\nHuman prerequisites to surface up front (don't discover them mid-build): {pre}"
    return out


def _apply_provider_ctx(r):
    """Wire the tenant's resolved provider into factory._ctx so model spend lands on THEIR account
    (engine + key), not the platform default — mirrors factory.build_product's engine routing. This
    SUPERSEDES the always-None `factory._ctx.api_key = api_key` set earlier in say()."""
    if (r or {}).get("engine") == "codex":
        factory._ctx.engine, factory._ctx.codex_key, factory._ctx.api_key = "codex", r.get("key"), None
    else:
        factory._ctx.engine, factory._ctx.api_key, factory._ctx.codex_key = "claude", (r or {}).get("key"), None


def _research_report_for_run(tid, run_id):
    """The synthesized research REPORT markdown (+ status + option cards) for a specific run — the raw doc
    behind the distilled option chips, so the console can show "what the research actually found", not just
    the titles. Reads research_runs.report_path off disk; best-effort, never raises. Empty report string if
    the run hasn't written its doc yet (or the file is gone)."""
    report, status, options = "", None, []
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT status, report_path FROM research_runs WHERE id=%s AND tenant_id=%s",
                        (run_id, tid))
            r = cur.fetchone()
        if r:
            status, path = r
            if path:
                try:
                    report = Path(path).read_text()
                except Exception:
                    report = ""
    except Exception:
        pass
    try:
        import research as _research
        options = (_research.run_state(tid, run_id) or {}).get("options", []) or []
    except Exception:
        options = []
    return {"run_id": run_id, "status": status, "report": report, "options": options}


def research_report(tid, org):
    """EXPOSE THE RESEARCH DOC (console surface): the CURRENT research run's report markdown for an org's
    controller thread — so the UI can render the full research document, not only the option cards. Returns
    {run_id, status, report, options}; `report` is the markdown ('' if not written yet). Best-effort; never
    raises. (Distinct from the distilled option summaries — this is the whole synthesized report.)"""
    _ensure()
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""SELECT research_run_id FROM controller_state
                           WHERE tenant_id=%s AND org_id=%s AND research_run_id IS NOT NULL
                           ORDER BY thread_id DESC LIMIT 1""", (tid, org))
            row = cur.fetchone()
    except Exception as e:
        return {"run_id": None, "status": None, "report": "", "options": [], "error": str(e)[:200]}
    if not row or not row[0]:
        return {"run_id": None, "status": None, "report": "", "options": []}
    return _research_report_for_run(tid, row[0])


def _enrich_options(opts, report_text=""):
    """Guarantee EVERY option card carries a non-empty summary/detail so the console renders CONTENT, not a
    bare title (#2). Research options already ship a `summary`; for any that don't, backfill from the
    option's own rationale/detail/description, else a snippet of the research report — never a blank card."""
    head = " ".join((report_text or "").split())[:240].strip()
    out = []
    for o in opts:
        if not isinstance(o, dict):
            out.append(o)
            continue
        o = dict(o)
        if not (o.get("summary") or "").strip():
            o["summary"] = ((o.get("rationale") or o.get("detail") or o.get("description") or "").strip()
                            or ((head + "…") if head else "")
                            or "See the full research report for details.")
        out.append(o)
    return out


def _answer_at_options(tid, thread_id, s, msg, on_delta=None):
    """ELABORATE at OPTIONS (#1): the CEO typed a QUESTION about the options/research instead of picking —
    answer it directly from the research report + option cards (compare, explain, recommend), never force a
    pick. Routes through _llm (fast model) with the report + options threaded in as context."""
    opts = s.get("options") or []
    report = ""
    if s.get("research_run_id"):
        try:
            report = (_research_report_for_run(tid, s["research_run_id"]).get("report") or "")[:6000]
        except Exception:
            report = ""
    opt_lines = []
    for i, o in enumerate(opts, 1):
        if isinstance(o, dict):
            rec = " (recommended)" if o.get("recommended") else ""
            opt_lines.append(f"{i}. {o.get('title', '')}{rec} — {o.get('summary', '')}")
    sysp = ("The CEO is looking at the research-backed OPTIONS below and asked a question ABOUT them (to "
            "compare, understand, or dig into the research) — answer it directly and concretely FROM the "
            "research, in a few sentences. Do NOT choose for them unless they explicitly ask you to pick. "
            "End by gently reminding them they can tap an option (or say e.g. \"option 2\") whenever they're "
            "ready — no pressure.\n\n"
            f"OPTIONS:\n{chr(10).join(opt_lines) or '(no options)'}\n\n"
            f"RESEARCH REPORT (excerpt):\n{report or '(report not available; answer from the options above)'}")
    return _llm(tid, thread_id, sysp, s, on_delta=on_delta)


def _agentic_lifecycle_decision(tid, thread_id, decision_type, state, revision, *,
                                authority_kind="business", default=None):
    """Route a reversible lifecycle choice through durable line management.

    A temporary decision-service failure keeps the transition autonomous by
    taking the explicit bounded default. Dangerous boundaries are still owned
    by consent/provider/spend gates and ``authority`` itself.
    """
    fallback = default or {"action": "proceed", "confidence": 1.0,
                           "rationale": "bounded reversible lifecycle default"}
    try:
        import decisionchain
        corr = decisionchain.stable_correlation(thread_id, decision_type, revision)
        return decisionchain.decide(
            tid, thread_id, f"controller:{thread_id}:{decision_type}", decision_type, state,
            correlation_id=corr, authority_kind=authority_kind, default=fallback)
    except Exception as exc:
        audit.append(actor="loopcontroller", action="AgenticDecisionFallback",
                     resource=str(thread_id), decision=decision_type,
                     payload={"error": str(exc)[:300], "default": fallback}, tenant_id=tid)
        if fallback.get("action") == "request_human" and fallback.get("boundary"):
            try:
                import authority
                corr = f"controller-fallback:{thread_id}:{decision_type}:{revision}"
                routed = authority.open_decision(
                    tid, f"controller:{thread_id}:{decision_type}", fallback["boundary"],
                    {"question": fallback.get("rationale"), "reason": fallback.get("rationale"),
                     "confidence": 1.0, "amount_usd": state.get("amount_usd", 0),
                     "campaign_spent_usd": state.get("campaign_spent_usd", 0),
                     "management_exhausted": True,
                     "requires_ceo_business_judgment": fallback.get("boundary") == "business"},
                    correlation_id=corr, thread_id=thread_id, owner_role="senior-product-director")
                if routed.get("disposition") == "human_required":
                    return {"status": "human_wait", "action": "request_human",
                            "boundary": fallback["boundary"], "authority_decision_id": routed.get("id"),
                            "rationale": routed.get("reason"), "decided_by": "senior-product-director"}
                if routed.get("disposition") == "delegated":
                    return {"status": "resolved", "action": "continue", "boundary": "none",
                            "rationale": routed.get("reason"), "decided_by": "senior-product-director"}
            except Exception:
                pass
        return {"status": "resolved", "decided_by": "controller-recovery",
                "tier": 2, "fallback": True, "boundary": "none", **fallback}


def _agentic_plan_review(tid, thread_id, state):
    """Review a durable plan internally; only a typed authority gap parks it."""
    plan = state.get("plan") or {}
    revision = json.dumps(plan, sort_keys=True, default=str)
    decision = _agentic_lifecycle_decision(
        tid, thread_id, "plan_acceptance",
        {"plan": plan, "chosen_option": state.get("chosen_option"),
         "allowed_actions": ["proceed", "revise", "request_human"]}, revision,
        default={"action": "proceed", "confidence": 1.0,
                 "rationale": "senior product director accepted the reversible implementation plan"})
    if decision.get("status") == "human_wait":
        _set(thread_id, awaiting="user_feedback")
        _report(tid, thread_id,
                "The product team finished its plan review and found one decision outside standing authority. "
                "A specific request is waiting for your answer; the plan is safely checkpointed.",
                {"kind": "authority_required", "decision": decision, "plan": plan}, urgent=True)
        return {"phase": "DEEP_DESIGN", "awaiting": "user_feedback", "decision": decision}
    if decision.get("action") == "revise":
        note = str(decision.get("rationale") or "internal plan review requested revisions")
        _set(thread_id, plan=None, awaiting=None,
             pending_intent=((state.get("pending_intent") or "") + "\n" + note).strip())
        _report(tid, thread_id, "The plan review found improvements, so the team is revising it internally.",
                {"kind": "internal_plan_revision", "decision": decision}, urgent=False)
        _advance_owned(thread_id)
        return {"phase": "DEEP_DESIGN", "revising": True, "decision": decision}
    _set(thread_id, awaiting=None)
    _to(thread_id, "PLAN_APPROVAL")
    _report(tid, thread_id,
            f"The plan was approved by {decision.get('decided_by') or 'the product team'} and is moving forward. "
            "You can still steer or cancel at any time.",
            {"kind": "internal_plan_approved", "decision": decision, "plan": plan}, urgent=False)
    _advance_owned(thread_id)
    return {"phase": "PLAN_APPROVAL", "advanced": True, "decision": decision}


def say(tid, thread_id, msg, api_key=None, on_delta=None, _internal=False):
    # on_delta (optional): a token sink the console's SSE endpoint passes in to STREAM the conversational
    # reply live. It's threaded only into the free-text LLM turns (DISCOVER clarify, DEEP_DESIGN plan draft,
    # generic answer); the fixed-string gate/status/affirmative branches never stream (nothing to stream) and
    # behave exactly as before. The persisted thread, block parsing and phase advance are all unchanged —
    # streaming is a pure live-preview overlay on top of the SAME say() control flow.
    _ensure()
    s = _st(thread_id)
    if not s:
        return {"error": "no such controller thread"}
    # SUPPRESS DUPLICATE identical user messages (#2.1): a double-tapped send / retried request must not be
    # stored twice (polluting the model transcript) nor earn a second identical reply. `dup` is True when this
    # turn repeats the immediately-prior user message; _store_user no-ops the duplicate insert.
    # Internal worker/manager turns use the same guarded lifecycle logic without
    # impersonating the CEO in the conversation transcript.
    dup = False if _internal else not _store_user(tid, thread_id, msg)
    phase = s["phase"]
    factory._ctx.api_key = api_key
    factory._ctx.tenant = tid          # lets factory.agent enforce the consent gate as a backstop (defense-in-depth)
    factory._ctx.org = s.get("org_id") # MEMORY SPINE (A3): scope company-memory injection to this org
    factory._ctx.run = f"controller-{thread_id}"
    factory._ctx.thread_id = thread_id
    factory._ctx.stage = phase
    factory._ctx.product = s.get("product")

    # CONSENT GATE (EU AI Act Art.50 / Apple 5.1.2(i) / Play AI policy): the controller's whole job is AI work —
    # every phase either sends the CEO's text to the provider (_llm) or fans out paid agent work. Refuse BEFORE
    # any of that, so we never send a single word to the model pre-consent (this is the "gate before the LLM"
    # layer; research.start() + factory.agent enforce the same downstream). The user accepts consent in Settings
    # out-of-band, then any message re-enters here and proceeds. Fail CLOSED (legal gate): a consent-infra error
    # is treated as "not on file" rather than waved through.
    try:
        import consent
        consented = consent.require_consent(tid)
    except Exception:
        consented = False
    if not consented:
        _report(tid, thread_id,
                 "Before I can research or build anything I need your OK to use AI: please accept the "
                 "AI-processing consent in Settings → Privacy (it names the provider your text is sent to), "
                 "then say \"ready\" and we'll get going.",
                 {"kind": "consent_required", "phase": phase}, urgent=True)
        audit.append(actor="loopcontroller", action="ConsentRequired", resource=str(thread_id),
                     decision=phase, payload={"tenant": tid})
        return {"phase": phase, "blocked": "consent_required"}

    # PROVIDER GATE (spend lands on the TENANT, not the platform): every phase below either sends the CEO's
    # text to the model (_llm) or fans out paid agent work. AFTER consent, require a RESOLVED provider — a
    # connected key, or a subscription login that runs on the CLI's own account — BEFORE any spend, and wire
    # it into factory._ctx so the cost is billed to THEIR account. Fail CLOSED: nothing connected -> ask them
    # to connect one in Settings; they say "ready" and the message re-enters here and proceeds.
    prov = _resolved_provider(tid)
    if not prov:
        _report(tid, thread_id,
                 "Before I can research or build I need a model provider connected — add Anthropic or "
                 "OpenAI/Codex in Settings → Providers (or sign in with your subscription), then say "
                 "\"ready\" and I'll pick up right where we left off.",
                 {"kind": "provider_required", "phase": phase}, urgent=True)
        audit.append(actor="loopcontroller", action="ProviderRequired", resource=str(thread_id),
                     decision=phase, payload={"tenant": tid})
        return {"phase": phase, "blocked": "provider_required"}
    _apply_provider_ctx(prov)

    # STATUS HONESTY (#2.1): a message typed WHILE a durable job is in flight must NEVER reach a free-form
    # LLM answer — that's the fall-through where the model hallucinated a confident "Done — research written
    # to…" while the real run was still going. Reply with the TRUE live status (running + elapsed + ETA) and
    # stop; the actual results post themselves (with a ping) when the job genuinely finishes.
    if s["awaiting"] == "fleet":
        ls = live_status(thread_id)
        em, eta = ls.get("elapsed_min") or 0, ls.get("eta_min")
        doing = (ls.get("status") or "Working").rstrip("…").lower()
        lo, hi = ls.get("eta_lo"), ls.get("eta_hi")
        eta_txt = (f", {'over the usual' if ls.get('overrun') else 'usually'} ~{lo}-{hi} min" if lo else "")
        # DEDUPE (#2.1): a CEO who taps send twice (or a client that retries) must NOT get a second identical
        # status bubble. `dup` is True when this message repeats the immediately-prior user turn; on the fleet
        # gate we already stated the live status, so just re-affirm running without re-posting a duplicate.
        if dup:
            return {"phase": phase, "awaiting": "fleet", "running": True, "duplicate": True}
        # MID-FLIGHT INTENT (#2.1 follow-up): the CEO can't tap a gate that hasn't appeared yet, but a real
        # instruction typed now ("go with your recommendation and start building") must NOT be dropped on the
        # floor — it used to be, so when results landed the thread re-parked on the approval gate and ignored a
        # pre-authorized directive. QUEUE anything that isn't a pure status check as a pending_intent and apply
        # it at the next gate (advance() honours it the instant results land). Bare "is it done yet?" pings
        # still just get the honest live status, as before. No LLM turn here — status path stays free-form-safe.
        queued = bool((msg or "").strip()) and not _is_status_query(msg)
        if queued:
            _set(thread_id, pending_intent=msg)
            if _intent_auto_proceed(msg):
                tail = (" Noted — the moment the results land I'll go with my recommended direction and tee up "
                        "the plan for you, so you don't have to come back and tap.")
            else:
                tail = " Noted — I'll fold what you just said into the work as soon as the results are in."
        else:
            # HONEST CHANNEL (#2.4): don't promise a "ping" we might not deliver — the always-on channel is the
            # in-app notification bell; a phone push only fires if they've connected one.
            tail = (" I'll post the results right here and light up your notifications (and push to your phone "
                    "if you've set that up) the moment they're ready.")
        _report(tid, thread_id,
                f"I'm already on it — {doing} ({em}m elapsed{eta_txt}).{tail} Say \"cancel\" to stop.",
                {"kind": "working", "phase": phase, "job": ls.get("job_kind"),
                 "elapsed_min": em, "eta_min": eta, "queued_intent": queued})
        return {"phase": phase, "awaiting": "fleet", "running": True, "queued_intent": queued}

    if phase == "DISCOVER":
        # DECISIVE gate (G2): the AGENT judges "do I know enough? then GO" — it must NOT be perky/chatty. Bias
        # HARD toward proceeding: at most ONE clarifying question total, and only if the brief is genuinely
        # unactionable. No plan/option drafting here (that's later phases). The moment a research question is
        # nameable, emit the block — so the controller never has to force the transition.
        sysp = ("You are a decisive product controller scoping a build for a non-technical CEO. Your DEFAULT is "
                "to PROCEED, not to chat. If the brief is already actionable (it usually is), do NOT ask anything "
                "— immediately emit the research block. Ask AT MOST ONE short clarifying question, and only when "
                "you genuinely cannot form a research question without it. Do NOT draft plans, option lists, or "
                "tech-stack choices here — that happens in later phases. The MOMENT you can name what to research, "
                "end your reply with EXACTLY:\n[[RESEARCH]]\n<the research question to investigate>\n[[/RESEARCH]]"
                + _ceo_context(tid, s.get("org_id")))
        reply = _llm(tid, thread_id, sysp, s, on_delta=on_delta)
        rq = _parse_block(reply, "RESEARCH")
        clean = re.sub(r"\[\[RESEARCH\]\].*?\[\[/RESEARCH\]\]", "", reply, flags=re.S | re.I).strip()
        if rq:
            _set(thread_id, brief={"question": rq})
            _report(tid, thread_id, clean or "Got it.")   # the research kickoff (with its ETA) is posted by _dispatch
            _to(thread_id, "RESEARCH"); _set(thread_id, awaiting=None); _advance_owned(thread_id)
        else:
            _report(tid, thread_id, reply)
        return {"phase": _st(thread_id)["phase"]}

    if phase == "DEEP_DESIGN" and s["awaiting"] != "user_feedback" and not _internal:
        return {"phase": phase}
    if phase == "DEEP_DESIGN":
        verdict = None
        if s["plan"] and _internal:
            return _agentic_plan_review(tid, thread_id, s)
        if s["plan"] and _is_proceed(msg):
            verdict = "approve"
        elif s["plan"]:
            verdict = _classify_intent(tid, thread_id, msg, phase, "user_feedback",
                                       api_key=api_key)["verdict"]
        if verdict in ("approve", "proceed"):
            _report(tid, thread_id, "Registered — I approved the plan and I’m moving the work forward.",
                    {"kind": "decision_registered", "verdict": verdict, "phase": phase})
            _set(thread_id, awaiting=None); _to(thread_id, "PLAN_APPROVAL"); _advance_owned(thread_id)
            return {"phase": _st(thread_id)["phase"], "advanced": True}
        sysp = ("Turn the chosen direction into a concrete, RIGOROUS plan that accounts for EVERYTHING before "
                "any code is written — un-propagated signature changes and un-analyzed enforcement edits are the "
                "#1 cause of rework loops, so the plan must leave nothing un-analyzed. Also cover THE CEO'S OWN "
                "SIDE: what the product looks like for (a) the CEO, (b) their staff/team to control & monitor, "
                "and (c) their external users — these surfaces are BUILT INTO their product (we don't host them).\n"
                "If the CEO wants AI AGENTS to handle part of their product ('let agents take care of X'), don't "
                "just pick a preset — ASK ONE follow-up about HOW the agent should be invoked, OR RECOMMEND an "
                "architecture: a button (on-demand/sync), an event→agent pipeline handled ASYNC (form-submit / "
                "inbound email / webhook — like Kafka but agent workers), or a schedule. Then describe that "
                "custom agentic feature + its invocation in the agentic line (free text). If they don't want any, "
                "put 'none'.\n"
                "The plan bullets MUST embed a detailed-design pass so implementation is right the FIRST time: "
                "(1) an IMPACT MAP — every file/component to change, and for any function/signature/schema/"
                "contract being changed, ALL its callers/dependents so nothing is left un-propagated; "
                "(2) the INVARIANTS to preserve (existing behaviour, guards, security constraints) plus the "
                "empty/error/loading/edge cases each surface must handle; "
                "(3) a PARALLELIZATION note — which work items are INDEPENDENT (can build concurrently) vs. "
                "ordered; (4) a DONE checklist mapping each item to the check that proves it.\n"
                "Decide TWO INDEPENDENT axes:\n"
                "• 'platform' = WHAT KIND OF PRODUCT the user actually wants a real person to use. Pick the "
                "HONEST one from: web (a browser app — most SaaS/dashboards/trackers/tools), game-web (a "
                "browser game), mobile-ios, mobile-android, mobile-cross (one codebase for both phones), "
                "desktop (a downloadable desktop app), pc-game, cli (a command-line tool), api (a backend "
                "service), lib (a code library). If the user says 'app' with no platform and it's clearly "
                "something they'd open in a browser, that's 'web' — NEVER silently deliver a code library when "
                "a person expected an app they can open.\n"
                "• 'kind' = build STRATEGY by complexity: 'project' when the product has MULTIPLE distinct, "
                "interdependent components a real team would split across engineers (a multi-view app: "
                "data-layer + views + import/export + dashboard) — it triggers an architect who decomposes it "
                "into a component DAG built by MULTIPLE dev agents recursively with per-component tests + an "
                "integration pass. 'web'/'lib'/'service' = a single-builder artifact. When in doubt for a real "
                "multi-feature app, choose 'project' (the platform still decides the actual tech stack).\n"
                "End with EXACTLY:\n[[PLAN]]\nname: <slug>\nplatform: web|game-web|mobile-ios|mobile-android|"
                "mobile-cross|desktop|pc-game|cli|api|lib\nkind: lib|web|service|project\n"
                "plan: <bullets incl. the impact map, invariants/edge cases, parallelization, and done checks; one per line '- '>\n"
                "agentic: <free-text: the agentic feature(s) the CEO wants + how each is invoked (button/event-async/"
                "schedule), or 'none'>\ncharter: <2-4 sentences incl. the team/external surfaces to build in>\n[[/PLAN]]"
                + _ceo_context(tid, s.get("org_id"))    # ground the plan in the CEO's standing vision + reqs
                + _decision_brief(s))                   # ...and in the ask/decision/findings this phase exists to serve
        reply = _llm(tid, thread_id, sysp, s, on_delta=on_delta)
        pb = _parse_block(reply, "PLAN")
        clean = re.sub(r"\[\[PLAN\]\].*?\[\[/PLAN\]\]", "", reply, flags=re.S | re.I).strip()
        if pb:
            plan = _parse_plan(pb)
            # EXPOSE THE FULL PLAN (#3): carry the COMPLETE plan text (impact map, invariants, parallelization,
            # done checks — everything the model wrote), not just the one-line charter, so the console can show
            # and explain the whole thing. `full` is the human-readable composed body; `body` keeps the raw
            # block for anything that wants it verbatim.
            plan["full"] = _plan_full_text(plan, pb)
            plan["body"] = pb.strip()
            _set(thread_id, plan=plan)
            try:  # item 10: durable PLAN checkpoint (external memory) so a context truncation can't lose the plan
                import companymemory
                companymemory.checkpoint(tid, str(thread_id), "plan", plan.get("full") or plan.get("body") or "",
                                         actor_id="loopcontroller", phase="PLAN")
            except Exception:
                pass
            _report(tid, thread_id, clean or "The product team completed the plan.",
                    {"kind": "plan", "plan": plan, "autonomous_review": True})
            return _agentic_plan_review(tid, thread_id, {**s, "plan": plan})
        else:
            _report(tid, thread_id, reply)
        return {"phase": phase}

    if phase == "OPTIONS":
        # Options MOVE only via choose() (a chip tap) or a typed ordinal — never clear `awaiting` here (the
        # generic affirmative branch would call advance(), a no-op at OPTIONS, stalling the thread). But a
        # typed message that ISN'T a pick is a REVIEW question, not a mis-tap: don't force ("tap one above").
        # Map an ordinal -> choose(); an EMPTY/whitespace message -> gently nudge to pick; ANYTHING ELSE ->
        # ELABORATE — answer their question about the options/research (fast model, report+options as
        # context), then remind they can pick when ready (#1). The gate is HELD throughout.
        if _is_retry(msg):
            _resume_halts(thread_id)
            _report(tid, thread_id, "Registered — I’m rerunning the research and will bring back better options.",
                    {"kind": "decision_registered", "verdict": "retry", "phase": phase})
            _set(thread_id, awaiting=None, options=None, chosen_option=None, research_run_id=None,
                 pending_intent=None)
            _to(thread_id, "RESEARCH")
            _advance_owned(thread_id)
            return {"phase": _st(thread_id)["phase"], "advanced": True, "retried": True}
        oid = _option_ordinal(msg, s.get("options") or [])
        if oid is not None:
            return choose(tid, thread_id, oid)
        if not (msg or "").strip():
            _report(tid, thread_id,
                     "Tap one of the options above to pick a direction — or ask me anything about them "
                     "(e.g. \"tell me more about option 2\" or \"which is cheaper?\") and I'll dig into the "
                     "research for you.", {"kind": "options_nudge", "options": s.get("options") or []})
            return {"phase": phase}
        reply = _answer_at_options(tid, thread_id, s, msg, on_delta=on_delta)
        _report(tid, thread_id, reply, {"kind": "options_qa", "options": s.get("options") or [],
                                        "research_run_id": s.get("research_run_id")})
        return {"phase": phase}

    if s["awaiting"] in ("user_feedback", "user_approval"):
        # DETERMINISTIC RETRY. When a phase fails we tell the CEO verbatim: 'say "retry" to run it again'.
        # Routing that word through the LLM intent classifier meant it could come back as 'revise', which
        # files it as FEEDBACK — so the one recovery instruction the product gives was swallowed. Observed
        # on thread 2090: the research failed, the CEO said "retry", and got "Got it — I'll fold that in and
        # rework it" while pending_intent quietly grew and nothing re-ran. A word we ourselves prescribe must
        # not depend on a model call to be understood.
        if _is_retry(msg):
            _resume_halts(thread_id)
            _set(thread_id, awaiting=None, pending_intent=None)
            _advance_owned(thread_id)
            return {"phase": _st(thread_id)["phase"], "advanced": True, "retried": True}
        if _is_proceed(msg):                       # a prescribed affirmative — never a model call away
            _resume_halts(thread_id)
            _report(tid, thread_id, "Registered — I approved that and I’m moving the work forward.",
                    {"kind": "decision_registered", "verdict": "approve", "phase": phase})
            _set(thread_id, awaiting=None)
            _advance_owned(thread_id)
            return {"phase": _st(thread_id)["phase"], "advanced": True}
        intent = _classify_intent(tid, thread_id, msg, phase, s["awaiting"], api_key=api_key)
        if intent["verdict"] in ("approve", "proceed"):
            _resume_halts(thread_id)   # a 'retry' after a cancel() must lift the halt before re-dispatching
            _report(tid, thread_id, "Registered — I approved that and I’m moving the work forward.",
                    {"kind": "decision_registered", "verdict": intent["verdict"], "phase": phase})
            _set(thread_id, awaiting=None); _advance_owned(thread_id)
            return {"phase": _st(thread_id)["phase"], "advanced": True}
        if intent["verdict"] == "cancel":
            cancel(tid, thread_id, reason="stopped by user")
            return {"phase": _st(thread_id)["phase"], "cancelled": True}
        # reject/revise/steer/question — the feedback is REAL; record it so the re-dispatch folds it in,
        # never silently ignore it (the "not good" that used to auto-approve now correctly holds the gate).
        _set(thread_id, pending_intent=((s.get("pending_intent") or "") + "\n" + (msg or "")).strip())
        _report(tid, thread_id, "Got it — I'll fold that in and rework it, not push it through.")
        return {"phase": phase}
    if s["awaiting"] == "credentials":
        # 'say "ready"' is what we tell them here — honour it directly.
        if _is_proceed(msg) or _classify_intent(tid, thread_id, msg, phase, "credentials", api_key=api_key)["verdict"] in ("approve", "proceed"):
            _resume_halts(thread_id)
            _set(thread_id, awaiting=None); _advance_owned(thread_id)
            return {"phase": _st(thread_id)["phase"], "advanced": True}
        _report(tid, thread_id, "When your provider is connected in Settings → Providers, say \"ready\".")
        return {"phase": phase}

    # LIFE AFTER DELIVER (REBUILD-PLAN A2): a company doesn't die at one product. Once delivered, a new
    # build / feature / v2 / next-product request starts a FRESH workstream (re-enters DISCOVER) instead of
    # the thread becoming a dead Q&A bot. The company's history (org, shipped products, portfolio) persists;
    # only the per-cycle fields reset. A pure question/status still just gets answered (below).
    if phase == "DELIVER" and (msg or "").strip() and not _is_status_query(msg):
        intent = _classify_intent(tid, thread_id, msg, phase, "delivered", api_key=api_key)
        if intent["verdict"] in ("steer", "proceed", "approve", "choose", "revise"):
            _set(thread_id, brief={"question": msg}, awaiting=None, plan=None, product=None,
                 research_run_id=None, chosen_option=None, pending_intent=None)
            _to(thread_id, "DISCOVER")
            _report(tid, thread_id, "On it — kicking off a new build for that. Let me scope it.",
                    {"kind": "new_workstream"})
            return say(tid, thread_id, msg, api_key=api_key, on_delta=on_delta)   # re-enter at DISCOVER

    _report(tid, thread_id, _llm(tid, thread_id, "Answer the CEO briefly.", s, on_delta=on_delta))
    return {"phase": phase}


def choose(tid, thread_id, option_id):
    s = _st(thread_id)
    if not s or s["phase"] != "OPTIONS":
        return {"error": "not awaiting an option choice"}
    chosen = {"option_id": option_id}
    try:
        import research
        chosen = research.select(tid, s["research_run_id"], option_id) or chosen
    except Exception:
        pass
    _set(thread_id, chosen_option=chosen, awaiting=None)
    _to(thread_id, "DEEP_DESIGN")
    _report(tid, thread_id, "Got it — your choice overrides the team's recommendation. The product team is "
                            "drafting and reviewing the technical plan now.",
            {"kind": "option_chosen", "user_override": True})
    _advance_owned(thread_id)
    return {"phase": "DEEP_DESIGN", "chosen": chosen}


def run_ceo_directive(directive, *, tenant_id=None, thread_id=None, functions=None):
    """CALLSITE for an ARBITRARY, ad-hoc CEO directive (beyond the product-build workstream): route it to the
    agentic COMPANY ORG (company.run_directive AI-plans the functions, then drives them). ISOLATED + additive —
    it does NOT touch the product-build phase machine (`advance`); it records a controller_jobs row for console
    visibility and returns the org run result. Production should dispatch this async (it can be long-running);
    here it's a clean, directly-callable entrypoint. Fail-open on the visibility bookkeeping."""
    import sys as _sys
    _orch = str(Path(__file__).resolve().parent / "orchestra")
    if _orch not in _sys.path:
        _sys.path.insert(0, _orch)
    import company
    tid = tenant_id or (_st(thread_id).get("tenant_id") if thread_id else None) or "ceo"
    execution_scope = (_st(thread_id).get("execution_scope") if thread_id else None) or "production"
    jid = None
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""INSERT INTO controller_jobs
                              (thread_id, tenant_id, phase, kind, execution_scope)
                           VALUES (%s,%s,'DIRECTIVE','company-directive',%s) RETURNING id""",
                        (thread_id, tid, execution_scope))
            jid = cur.fetchone()[0]; c.commit()
    except Exception:
        pass
    out = company.run_directive(directive, tenant=tid, functions=functions)
    try:
        if jid is not None:
            with _conn() as c, c.cursor() as cur:
                cur.execute("UPDATE controller_jobs SET status=%s, result=%s WHERE id=%s",
                            (out.get("status", "done"), json.dumps({"run_id": out.get("run_id"),
                             "functions": len(out.get("functions") or [])}), jid))
                c.commit()
    except Exception:
        pass
    return out


def _manage_operational_failure(thread_id, state, error):
    """Get an evidence-backed line-management decision for a failed internal step.

    The durable management case is stable per thread/phase, so repeated failures build one transcript instead
    of creating disconnected alerts.  A targeted leased review cannot overlap the duty manager.  If management
    itself is temporarily unavailable we choose one fresh bounded worker and leave an auditable internal alert;
    infrastructure failure never silently manufactures CEO work.
    """
    phase = state.get("phase") or "unknown"
    tid = state.get("tenant_id")
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""SELECT count(*) FROM controller_jobs
                           WHERE thread_id=%s AND phase=%s AND status IN ('failed','crashed')""",
                        (thread_id, phase))
            failures = int(cur.fetchone()[0])
        import management
        case = management.signal(
            f"controller:{thread_id}:{phase}", f"{phase} worker repeatedly failed",
            "worker_state_changed", {"thread_id": thread_id, "phase": phase,
                "error": str(error)[:1000], "failure_count": failures,
                "objective": "recover the phase and continue the authorized workstream"},
            tenant_id=tid, product=state.get("product"), work_id=f"controller:{thread_id}:{phase}",
            worker=None, manager_role="team-lead", progress=False)
        return management.review_now(case["case_id"])
    except Exception as exc:
        try:
            import alerts
            alerts.raise_alert("loopcontroller", "incident-commander",
                               f"management review unavailable for thread {thread_id}/{phase}: {exc}",
                               severity="high", signature=f"management-unavailable:{thread_id}:{phase}")
        except Exception:
            pass
        return {"action": "retry", "status": "open", "manager_role": "incident-commander",
                "reason": f"management review unavailable: {str(exc)[:200]}"}


def advance(thread_id, job_result=None):
    s = _st(thread_id)
    if not s:
        return
    if s["awaiting"] in ("user_feedback", "user_approval", "credentials", "fleet"):
        return
    tid, phase = s["tenant_id"], s["phase"]

    # A deliberate rolling-runtime handoff is neither product work nor a crash. The old generation was fenced
    # only after it had no children, so resume the SAME durable phase from its checkpoints regardless of the
    # historical crash count. Treating an operator-controlled code rotation as another crash can strand a
    # healthy long-running QA campaign merely because earlier infrastructure failures exhausted its retry cap.
    if job_result and job_result.get("controlled_handoff"):
        audit.append(actor="loopcontroller", action="ControlledRuntimeResume", resource=str(thread_id),
                     decision=phase, payload={"job_id": job_result.get("job_id"),
                                              "reason": job_result.get("reason")})
        _set(thread_id, awaiting=None)
        _job_clear(thread_id)
        advance(thread_id)                         # phase unchanged -> loads a fresh worker from current source
        return

    # A worker CRASH (its process died -> reaped by heartbeat-lapse/ceiling, marked crashed=true) is NOT a real
    # job failure — it's transient infra. Transparently RE-RUN the phase (builds resume from their _stage_done
    # checkpoint; research reconciles against research_runs) instead of escalating to the human — this is
    # "zero bugs reach a human" for crashes. BOUNDED: after CRASH_RETRY_MAX crashes on the same phase something
    # is systematically killing the worker (real bug / OOM), so fall through to the user-facing surface below.
    if job_result and job_result.get("crashed"):
        crashes = _crash_count(thread_id, phase)
        crash_cap = QA_CRASH_RETRY_MAX if phase == "TESTQA" else CRASH_RETRY_MAX
        if crashes <= crash_cap:
            audit.append(actor="loopcontroller", action="CrashResume", resource=str(thread_id), decision=phase,
                         payload={"crash_count": crashes})
            # NOTHING FAILS INVISIBLY: crash-resume is silent to the CEO, but an OPERATOR should see repeated
            # crashes (a worker that keeps dying = an infra/OOM problem worth attention before it hits the cap).
            try:
                import alerts
                alerts.raise_alert("loopcontroller", "controller",
                                   f"thread {thread_id} phase {phase}: worker crashed {crashes}× — auto-resuming "
                                   f"from checkpoint (cap {crash_cap})",
                                   severity="warn" if crashes >= 2 else "info",
                                   signature=f"crashresume:{thread_id}:{phase}")
            except Exception:
                pass
            _set(thread_id, awaiting=None)
            _job_clear(thread_id)
            advance(thread_id)                        # phase unchanged -> re-dispatches; resumes from checkpoint
            return
        # crashes exhausted -> a human should know; fall through to the user-facing failure surface below

    # A dispatched job came back BROKEN. Concrete consent/quota boundaries still go to the person who owns
    # them; ordinary operational failures go to the durable management hierarchy for an agentic decision.
    if job_result and (job_result.get("error") or job_result.get("status") in ("failed", "timeout")):
        err = job_result.get("error") or job_result.get("status")
        es = str(err).lower()
        if "consent" in es:
            _set(thread_id, awaiting="user_feedback")
            text = ("⚠️ I can't research or build yet because AI-processing consent isn't on file. Please accept "
                    "it in Settings → Privacy (it names the provider your text is sent to), then say \"ready\" "
                    "and I'll pick up right where we left off.")
            meta_kind = "consent_required"
        elif "quota reached" in es or "quota exceeded" in es or "over quota" in es:
            _set(thread_id, awaiting="user_feedback")
            # ONLY a genuine over-quota (research.py emits "quota reached (...)"); an internal spend-gate error
            # ("internal_error: quota check failed …") must NOT be mis-rendered as a billing/upgrade message.
            text = ("⚠️ You've hit your plan's build quota, so I paused before spending anything. Upgrade your "
                    "plan (or wait for it to reset) in Settings → Billing, then say \"ready\" to continue.")
            meta_kind = "quota_reached"
        else:
            decision = _manage_operational_failure(thread_id, s, err)
            if decision.get("status") != "human_wait":
                _job_clear(thread_id)
                _report(tid, thread_id,
                        f"The {phase} worker hit a problem. {decision.get('manager_role') or 'Its manager'} "
                        f"reviewed the evidence and chose **{decision.get('action') or 'retry'}**; the company "
                        "is continuing internally and does not need anything from you.",
                        {"kind": "internal_management_decision", "phase": phase,
                         "error": str(err)[:300], "decision": decision}, urgent=False)
                audit.append(actor="loopcontroller", action="OperationalFailureManaged",
                             resource=str(thread_id), decision=decision.get("action") or "retry",
                             payload={"phase": phase, "error": str(err)[:200], "management": decision})
                _set(thread_id, awaiting=None)
                advance(thread_id)
                return
            # `human_wait` can only be produced through authority.open_decision after a named, typed boundary.
            _set(thread_id, awaiting="user_feedback")
            text = (f"⚠️ The {phase} manager found a decision outside the company's standing authority. "
                    "A correlated request is now waiting for your answer; the failed work remains checkpointed.")
            meta_kind = "authority_required"
        # Clear the live-progress fields. Without this the thread keeps reporting the LAST in-flight status
        # ("Still working — this one's taking a little longer…") for a job that has already failed, so the
        # status line contradicts the failure message directly above it. Observed on thread 2090 hours after
        # the run died.
        _job_clear(thread_id)
        _report(tid, thread_id, text, {"kind": meta_kind, "phase": phase}, urgent=True)
        audit.append(actor="loopcontroller", action="JobFailed", resource=str(thread_id), decision=phase,
                     payload={"error": str(err)[:200]})
        return

    # research job finished -> present options
    if job_result and job_result.get("run_id") and "options" in job_result:
        # ENRICH the option cards so each carries a real summary/detail (not just a title) — backfilling from
        # the research report for any that lack one (#2), so the console renders CONTENT on every card.
        report_text = ""
        try:
            report_text = _research_report_for_run(tid, job_result["run_id"]).get("report", "") or ""
        except Exception:
            report_text = ""
        opts = _enrich_options(job_result.get("options", []), report_text)
        _set(thread_id, research_run_id=job_result["run_id"], options=opts)
        _job_clear(thread_id)
        pend = (s.get("pending_intent") or "").strip()
        rec = next((o for o in opts if isinstance(o, dict) and o.get("recommended")), None)
        default_option = rec or next((o for o in opts if isinstance(o, dict)), None) or {}
        decision = _agentic_lifecycle_decision(
            tid, thread_id, "option_selection",
            {"options": opts, "research_run_id": job_result["run_id"], "ceo_steering": pend,
             "allowed_actions": ["select", "experiment", "request_human"]},
            str(job_result["run_id"]),
            default={"action": "select", "selection": default_option.get("id"), "confidence": 1.0,
                     "rationale": "senior product director selected the research recommendation"})
        _to(thread_id, "OPTIONS")
        if decision.get("status") == "human_wait":
            _set(thread_id, awaiting="user_feedback")
            _report(tid, thread_id,
                    "Research is complete. The product team narrowed the options but found one typed decision "
                    "outside standing authority; a specific request is waiting for you.",
                    {"kind": "authority_required", "options": opts, "decision": decision}, urgent=True)
            return
        selected = decision.get("selection")
        if isinstance(selected, dict):
            selected = selected.get("id") or selected.get("option_id")
        picked = next((o for o in opts if isinstance(o, dict) and str(o.get("id")) == str(selected)),
                      default_option)
        rid = picked.get("id")
        chosen = {"option_id": rid}
        try:
            import research as _research
            chosen = _research.select(tid, job_result["run_id"], rid) or chosen
        except Exception:
            pass
        _set(thread_id, chosen_option=chosen, awaiting=None, pending_intent=None)
        _to(thread_id, "DEEP_DESIGN")
        _report(tid, thread_id,
                "Research is in. The product team selected "
                f"“{picked.get('title', 'the recommended direction')}” and is drafting the plan now. "
                "The options and rationale remain visible, and you can override or cancel at any time.",
                {"kind": "option_chosen", "auto_selected": rid, "title": picked.get("title"),
                 "options": opts, "decision": decision})
        _ping(tid, "Research done — the product team chose a direction",
              "The team selected a reversible direction and is drafting the plan. Open the chat to steer it.",
              level="normal")
        advance(thread_id)
        return
    if job_result and job_result.get("screens") is not None:        # prototype finished -> internal review
        _job_clear(thread_id)
        decision = _agentic_lifecycle_decision(
            tid, thread_id, "prototype_acceptance",
            {"prototype": job_result, "plan": s.get("plan"),
             "allowed_actions": ["proceed", "revise", "retry", "request_human"]},
            json.dumps(job_result, sort_keys=True, default=str),
            default={"action": "proceed", "confidence": 1.0,
                     "rationale": "senior product director accepted the reversible prototype"})
        # Always surface the artifact and the team's decision. Visibility is not an approval tax.
        _report(tid, thread_id, f"The team drafted {job_result.get('screens', 0)} prototype screens "
                                "(cockpit / team / external) and completed its internal review.",
                {"kind": "prototype", "design_ready": True, "org": s.get("org_id"),
                 "product": s.get("product"), "screens": job_result.get("screens", 0),
                 "surfaces": job_result.get("surfaces"), "decision": decision})
        if decision.get("status") == "human_wait":
            _set(thread_id, awaiting="user_feedback")
            _report(tid, thread_id, "Prototype review found a typed authority boundary. A specific request "
                                    "is waiting for you; the prototype remains checkpointed.",
                    {"kind": "authority_required", "decision": decision}, urgent=True)
            return
        if decision.get("action") == "revise":
            _set(thread_id, plan=None, awaiting=None,
                 pending_intent=str(decision.get("rationale") or "revise the prototype plan"))
            _to(thread_id, "DEEP_DESIGN")
            advance(thread_id)
            return
        if decision.get("action") == "retry":
            _set(thread_id, awaiting=None)
            advance(thread_id)
            return
        _to(thread_id, "IMPLEMENT"); _set(thread_id, awaiting=None)
        _ping(tid, "Prototype reviewed — build is starting",
              f"{job_result.get('screens', 0)} screens passed internal review. The build is moving forward; "
              "open the chat any time to steer or cancel.", level="normal")
        advance(thread_id)
        return
    if job_result and (job_result.get("shipped") is not None or job_result.get("result")):  # build done
        product = _st(thread_id).get("product")
        # BOUNDARY CONTRACT (root-cause fix): record the build outcome to the single-source-of-truth registry,
        # then VALIDATE that QA is even allowed to run — build genuinely succeeded AND its artifact exists at the
        # REGISTERED path. A failed/empty build must NOT advance to a QA that can't find it (F6/F7); it auto-loops
        # back to the builder instead. Fail-open: a registry hiccup never blocks the pipeline.
        build_ok = _build_result_ok(job_result)
        proceed, why = True, ""
        try:
            import productregistry as _preg
            _preg.record_phase(product, "build", ok=build_ok, artifact=_preg.path(product),
                               verdict=str(job_result.get("status") or job_result.get("result") or "")[:200])
            proceed, why = _preg.precondition(product, "qa")
        except Exception:
            proceed = True
        if proceed:
            # A newly-authorized QA campaign owns its internal checkpoint hand-offs.  Reset the campaign
            # counters here; a CEO should not have to approve each safe worker rotation.
            _set(thread_id, qa_checkpoint_count=0, qa_last_completed=None, qa_no_progress_count=0,
                 qa_campaign_key=None)
            _to(thread_id, "TESTQA"); advance(thread_id)
        else:
            _autoloop_build(thread_id, tid, product, reason=why)
        return
    if job_result and "qa_ok" in job_result:                        # qa verdict in -> ENFORCE it (#48)
        product = _st(thread_id).get("product")
        try:
            import productregistry as _preg
            _preg.record_phase(product, "qa", ok=bool(job_result.get("qa_ok")),
                               verdict=str(job_result.get("verdict") or job_result.get("error") or "")[:300])
        except Exception:
            pass
        if job_result.get("qa_ok"):
            try:
                import workstreamspine
                assurance = workstreamspine.record_assurance(
                    tid, f"controller:{thread_id}", job_result,
                    executor_id="build-team", manager_id="controller",
                    reviewer_id="qa-gate-auditor", submitted_by="qa-coordinator",
                    subject_id=product)
                audit.append(actor="qa-gate-auditor", action="DeliveryAssuranceRecorded",
                             resource=str(thread_id), decision=assurance.get("verdict") or "recorded",
                             payload={"review_id": assurance.get("review_id")}, tenant_id=tid)
            except Exception as exc:
                # Existing qa_ok remains the fail-closed delivery gate. Persistence
                # is retried by idempotent phase callbacks and never reruns live QA.
                audit.append(actor="loopcontroller", action="DeliveryAssuranceRecord",
                             resource=str(thread_id), decision="deferred",
                             payload={"error": str(exc)[:300]}, tenant_id=tid)
            _to(thread_id, "DELIVER"); advance(thread_id)
        elif job_result.get("safety_limited"):
            if job_result.get("internal_management_wait"):
                # The durable QA hierarchy—not another browser worker—owns this checkpoint.  A fresh slice
                # would merely rediscover the same disputed evidence, while a generic user gate would bypass
                # the named authority request already opened by QA management (when one is actually needed).
                _job_clear(thread_id)
                _set(thread_id, awaiting="internal_management")
                review_ids = list(job_result.get("internal_review_ids") or [])
                case_ids = list(job_result.get("qa_review_case_ids") or [])
                authority_ids = list(job_result.get("authority_decision_ids") or [])
                _report(tid, thread_id,
                        "🧪 QA is checkpointed while its internal management chain resolves disputed "
                        "evidence. No new QA slice will be started, and nothing is needed from you unless "
                        "the correlated named-authority request asks for a decision.",
                        {"kind": "qa_internal_management", "autonomous": True,
                         "internal_review_ids": review_ids, "qa_review_case_ids": case_ids,
                         "authority_decision_ids": authority_ids,
                         "internal_review_states": job_result.get("internal_review_states") or {}},
                        urgent=False)
                audit.append(actor="loopcontroller", action="QAInternalManagementCheckpoint",
                             resource=str(thread_id), decision="internal_management",
                             payload={"internal_review_ids": review_ids,
                                      "qa_review_case_ids": case_ids,
                                      "authority_decision_ids": authority_ids})
                return
            # A slice limit protects the host; it is NOT a business decision and must not silently turn the CEO
            # into the scheduler.  Checkpoint, account for forward progress, and rotate to a fresh bounded worker.
            # Only escalate after repeated zero-progress recovery attempts, incomplete cleanup, or the campaign
            # hard ceiling.  This is the same behaviour expected from a competent human QA lead changing shifts.
            # Story evidence, not terminated worker processes, is progress. An infrastructure failure can end
            # ten explorer actors without proving a single story; counting those exits reset the stall detector
            # and produced misleading "11/12 complete" updates while eight stories still lacked coverage.
            completed = job_result.get("stories_done")
            if completed is None:                    # rolling compatibility with an already-running old worker
                completed = job_result.get("explorers_done")
            completed_total = job_result.get("stories_total") or job_result.get("stories")
            campaign_key = job_result.get("qa_campaign_key")
            checkpoints, stalls, campaign_changed = _qa_campaign_checkpoint_counts(
                s.get("qa_campaign_key"), campaign_key, s.get("qa_last_completed"), completed,
                s.get("qa_checkpoint_count"), s.get("qa_no_progress_count"))
            # A timed-out shift may still have Python tool threads unwinding after their browser/Codex child
            # groups have been killed.  Those threads are contained by this short-lived run_job process, and
            # jobd cannot start its successor until that process exits.  They are therefore telemetry, not a
            # CEO decision or evidence of host leakage.  Only surviving OS descendants are a cleanup fault.
            cleanup_incomplete = int(job_result.get("cleanup_incomplete") or 0)
            cleanup_processes = int(job_result.get("cleanup_processes_incomplete") or 0)
            _set(thread_id, qa_checkpoint_count=checkpoints,
                 qa_last_completed=completed if completed is not None else s.get("qa_last_completed"),
                 qa_no_progress_count=stalls,
                 qa_campaign_key=campaign_key or s.get("qa_campaign_key"))
            # A rotation count is an internal management signal, not evidence that money or CEO authority
            # was exhausted.  The old hardcoded threshold silently parked healthy campaigns even when the CEO
            # had explicitly asked the company to continue.  Real spend/credential/legal boundaries must come
            # from their durable authority/resource ledgers, not be invented from elapsed wall-clock time.
            authority_boundaries = []
            # Every shift boundary is a real management event.  The QA director reviews current evidence and
            # chooses the next organizational action even when progress is healthy; the timer itself never
            # makes that decision.  Deterministic safety/accounting remains an invariant around the decision.
            manager = _qa_manager_decision(thread_id, {
                "event": "qa_shift_checkpoint", "thread_id": thread_id, "product": product,
                "checkpoint": checkpoints, "completed": completed,
                "previous_completed": s.get("qa_last_completed"),
                "campaign_key": campaign_key, "campaign_changed": campaign_changed,
                "total": completed_total, "no_progress_streak": stalls,
                "open_blockers": job_result.get("blocking_open"),
                "cleanup_incomplete": cleanup_incomplete,
                "cleanup_processes_incomplete": cleanup_processes,
                "management_review_due": checkpoints >= QA_AUTO_CHECKPOINT_MAX,
                "standing_authority_exhausted": False,
                "authority_boundaries": authority_boundaries,
                "verdict": str(job_result.get("verdict") or "")[:500]})
            can_continue = _qa_checkpoint_can_continue(cleanup_processes, manager)
            audit.append(actor="qa-director", action="QAManagementDecision", resource=str(thread_id),
                         decision=manager.get("action"), payload=manager)
            if manager.get("action") == "open_internal_incident_and_cleanup":
                try:
                    import alerts
                    alerts.raise_alert("qa-director", "controller",
                                       f"QA campaign {thread_id} opened an internal management incident at "
                                       f"checkpoint {checkpoints}: {manager.get('reason')}",
                                       severity="high", signature=f"qa-stalled:{thread_id}")
                except Exception:
                    pass
            _job_clear(thread_id)
            if can_continue:
                _report(tid, thread_id,
                        f"🧪 QA checkpoint {checkpoints}: "
                        f"{completed if completed is not None else '?'}"
                        f"/{completed_total or '?'} stories have settled evidence. "
                        "The QA coordinator is handing unfinished work to a fresh bounded worker; "
                        "nothing is needed from you.",
                        {"kind": "qa_checkpoint", "checkpoint": checkpoints,
                         "stories_done": completed, "stories_total": completed_total,
                         "explorers_done": job_result.get("explorers_done"),
                         "explorers_total": job_result.get("explorers_total"),
                         "no_progress_streak": stalls, "autonomous": True,
                         "manager_decision": manager}, urgent=False)
                audit.append(actor="loopcontroller", action="QACheckpointContinue", resource=str(thread_id),
                             decision="continue", payload={"checkpoint": checkpoints,
                             "completed": completed, "no_progress_streak": stalls})
                _set(thread_id, awaiting=None)
                advance(thread_id)
            else:
                reason = (f"cleanup left {cleanup_processes} OS process(es)" if cleanup_processes else
                          f"QA director requested {manager.get('authority_gap')} authority: "
                          f"{manager.get('reason')}" if manager else
                          f"campaign reached its {QA_AUTO_CHECKPOINT_MAX}-slice standing spend/time authority")
                _set(thread_id, awaiting="user_feedback")
                _report(tid, thread_id,
                        f"⚠️ QA management escalated after autonomous recovery was exhausted: {reason}. "
                        "The build remains held back and the durable checkpoint is intact.",
                        {"kind": "qa_management_escalation", "reason": reason,
                         "checkpoint": checkpoints, "no_progress_streak": stalls,
                         "actions": ["continue", "cancel"]}, urgent=True)
                audit.append(actor="loopcontroller", action="QACheckpointEscalate", resource=str(thread_id),
                             decision="user_feedback", payload={"reason": reason})
        else:
            # A failed/unverifiable build must NOT reach DELIVER — but the CEO is NOT the first responder.
            # Auto-loop back to the builder (bounded); escalate to the human ONLY when the loop is exhausted.
            _autoloop_build(thread_id, tid, product,
                            reason=(job_result.get("verdict") or job_result.get("error") or "QA did not pass"))
        return

    if phase == "RESEARCH":
        # Phase work (start the durable research run, persist its id so resume_stalled can reconcile it, poll
        # within the crash window, hand off 'pending' if it outlives the budget) lives in _phase_fn("research")
        # so it runs identically in-process or in a parked worker process.
        _dispatch(thread_id, "research", eta_min=_estimate_runtime("RESEARCH"),
                  kickoff="On it — I'm researching this now and will bring back a few directions.",
                  status="Researching directions…")
        return

    if phase == "DEEP_DESIGN":
        # The durable phase itself is the retry record. Internal turns are not
        # written as CEO messages; a crash before the plan checkpoint leaves the
        # state runnable and jobd simply asks the product team again.
        say(tid, thread_id, "", _internal=True)
        return

    if phase == "PLAN_APPROVAL":
        try:
            if not _resolved_provider(tid):
                import agent_request
                agent_request.ask(tid, "To build this I need a model provider connected (Anthropic or Codex) — "
                                       "add one in Settings → Providers, then say \"ready\".",
                                  kind="credential", org_id=s["org_id"], thread_id=thread_id,
                                  correlation_id=f"controller:{thread_id}:provider")
                _set(thread_id, awaiting="credentials")
                return
        except Exception:
            pass
        _to(thread_id, "PROTOTYPE"); advance(thread_id)
        return

    if phase == "PROTOTYPE":
        plan = s["plan"] or {}
        product = (s.get("product") or f"{s['org_id'] or 'o'}-{plan.get('name', 'app')}")[:30]
        _set(thread_id, product=product)
        try:  # SINGLE SOURCE OF TRUTH: register the product ONCE — every later phase reads its id+path from here
            import productregistry as _preg
            _preg.register(product, tenant_id=tid, org_id=s.get("org_id"), plan=plan)
        except Exception:
            pass
        _dispatch(thread_id, "design", eta_min=_estimate_runtime("PROTOTYPE", plan),
                  kickoff="Designing your prototype screens (cockpit, team & external).",
                  status="Designing prototype…")
        return

    if phase == "IMPLEMENT":
        plan = s["plan"] or {}
        product = (s.get("product") or f"{s['org_id'] or 'o'}-{plan.get('name', 'app')}")[:30]
        _set(thread_id, product=product)
        try:  # ensure the authoritative record exists (idempotent) before the build writes its artifact
            import productregistry as _preg
            _preg.register(product, tenant_id=tid, org_id=s.get("org_id"), plan=plan)
        except Exception:
            pass
        _dispatch(thread_id, "build", eta_min=_estimate_runtime("IMPLEMENT", plan),
                  kickoff="Building it now — I'll ping you the moment it's ready.",
                  status="Building…")
        return

    if phase == "TESTQA":
        if not _qa_spend_gate(thread_id, s):
            return
        _dispatch(thread_id, "qa", eta_min=_estimate_runtime("TESTQA", s.get("plan")),
                  kickoff="Running QA on the build…", status="Testing…")
        return

    if phase == "DELIVER":
        product = s.get("product")
        try:
            import orgs
            orgs.record_artifact(s["org_id"], "product_repo", f"Shipped {product}",
                                 product=product, tenant_id=tid)
            orgs.set_stage(tid, s["org_id"], "live")
        except Exception:
            pass
        try:   # MEMORY SPINE (A3): the company remembers what it shipped, so a v2/next build knows its history
            import companymemory
            companymemory.remember(tid, s.get("org_id"), "product",
                                   f"Shipped '{product}'. Future work should build ON it, not re-decide its basics.")
        except Exception:
            pass
        _report(tid, thread_id, f"✅ Done — **{product}** is built, tested and ready. Download it from Projects. "
                                f"Want to keep going?", {"kind": "next_steps", "product": product,
                                "suggestions": ["Add a web UI", "Add user accounts", "Start another org"]}, urgent=True)
        _set(thread_id, awaiting=None)
        return

    # OPTIONS waits for choose(); the prototype->IMPLEMENT gate is handled by the proto-finished branch.
    if phase == "OPTIONS":
        return


def qa_gate(product, platform=None) -> dict:
    """TESTQA's verification body (REBUILD-PLAN C1): run the AGENTIC QA stack against the RUNNING build
    and consume the SAME machine verdict the LAUNCH gate reads (docs/QA-VERDICT.json: passed==true,
    blocking_open==0, stories>0). The build is brought up via devserve (web/service get a stable dev
    URL; non-servable kinds fall through to factory's independent qa-security verification inside
    run_grounded_qa). `platform` (from the plan) tells QA whether a real interface is REQUIRED (a web/game
    UI can't pass without a browser session) and whether the target is even testable on this box. The
    builder never grades its own homework. FAIL-CLOSED: an unverifiable build is a QA failure — DELIVER
    stays blocked."""
    try:
        target = None
        try:
            import devserve
            up = devserve.up(product)                 # idempotent: reuses a live instance + stable port
            target = up.get("url")
        except Exception:
            target = None                             # not servable / didn't come up -> independent path
        v = factory.run_grounded_qa(product, target_url=target, platform=platform)
        # ONE ship condition, shared with the factory QA stage and gate_check's LAUNCH validator:
        # passed==true AND blocking_open==0 AND stories>0 (fail-closed on missing/garbled facts).
        qa_ok = factory.qa_verdict_ok(v)
        return {"qa_ok": qa_ok, "stories": (v or {}).get("stories"),
                "blocking_open": (v or {}).get("blocking_open"),
                "verdict": (v or {}).get("verdict"), "verdict_json": (v or {}).get("verdict_json"),
                "safety_limited": bool((v or {}).get("safety_limited")),
                "deferred_stories": (v or {}).get("deferred_stories"),
                "timed_out": bool((v or {}).get("timed_out")),
                "cleanup_incomplete": int((v or {}).get("cleanup_incomplete") or 0),
                "cleanup_threads_incomplete": int((v or {}).get("cleanup_threads_incomplete") or 0),
                "cleanup_processes_incomplete": int((v or {}).get("cleanup_processes_incomplete") or 0),
                "cleanup_process_contained": bool((v or {}).get("cleanup_process_contained")),
                "explorers_done": (v or {}).get("explorers_done"),
                "explorers_total": (v or {}).get("explorers_total"),
                "stories_done": (v or {}).get("stories_done"),
                "stories_total": (v or {}).get("stories_total"),
                "qa_campaign_run_id": (v or {}).get("qa_campaign_run_id"),
                "qa_campaign_key": (v or {}).get("qa_campaign_key"),
                "evidence_policy_revision": (v or {}).get("evidence_policy_revision"),
                "internal_management_wait": bool((v or {}).get("internal_management_wait")),
                "internal_review_states": (v or {}).get("internal_review_states") or {},
                "internal_review_ids": (v or {}).get("internal_review_ids") or [],
                "qa_review_case_ids": (v or {}).get("qa_review_case_ids") or [],
                "authority_decision_ids": (v or {}).get("authority_decision_ids") or []}
    except Exception as e:
        # FAIL-CLOSED: if verification cannot run, we have NO evidence the build is good, so we
        # must not let it ship. Treat an unverifiable build as a QA failure (gate blocks DELIVER).
        return {"qa_ok": False, "error": str(e)[:200]}


RUNNING_TIMEOUT_MIN = 30   # (legacy constant kept for callers/tests; superseded by heartbeat liveness below)
RUNNING_FLOOR_MIN = int(os.environ.get("AOS_JOB_FLOOR_MIN", "20"))       # never reap younger than this
# ARCHITECTURE-OVERHAUL Step 1 — the CORRECT liveness model (Temporal/Step-Functions shape):
# the worker beats heartbeat_at every HEARTBEAT_S on a background timer, DECOUPLED from `claude` output. So a job
# is dead ONLY if its heartbeat lapsed (HEARTBEAT_TIMEOUT_S — the worker process is gone) OR it exceeded the hard
# ceiling (HARD_CEILING_MIN — a runaway). We NEVER reap on output silence. This kills the false-positive that
# killed a healthy-but-quiet build (F8).
HEARTBEAT_S = int(os.environ.get("AOS_JOB_HEARTBEAT_S", "45"))           # worker beats this often (timer, not output)
HEARTBEAT_TIMEOUT_S = int(os.environ.get("AOS_JOB_HEARTBEAT_TIMEOUT_S", "180"))  # missed ~4 beats = worker dead
# Hard ceiling = a LIBERAL runaway backstop, deliberately generous: real Opus builds (build + quality loop + QA)
# legitimately run for hours, and the heartbeat above already catches a genuinely DEAD worker in ~3 min regardless
# of this ceiling. So this only guards a worker that is ALIVE and beating but stuck looping forever (rare) — keep
# it big so it never cuts off slow-but-healthy work (per the "backstops must not be conservative" principle).
HARD_CEILING_MIN = int(os.environ.get("AOS_JOB_CEILING_MIN", "360"))     # 6h runaway backstop (liberal)
# A worker CRASH (process died -> reaped) is transient infra, not a real job failure: re-run the phase
# transparently (builds resume from their _stage_done checkpoint) rather than escalate to the human. Bounded —
# after this many crashes on the SAME phase something is systematically killing the worker, so we surface it.
CRASH_RETRY_MAX = int(os.environ.get("AOS_CRASH_RETRY_MAX", "2"))
# QA is checkpointed and tool-side-effect fenced, so a dead worker can safely receive the same bounded
# infrastructure retry allowance as other phases.  Zero made the first process loss look systematic and routed
# an otherwise resumable campaign into management before a fresh worker had even tried its durable checkpoint.
QA_CRASH_RETRY_MAX = int(os.environ.get("AOS_QA_CRASH_RETRY_MAX", "2"))
# QA owns a durable, progress-leased campaign.  A 30-minute controller guillotine raced the tool's 25-minute
# checkpoint and repeatedly killed healthy finalization.  Use the same liberal runaway backstop as other live,
# heartbeating work; appguard/spend authority and QA's no-progress management path remain the real boundaries.
QA_HARD_CEILING_MIN = int(os.environ.get("AOS_QA_JOB_CEILING_MIN", str(HARD_CEILING_MIN)))
# A QA hard ceiling is only an emergency backstop after this much *progress silence*. This is intentionally
# longer than QA's own renewable progress lease (default 15m): the worker gets the first chance to checkpoint
# itself cleanly. Unlike an absolute wall-clock ceiling, active progress can renew forever until the story is
# genuinely complete; spend authority and kill switches remain independent boundaries.
QA_PROGRESS_STALL_MIN = int(os.environ.get("AOS_QA_PROGRESS_STALL_MIN", "30"))
BUILD_HARD_CEILING_MIN = int(os.environ.get("AOS_BUILD_JOB_CEILING_MIN", "60"))
# IMPLEMENT uses the nominal ceiling only as the earliest point at which a no-progress generation may be
# recovered. Every real factory/project/quality checkpoint renews progress_at, so forward movement can
# continue indefinitely. This is not an absolute wall-clock timeout.
BUILD_PROGRESS_STALL_MIN = int(os.environ.get("AOS_BUILD_PROGRESS_STALL_MIN", "30"))


def _crash_count(thread_id, phase):
    """How many times a worker has CRASHED (been reaped) on this thread's current phase — the bound on
    transparent crash-resume before we stop hiding it and escalate to the human."""
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT count(*) FROM controller_jobs WHERE thread_id=%s AND phase=%s "
                        "AND status='failed' AND result->>'crashed'='true'", (thread_id, phase))
            return cur.fetchone()[0]
    except Exception:
        return CRASH_RETRY_MAX          # on a counting error, DON'T loop forever — treat as exhausted


def _reap_dead_jobs(thread_ids=None, limit=RESUME_SWEEP_BATCH, execution_scope="production"):
    """Reap ONLY jobs whose worker is genuinely dead — its heartbeat lapsed (process gone) OR it blew the hard
    ceiling (runaway) — and NEVER on output silence (overhaul Step 1; the correct Temporal/Step-Functions liveness
    model). A long, quiet-but-alive build keeps beating heartbeat_at on its background timer, so it is never
    falsely reaped (F8). Bumps lease_token to fence a wrongly-reaped-but-alive worker. Returns rows reaped.

    FAST PATH: a PARKED worker whose recorded pid is provably gone is dead RIGHT NOW — we don't wait out the
    20-min floor or the heartbeat timeout for it. Single-box: the worker runs in our PID namespace. Safe in
    only one direction — PID reuse can make a dead worker LOOK alive (_pid_alive True), which merely DELAYS
    its reap to the heartbeat/floor path below; it can never falsely reap a LIVE worker, because _pid_alive
    returns False only on ProcessLookupError (the pid truly doesn't exist). Reaped jobs carry crashed:true so
    advance() transparently re-runs the phase (crash-resume)."""
    if execution_scope not in {"production", "test"}:
        raise ValueError("execution_scope must be 'production' or 'test'")
    _ensure()
    scoped = [int(x) for x in thread_ids] if thread_ids is not None else None
    limit = max(1, min(500, int(limit or 1)))
    reaped_workers = []
    with _conn() as c, c.cursor() as cur:
        cur.execute("""SELECT id,worker_pid,worker_start_ticks,worker_boot_id FROM controller_jobs
                       WHERE status='running' AND worker_pid IS NOT NULL
                         AND execution_scope=%s
                         AND (%s::bigint[] IS NULL OR thread_id=ANY(%s))
                       ORDER BY COALESCE(heartbeat_at,started_at), id LIMIT %s""",
                    (execution_scope, scoped, scoped, limit))
        candidates = cur.fetchall()
        dead_pids = [jid for jid, pid, started, boot in candidates
                     if _pid_alive(pid, started, boot) is False]
        if dead_pids:
            cur.execute("""
                UPDATE controller_jobs cj SET status='failed', lease_token = cj.lease_token + 1,
                    result = COALESCE(cj.result,'{}'::jsonb)
                             || '{"error":"parked worker process gone (pid dead)","status":"failed","crashed":true}'::jsonb,
                    finished_at = now()
                WHERE cj.id = ANY(%s) AND cj.status='running'
                RETURNING cj.worker_pid,cj.worker_start_ticks,cj.worker_boot_id""", (dead_pids,))
            reaped_workers.extend(cur.fetchall())
            c.commit()
    with _conn() as c, c.cursor() as cur:
        cur.execute("""WITH candidates AS (
              SELECT cj.id FROM controller_jobs cj
               WHERE cj.status='running'
                 AND cj.execution_scope=%s
                 AND (%s::bigint[] IS NULL OR cj.thread_id=ANY(%s))
                 AND cj.started_at < now() - make_interval(mins => %s)
                 AND (
                       COALESCE(cj.heartbeat_at,cj.started_at) < now()-make_interval(secs => %s)
                    OR (cj.phase NOT IN ('TESTQA','IMPLEMENT')
                        AND cj.started_at < now()-make_interval(mins => %s))
                    OR (cj.phase='TESTQA'
                        AND cj.started_at < now()-make_interval(mins => %s)
                        AND COALESCE(cj.progress_at,cj.started_at)
                            < now()-make_interval(mins => %s))
                    OR (cj.phase='IMPLEMENT'
                        AND cj.started_at < now()-make_interval(mins => %s)
                        AND COALESCE(cj.progress_at,cj.started_at)
                            < now()-make_interval(mins => %s))
                 )
               ORDER BY cj.started_at,cj.id FOR UPDATE SKIP LOCKED LIMIT %s
            )
            UPDATE controller_jobs cj SET status='failed', lease_token=cj.lease_token+1,
                result=COALESCE(cj.result,'{}'::jsonb)
                       || '{"error":"worker died (heartbeat lapsed or progress stalled after runaway threshold)","status":"failed","crashed":true}'::jsonb,
                finished_at=now()
            FROM candidates c WHERE cj.id=c.id
            RETURNING cj.worker_pid,cj.worker_start_ticks,cj.worker_boot_id""",
                    (execution_scope, scoped, scoped, RUNNING_FLOOR_MIN, HEARTBEAT_TIMEOUT_S, HARD_CEILING_MIN,
                     QA_HARD_CEILING_MIN, QA_PROGRESS_STALL_MIN, BUILD_HARD_CEILING_MIN,
                     BUILD_PROGRESS_STALL_MIN, limit))
        hard_reaped = cur.fetchall()
        reaped_workers.extend(hard_reaped)
        n = len(hard_reaped)
        # A non-research pending result has no external durable run to reconcile. If its worker identity
        # is gone/reused or its heartbeat lapsed, fence it so resume_stalled can advance the failure.
        cur.execute("""SELECT id,worker_pid,worker_start_ticks,worker_boot_id
                       FROM controller_jobs WHERE status='pending' AND phase<>'RESEARCH'
                         AND execution_scope=%s
                         AND (%s::bigint[] IS NULL OR thread_id=ANY(%s))
                         AND started_at < now()-make_interval(mins=>%s)
                         AND COALESCE(heartbeat_at,started_at)<now()-make_interval(secs=>%s)
                       ORDER BY started_at,id LIMIT %s""",
                    (execution_scope, scoped, scoped, RUNNING_FLOOR_MIN, HEARTBEAT_TIMEOUT_S, limit))
        stale_pending = [jid for jid, pid, started, boot in cur.fetchall()
                         if pid is None or _pid_alive(pid, started, boot) is not True]
        if stale_pending:
            cur.execute("""UPDATE controller_jobs SET status='failed',lease_token=lease_token+1,
                result=COALESCE(result,'{}'::jsonb)||
                  '{"error":"pending worker ownership lost","status":"failed","crashed":true}'::jsonb,
                finished_at=now() WHERE id=ANY(%s) AND status='pending'""", (stale_pending,))
        c.commit()
    # Fencing the database row is necessary but not sufficient: the old worker still has CPU, credentials,
    # and child processes. Terminate only the birth-identity-verified process tree after the transaction commits.
    # A replacement generation cannot be launched until the terminal row is observed, and durable tool leases
    # provide a second fence during that short hand-off.
    for pid, started, boot in reaped_workers:
        if pid:
            _terminate_worker_group(pid, started, boot, grace_s=2.0)
    return n + len(dead_pids) + len(stale_pending)


def liveness_selftest():
    """Prove heartbeat liveness plus progress-renewable long work.

    A quiet but healthy worker survives; a lapsed worker is recovered; generic runaway work is bounded; and
    IMPLEMENT may exceed its nominal threshold forever while substantive checkpoints remain fresh. Only an
    old IMPLEMENT generation with both fresh heartbeats *and* stale progress is treated as a runaway.
    """
    _ensure()
    def mk(started_min_ago, beat_secs_ago, phase="IMPLEMENT", progress_min_ago=None):
        with _conn() as c, c.cursor() as cur:
            cur.execute("""INSERT INTO controller_jobs
                              (thread_id, tenant_id, phase, kind, status, started_at, heartbeat_at,
                               progress_at, execution_scope)
                           VALUES (0,'live-selftest',%s,'build','running',
                                   now() - make_interval(mins => %s),
                                   CASE WHEN %s IS NULL THEN NULL ELSE now() - make_interval(secs => %s) END,
                                   CASE WHEN %s::int IS NULL THEN now() - make_interval(mins => %s)
                                        ELSE now() - make_interval(mins => %s) END,
                                   'test')
                           RETURNING id""",
                        (phase, started_min_ago, beat_secs_ago, beat_secs_ago or 0,
                         progress_min_ago, started_min_ago, progress_min_ago or 0))
            jid = cur.fetchone()[0]; c.commit(); return jid
    def status(jid):
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT status FROM controller_jobs WHERE id=%s", (jid,)); return cur.fetchone()[0]
    ok = True
    try:
        alive = mk(45, 20)                 # quiet but within IMPLEMENT ceiling + fresh heartbeat → ALIVE
        dead = mk(40, 600)                 # 40 min old, last beat 10 min ago → worker dead, MUST reap
        runaway = mk(HARD_CEILING_MIN + 30, 10, phase="PROTOTYPE")  # generic ceiling → MUST reap
        build_progressing = mk(BUILD_HARD_CEILING_MIN + 90, 10, progress_min_ago=1)
        build_runaway = mk(BUILD_HARD_CEILING_MIN + 90, 10,
                           progress_min_ago=BUILD_PROGRESS_STALL_MIN + 5)
        young = mk(5, 600)                 # heartbeat lapsed but under the floor → too young, must NOT reap
        _reap_dead_jobs(execution_scope="test")
        checks = [(status(alive) == "running", "quiet-but-beating build is NOT reaped (F8 fixed)"),
                  (status(dead) == "failed", "heartbeat-lapsed worker IS reaped"),
                  (status(runaway) == "failed", "past-hard-ceiling runaway IS reaped"),
                  (status(build_progressing) == "running",
                   "progressing build may exceed nominal IMPLEMENT threshold"),
                  (status(build_runaway) == "failed",
                   "heartbeat-alive but progress-stalled IMPLEMENT runaway IS reaped"),
                  (status(young) == "running", "job under the floor is NOT reaped")]
        for cond, label in checks:
            print(("PASS" if cond else "FAIL") + f": {label}"); ok = ok and cond
        print("liveness_selftest: PASS (output-independent heartbeat liveness; no false reap of a quiet build)"
              if ok else "liveness_selftest: FAIL")
        return 0 if ok else 1
    finally:
        with _conn() as c, c.cursor() as cur:
            cur.execute("DELETE FROM controller_jobs WHERE tenant_id='live-selftest'"); c.commit()


# ARCHITECTURE-OVERHAUL Step 2 — SINGLE OWNER PER BUILD. Every path that ADVANCES a thread (jobd's
# runnable loop, a resume-sweep in ANY process, a live worker completion) first takes this per-thread
# Postgres advisory lock. At most one driver advances a given thread at a time, so two sweepers — or a
# sweeper racing jobd — can never double-advance the same build. jobd imports this so the lock NAMESPACE
# is defined in exactly one place (no drifting magic numbers).
_DRIVE_LOCK_NS = 841000


def _exit_conn_quietly(cm):
    if cm is not None:
        try:
            cm.__exit__(None, None, None)
        except Exception:
            pass


def _latest_job_status(thread_id):
    """Status of the thread's most recent controller_job (None if it has none). Used to re-check UNDER the
    drive lock that a pre-lock read isn't stale — another owner may have advanced the thread and dispatched
    the next phase (a fresh 'running' job) between our read and our lock acquisition."""
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT status FROM controller_jobs WHERE thread_id=%s ORDER BY id DESC LIMIT 1",
                        (thread_id,))
            r = cur.fetchone()
            return r[0] if r else None
    except Exception:
        return None


def _build_result_ok(job_result: dict) -> bool:
    """True only for explicit successful build outcomes. HOLD/BLOCKED/ERROR values must not advance to QA."""
    job_result = job_result or {}
    result_name = str(job_result.get("result") or "").strip().upper()
    status_name = str(job_result.get("status") or "").strip().lower()
    return (
        bool(job_result.get("shipped"))
        or bool(job_result.get("passed"))
        # project.build_complex's terminal success contract is INTEGRATED. Omitting it caused a verified,
        # deployable complex build to be recorded as build.ok=false and wastefully routed back to BUILD.
        or result_name in ("OK", "PASS", "PASSED", "SUCCESS", "SUCCEEDED", "SHIPPED", "GREEN", "INTEGRATED")
        or status_name in ("ok", "pass", "passed", "success", "succeeded", "shipped")
    )


@contextlib.contextmanager
def thread_drive_lock(thread_id):
    """Yield True if THIS caller now owns the right to advance `thread_id` (lock acquired), False if another
    driver already holds it (caller must skip and let the owner proceed). Session-scoped advisory lock held
    on a dedicated connection for the whole block. FAIL-CLOSED (yields False) on a DB hiccup: uncertain
    ownership must defer to a later tick, never create duplicate controller drivers during DB distress.

    Structured so the generator yields EXACTLY once on every path (setup error, not-owned, owned) and an
    exception raised inside the caller's block propagates normally after the lock is released."""
    cm = None
    conn = None
    got = False
    try:                                              # SETUP: acquire the lock (fail-closed on infra error)
        # Session advisory locks survive transaction boundaries. Use autocommit
        # so the dedicated pooled backend is `idle`, never `idle in transaction`,
        # while advance() performs model/network work under the ownership lock.
        cm = connection(autocommit=True)
        conn = cm.__enter__()
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s, %s)", (_DRIVE_LOCK_NS, int(thread_id)))
            got = cur.fetchone()[0]
    except Exception:
        _exit_conn_quietly(cm)
        # Ownership uncertainty is not ownership.  Failing open here creates
        # duplicate controller drivers precisely while PostgreSQL is distressed.
        # The durable transition remains runnable and a later tick retries it.
        yield False
        return
    if not got:
        _exit_conn_quietly(cm)
        yield False                                   # another driver owns this thread -> caller skips
        return
    try:
        yield True                                    # we hold the lock — caller drives
    finally:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s, %s)", (_DRIVE_LOCK_NS, int(thread_id)))
        except Exception:
            pass                                      # closing the session below drops the lock regardless
        _exit_conn_quietly(cm)


def _advance_owned(thread_id, job_result=None):
    """Advance the INTERACTIVE (say()-initiated) path under the single-owner drive lock — exactly as jobd
    and resume_stalled do. Without this a CEO chat turn could advance a thread at the very instant a sweep or
    a jobd tick advances the same thread (the double-driver race). If another driver currently owns the
    thread we skip: the transition the caller already persisted (awaiting cleared, phase set) is carried
    forward by that owner's in-flight advance or the next jobd tick within seconds, so no progress is lost —
    and _dispatch's single-writer guard remains the final backstop. Ownership failures defer safely."""
    with thread_drive_lock(thread_id) as owned:
        if owned:
            advance(thread_id, job_result=job_result)


def _resume_agentic_answers(limit=1, execution_scope="production"):
    """Apply answered typed authority requests to their durable lifecycle phase."""
    try:
        import decisionchain
        answered = decisionchain.reconcile_human_answers(limit=limit)
    except Exception:
        return 0
    resumed = 0
    for item in answered:
        thread_id, tid = item.get("thread_id"), item.get("tenant_id")
        if thread_id is None:
            continue
        with thread_drive_lock(thread_id) as owned:
            if not owned:
                continue
            # Another sweeper may have selected the same durable answer before
            # we acquired this thread lock.  Recheck the application ack while
            # holding ownership so only the first successful side effect runs.
            if not decisionchain.needs_application(item.get("id"), tid):
                continue
            s = _st(thread_id) or {}
            if s.get("execution_scope", "production") != execution_scope:
                continue
            outcome = item.get("outcome") or {}
            action = outcome.get("action") or "revise"
            answer = str(outcome.get("human_answer") or "").strip()
            if action == "cancel":
                cancel(tid, thread_id, reason="cancelled by answered authority request", who="user")
                decisionchain.mark_applied(item.get("id"), tid)
                resumed += 1
                continue
            if _apply_qa_budget_answer(item, s):
                decisionchain.mark_applied(item.get("id"), tid)
                resumed += 1
                continue
            if _apply_build_budget_answer(item, s):
                decisionchain.mark_applied(item.get("id"), tid)
                resumed += 1
                continue
            if s.get("phase") == "OPTIONS":
                options = s.get("options") or []
                selected = outcome.get("selection")
                if isinstance(selected, dict):
                    selected = selected.get("id") or selected.get("option_id")
                if selected is None:
                    selected = _option_ordinal(answer, options)
                chosen_option = next((o for o in options if isinstance(o, dict)
                                      and str(o.get("id")) == str(selected)), None)
                chosen_option = chosen_option or next((o for o in options
                    if isinstance(o, dict) and o.get("recommended")), None)
                chosen_option = chosen_option or next((o for o in options if isinstance(o, dict)), {})
                oid = chosen_option.get("id")
                chosen = {"option_id": oid}
                try:
                    import research
                    chosen = research.select(tid, s.get("research_run_id"), oid) or chosen
                except Exception:
                    pass
                _set(thread_id, chosen_option=chosen, pending_intent=answer or None, awaiting=None)
                _to(thread_id, "DEEP_DESIGN")
                advance(thread_id)
            elif s.get("phase") == "PROTOTYPE":
                if action == "revise":
                    _set(thread_id, plan=None, pending_intent=answer or None, awaiting=None)
                    _to(thread_id, "DEEP_DESIGN")
                    advance(thread_id)
                else:
                    with _conn() as c, c.cursor() as cur:
                        cur.execute("""SELECT result FROM controller_jobs WHERE thread_id=%s
                                       AND kind='design' AND status='done' ORDER BY id DESC LIMIT 1""",
                                    (thread_id,))
                        row = cur.fetchone()
                    _set(thread_id, pending_intent=answer or None, awaiting=None)
                    advance(thread_id, job_result=(row[0] if row and isinstance(row[0], dict) else {}))
            else:
                if action == "revise" and s.get("phase") == "DEEP_DESIGN":
                    _set(thread_id, plan=None, pending_intent=answer or None, awaiting=None)
                else:
                    _set(thread_id, pending_intent=answer or None, awaiting=None)
                advance(thread_id)
            decisionchain.mark_applied(item.get("id"), tid)
            resumed += 1
    return resumed


def _resume_resolved_internal_management(*, execution_scope="production", limit=RESUME_SWEEP_BATCH):
    """Resume parked TESTQA controllers once every durable evidence dispute is terminal.

    The QA actor still owns applying each outcome to its story ledger. This function only reopens a bounded
    worker slice so that actor can consume its durable event/reconcile state. It cannot run while a controller
    job or unresolved dispute exists, and the per-thread drive lock prevents duplicate dispatch.
    """
    limit = max(1, min(500, int(limit or 1)))
    # A clean current-revision replay can supersede a historical disputed finding inside the owning QA
    # coordinator before the independently durable dispute row observes that result. Reconcile those exact
    # terminal identities first; otherwise the generic unresolved-dispute guard below parks this controller
    # forever even though its authoritative runtime ledger has already retired the finding.
    try:
        import qareview
        qareview.reconcile_runtime_resolutions(limit=limit)
    except Exception:
        # Controller recovery remains fail-closed: a reconciliation outage simply leaves the internal
        # management checkpoint parked for the next sweep rather than bypassing an unresolved dispute.
        pass
    with _conn() as c, c.cursor() as cur:
        _set_controller_db_timeouts(cur)
        cur.execute("""SELECT cs.thread_id,cs.tenant_id
                         FROM controller_state cs
                        WHERE cs.phase='TESTQA' AND cs.awaiting='internal_management'
                          AND cs.execution_scope=%s
                          AND NOT EXISTS (SELECT 1 FROM controller_jobs j
                                           WHERE j.thread_id=cs.thread_id
                                             AND j.status IN ('pending','running'))
                          AND NOT EXISTS (SELECT 1 FROM qa_evidence_disputes q
                                           WHERE q.tenant_id=cs.tenant_id
                                             AND q.thread_id=cs.thread_id
                                             AND q.status NOT IN ('resolved','closed','cancelled'))
                        ORDER BY cs.updated_at,cs.thread_id LIMIT %s""",
                    (execution_scope, limit))
        candidates = cur.fetchall()
    resumed = 0
    for thread_id, tenant_id in candidates:
        with thread_drive_lock(thread_id) as owned:
            if not owned:
                continue
            with _conn() as c, c.cursor() as cur:
                _set_controller_db_timeouts(cur)
                cur.execute("""UPDATE controller_state cs SET awaiting=NULL,
                                      job_status='QA management resolved; resuming durable checkpoint',
                                      updated_at=now()
                                WHERE cs.thread_id=%s AND cs.tenant_id=%s
                                  AND cs.phase='TESTQA' AND cs.awaiting='internal_management'
                                  AND cs.execution_scope=%s
                                  AND NOT EXISTS (SELECT 1 FROM controller_jobs j
                                                   WHERE j.thread_id=cs.thread_id
                                                     AND j.status IN ('pending','running'))
                                  AND NOT EXISTS (SELECT 1 FROM qa_evidence_disputes q
                                                   WHERE q.tenant_id=cs.tenant_id
                                                     AND q.thread_id=cs.thread_id
                                                     AND q.status NOT IN ('resolved','closed','cancelled'))
                                RETURNING cs.thread_id""",
                            (int(thread_id), str(tenant_id), execution_scope))
                claimed = cur.fetchone()
            if not claimed:
                continue
            advance(int(thread_id))
            resumed += 1
    return resumed


def resume_stalled(execution_scope="production"):
    """Crash-recovery sweep (run from the scheduler). Recovers EVERY durable job a killed worker left
    behind on the 'fleet' gate — not just the clean 'done' ones the original query saw (#10):
      * status='running' older than RUNNING_TIMEOUT_MIN -> the worker died (often before writing status
        at all), so the row would pin the thread on 'fleet' forever -> flip it to 'failed' durably.
      * status='done'   -> the worker finished but died before calling advance() -> advance now.
      * status='failed' -> the worker caught an error but its advance() was lost -> advance now, which
        surfaces the failure (advance() handles error/failed dicts instead of re-dispatching).
    For each parked thread we act ONLY on its LATEST job: a thread whose newest job is still legitimately
    'running'/'pending' (not timed out) is left untouched, and stale older jobs never trigger a spurious
    advance.
    RESEARCH threads are special-cased FIRST (block 0): they are reconciled against the real research run,
    so a long fleet that outran the in-worker poll budget — or that completed after the thread was parked
    on a feedback gate by an old false-timeout build — still surfaces its options instead of being orphaned.
    """
    if execution_scope not in {"production", "test"}:
        raise ValueError("execution_scope must be 'production' or 'test'")
    _ensure()
    deadline = time.monotonic() + RESUME_SWEEP_BUDGET_S
    degraded = []
    advanced = _resume_agentic_answers(limit=1, execution_scope=execution_scope)
    # USER-FACING SLA first (#2.6): surface "taking longer than usual" the moment a job overruns its ETA —
    # well before the 30-min crash-reap below — so the same scheduler tick that recovers dead workers also
    # keeps live-but-slow jobs honest. Best-effort: a watchdog hiccup must never block crash recovery.
    try:
        sla_watchdog(execution_scope=execution_scope)
    except Exception as exc:
        degraded.append({"source": "sla", "error": str(exc)[:200]})
    # 0) RESEARCH threads are reconciled against the REAL research run (research_runs) — NOT the dispatch
    #    poll. A fleet run that outlived the in-worker poll budget (-> 'pending'), or whose worker/process
    #    died, still reaches a terminal state in its own daemon; pull its result through so the extracted
    #    options are never orphaned — even if an older build already parked the thread on a feedback gate.
    #    This OWNS RESEARCH recovery; the generic fleet sweep below skips RESEARCH to avoid a double-advance
    #    or a spurious failure from a controller_job the reaper marked 'failed' while the run was healthy.
    try:
        import research as _r
        with _conn() as c, c.cursor() as cur:
            _set_controller_db_timeouts(cur)
            cur.execute("""SELECT cs.thread_id, cs.tenant_id, cs.research_run_id, cs.awaiting
                           FROM controller_state cs
                           JOIN research_runs rr ON rr.id=cs.research_run_id
                           WHERE cs.phase='RESEARCH' AND cs.research_run_id IS NOT NULL
                             AND cs.execution_scope=%s
                             AND rr.status IN ('done','failed')
                             AND (rr.status='done' OR cs.awaiting='fleet')
                           ORDER BY cs.thread_id LIMIT %s""", (execution_scope, RESUME_SWEEP_BATCH))
            rrows = cur.fetchall()
        for thread_id, rtid, rid, awaiting in rrows:
            if time.monotonic() >= deadline:
                break
            # Step 2 single-owner: hold the per-thread drive lock across the reconcile+advance so a second
            # sweeper (or jobd's advance loop) can't also advance this thread. Not owned -> the owner has it.
            with thread_drive_lock(thread_id) as owned:
                if not owned:
                    continue
                try:
                    rs = _r.run_state(rtid, rid)
                except Exception:
                    continue
                rstatus = rs.get("status")
                if rstatus == "done":
                    jr = {"run_id": rid, "status": "done", "options": rs.get("options", [])}
                elif rstatus == "failed" and awaiting == "fleet":
                    # Surface the failure once, from the active dispatch gate; don't re-spam a thread already
                    # parked on a feedback gate (the failure was surfaced when it was first parked there).
                    jr = {"run_id": rid, "status": "failed", "error": "research failed"}
                else:
                    continue                      # still running, or an already-surfaced failure — leave it
                # Mark the research job terminal for bookkeeping. NOTE: in PARK mode the detached worker already
                # marks the job 'done' itself (without advancing the phase — advancement is left to this poller),
                # so this UPDATE legitimately claims 0 rows. The anti-double-advance guard must therefore be the
                # PHASE state under the drive lock, NOT this rowcount — otherwise a park-completed research run
                # is stranded at RESEARCH/fleet forever (the job is 'done' so nothing ever re-advances it).
                with _conn() as c, c.cursor() as cur:
                    cur.execute("""UPDATE controller_jobs SET status=%s, finished_at=COALESCE(finished_at, now())
                                   WHERE thread_id=%s AND kind='research' AND status IN ('running','pending')""",
                                (rstatus, thread_id))
                    c.commit()
                    # re-read the live phase/awaiting UNDER the lock: only advance if still parked at RESEARCH
                    cur.execute("SELECT phase, awaiting FROM controller_state WHERE thread_id=%s", (thread_id,))
                    pr = cur.fetchone()
                if not pr or pr[0] != "RESEARCH" or pr[1] is None:
                    continue                      # already advanced by a prior owner -> don't double-advance
                _set(thread_id, awaiting=None)
                advance(thread_id, job_result=jr)  # done -> OPTIONS; failed -> surfaces failure
                advanced += 1
    except Exception as exc:
        degraded.append({"source": "research", "error": str(exc)[:200]})
    try:
        _reap_dead_jobs(limit=RESUME_SWEEP_BATCH, execution_scope=execution_scope)
    except Exception as exc:
        degraded.append({"source": "job-reaper", "error": str(exc)[:200]})
    # A terminal QA-management decision has no fleet job to advance. Reopen the same durable QA phase once
    # all correlated disputes are terminal so its coordinator can apply the outcomes and continue its queues.
    # This is bounded and independently fenced; a failed sweep is surfaced as degraded recovery blindness.
    try:
        advanced += _resume_resolved_internal_management(
            execution_scope=execution_scope, limit=RESUME_SWEEP_BATCH)
    except Exception as exc:
        degraded.append({"source": "qa-internal-management", "error": str(exc)[:200]})
    # 1.5) RESEARCH-CRASH STRAND: a research worker that died BEFORE persisting research_run_id (crash between
    # dispatch and _set(research_run_id=...), or research.start() returning run_id=None) leaves the thread at
    # RESEARCH/fleet with research_run_id IS NULL — invisible to block 0 (needs NOT NULL) AND block 2 (excludes
    # RESEARCH). Nobody would ever advance it => permanent freeze. Here: if such a thread's newest research job
    # is terminal (crashed/failed, i.e. the reaper gave up on it), surface the failure so the CEO can retry,
    # instead of stranding forever. (A still-running job is left alone; a healthy run always persists its id.)
    try:
        with _conn() as c, c.cursor() as cur:
            _set_controller_db_timeouts(cur)
            cur.execute("""SELECT cs.thread_id, cj.status, cj.result
                           FROM controller_state cs
                           JOIN LATERAL (SELECT status, result FROM controller_jobs
                                         WHERE thread_id=cs.thread_id AND kind='research'
                                         ORDER BY id DESC LIMIT 1) cj ON true
                           WHERE cs.phase='RESEARCH' AND cs.awaiting='fleet'
                             AND cs.execution_scope=%s
                             AND cs.research_run_id IS NULL
                             AND cj.status NOT IN ('running','pending')
                           ORDER BY cs.thread_id LIMIT %s""", (execution_scope, RESUME_SWEEP_BATCH))
            strays = cur.fetchall()
        for thread_id, jstatus, jresult in strays:
            if time.monotonic() >= deadline:
                break
            with thread_drive_lock(thread_id) as owned:
                if not owned:
                    continue
                cur_pr = _st(thread_id) or {}
                if cur_pr.get("phase") != "RESEARCH" or cur_pr.get("awaiting") is None or cur_pr.get("research_run_id"):
                    continue                 # advanced / recovered by another owner in the meantime
                _set(thread_id, awaiting=None)
                advance(thread_id, job_result={"status": "failed",
                        "error": "research did not start (worker died before it began); say \"retry\" to run it again"})
                advanced += 1
    except Exception as exc:
        degraded.append({"source": "research-strays", "error": str(exc)[:200]})
    rows = []
    try:
        with _conn() as c, c.cursor() as cur:
            # Terminal-only candidates disappear when advanced, so a persistent healthy prefix cannot starve
            # the tail. The lateral latest-row lookup preserves the original "latest job only" invariant.
            _set_controller_db_timeouts(cur)
            cur.execute("""SELECT cs.thread_id, cj.result, cj.status
                           FROM controller_state cs
                           JOIN LATERAL (
                             SELECT result,status FROM controller_jobs
                              WHERE thread_id=cs.thread_id ORDER BY id DESC LIMIT 1
                           ) cj ON true
                           WHERE cs.awaiting='fleet' AND cs.phase<>'RESEARCH'
                             AND cs.execution_scope=%s
                             AND cj.status NOT IN ('running','pending')
                           ORDER BY cs.thread_id LIMIT %s""", (execution_scope, RESUME_SWEEP_BATCH))
            rows = cur.fetchall()
    except Exception as exc:
        degraded.append({"source": "fleet-terminal", "error": str(exc)[:200]})
    for thread_id, result, status in rows:
        if time.monotonic() >= deadline:
            break
        # Step 2 single-owner: claim the per-thread drive lock before advancing so concurrent sweepers /
        # jobd never double-advance. Re-check the newest job UNDER the lock — another owner may have just
        # advanced it, making our pre-lock read stale.
        with thread_drive_lock(thread_id) as owned:
            if not owned:
                continue
            if _latest_job_status(thread_id) in ("running", "pending", None):
                continue                          # owner advanced it (new job dispatched) or nothing to do
            res = result if isinstance(result, dict) else {}
            if status == "failed" and not (res.get("error") or res.get("status") == "failed"):
                res = {**res, "error": "job failed", "status": "failed"}
            _set(thread_id, awaiting=None)
            advance(thread_id, job_result=res)    # done -> advances phase; failed -> surfaces failure
            advanced += 1
    return {"resumed": advanced, "degraded": degraded,
            "budget_exhausted": time.monotonic() >= deadline,
            "batch_limit": RESUME_SWEEP_BATCH}


def _sla_pageable_phase(phase):
    """Healthy QA slices checkpoint and continue autonomously; their ETA boundary is not a CEO incident."""
    return phase != "TESTQA"


def _claim_sla_warnings(limit=SLA_WATCHDOG_BATCH, execution_scope="production"):
    """Claim one bounded overdue page without holding a transaction across delivery."""
    token = uuid.uuid4().hex
    with _conn() as c, c.cursor() as cur:
        _set_controller_db_timeouts(cur)
        cur.execute("""WITH candidates AS (
                         SELECT thread_id
                           FROM controller_state
                          WHERE awaiting='fleet' AND phase <> 'TESTQA' AND execution_scope=%s
                            AND job_started_at IS NOT NULL AND job_eta_min IS NOT NULL
                            AND now()-job_started_at > make_interval(mins => job_eta_min)
                            AND (job_sla_warned_at IS NULL
                                 OR now()-job_sla_warned_at > make_interval(mins => %s))
                            AND (job_sla_claimed_at IS NULL
                                 OR now()-job_sla_claimed_at > make_interval(mins => %s))
                          ORDER BY job_started_at, thread_id
                          FOR UPDATE SKIP LOCKED
                          LIMIT %s
                       ), claimed AS (
                         UPDATE controller_state cs
                            SET job_sla_claimed_at=now(), job_sla_claim_token=%s
                           FROM candidates c
                          WHERE cs.thread_id=c.thread_id
                         RETURNING cs.thread_id, cs.tenant_id, cs.phase, cs.job_kind,
                                   cs.job_eta_min, cs.job_started_at,
                                   EXTRACT(EPOCH FROM (now()-cs.job_started_at))::int
                       ) SELECT * FROM claimed""",
                    (execution_scope, _SLA_REWARN_MIN, SLA_CLAIM_TTL_MIN,
                     max(1, int(limit or 1)), token))
        rows = cur.fetchall()
    return token, rows


def _finish_sla_warning(thread_id, token, accepted, new_eta=None):
    """Token-fenced completion. Failed delivery releases immediately; a dead claimant cannot stamp."""
    with _conn() as c, c.cursor() as cur:
        _set_controller_db_timeouts(cur)
        if accepted:
            cur.execute("""UPDATE controller_state
                              SET job_sla_warned=true, job_sla_warned_at=now(), updated_at=now(),
                                  job_eta_min=GREATEST(COALESCE(job_eta_min,0)+1, %s),
                                  job_sla_claimed_at=NULL, job_sla_claim_token=NULL
                            WHERE thread_id=%s AND awaiting='fleet' AND job_sla_claim_token=%s""",
                        (int(new_eta or 1), thread_id, token))
        else:
            cur.execute("""UPDATE controller_state
                              SET job_sla_claimed_at=NULL, job_sla_claim_token=NULL
                            WHERE thread_id=%s AND job_sla_claim_token=%s""", (thread_id, token))
        return cur.rowcount


def sla_watchdog(limit=SLA_WATCHDOG_BATCH, execution_scope="production"):
    """USER-FACING SLA WATCHDOG (#2.6). The moment a still-running fleet job overruns its ETA, post ONE
    visible "this is taking longer than usual — retry or cancel?" heads-up (with the retry/cancel
    affordances the failure UI already understands), well BEFORE the 30-min crash-reaper — so the CEO is
    never left staring at a static "give me a little time" bubble wondering whether the job is dead.

    Throttled RE-PING (#5): warns the FIRST time a job overruns its ETA, then AGAIN every _SLA_REWARN_MIN
    minutes while it keeps overrunning — so a CEO who left the tab gets a fresh heads-up instead of one-and-
    done silence — but never twice inside the same window. The claim UPDATE is guarded on `awaiting='fleet'`
    + the re-warn throttle so a racing tick, or a job that finishes mid-sweep, can never double-post. Each
    warn also RAISES the stored ETA (#2) to at least the current elapsed + a buffer, so the promised range
    tracks reality (no more "~3 min" pinned under a 13-min run). The job keeps running untouched."""
    if execution_scope not in {"production", "test"}:
        raise ValueError("execution_scope must be 'production' or 'test'")
    _ensure()
    token, rows = _claim_sla_warnings(limit, execution_scope)
    warned = 0
    failed = 0
    for thread_id, tid, phase, jk, eta, started_at, elapsed in rows:
        em = int((elapsed or 0) // 60)
        new_eta = max(int(eta or 0) + 1, em + 2)
        again = eta and em >= eta + _SLA_REWARN_MIN     # a follow-up re-ping vs. the first overrun warning
        lead = "It's still running" if again else "This is taking longer than usual — still running"
        started_key = started_at.isoformat() if hasattr(started_at, "isoformat") else str(started_at)
        bucket = max(0, em // max(1, _SLA_REWARN_MIN))
        context_key = f"controller-sla:{thread_id}:{started_key}:{bucket}"
        text = (f"⏳ {lead} ({em}m elapsed; now expecting up to ~{new_eta} min total). "
                "The manager is still monitoring it; you can retry or cancel from the chat.")
        delivered = False
        try:
            import notifications
            result = notifications.send(
                tid, "build", "Still working — taking longer than usual",
                f"Your {(phase or 'current').lower()} step is still running ({em}m elapsed). "
                "I'll post the results in the chat the moment it's done.",
                level="urgent", url=f"/#assistant/{thread_id}", context_key=context_key)
            delivered = isinstance(result, dict) and bool(result.get("id"))
        except Exception:
            delivered = False
        if not delivered:
            _finish_sla_warning(thread_id, token, False)
            failed += 1
            continue
        try:
            _report(tid, thread_id, text,
                    {"kind": "sla_warning", "phase": phase, "job": jk, "elapsed_min": em,
                     "eta_min": new_eta, "reping": bool(again), "actions": ["retry", "cancel"],
                     "context_key": context_key}, urgent=False)
        except Exception:
            # The tenant notification is the durable always-available delivery. Chat is idempotent and will
            # be retried only if finalization itself is lost; do not manufacture an undelivered warning.
            pass
        if not _finish_sla_warning(thread_id, token, True, new_eta):
            failed += 1
            continue
        audit.append(actor="loopcontroller", action="SLAWarn", resource=str(thread_id), decision=phase,
                     payload={"elapsed_min": em, "eta_min": new_eta, "reping": bool(again)})
        warned += 1
    return {"warned": warned, "failed": failed, "claimed": len(rows), "batch_limit": int(limit)}


MAX_BUILD_RETRY = int(os.environ.get("AOS_MAX_BUILD_RETRY", "3"))
BUILD_BUDGET_USD = float(os.environ.get("AOS_BUILD_BUDGET_USD", "40"))   # hard $ ceiling for ONE product's build


def _spend_usd(product):
    """Real cumulative $ this product's build has spent (from the same traces ledger appguard reads)."""
    try:
        import appguard
        return float(appguard.economics(product).get("spend") or 0.0)
    except Exception:
        return 0.0


def _product_spend_policy(product):
    """Return the product's actual persisted circuit-breaker envelope.

    ``_spend_usd`` is a product-lifetime ledger that includes build, QA, and review calls. Comparing it to a
    separate fixed build fallback made every post-QA repair look over budget even after the CEO had raised the
    product cap. The appguard policy is the authoritative cumulative envelope; the environment fallback is
    used only when that ledger cannot be read.
    """
    try:
        import appguard
        policy = appguard._policy(product)
        return {"cap": max(0.0, float(policy.get("spend_cap") or 0.0)),
                "status": str(policy.get("status") or "active"),
                "reason": policy.get("reason")}
    except Exception:
        return {"cap": max(0.0, BUILD_BUDGET_USD), "status": "unknown", "reason": None}


def _next_product_spend_cap(spent, current_cap):
    """Request enough runway for a meaningful bounded retry, rounded to an auditable $50 boundary."""
    target = max(float(current_cap or 0.0) * 1.25, float(spent or 0.0) + 100.0, 50.0)
    return float(int((target + 49.999999) // 50) * 50)


def _qa_spend_gate(thread_id, state):
    """Stop new QA shifts at an actual product spend boundary and open one typed authority request.

    The circuit breaker previously wrote a pause marker but the controller kept dispatching QA workers, so
    they either spent past the boundary (because background threads lost product context) or ran browsers whose
    model judgements were guaranteed to be refused. Existing work is allowed to checkpoint; this gate controls
    the next shift and never fabricates a generic CEO/debugging request.
    """
    state = dict(state or {})
    product, tid = state.get("product"), state.get("tenant_id")
    if not product or not tid:
        return True
    try:
        import appguard
        policy = appguard._policy(product)
        economics = appguard.economics(product)
        blocked = appguard.blocks(product)
    except Exception:
        return True                       # factory's independent spawn chokepoint still fails closed on a pause
    if not blocked:
        return True
    spent = float(economics.get("spend") or 0.0)
    cap = float(policy.get("spend_cap") or appguard.DEFAULT_CAP)
    # Ask one concrete yes/no question. A round $50 step above both the current cap and observed spend avoids
    # an answer that immediately re-trips on the next model call while remaining tightly bounded.
    requested_cap = float(max(50, int((max(cap * 1.25, spent + 100) + 49) // 50) * 50))
    amount = max(0.0, requested_cap - cap)
    question = (f"QA for {product} has a tracked model-cost estimate of ${spent:.2f} and reached its "
                f"${cap:.2f} standing limit. Authorize ${max(0.0, requested_cap - spent):.2f} more under "
                f"this ledger by raising its cumulative cap to ${requested_cap:.2f} so the checkpointed "
                "campaign can continue?")
    decision = _agentic_lifecycle_decision(
        tid, thread_id, "qa_budget_extension",
        {"product": product, "campaign_spent_usd": spent, "budget_usd": cap,
         "requested_cap_usd": requested_cap, "amount_usd": amount, "question": question,
         "allowed_actions": ["proceed", "cancel", "request_human"]},
        f"{product}:{cap:.2f}:{requested_cap:.2f}", authority_kind="spend",
        default={"action": "request_human", "boundary": "spend", "confidence": 1.0,
                 "amount_usd": amount, "campaign_spent_usd": spent, "rationale": question})
    if decision.get("status") == "human_wait" or decision.get("action") == "request_human":
        _job_clear(thread_id)
        _set(thread_id, awaiting="user_feedback")
        _report(tid, thread_id, question,
                {"kind": "authority_required", "boundary": "spend", "product": product,
                 "spent_usd": round(spent, 2), "current_cap_usd": cap,
                 "requested_cap_usd": requested_cap, "decision": decision,
                 "actions": ["authorize", "cancel"]}, urgent=True)
        audit.append(actor="qa-director", action="QASpendAuthorityRequired", resource=product,
                     decision="spend", payload={"spent": spent, "cap": cap,
                                                "requested_cap": requested_cap}, tenant_id=tid)
        return False
    return True


def _apply_qa_budget_answer(item, state):
    """Apply an explicitly answered fixed-cap request before resuming TESTQA."""
    if (item or {}).get("decision_type") != "qa_budget_extension":
        return False
    payload = dict((item or {}).get("state") or {})
    outcome = dict((item or {}).get("outcome") or {})
    action = str(outcome.get("action") or "revise").lower()
    tid, thread_id = item.get("tenant_id"), item.get("thread_id")
    product = payload.get("product") or (state or {}).get("product")
    if action == "cancel":
        cancel(tid, thread_id, reason="QA spend extension declined", who="user")
        return True
    if action != "proceed":
        _set(thread_id, awaiting="user_feedback")
        _report(tid, thread_id,
                "The QA spend extension was not authorized, so the release remains safely checkpointed.",
                {"kind": "authority_declined", "boundary": "spend", "product": product}, urgent=True)
        return True
    try:
        cap = float(payload.get("requested_cap_usd"))
        if not product or cap <= 0:
            raise ValueError("invalid authorized QA cap")
        import appguard
        import killswitch
        current = appguard._policy(product)
        appguard.set_policy(product, cap=cap, loss=current.get("loss_limit"))
        appguard.resume(product)
        # The automatic spend stop owns both scopes.  Raising appguard's cap
        # without clearing these exact stops leaves advance() permanently
        # fenced, making an approved request appear accepted while no work can
        # resume.  Clear only the product/thread scopes tied to this decision.
        if killswitch.is_halted(product).get("halted"):
            killswitch.resume(product)
        thread_scope = f"thread-{int(thread_id)}"
        if killswitch.is_halted(thread_scope).get("halted"):
            killswitch.resume(thread_scope)
    except Exception as exc:
        _set(thread_id, awaiting="user_feedback")
        _report(tid, thread_id, f"The authorized cap could not be applied safely: {str(exc)[:180]}",
                {"kind": "authority_apply_failed", "boundary": "spend", "product": product}, urgent=True)
        return True
    _set(thread_id, awaiting=None)
    audit.append(actor="human", action="QASpendCapAuthorized", resource=str(product), decision=str(cap),
                 payload={"decision_id": item.get("id"), "answer": outcome.get("human_answer")}, tenant_id=tid)
    advance(thread_id)
    return True


def _apply_build_budget_answer(item, state):
    """Apply a human-approved cumulative product cap before another build/repair dispatch.

    Generic decision reconciliation only clears ``awaiting``. For a spend boundary that is insufficient: the
    factory's independent appguard/killswitch checks would refuse the very next agent call and reopen the same
    request. Apply the exact persisted cap and clear only this product/thread's automatic stops first.
    """
    if (item or {}).get("decision_type") != "build_budget_extension":
        return False
    payload = dict((item or {}).get("state") or {})
    outcome = dict((item or {}).get("outcome") or {})
    action = str(outcome.get("action") or "revise").lower()
    tid, thread_id = item.get("tenant_id"), item.get("thread_id")
    product = payload.get("product") or (state or {}).get("product")
    if action == "cancel":
        cancel(tid, thread_id, reason="build spend extension declined", who="user")
        return True
    if action != "proceed":
        _set(thread_id, awaiting="user_feedback")
        _report(tid, thread_id,
                "The build spend extension was not authorized, so the release remains safely checkpointed.",
                {"kind": "authority_declined", "boundary": "spend", "product": product}, urgent=True)
        return True
    try:
        import appguard
        import killswitch
        spent = _spend_usd(product)
        current = appguard._policy(product)
        requested = payload.get("requested_cap_usd")
        cap = float(requested) if requested is not None else _next_product_spend_cap(
            spent, current.get("spend_cap"))
        if not product or cap <= spent:
            raise ValueError("authorized cumulative cap does not exceed recorded spend")
        appguard.set_policy(product, cap=cap, loss=current.get("loss_limit"))
        appguard.resume(product)
        if killswitch.is_halted(product).get("halted"):
            killswitch.resume(product)
        thread_scope = f"thread-{int(thread_id)}"
        if killswitch.is_halted(thread_scope).get("halted"):
            killswitch.resume(thread_scope)
    except Exception as exc:
        _set(thread_id, awaiting="user_feedback")
        _report(tid, thread_id, f"The authorized build cap could not be applied safely: {str(exc)[:180]}",
                {"kind": "authority_apply_failed", "boundary": "spend", "product": product}, urgent=True)
        return True
    _set(thread_id, awaiting=None)
    audit.append(actor="human", action="BuildSpendCapAuthorized", resource=str(product), decision=str(cap),
                 payload={"decision_id": item.get("id"), "answer": outcome.get("human_answer")}, tenant_id=tid)
    advance(thread_id)
    return True


def _autoloop_build(thread_id, tid, product, reason=""):
    """Route failed builds through bounded retries, then durable line management.

    Retry counts and spend remain hard evidence, but they no longer make the CEO
    the default debugger. Reversible retry/reassignment/plan revision stays in
    the org; only a typed authority result may create a human wait.
    """
    spent = _spend_usd(product)
    try:
        import productregistry as _preg
        n = _preg.attempt(product, "build_retry")
    except Exception:
        n = MAX_BUILD_RETRY + 1                     # registry unavailable -> be conservative, escalate
    spend_policy = _product_spend_policy(product)
    current_cap = float(spend_policy.get("cap") or BUILD_BUDGET_USD)
    over_budget = spent >= current_cap or spend_policy.get("status") == "paused"
    if n <= MAX_BUILD_RETRY and not over_budget:
        _to(thread_id, "IMPLEMENT"); _set(thread_id, awaiting=None)     # runnable -> re-dispatch the build
        _report(tid, thread_id,
                f"🔧 Verification didn't pass (attempt {n}/{MAX_BUILD_RETRY}: {str(reason)[:150]}). Handing it "
                f"back to the builder to fix and re-test — nothing needed from you.",
                {"kind": "auto_rebuild", "attempt": n}, urgent=False)
        audit.append(actor="loopcontroller", action="AutoRebuild", resource=str(thread_id), decision=f"attempt-{n}")
        advance(thread_id)
        return

    decision_type = "build_budget_extension" if over_budget else "build_recovery"
    authority_kind = "spend" if over_budget else "internal_recovery"
    requested_cap = _next_product_spend_cap(spent, current_cap) if over_budget else current_cap
    extra = max(0.0, round(requested_cap - spent, 2)) if over_budget else 0.0
    question = (f"The {product} workstream has a tracked model-cost estimate of ${spent:.2f} and reached "
                f"its ${current_cap:.2f} cumulative product cap. Authorize up to ${extra:.2f} more by "
                f"raising that cap to ${requested_cap:.2f} for one bounded repair and re-verification? "
                f"Current release blocker: {str(reason)[:420]}" if over_budget else "")
    decision = _agentic_lifecycle_decision(
        tid, thread_id, decision_type,
        {"product": product, "reason": str(reason)[:1000], "failed_attempts": n,
         "retry_cap": MAX_BUILD_RETRY, "campaign_spent_usd": spent,
         "amount_usd": extra, "budget_usd": current_cap,
         "requested_cap_usd": requested_cap, "question": question,
         "allowed_actions": ["retry", "reassign", "revise", "request_human"]},
        f"{n}:{round(spent, 2)}:{str(reason)[:120]}", authority_kind=authority_kind,
        default={"action": "request_human" if over_budget else "retry", "confidence": 1.0,
                 "boundary": "spend" if over_budget else "none",
                 "amount_usd": extra, "campaign_spent_usd": spent,
                 "question": question, "rationale": question if over_budget
                              else "senior engineering manager authorized one bounded recovery"})
    if decision.get("status") == "human_wait" or decision.get("action") == "request_human":
        _set(thread_id, awaiting="user_feedback"); _to(thread_id, "IMPLEMENT")
        _report(tid, thread_id,
                question or ("The engineering management chain exhausted its standing authority and opened "
                             "one specific decision request. The failed build and evidence remain checkpointed."),
                {"kind": "authority_required", "spent": round(spent, 2),
                 "current_cap_usd": current_cap, "requested_cap_usd": requested_cap,
                 "decision": decision, "actions": ["authorize", "cancel"]}, urgent=True)
        audit.append(actor="loopcontroller", action="BuildAuthorityRequired", resource=str(product),
                     decision=decision.get("boundary") or authority_kind,
                     payload={"spent": round(spent, 2), "attempt": n})
        return
    if decision.get("action") == "revise":
        _set(thread_id, plan=None, awaiting=None,
             pending_intent=str(decision.get("rationale") or reason)[:2000])
        _to(thread_id, "DEEP_DESIGN")
        _report(tid, thread_id, "Engineering escalated internally and product leadership is revising the plan "
                                "before another build. Nothing is needed from you.",
                {"kind": "internal_build_replan", "decision": decision}, urgent=False)
        advance(thread_id)
        return
    _to(thread_id, "IMPLEMENT"); _set(thread_id, awaiting=None)
    _report(tid, thread_id,
            f"Engineering management reviewed {n} failed build attempts and chose "
            f"**{decision.get('action') or 'retry'}** within standing authority. A fresh bounded worker is "
            "continuing; nothing is needed from you.",
            {"kind": "internal_build_recovery", "decision": decision, "attempt": n}, urgent=False)
    audit.append(actor="loopcontroller", action="BuildRecoveryManaged", resource=str(product),
                 decision=decision.get("action") or "retry", payload={"attempt": n, "spent": spent})
    advance(thread_id)


def _to(thread_id, phase):
    _set(thread_id, phase=phase)
    audit.append(actor="loopcontroller", action="PhaseChange", resource=str(thread_id), decision=phase)
    try:  # item 10: durable per-phase ledger in the memory layer (distinct from event history), best-effort
        import companymemory
        s = _st(thread_id) or {}
        companymemory.checkpoint(s.get("tenant_id") or "ceo", str(thread_id), "phase_summary",
                                 f"Entered {phase}" + (f" · product '{s.get('product')}'" if s.get("product") else ""),
                                 actor_id="loopcontroller", phase=phase)
    except Exception:
        pass
    try:
        import workstreamspine
        s = _st(thread_id) or {}
        workstreamspine.record_progress(
            s.get("tenant_id") or "ceo", f"controller:{thread_id}", "phase_changed",
            {"phase": phase, "product": s.get("product"), "awaiting": s.get("awaiting"),
             "brief": s.get("brief")},
            actor="loopcontroller", substantive=True)
    except Exception as exc:
        try:
            audit.append(actor="loopcontroller", action="WorkstreamProgressRecord",
                         resource=str(thread_id), decision="deferred",
                         payload={"phase": phase, "error": str(exc)[:300]})
        except Exception:
            pass


def _store_user(tid, thread_id, msg):
    """Persist a user turn, SUPPRESSING a consecutive identical duplicate (a double-tapped send / client
    retry). Returns True if it stored a new turn, False if it suppressed an exact repeat of the last user
    message — so callers can also skip re-posting a duplicate reply."""
    with _conn() as c, c.cursor() as cur:
        cur.execute("""SELECT content FROM chat_messages WHERE thread_id=%s AND role='user'
                       ORDER BY id DESC LIMIT 1""", (thread_id,))
        last = cur.fetchone()
        if last and last[0] == msg:
            return False
        cur.execute("INSERT INTO chat_messages (thread_id, tenant_id, role, content) VALUES (%s,%s,'user',%s)",
                    (thread_id, tid, msg))
        c.commit()
    return True


def _llm(tid, thread_id, sysp, s, on_delta=None):
    # CONSENT/PROVIDER ON FILE (#3.4): _llm only ever runs AFTER say()'s consent + provider gates have BOTH
    # passed, so those prerequisites are satisfied right now. (a) Drop any stale resolved-gate messages from
    # the context so the model can't parrot an old "please accept consent / connect a provider" back at the
    # CEO, and (b) tell it plainly they're on file — together this kills the "re-asked for consent I already
    # accepted" bug. Filter BEFORE the [-12:] window so the note + filtered turns are what the model sees.
    hist = [m for m in orchestrator.history(tid, thread_id)
            if (m.get("meta") or {}).get("kind") not in _RESOLVED_GATE_KINDS][-12:]
    convo = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in hist)
    onfile = ("NOTE: AI-processing consent is already on file and a model provider is connected — you are "
              "cleared to proceed. Do NOT ask the CEO to accept consent or connect a provider again; if an "
              "earlier turn asked for either, treat it as already resolved.")
    task = f"{sysp}\n\n{onfile}\n\n{_ctx_brief(s)}\n\nCONVERSATION:\n{convo}\n\nReply now:"
    # light=True: these are QUICK conversational turns (clarify / scope / plan-draft chat). Route them to the
    # FAST model (haiku) with a minimal prompt and NO estimate handshake, so a reply feels near-instant instead
    # of blocking ~30s on a cold Opus + full charter. `task` already carries the system prompt + context, so we
    # lose nothing. Heavy work (research/build/QA/review) keeps calling factory.agent WITHOUT light.
    # STREAMING fast path: when the caller supplies on_delta (the console's SSE endpoint), stream the reply
    # token-by-token via factory.agent_stream so the chat bubble fills live instead of revealing the whole
    # message after a blocking turn. agent_stream runs the SAME gates + fast model as agent(light=True); on
    # ANY stream failure we fall back to the proven blocking path so a streamed reply is never worse. The
    # console's on_delta hides leading control markup ([[RESEARCH]]/[[PLAN]]) from the wire — the FULL text
    # returned here (incl. those blocks) is what we parse/persist below, identical to the non-stream path.
    # STREAMING fast path (first attempt only): stream token-by-token when the caller wants it.
    r = None
    if on_delta is not None and hasattr(factory, "agent_stream"):
        r = factory.agent_stream("research-growth", str(factory.PRODUCTS), task, on_delta, tools=[])
        if r.get("cancelled"):
            # The CEO hit Stop (client gone). factory already terminated the worker; DISCARD this turn —
            # raise so say() never persists a reply for it and never falls back to a fresh (billed) run.
            raise factory.StreamStopped("conversational turn cancelled by user")
        if r.get("failed") or r.get("rc") not in (0,) or not (r.get("out_full") or r.get("out")):
            r = None    # stream errored/empty -> fall through to the blocking (retried) path
    # RESILIENT blocking call (F1): a single transient CLI timeout must NOT strand the thread with a bare
    # "timeout" reply and no phase change. Retry with backoff + a longer timeout each attempt; only on genuine
    # exhaustion return an HONEST, RESUMABLE message (no control block, so the phase safely holds).
    def _ok(res):
        out = (res.get("out_full") or res.get("out") or "").strip()
        return res.get("rc") in (0,) and out and out.lower() != "timeout"
    for attempt in range(3):
        if r is not None and _ok(r):
            break
        r = factory.agent("research-growth", str(factory.PRODUCTS), task, tools=[], light=True,
                          timeout=90 + attempt * 60)
        if _ok(r):
            break
        time.sleep(2 * (attempt + 1))
    if not _ok(r):
        # Exhausted — surface the actual typed failure instead of calling auth, quota, budget, and provider
        # errors all "timeout". The complete attempt I/O is also persisted under controller-<thread>.
        failure = str(r.get("reason") or r.get("blocker") or r.get("out") or "model call failed")
        try:
            import redact
            failure = redact.scrub(failure)
        except Exception:
            pass
        failure = re.sub(r"\s+", " ", failure).strip()[:240]
        return (f"⚠️ The model call could not complete ({failure}). Say \"retry\" and I'll pick right back "
                "up from this checkpoint.")
    # Use the COMPLETE output (out_full) — never the tail-truncated 'out'. The controller's reply carries
    # leading control blocks ([[RESEARCH]]/[[PLAN]]); a >1500-char plan would lose its OPENING tag under
    # front-truncation, so _parse_block fails (plan never persists) and a dangling [[/PLAN]] leaks to chat.
    return (r.get("out_full") or r.get("out") or "").strip() or "Tell me a bit more."


_PLAN_FIELDS = "name|platform|kind|plan|agentic|charter"

# The target platforms the CEO pipeline understands. Each maps (in factory/project) to a real build stack +
# the QA harness that can actually exercise it — or, for targets with no toolchain on this box, an HONEST
# "built, device-QA needs a real device/simulator" verdict instead of a faked pass.
_PLATFORMS = ("web", "game-web", "mobile-ios", "mobile-android", "mobile-cross",
              "desktop", "pc-game", "cli", "api", "lib")


def _parse_plan(body):
    def f(name, d=""):
        # A field runs until the NEXT known field label — which the model routinely bolds (**kind:**). The old
        # terminator (\n[a-z]+:) missed a bolded label, so every field then greedily ate the rest of the block
        # (observed live: kind captured "web\nplan\n..." -> squashed to garbage -> wrong builder). Match the
        # label optionally wrapped in markdown, and terminate ONLY on a real next field (never an incidental
        # "invariants:" inside the plan bullets).
        m = re.search(rf"{name}\s*[*_`]*\s*:\s*(.+?)"
                      rf"(?:\n\s*[*_`>#-]*\s*(?:{_PLAN_FIELDS})\s*[*_`]*\s*:|\Z)", body, re.S | re.I)
        if not m:
            return d
        v = m.group(1).strip()
        # Models routinely wrap field VALUES in markdown emphasis (**bold**, `code`) or bold the LABEL
        # ("**name:** finance-tracker" -> capture "** finance-tracker"). Strip surrounding *,_,` and stray
        # leading emphasis so scalar fields don't come back as "**". The multi-line `plan` bullets keep their
        # internal "- "/"**" — we only trim the head/tail of the whole captured value.
        v = re.sub(r"^[\s*_`>#]+", "", v)
        v = re.sub(r"[\s*_`]+$", "", v)
        return v.strip() or d
    # name -> a clean slug token (drop any residual markdown/punctuation the model added)
    name = re.sub(r"[^A-Za-z0-9._-]", "", (f("name", "app").split() or ["app"])[0])[:24] or "app"
    kind = re.sub(r"[^a-z]", "", f("kind", "service").lower())          # "service." / "**web**" -> "web"
    platform = re.sub(r"[^a-z-]", "", f("platform", "").lower())        # "**web**" -> "web"; "" if absent
    return {"name": name,
            "kind": kind if kind in ("lib", "web", "service", "project") else "service",
            # target platform: honour it if recognized; else infer a sane default from kind (a 'web' build kind
            # is a web platform; anything else with no platform declared falls back to 'lib' — a plain library).
            "platform": platform if platform in _PLATFORMS else ("web" if kind == "web" else "lib"),
            "plan": f("plan"), "agentic": f("agentic", "").strip(),   # free-text: feature(s) + invocation
            "charter": f("charter", "Build a small, well-tested product.")}


def _plan_full_text(plan, raw=""):
    """Compose the COMPLETE, human-readable plan text from the parsed fields (#3) so the console can render
    the whole plan — title, charter, the full bullet list, and the agentic feature — not a one-line charter.
    Falls back to the raw [[PLAN]] block body if the parsed fields are somehow empty."""
    parts = [f"**{plan.get('name', 'app')}** ({plan.get('kind', 'service')})"]
    if plan.get("charter"):
        parts.append(plan["charter"].strip())
    if plan.get("plan"):
        parts.append("Plan:\n" + plan["plan"].strip())
    ag = (plan.get("agentic") or "").strip()
    if ag and ag.lower() != "none":
        parts.append("Agentic feature: " + ag)
    full = "\n\n".join(p for p in parts if p).strip()
    return full or (raw or "").strip()


_NEGATION = re.compile(r"\b(not|no|don'?t|isn'?t|aren'?t|doesn'?t|didn'?t|never|stop|wait|hold|bad|wrong|"
                       r"terrible|awful|hate|nope|nah|un-?happy|change|revise|redo it differently|"
                       r"instead|rather|but )\b", re.I)
_AFFIRM = re.compile(r"\b(looks good|approve|approved|go ahead|yes|yep|yeah|ship it|do it|ready|lgtm|"
                     r"perfect|great|good|sounds good|proceed|retry|re-?run|try again|redo|run it again)\b", re.I)


def _affirmative(msg):
    # FAIL-SAFE fallback for _classify_intent (used only when the model call fails). NEGATION-AWARE so the
    # review's bug — "\bgood\b" matching "not good" -> false approval — can NEVER recur: an affirmative word
    # is only an approval when NO negation is present in the message.
    m = (msg or "")
    return bool(_AFFIRM.search(m)) and not _NEGATION.search(m)


# ───────────────────────────────────────────────────────────────────────────────────────────────────
# A2 — the AGENTIC intent gate. The regex waterfall (which read "not good" as approval) is replaced by a
# real model decision: given the CEO's message + what the controller is awaiting, classify intent. Every
# gate decision is now an AI call (cost is not a concern; NORTH-STAR). Fails SAFE to the negation-aware
# _affirmative above, never to a blind approve.
# ───────────────────────────────────────────────────────────────────────────────────────────────────
_INTENT_SYS = (
    "Text-classification task. A user replied to a software build assistant that is waiting for '{awaiting}' "
    "(stage '{phase}'). Classify the reply's INTENT into exactly one label and output ONLY a JSON object, "
    "nothing else:\n"
    '{{"verdict":"approve|reject|revise|proceed|choose|status|steer|question|cancel","option":<int or null>,'
    '"reason":"<=8 words"}}\n'
    "Label meanings — approve: a clear yes / go-ahead / 'looks good'. reject or revise: any dissatisfaction, "
    "doubt, or requested change, e.g. 'not good', 'this looks off', 'change the layout' (a negated phrase like "
    "'not good' is NEVER approve). proceed: hands the decision to the assistant, e.g. 'go with your "
    "recommendation', 'just build it'. choose: picks a numbered option (put the 1-based number in option). "
    "status: asks about progress, e.g. 'done yet?'. cancel: stop/halt. steer: a new instruction to fold in. "
    "question: asks something. If unsure between approve and any negative reading, do not pick approve."
)


# Phrasings that mean "run that step again", as a STANDALONE instruction. Deliberately narrow: it must be
# the whole message (modulo punctuation/politeness), so "don't retry" or a sentence merely containing the
# word is left to the real classifier. Negation-safety is a property the selftest asserts.
_RETRY_RE = re.compile(
    r"^\s*(?:please\s+|just\s+|ok(?:ay)?[,\s]+)*"
    r"(?:retry|re-?try|rerun|re-?run|try\s+again|run\s+it\s+again|start\s+it\s+again|go\s+again)"
    r"(?:\s+it)?(?:\s+please)?[.!\s]*$", re.I)


# The affirmatives the product PRESCRIBES at a gate: 'say "ready"' appears in five places (provider
# connected, consent accepted, quota raised, credentials supplied), 'say "go ahead"' in another. Same rule
# as retry: a word we instruct the CEO to type must be understood deterministically. Routing it through the
# LLM classifier is what turned "retry" into filed feedback and stranded a thread with no way out.
_PROCEED_RE = re.compile(
    r"^\s*(?:ok(?:ay)?[,\s]+|yes[,\s]+|yep[,\s]+|sure[,\s]+|please\s+|just\s+)*"
    r"(?:ready|go\s*ahead|go|continue|proceed|carry\s+on|keep\s+going|resume|done|"
    r"approve(?:\s+the\s+(?:plan|screens|proposal|direction))?|looks\s+good|lgtm)"
    r"(?:\s+now|\s+please|\s+with\s+it)?[.!\s]*$", re.I)

_NEGATION_RE = re.compile(r"\b(?:do\s*n[o']?t|dont|do not|never|no\s+need|not\s+yet|hold\s+off|wait|stop|cancel|abort)\b", re.I)


def _standalone(msg, pattern, max_len=40):
    """True only for an unambiguous, STANDALONE instruction: short, unnegated, and matching end-to-end.
    Anything longer or hedged ("retry but only after we check the budget") falls through to the real
    classifier, which is the right owner for genuine prose."""
    t = str(msg or "").strip()
    if not t or len(t) > max_len:
        return False
    if _NEGATION_RE.search(t):
        return False
    return bool(pattern.match(t))


def _is_retry(msg):
    """True only for an unambiguous, standalone retry instruction (never for a negated or embedded one)."""
    return _standalone(msg, _RETRY_RE)


def _is_proceed(msg):
    """True only for an unambiguous, standalone go-ahead — the affirmatives the product tells the CEO to say."""
    return _standalone(msg, _PROCEED_RE)


def _classify_intent(tid, thread_id, msg, phase, awaiting, options=None, api_key=None):
    """One AI DECISION replacing the regex gates. Returns {verdict, option, reason}. Fail-SAFE: on any model
    error, derive a conservative verdict from the negation-aware _affirmative (approve/steer only)."""
    m = (msg or "").strip()
    if not m:
        return {"verdict": "status", "option": None, "reason": "empty"}
    try:
        opt_txt = ""
        if options:
            opt_txt = " OPTIONS: " + "; ".join(f"{i+1}. {(o.get('title') if isinstance(o,dict) else o)}"
                                                for i, o in enumerate(options))
        sysp = _INTENT_SYS.format(awaiting=awaiting or "-", phase=phase or "-")
        if api_key is not None:                      # factory routes the tenant key via thread-local _ctx
            try:
                factory._ctx.api_key = api_key
            except Exception:
                pass
        reply = factory.agent("classifier", ".",
                              sysp + "\nUSER REPLY: " + m[:600] + opt_txt,
                              light=True, model=factory.CHEAP_MODEL)
        data = factory._extract_json((reply or {}).get("out_full") or (reply or {}).get("out") or "")
        v = (data or {}).get("verdict")
        if v in ("approve", "reject", "revise", "proceed", "choose", "status", "steer", "question", "cancel"):
            opt = data.get("option")
            return {"verdict": v, "option": int(opt) if isinstance(opt, (int, float)) or
                    (isinstance(opt, str) and opt.isdigit()) else None, "reason": data.get("reason", "")}
    except Exception:
        pass
    # fail-safe: negation-aware, conservative
    if _affirmative(m):
        return {"verdict": "approve", "option": None, "reason": "fallback-affirm"}
    return {"verdict": "steer", "option": None, "reason": "fallback-nonaffirm"}


def _is_status_query(msg):
    # A pure "where are we?" check typed while a job runs — it only deserves the honest live status, NOT to be
    # queued as a pending instruction. Keep this tight so real directives ("go with your rec") are NOT swallowed.
    return bool(re.search(r"\b(is it done|done yet|are we (there|done)|there yet|ready yet|finished\??$"
                          r"|how('?s| is| are)\s+(it|we|things|that)\s+(go|do|com|look)|how long|how much longer"
                          r"|any (update|progress|news)|status\??$|where are we|eta\b|still (going|working|on it))\b",
                          (msg or "").lower()))


def _intent_auto_proceed(msg):
    # A mid-flight directive that PRE-AUTHORIZES us to pick the recommended direction and keep moving once
    # results land — "go with your recommendation", "you choose", "best option", "start building", "just build
    # it", "don't wait for me", "proceed". Conservative on purpose: only an explicit hand-off auto-advances.
    m = (msg or "").lower()
    return bool(re.search(r"\b(your (recommendation|rec|pick|call|choice|judgement|judgment)"
                          r"|you (choose|pick|decide|recommend)|whatever you (think|recommend|suggest)"
                          r"|go with (the |your )?(recommend|rec|best|top|first|that)|recommended option"
                          r"|best option|pick (the |a |one)?(recommend|best|top|for me)"
                          r"|start build|start building|begin build|just build|build it"
                          r"|don'?t (wait|ask)|no need to (ask|check)|proceed without|keep (going|moving)"
                          r"|move forward|run with it|full speed)\b", m))


def _option_ordinal(msg, options):
    """Map a TYPED option choice ('option 2', 'the first one', '#3') to that option's id.
    Returns the option id, or None if the message isn't an unambiguous ordinal pick."""
    m = (msg or "").lower()
    words = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
             "one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
    idx = None
    num = re.search(r"\b(?:option|number|num|#|no\.?)\s*#?\s*(\d+)\b", m) or re.search(r"#\s*(\d+)\b", m)
    if num:
        idx = int(num.group(1))
    else:
        for word, n in words.items():
            if re.search(rf"\b{word}\b", m):
                idx = n
                break
    if idx is None or idx < 1 or idx > len(options):
        return None
    opt = options[idx - 1]
    return opt.get("id", idx) if isinstance(opt, dict) else idx


def cancel(tid, thread_id, reason="stopped by user", who="user"):
    """Halt the in-flight run for this thread (the CANCEL the console exposes). Trips the kill-switch on the
    product scope (and a thread-scope) so factory.agent() stops the fleet at its next step boundary, marks
    the running/pending controller_job 'cancelled' (so its worker can't double-advance), clears the
    live-progress fields, and parks the thread on a feedback gate — say "retry" to re-dispatch the phase
    (which lifts the halt via _resume_halts). Idempotent + best-effort; never raises.

    Signature mirrors say()/choose() ((tid, thread_id, ...)); `tid` is accepted for call-symmetry but the
    authoritative tenant is read from controller_state (so a wrong/None caller tid can't mis-route)."""
    s = _st(thread_id)
    if not s:
        return {"error": "no such thread"}
    tid, phase, product = s["tenant_id"], s["phase"], s.get("product")
    scopes = []
    try:
        import killswitch
        if product:
            killswitch.halt(product, reason, set_by=who); scopes.append(product)
        killswitch.halt(f"thread-{thread_id}", reason, set_by=who); scopes.append(f"thread-{thread_id}")
    except Exception:
        pass
    worker_pids = []
    with _conn() as c, c.cursor() as cur:
        cur.execute("""SELECT worker_pid,worker_start_ticks,worker_boot_id FROM controller_jobs
                       WHERE thread_id=%s AND status IN ('running', 'pending') AND worker_pid IS NOT NULL""",
                    (thread_id,))
        worker_pids = [(int(r[0]), r[1], r[2]) for r in cur.fetchall() if r[0]]
        cur.execute("""UPDATE controller_jobs SET status='cancelled',
                          result = COALESCE(result, '{}'::jsonb) || '{"cancelled":true}'::jsonb,
                          finished_at = now()
                       WHERE thread_id=%s AND status IN ('running', 'pending')""", (thread_id,))
        jobs = cur.rowcount; c.commit()
    for pid, started, boot in worker_pids:
        _terminate_worker_group(pid, started, boot)
    # Terminalize the durable subordinate org only after the exact worker generation is gone.  Doing this
    # before process containment leaves a race where a still-running research synthesizer can overwrite
    # ``halted`` with ``done``.  The link is the controller thread/research-run identity (with a narrow legacy
    # QA product fallback), never "all runs for this tenant".
    halted_org_runs = _halt_linked_orchestra_runs(
        tid, thread_id, research_run_id=s.get("research_run_id"), product=product, reason=reason)
    _set(thread_id, awaiting="user_feedback")
    _job_clear(thread_id)
    _report(tid, thread_id,
            f"⏹️ Stopped the **{phase}** step — nothing more will run until you say so. Say \"retry\" to "
            f"start it again, or tell me what to change.", {"kind": "cancelled", "phase": phase})
    audit.append(actor="loopcontroller", action="JobCancelled", resource=str(thread_id), decision=phase,
                 payload={"scopes": scopes, "jobs": jobs, "halted_org_runs": halted_org_runs,
                          "reason": str(reason)[:200]})
    return {"cancelled": True, "phase": phase, "scopes": scopes, "jobs_cancelled": jobs}


def _halt_linked_orchestra_runs(tid, thread_id, *, research_run_id=None, product=None,
                                reason="cancelled"):
    """Close only durable org runs owned by this controller workstream.

    Research has no product yet, so the older product-only QA cleanup could not see it.  A cancelled
    controller therefore killed every OS process while leaving ``research_runs`` and ``orchestra_runs``
    falsely ``running`` forever.  Besides lying to the CEO, those ghosts block migration quiescence.

    The research coordinator carries ``research_run_id``; modern QA coordinators carry ``thread_id`` and
    ``product`` in their context.  Legacy QA checkpoints may lack ``thread_id``, so product matching is
    accepted only for the qa-coordinator role.  Completed actors remain evidence; unfinished actors become
    dead and all claims/leases for the halted generation are released after its worker has been reaped.
    """
    tenant = str(tid or "").strip()
    if not tenant or thread_id is None:
        return 0
    thread_text = str(int(thread_id))
    research_text = None if research_run_id is None else str(research_run_id)
    product_text = None if not product else str(product)
    payload = json.dumps({"cancelled": True, "reason": str(reason)[:240],
                          "thread_id": int(thread_id), "product": product_text,
                          "research_run_id": research_run_id})
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""SELECT DISTINCT r.run_id
                              FROM orchestra_runs r
                             WHERE r.tenant_id=%s AND r.status='running'
                               AND EXISTS (
                                   SELECT 1 FROM orchestra_actors a
                                    WHERE a.run_id=r.run_id AND a.tenant_id=r.tenant_id
                                      AND (
                                          (%s::text IS NOT NULL AND
                                           a.memory->>'research_run_id'=%s::text)
                                       OR a.memory->'context'->>'thread_id'=%s
                                       OR (%s::text IS NOT NULL AND a.role='qa-coordinator'
                                           AND a.memory->'context'->>'product'=%s
                                           AND COALESCE(a.memory->'context'->>'thread_id',%s)=%s)
                                      ))
                             ORDER BY r.run_id""",
                        (tenant, research_text, research_text, thread_text,
                         product_text, product_text, thread_text, thread_text))
            run_ids = [int(row[0]) for row in cur.fetchall()]
            if run_ids:
                cur.execute("""UPDATE orchestra_actors
                                  SET status=CASE WHEN status='done' THEN status ELSE 'dead' END,
                                      result=CASE WHEN status='done' THEN result
                                                  ELSE COALESCE(result,'{}'::jsonb) || %s::jsonb END,
                                      last_active=now(),step_claimed_at=NULL,step_claimed_by=NULL
                                WHERE tenant_id=%s AND run_id=ANY(%s)""",
                            (payload, tenant, run_ids))
                cur.execute("DELETE FROM orchestra_tool_leases WHERE tenant_id=%s AND run_id=ANY(%s)",
                            (tenant, run_ids))
                cur.execute("""UPDATE orchestra_runs
                                  SET status='halted',finished_at=now(),
                                      result=COALESCE(result,'{}'::jsonb) || %s::jsonb
                                WHERE tenant_id=%s AND run_id=ANY(%s) AND status='running'""",
                            (payload, tenant, run_ids))
                halted = cur.rowcount
            else:
                halted = 0
            if research_run_id is not None:
                cur.execute("""UPDATE research_runs SET status='cancelled',finished_at=now()
                                WHERE id=%s AND tenant_id=%s AND status='running'""",
                            (research_run_id, tenant))
            c.commit()
        return halted
    except Exception:
        # Cancellation's OS-process containment and controller-job fence must still succeed if an older
        # database does not yet have the orchestra linkage columns.  The reconciliation sweep can retry.
        return 0


def _halt_agentic_qa_runs(product, reason="cancelled"):
    """Mark product-owned agentic QA org runs halted when their controller job is cancelled.

    The controller worker process may die before qa_agentic can finish its run row. Without this, the orgview
    shows stale QA organizations as `running` forever, which is false liveness and confuses retries.
    """
    if not product:
        return 0
    payload = json.dumps({"cancelled": True, "reason": str(reason)[:240], "product": product})
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""UPDATE orchestra_actors a
                              SET status='dead',
                                  result = COALESCE(a.result, '{}'::jsonb) || %s::jsonb,
                                  last_active=now(),
                                  step_claimed_at=NULL,
                                  step_claimed_by=NULL
                            WHERE a.status NOT IN ('done','dead')
                              AND EXISTS (
                                  SELECT 1 FROM orchestra_actors q
                                  WHERE q.run_id=a.run_id
                                    AND q.tenant_id=a.tenant_id
                                    AND q.role='qa-coordinator'
                                    AND q.memory->'context'->>'product'=%s)""",
                        (payload, product))
            cur.execute("""UPDATE orchestra_runs r
                              SET status='halted', finished_at=now(), result=%s::jsonb
                            WHERE r.status='running'
                              AND EXISTS (
                                  SELECT 1 FROM orchestra_actors q
                                  WHERE q.run_id=r.run_id
                                    AND q.tenant_id=r.tenant_id
                                    AND q.role='qa-coordinator'
                                    AND q.memory->'context'->>'product'=%s)""",
                        (payload, product))
            n = cur.rowcount
            c.commit()
            return n
    except Exception:
        return 0


def _terminate_worker_group(pid, start_ticks=None, boot_id=None, grace_s=5.0):
    """Terminate a parked phase worker and descendant process groups.

    Phase workers are launched with start_new_session=True, and some children (notably the QA browser bridge)
    also start their own sessions. Killing only the root process group can therefore orphan a browser tree.
    Snapshot descendants first, terminate every distinct process group, then fall back to individual pids.
    """
    if not pid:
        return False
    root = int(pid)
    snapshots = process_assurance.scan_snapshots()
    if start_ticks is None or not boot_id:
        # Compatibility for an immediate parent cleaning up a child it just spawned: ancestry itself proves
        # ownership, and we bind the observed birth identity before signaling. Arbitrary legacy PIDs fail shut.
        caller = snapshots.get(os.getpid())
        if caller is None or root not in process_assurance.descendant_pids(caller.identity, snapshots):
            return False
        root_id = snapshots[root].identity
    else:
        root_id = process_assurance.ProcessIdentity(root, int(start_ticks), str(boot_id))
    if not process_assurance.same_process(root_id, snapshots.get(root)):
        return False
    plan = process_assurance.cleanup_plan(root_id, snapshots) + [root_id]
    ok = True
    for expected in plan:
        try:
            if process_assurance.same_process(expected, process_assurance.read_snapshot(expected.pid)):
                os.kill(expected.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except Exception:
            ok = False
    deadline = time.time() + float(grace_s)
    while time.time() < deadline:
        if all(not process_assurance.same_process(p, process_assurance.read_snapshot(p.pid)) for p in plan):
            return ok
        time.sleep(0.1)
    for expected in plan:
        try:
            if process_assurance.same_process(expected, process_assurance.read_snapshot(expected.pid)):
                os.kill(expected.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception:
            ok = False
    return ok and all(not process_assurance.same_process(
        p, process_assurance.read_snapshot(p.pid)) for p in plan)


def _descendant_pids(root_pid):
    children = {}
    try:
        for p in Path("/proc").iterdir():
            if not p.name.isdigit():
                continue
            try:
                parts = (p / "stat").read_text(errors="ignore").split()
                ppid = int(parts[3])
                children.setdefault(ppid, []).append(int(p.name))
            except Exception:
                continue
    except Exception:
        return []
    out, stack = [], list(children.get(int(root_pid), []))
    while stack:
        p = stack.pop()
        out.append(p)
        stack.extend(children.get(p, []))
    return out


def state(thread_id):
    s = _st(thread_id)
    if not s:
        return {"error": "no such thread"}
    live = live_status(thread_id)
    # Merge the LIVE-PROGRESS view so a single state() call tells the console both WHERE the loop is and
    # what it's doing right now (running + elapsed + ETA, or the real awaiting status) — never a false done.
    return {"thread_id": thread_id, "phase": s["phase"], "awaiting": s["awaiting"],
            "org_id": s["org_id"], "product": s.get("product"),
            "running": live.get("running"), "done": live.get("done"), "status": live.get("status"),
            "live": live.get("label"), "elapsed_min": live.get("elapsed_min"),
            "eta_min": live.get("eta_min"), "job_kind": live.get("job_kind")}


def _selftest():
    import os
    import time
    import billing
    import orgs as _orgs
    # The selftest stubs research/factory in this interpreter. A detached worker
    # would re-import the real providers and could spend money, so replace only
    # the process-launch seam with a synchronous, non-background test driver.
    # Production dispatch remains parked-only: there is no environment switch
    # and no daemon-thread fallback that can outlive a terminal durable row.
    global _spawn_parked_worker
    _spawn_was = _spawn_parked_worker

    def _selftest_worker(thread_id, kind, jid):
        result, status = {}, "done"
        try:
            result = _phase_fn(thread_id, kind)() or {}
            if isinstance(result, dict) and (result.get("error") or result.get("blocked")):
                status = "failed"
        except Exception as exc:
            result, status = {"error": str(exc)[:200]}, "failed"
        if _finish_job(thread_id, jid, result, status):
            # Only this fully stubbed selftest drives synchronously; production
            # workers always leave the ownership transition to the poller.
            _set(thread_id, awaiting=None)
            _job_clear(thread_id)
            advance(thread_id, job_result=result)
        return True

    _spawn_parked_worker = _selftest_worker
    tid = billing.signup("loopctl-selftest", "free")["tenant_id"]
    org = _orgs.create(tid, "Test Org", "a test")["org_id"]
    real_agent = factory.agent
    real_build = getattr(factory, "build_product", None)
    import tenantproviders as _tp
    real_tp_resolve = _tp.resolve
    import research as _r, design_fleet as _d, qualityloop as _q
    real = (_r.start, _r.run_state, _r.select, _d.prototype, _q.run, factory.run_grounded_qa)
    # the IMPLEMENT phase now SCAFFOLDS via build_product before the quality loop — stub it to scaffold the
    # registered path (a real build's job), so no real spend and the boundary contract sees a real artifact.
    def _fake_build_product(product, charter, **k):
        try:
            import productregistry as _preg, pathlib
            d = pathlib.Path(_preg.path(product)); d.mkdir(parents=True, exist_ok=True)
            (d / "app.py").write_text("# scaffolded by selftest")
        except Exception:
            pass
        return {"product": product, "shipped": True}
    factory.build_product = _fake_build_product

    tasks = []                                          # every prompt _llm hands the model (for #3.4 checks)

    def fake_agent(role, repo, task, **k):
        # Mirror factory.agent's real contract: a tail-truncated 'out' (1500-char preview) PLUS the
        # complete 'out_full'. A long preamble pushes the OPENING [[..]] tag out of the 1500-char tail,
        # so this is a regression test: _llm MUST read out_full or the opening tag is lost.
        tasks.append(task)
        def both(body):
            full = ("preamble. " * 220) + body          # >1500 chars before the control block
            return {"rc": 0, "out": full[-1500:], "out_full": full}
        if "[[RESEARCH]]" in task:
            return both("Great.\n[[RESEARCH]]\nHow to build a YouTube competitor\n[[/RESEARCH]]")
        if "[[PLAN]]" in task:
            return both("Plan:\n[[PLAN]]\nname: vid\nkind: service\nplan: - api\n- ui\n"
                        "charter: A video API.\n[[/PLAN]]")
        return {"rc": 0, "out": "ok", "out_full": "ok"}
    factory.agent = fake_agent
    research_kw = {}                                        # captures HOW the RESEARCH phase dispatches
    def _fake_rstart(t, o, th, q, **k):
        research_kw.update(k)
        return {"run_id": 999}
    _r.start = _fake_rstart
    _r.run_state = lambda t, rid: {"status": "done", "options": [{"id": 1, "title": "A", "recommended": True}]}
    _r.select = lambda t, rid, oid: {"option_id": oid, "title": "A"}
    _d.prototype = lambda t, o, p, pl, **k: {"screens": 3, "surfaces": ["cockpit", "team", "external"]}
    def _fake_build(product, **k):
        # a REAL (stubbed) build produces its artifact at the REGISTERED path, so the boundary contract
        # (QA requires the artifact to exist) passes — exactly as a real successful build would.
        try:
            import productregistry as _preg, pathlib
            d = pathlib.Path(_preg.path(product)); d.mkdir(parents=True, exist_ok=True)
            (d / "app.py").write_text("# built by selftest")
        except Exception:
            pass
        return {"run_id": 1, "status": "shipped", "shipped": True, "rounds": 1}
    _q.run = _fake_build
    # TESTQA consumes the machine qa verdict (C1): stub the grounded-QA seam with a GREEN verdict shaped
    # exactly like factory.run_grounded_qa's contract (passed / blocking_open / stories / verdict_json).
    _green_gq = lambda product, **k: {
        "passed": True, "blocking_open": 0, "stories": 3,
        "verdict": "ALL 3 STORIES PASSED", "verdict_json": "/tmp/aos-qa/selftest-verdict.json"}
    factory.run_grounded_qa = _green_gq
    _tp.resolve = lambda _tid: {"engine": "codex", "provider": "openai", "key": None,
                                "auth_mode": "subscription"}
    import consent

    def wait(th, target, gate=None, tmax=14):
        for _ in range(tmax * 5):
            s = _st(th)
            if s["phase"] == target and (gate is None or s["awaiting"] == gate):
                return True
            time.sleep(0.2)
        return False
    try:
        th = start(tid, org, execution_scope="test")["thread_id"]
        # CONSENT GATE: pre-consent, say() must REFUSE before touching the LLM (no phase change, no spend) and
        # tell the CEO to accept consent — not silently send their text to the provider.
        pre = say(tid, th, "I want a YouTube competitor")
        consent_gate_ok = (pre.get("blocked") == "consent_required" and _st(th)["phase"] == "DISCOVER")
        consent.record(tid)                                       # CEO accepts AI-processing consent in Settings
        say(tid, th, "I want a YouTube competitor")               # autonomous research + manager decisions
        # Internal/reversible direction, plan, and prototype calls are manager-owned now. The controller must
        # keep advancing without requiring the CEO to type "go ahead" at each phase; only a typed authority
        # boundary may create a human gate. Drive explicit no-gate phase ticks in this synchronous harness.
        phase_trail = []
        human_gate_seen = False
        for _ in range(40):
            current = _st(th) or {}
            phase_trail.append(current.get("phase"))
            if current.get("phase") == "DELIVER":
                break
            if current.get("awaiting") in ("user_feedback", "user_approval", "credentials"):
                human_gate_seen = True
                break
            if current.get("awaiting") is None:
                advance(th)
            else:
                time.sleep(0.05)
        opts_now = _st(th).get("options") or []
        opt = bool(opts_now)
        options_elaborate_ok = opt and not human_gate_seen
        options_nudge_ok = not human_gate_seen
        # (#2) EXPOSE THE RESEARCH DOC + option summaries: research_report() returns the report+options for the
        # org, and every presented option card carries a non-empty summary (not a bare title).
        rr = research_report(tid, org)
        research_report_ok = (isinstance(rr, dict) and "report" in rr and isinstance(rr.get("options"), list)
                              and bool(opts_now)
                              and all((o.get("summary") or "").strip() for o in opts_now if isinstance(o, dict)))
        gate_held = not human_gate_seen                             # internal choices never invent a CEO gate
        in_design = "DEEP_DESIGN" in phase_trail or bool((_st(th).get("plan") or {}).get("name"))
        # PLAN must parse from the (front-truncatable) LLM reply: persisted to state, rendered as a plan
        # card (meta.kind='plan'), and NO dangling control tag leaked into the user-visible chat.
        plan_persisted = bool((_st(th).get("plan") or {}).get("name"))
        with _conn() as c, c.cursor() as cur:
            cur.execute("""SELECT content, meta FROM chat_messages WHERE thread_id=%s AND role='assistant'
                           AND meta->>'kind'='plan' ORDER BY id DESC LIMIT 1""", (th,))
            pc, pm = cur.fetchone()
        plan_card = isinstance(pm, dict) and pm.get("kind") == "plan"
        no_tag_leak = "[[" not in (pc or "")
        # (#3) EXPOSE THE FULL PLAN: the plan meta carries the COMPLETE plan text (meta.plan.full), not just a
        # one-line charter — persisted to state AND on the plan card — so the console can show & explain it.
        plan_full_ok = (bool((_st(th).get("plan") or {}).get("full"))
                        and isinstance(pm, dict) and bool(((pm.get("plan") or {}).get("full")))
                        and "Plan:" in ((_st(th).get("plan") or {}).get("full") or ""))
        proto = "PROTOTYPE" in phase_trail or "IMPLEMENT" in phase_trail or _st(th)["phase"] == "DELIVER"
        # (#4) SURFACE THE DESIGN: the prototype message meta references the design artifact (org + a flag) so
        # the console can link "Review the design".
        with _conn() as c, c.cursor() as cur:
            cur.execute("""SELECT meta FROM chat_messages WHERE thread_id=%s AND role='assistant'
                           AND meta->>'kind'='prototype' ORDER BY id DESC LIMIT 1""", (th,))
            _pr = cur.fetchone()
        design_surface_ok = (bool(_pr) and isinstance(_pr[0], dict) and _pr[0].get("design_ready") is True
                             and _pr[0].get("org") == org)
        deliver = wait(th, "DELIVER")
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT count(*) FROM controller_jobs WHERE thread_id=%s AND status='done'", (th,))
            jobs = cur.fetchone()[0]

        # (1) ETA on kickoff: estimate.py-backed, positive, and surfaced as an HONEST RANGE "~lo-hi min" in a
        # kickoff message — never a false-precision point (#2). The _eta_range helper must bracket the point.
        eta_min = _estimate_runtime("IMPLEMENT", {"kind": "service", "charter": "auth billing dashboard api"})
        with _conn() as c, c.cursor() as cur:
            cur.execute("""SELECT count(*) FROM chat_messages WHERE thread_id=%s AND role='assistant'
                           AND content ~ '~[0-9]+-[0-9]+ min'""", (th,))
            eta_msg = cur.fetchone()[0]
        r_lo, r_hi = _eta_range(eta_min)
        eta_range_ok = r_lo is not None and r_lo < eta_min < r_hi and _eta_phrase(eta_min) == f"~{r_lo}-{r_hi} min"
        eta_ok = isinstance(eta_min, int) and eta_min > 0 and eta_msg >= 1 and eta_range_ok
        # (1c) CONSTANTS RECONCILED with console.py:163 — the controller's coarse fallbacks equal the console's,
        # so the two live-progress bubbles can never promise different numbers.
        consts_ok = _PHASE_ETA_DEFAULT == {"RESEARCH": 10, "PROTOTYPE": 6, "IMPLEMENT": 14,
                                           "TESTQA": qatiming.slice_eta_min()}
        # (1b) RESEARCH ETA REALISM: research is a multi-agent fleet (~10-14 min), so its promised ETA must be
        # realistic (history-backed median of real research_runs when available, else a sane default) — never
        # the old unrealistic ~3 min that under-promised and over-ran.
        research_eta = _estimate_runtime("RESEARCH")
        research_eta_ok = isinstance(research_eta, int) and research_eta >= 8

        # (3) PING: landing async results wrote a tenant notification (not only on failure/final ship).
        import notifications as _n
        ping_ok = len(_n.feed(tid)) >= 1

        # (3.4) CONSENT RE-ASK: every prompt _llm built AFTER consent was recorded must (a) carry the
        # 'consent is already on file' note and (b) NOT replay the stale consent_required gate message into
        # the model context — so the assistant never re-asks for consent the CEO already gave.
        consent_note_ok = any("consent is already on file" in t.lower() for t in tasks)
        no_stale_consent_ctx = not any("accept the AI-processing consent in Settings" in t for t in tasks)
        consent_reask_ok = consent_note_ok and no_stale_consent_ctx

        # (2.6) USER-FACING SLA WATCHDOG: a fleet job that overruns its ETA gets ONE visible
        # 'taking longer than usual — retry or cancel?' heads-up, well before the 30-min reaper, and the
        # watchdog never double-warns the same job.
        th3 = start(tid, org, execution_scope="test")["thread_id"]
        _set(th3, awaiting="fleet")
        _job_begin(th3, "build", 5, "Building…")
        with _conn() as c, c.cursor() as cur:    # backdate start so it has clearly overrun ~5m ETA
            cur.execute("UPDATE controller_state SET job_started_at=now()-interval '9 min' WHERE thread_id=%s",
                        (th3,))
            c.commit()
        w1 = sla_watchdog(execution_scope="test").get("warned", 0)
        with _conn() as c, c.cursor() as cur:   # ETA must be RAISED past the elapsed time (#2)
            cur.execute("SELECT job_eta_min FROM controller_state WHERE thread_id=%s", (th3,))
            eta_after_warn = cur.fetchone()[0]
        w2 = sla_watchdog(execution_scope="test").get("warned", 0)  # immediate re-sweep must NOT re-warn
        # (5) RE-PING ON CONTINUED OVERRUN: after the (hourly by default) re-warn window elapses and the job is
        # STILL overrunning,
        # the watchdog warns + pings AGAIN — not one-and-done silence. Backdate the last-warn + start so both
        # the throttle and the (now-raised) ETA are exceeded, then a fresh sweep must re-warn exactly once.
        with _conn() as c, c.cursor() as cur:
            cur.execute("""UPDATE controller_state
                           SET job_sla_warned_at=now()-interval '90 min',
                               job_started_at=now()-interval '180 min' WHERE thread_id=%s""", (th3,))
            c.commit()
        w3 = sla_watchdog(execution_scope="test").get("warned", 0)  # continued overrun -> re-ping fires again
        with _conn() as c, c.cursor() as cur:
            cur.execute("""SELECT count(*) FROM chat_messages WHERE thread_id=%s AND role='assistant'
                           AND meta->>'kind'='sla_warning'""", (th3,))
            sla_msgs = cur.fetchone()[0]
        sla_ok = (w1 >= 1 and w2 == 0 and w3 >= 1 and sla_msgs == 2
                  and isinstance(eta_after_warn, int) and eta_after_warn >= 9)

        # (2.1) STATUS HONESTY: a message typed WHILE a durable job is in flight must return the TRUE running
        # status (kind='working') and run NO free-form LLM turn — never a hallucinated "Done".
        th4 = start(tid, org, execution_scope="test")["thread_id"]
        _to(th4, "RESEARCH"); _set(th4, awaiting="fleet")
        _job_begin(th4, "research", 6, "Researching directions…")
        n_before = len(tasks)
        r4 = say(tid, th4, "is it done yet?")
        with _conn() as c, c.cursor() as cur:
            cur.execute("""SELECT meta->>'kind' FROM chat_messages WHERE thread_id=%s AND role='assistant'
                           ORDER BY id DESC LIMIT 1""", (th4,))
            last_kind = cur.fetchone()[0]
        status_honest_ok = (r4.get("running") is True and len(tasks) == n_before and last_kind == "working")

        # (2.1b) MID-FLIGHT PRE-AUTHORIZED INTENT: a real directive typed WHILE research runs must be QUEUED
        # (not dropped, no LLM turn, gate held) and then APPLIED when results land — auto-selecting the
        # recommended option into DEEP_DESIGN instead of silently re-parking on the user_approval gate.
        th5 = start(tid, org, execution_scope="test")["thread_id"]
        _to(th5, "RESEARCH"); _set(th5, awaiting="fleet", research_run_id=777)
        _job_begin(th5, "research", 6, "Researching…")
        n5 = len(tasks)
        r5 = say(tid, th5, "go with your recommendation and start building it now")
        intent_queued = (r5.get("queued_intent") is True and len(tasks) == n5
                         and bool((_st(th5).get("pending_intent") or "")) and _st(th5)["awaiting"] == "fleet")
        _set(th5, awaiting=None)                                   # worker clears the gate before advancing
        advance(th5, {"run_id": 777, "options": [{"id": 1, "title": "A", "recommended": True}]})
        s5 = _st(th5)
        intent_applied = (s5["phase"] not in ("RESEARCH", "OPTIONS")
                          and s5["awaiting"] not in ("user_feedback", "user_approval")
                          and bool(s5.get("chosen_option")) and not (s5.get("pending_intent") or ""))
        # a pure status ping must NOT be queued as an intent (stays a status reply, no pending_intent)
        th5b = start(tid, org, execution_scope="test")["thread_id"]
        _to(th5b, "RESEARCH"); _set(th5b, awaiting="fleet")
        _job_begin(th5b, "research", 6, "Researching…")
        rq = say(tid, th5b, "is it done yet?")
        status_not_queued = (rq.get("queued_intent") is False and not (_st(th5b).get("pending_intent") or ""))
        midflight_intent_ok = intent_queued and intent_applied and status_not_queued

        # (2.1c) DUPLICATE SUPPRESSION: a double-tapped identical message must NOT store a second user turn nor
        # post a second identical status bubble — the first reply stands (flagged duplicate on the retry).
        th6 = start(tid, org, execution_scope="test")["thread_id"]
        _to(th6, "RESEARCH"); _set(th6, awaiting="fleet")
        _job_begin(th6, "research", 6, "Researching…")
        d1 = say(tid, th6, "how's it going?")
        d2 = say(tid, th6, "how's it going?")                       # exact repeat -> suppressed
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT count(*) FROM chat_messages WHERE thread_id=%s AND role='user' "
                        "AND content='how''s it going?'", (th6,))
            dup_user_rows = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM chat_messages WHERE thread_id=%s AND role='assistant' "
                        "AND meta->>'kind'='working'", (th6,))
            dup_status_bubbles = cur.fetchone()[0]
        dedupe_ok = (d1.get("duplicate") is not True and d2.get("duplicate") is True
                     and dup_user_rows == 1 and dup_status_bubbles == 1)

        # (2) LIVE PROGRESS + (4) NO FALSE DONE + (5) CANCEL — on a fresh thread with a stamped in-flight job:
        th2 = start(tid, org, execution_scope="test")["thread_id"]
        _set(th2, awaiting="fleet", product="liveprod-" + os.urandom(2).hex())
        prod2 = _st(th2)["product"]
        _job_begin(th2, "build", 9, "Building…")
        ls = live_status(th2)
        # progress_pct is an honest time-fraction of the ETA (0<pct<100 while running) so the console can draw a
        # moving bar the CEO can tell from a stall (#3); eta_lo/eta_hi expose the honest range (#2).
        live_ok = (ls["running"] is True and ls["done"] is False and ls.get("eta_min") == 9
                   and isinstance(ls.get("progress_pct"), int) and 0 <= ls["progress_pct"] < 100
                   and ls.get("eta_lo") and ls.get("eta_hi") and ls["eta_lo"] < ls["eta_hi"]
                   and "elapsed" in (ls.get("label") or "").lower())
        no_false_done_running = (state(th2)["done"] is False and state(th2)["running"] is True)
        import killswitch as _k
        cr = cancel(tid, th2, "selftest stop")
        cancel_ok = (cr.get("cancelled") and prod2 in cr.get("scopes", [])
                     and _k.is_halted(prod2)["halted"] and _st(th2)["awaiting"] == "user_feedback")
        no_false_done_after = (live_status(th2)["done"] is False and live_status(th2)["running"] is False)
        _k.resume(prod2); _k.resume(f"thread-{th2}")               # lift the test halt (as a 'retry' would)
        live_cancel_ok = live_ok and no_false_done_running and cancel_ok and no_false_done_after

        # (C1) TESTQA GATE HONESTY: qa_gate consumes the machine verdict JSON facts — the green stub
        # passes; a zero-story 'pass', an open-blocking verdict, and a crashed verifier all FAIL CLOSED
        # (qa_ok=False -> DELIVER stays blocked). The DELIVER wait above already proved the green path
        # end-to-end through the TESTQA dispatch.
        g = qa_gate("qa-gate-selftest-nonexistent")            # devserve can't serve it -> stubbed verdict
        qa_green = g.get("qa_ok") is True and g.get("stories") == 3 and g.get("blocking_open") == 0
        factory.run_grounded_qa = lambda product, **k: {"passed": True, "blocking_open": 0, "stories": 0}
        qa_zero_story = qa_gate("x").get("qa_ok") is False
        factory.run_grounded_qa = lambda product, **k: {"passed": True, "blocking_open": 2, "stories": 5}
        qa_blocking = qa_gate("x").get("qa_ok") is False
        def _gq_boom(product, **k):
            raise RuntimeError("verifier down")
        factory.run_grounded_qa = _gq_boom
        qa_crash = qa_gate("x").get("qa_ok") is False
        factory.run_grounded_qa = _green_gq                    # back to green for anything downstream
        qa_gate_ok = qa_green and qa_zero_story and qa_blocking and qa_crash

        # LIVE RESEARCH SUB-PROGRESS: long research should narrate real worker progress, not only elapsed time.
        # Seed a tiny durable orchestra run linked to a controller research_run_id and prove live_status reads
        # the actual researcher rows: assignments, done/blocked counts, and a percent.
        th_prog = start(tid, org, execution_scope="test")["thread_id"]
        _to(th_prog, "RESEARCH"); _set(th_prog, awaiting="fleet", research_run_id=424242)
        _job_begin(th_prog, "research", 10, "Researching directions")
        with _conn() as c, c.cursor() as cur:
            cur.execute("""INSERT INTO orchestra_runs (tenant_id, org_id, vision, status)
                           VALUES (%s,%s,'progress proof','running') RETURNING run_id""", (tid, org))
            orc_prog = cur.fetchone()[0]
            cur.execute("""INSERT INTO orchestra_actors
                           (run_id, tenant_id, org_id, name, role, kind, status, assignment, memory)
                           VALUES (%s,%s,%s,'research-coordinator','research-coordinator','supervisor',
                                   'working','coordinate research', %s::jsonb)""",
                        (orc_prog, tid, org, json.dumps({"research_run_id": 424242})))
            for name, status, assignment in (
                ("researcher-01", "done", "competitor pricing"),
                ("researcher-02", "working", "payment providers"),
                ("researcher-03", "blocked", "regulatory edge cases"),
            ):
                cur.execute("""INSERT INTO orchestra_actors
                               (run_id, tenant_id, org_id, name, role, kind, status, assignment)
                               VALUES (%s,%s,%s,%s,'research-growth','worker',%s,%s)""",
                            (orc_prog, tid, org, name, status, assignment))
            c.commit()
        lp = live_status(th_prog)
        research_subprogress_ok = (lp.get("researchers_total") == 3 and lp.get("researchers_done") == 1
                                   and lp.get("researchers_blocked") == 1
                                   and lp.get("subprogress_pct") == 33
                                   and "payment providers" in (lp.get("progress_detail") or ""))

        # A1 WIRING: with AOS_ORCHESTRA on (default), the RESEARCH phase must have dispatched the
        # run as an ORCHESTRA org run (engine='orchestra' through research.start) — the review's
        # "zero production callers" verdict is dead only if the LIVE path actually routes there.
        import research as _rmod
        orchestra_wired_ok = research_kw.get("engine") == ("orchestra" if _rmod.orchestra_on()
                                                           else "fleet")

        ok = (consent_gate_ok and opt and gate_held and in_design and plan_persisted and plan_card
              and no_tag_leak and proto and deliver and jobs >= 3 and qa_gate_ok
              and eta_ok and research_eta_ok and consts_ok and ping_ok and live_cancel_ok
              and consent_reask_ok and sla_ok and status_honest_ok and midflight_intent_ok and dedupe_ok
              and options_elaborate_ok and options_nudge_ok and research_report_ok and plan_full_ok
              and design_surface_ok and research_subprogress_ok and orchestra_wired_ok)
        print(f"consent_gate={consent_gate_ok} options={opt} gate_held={gate_held} design={in_design} "
              f"plan_persisted={plan_persisted} plan_card={plan_card} no_tag_leak={no_tag_leak} "
              f"prototype={proto} deliver={deliver} jobs_done={jobs}")
        print(f"eta(kickoff range)={eta_ok}(est={eta_min}m,range={_eta_phrase(eta_min)}) "
              f"consts_reconciled={consts_ok} research_eta_realistic={research_eta_ok}"
              f"(={research_eta}m) ping(results-land)={ping_ok} "
              f"live_progress={live_ok} no_false_done={no_false_done_running and no_false_done_after} "
              f"cancel(killswitch)={cancel_ok}")
        print(f"research_subprogress={research_subprogress_ok}"
              f"(detail={lp.get('progress_detail')}, pct={lp.get('subprogress_pct')})")
        print(f"consent_reask_fixed={consent_reask_ok}(note={consent_note_ok},no_stale={no_stale_consent_ctx}) "
              f"sla_watchdog={sla_ok}(w1={w1},w2={w2},w3={w3},msgs={sla_msgs},eta_raised={eta_after_warn}) "
              f"status_honest={status_honest_ok}")
        print(f"midflight_intent={midflight_intent_ok}(queued={intent_queued},applied={intent_applied},"
              f"status_not_queued={status_not_queued}) dedupe={dedupe_ok}")
        print(f"options_elaborate={options_elaborate_ok} options_nudge={options_nudge_ok} "
              f"research_report={research_report_ok} plan_full={plan_full_ok} "
              f"design_surface={design_surface_ok}")
        print(f"qa_gate(C1 verdict-consumed)={qa_gate_ok}(green={qa_green},zero_story={qa_zero_story},"
              f"blocking={qa_blocking},crash_fail_closed={qa_crash})")
        print(f"orchestra_wired(A1 research engine)={orchestra_wired_ok}"
              f"(engine={research_kw.get('engine')})")
        # A2 GUARD: the agentic intent gate must NEVER read a negation as approval (the regex bug). Tests the
        # fail-SAFE path (offline, no model call) — the model path is strictly better; this locks the floor.
        intent_neg_ok = (not _affirmative("this is not good") and not _affirmative("no, change it")
                         and not _affirmative("the plan doesn't look right") and _affirmative("looks good")
                         and _affirmative("approve") and not _affirmative("bad, redo"))
        ok = ok and intent_neg_ok
        print(f"A2_intent_gate(negation-safe)={intent_neg_ok}")
        print("PASS: loopcontroller DISCOVER->DELIVER + ETA/live/ping/no-false-done/cancel"
              " + consent-reask/sla/status-honesty + A2 agentic-intent-gate ✅" if ok else "FAIL")
    finally:
        _spawn_parked_worker = _spawn_was
        factory.agent = real_agent
        if real_build is not None:
            factory.build_product = real_build
        _tp.resolve = real_tp_resolve
        _r.start, _r.run_state, _r.select, _d.prototype, _q.run, factory.run_grounded_qa = real
        with _conn() as c, c.cursor() as cur:
            for t in ("controller_jobs", "controller_state", "chat_messages", "chat_threads", "orgs",
                      "orchestra_events", "orchestra_actors", "orchestra_runs",
                      "tenant_providers", "tenant_products", "ai_consent", "notifications", "push_targets",
                      "tenants", "memory_checkpoints"):
                cur.execute(f"DELETE FROM {t} WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM product_registry WHERE tenant_id=%s RETURNING repo_path", (tid,))
            paths = [r[0] for r in cur.fetchall()]
            c.commit()
        import shutil as _sh                     # remove the stubbed-build artifact dirs the test created
        for p in paths:
            if p and "/products/" in p:
                _sh.rmtree(p, ignore_errors=True)
    sys.exit(0 if ok else 1)


def _main(a):
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "start" and len(a) > 2:
        print(json.dumps(start(a[1], int(a[2]))))
    elif a[0] == "say" and len(a) > 3:
        print(json.dumps(say(a[1], int(a[2]), a[3])))
    elif a[0] == "choose" and len(a) > 3:
        print(json.dumps(choose(a[1], int(a[2]), int(a[3]))))
    elif a[0] == "state" and len(a) > 1:
        print(json.dumps(state(int(a[1])), indent=2))
    elif a[0] == "research" and len(a) > 2:
        print(json.dumps(research_report(a[1], int(a[2])), indent=2))
    elif a[0] == "live" and len(a) > 1:
        print(json.dumps(live_status(int(a[1])), indent=2))
    elif a[0] == "cancel" and len(a) > 2:
        print(json.dumps(cancel(a[1], int(a[2]), a[3] if len(a) > 3 else "stopped by user")))
    elif a[0] == "resume":
        result = resume_stalled()
        print(json.dumps(result))
        if result.get("degraded"):
            raise SystemExit(2)
    elif a[0] == "run_job" and len(a) > 3:
        # dispatch-and-park worker entrypoint: run_job <thread_id> <kind> <jid>
        result = run_job(int(a[1]), a[2], int(a[3]))
        print(json.dumps(result), flush=True)
        # This CLI is a dedicated, disposable phase process. Some provider/HTTP libraries leave background
        # helper threads behind even after the phase's own bounded child cleanup and durable result commit;
        # normal interpreter shutdown then waits indefinitely and defeats process containment. At this point
        # run_job has committed the controller_jobs result and phase cleanup has killed/reaped owned OS child
        # groups, so a direct process exit is the final containment boundary. In-process callers are unaffected.
        os._exit(0)
    elif a[0] == "_park_crash_driver" and len(a) > 1:
        _park_crash_driver(int(a[1]))       # test-only helper spawned by parkcrash
    elif a[0] == "watchdog":
        print(json.dumps(sla_watchdog()))
    elif a[0] == "liveness":
        sys.exit(liveness_selftest())
    elif a[0] == "parktest":
        sys.exit(_park_selftest())
    elif a[0] == "parkcrash":
        sys.exit(_park_crash_selftest())
    elif a[0] == "parkstatus":
        print(json.dumps(park_status(), indent=2, default=str))
    elif a[0] == "handoff" and len(a) > 1:
        result = controlled_handoff(int(a[1]), " ".join(a[2:]) or "rolling runtime upgrade")
        print(json.dumps(result, indent=2, default=str))
        if not result.get("handoff"):
            raise SystemExit(2)
    else:
        sys.exit("usage: loopcontroller.py "
                 "start|say|choose|state|research|live|cancel|resume|run_job|watchdog|liveness|parktest|"
                 "parkstatus|handoff|selftest ...")


def _park_crash_driver(thread_id):
    """Test-only driver: create a SLOW parked job, launch its DETACHED worker, print the job id, then idle so
    the harness can hard-KILL this process mid-phase and prove the worker outlives it."""
    import time
    _ensure()
    with _conn() as c, c.cursor() as cur:
        cur.execute("""INSERT INTO controller_state
                          (thread_id, tenant_id, org_id, phase, awaiting, updated_at, execution_scope)
                       VALUES (%s,1,1,'__PARKTEST__','fleet',now(),'test')
                       ON CONFLICT (thread_id) DO UPDATE SET phase='__PARKTEST__', awaiting='fleet',
                         execution_scope='test'""", (thread_id,))
        cur.execute("""INSERT INTO controller_jobs
                          (thread_id, tenant_id, phase, kind, status, heartbeat_at, execution_scope)
                       VALUES (%s,1,'__PARKTEST__','__selftest_slow__','running',now(),'test')
                       RETURNING id""", (thread_id,))
        jid = cur.fetchone()[0]; c.commit()
    _spawn_parked_worker(thread_id, "__selftest_slow__", jid)
    print(f"DRIVER_UP jid={jid}", flush=True)
    time.sleep(3600)                       # idle until the harness kills us


def _park_crash_selftest():
    """Prove G1 is RETIRED: a driver that hard-CRASHES right after dispatching a parked phase does NOT take the
    worker down — the detached worker (its own session) finishes the job on its own. This is the validation that
    unlocks overhaul Steps 4-5. Uses a fake ~4s phase so there's a window to kill the driver mid-run; no claude."""
    import time
    _ensure()
    tid = 950000 + int(os.urandom(2).hex(), 16) % 1000
    driver = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "_park_crash_driver", str(tid)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)

    def _status(jid):
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT status FROM controller_jobs WHERE id=%s", (jid,))
            r = cur.fetchone()
            return r[0] if r else None

    ok = True
    try:
        jid, deadline = None, time.time() + 15
        while time.time() < deadline:
            line = driver.stdout.readline()
            if line.startswith("DRIVER_UP"):
                jid = int(line.strip().split("jid=")[1]); break
        assert jid, "driver failed to dispatch the parked job"
        assert _status(jid) == "running", "worker should still be mid-phase when we crash the driver"
        driver.kill(); driver.wait(timeout=5)                 # HARD-CRASH the driver mid-phase
        print(f"PASS: driver (pid {driver.pid}) hard-killed while the phase was still running")
        st, deadline = _status(jid), time.time() + 20
        while time.time() < deadline and st != "done":
            time.sleep(0.5); st = _status(jid)
        assert st == "done", f"parked worker did NOT survive the driver crash (job status={st})"
        print("PASS: the detached worker SURVIVED the driver crash and finished the job — G1 retired ✅")
        print("park_crash_selftest: PASS")
    except (AssertionError, Exception) as e:
        ok = False
        print(f"park_crash_selftest: FAIL — {type(e).__name__}: {e}")
    finally:
        try:
            driver.kill()
        except Exception:
            pass
        with _conn() as c, c.cursor() as cur:
            cur.execute("DELETE FROM controller_jobs WHERE thread_id=%s", (tid,))
            cur.execute("DELETE FROM controller_state WHERE thread_id=%s", (tid,))
            c.commit()
    return 0 if ok else 1


def _park_selftest():
    """Prove the dispatch-and-park machinery end-to-end WITHOUT a real claude build, using the trivial
    '__selftest__' phase: (1) run_job in-process writes the terminal result + leaves awaiting='fleet' and does
    NOT advance (single-owner: the poller advances); (2) a genuinely DETACHED worker process launched by
    _spawn_parked_worker rebuilds state, runs, and writes 'done' — surviving as its own process. Phase is a
    no-op sentinel so even a concurrent daemon can't turn this into real work."""
    import time
    _ensure()
    tid_thread = 960000 + int(os.urandom(2).hex(), 16) % 1000

    def _mkjob():
        with _conn() as c, c.cursor() as cur:
            cur.execute("""INSERT INTO controller_state
                              (thread_id, tenant_id, org_id, phase, awaiting, updated_at, execution_scope)
                           VALUES (%s,1,1,'__PARKTEST__','fleet',now(),'test')
                           ON CONFLICT (thread_id) DO UPDATE SET phase='__PARKTEST__', awaiting='fleet',
                             execution_scope='test'""",
                        (tid_thread,))
            cur.execute("""INSERT INTO controller_jobs
                              (thread_id, tenant_id, phase, kind, status, heartbeat_at, execution_scope)
                           VALUES (%s,1,'__PARKTEST__','__selftest__','running',now(),'test')
                           RETURNING id""", (tid_thread,))
            jid = cur.fetchone()[0]; c.commit()
        return jid

    def _status(jid):
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT status, result FROM controller_jobs WHERE id=%s", (jid,))
            return cur.fetchone()

    def _awaiting():
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT awaiting FROM controller_state WHERE thread_id=%s", (tid_thread,))
            return cur.fetchone()[0]

    ok = True
    try:
        # (1) in-process run_job
        jid = _mkjob()
        run_job(tid_thread, "__selftest__", jid)
        st, res = _status(jid)
        assert st == "done", f"run_job must finish the job (got {st})"
        assert isinstance(res, dict) and res.get("parked_selftest"), f"result not written: {res}"
        assert _awaiting() == "fleet", "parked worker must NOT advance (leaves awaiting='fleet' for the poller)"
        print("PASS: run_job writes terminal result, leaves awaiting='fleet', does not advance")

        # (2) genuinely detached worker process
        jid2 = _mkjob()
        launched = _spawn_parked_worker(tid_thread, "__selftest__", jid2)
        assert launched, "detached worker must launch"
        deadline = time.time() + 30
        while time.time() < deadline:
            st2, _ = _status(jid2)
            if st2 == "done":
                break
            time.sleep(0.5)
        assert st2 == "done", f"detached worker must complete the job (got {st2})"
        print("PASS: detached worker process rebuilt state, ran, and wrote 'done' (survives as its own process)")
        print("park_selftest: PASS (parked-only dispatch invariant verified)")
    except AssertionError as e:
        ok = False
        print(f"park_selftest: FAIL — {e}")
    finally:
        with _conn() as c, c.cursor() as cur:
            cur.execute("DELETE FROM controller_jobs WHERE thread_id=%s", (tid_thread,))
            cur.execute("DELETE FROM controller_state WHERE thread_id=%s", (tid_thread,))
            c.commit()
    return 0 if ok else 1


if __name__ == "__main__":
    _main(sys.argv[1:])
