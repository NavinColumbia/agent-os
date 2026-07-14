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
import sys
import threading
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit         # noqa: E402
import factory       # noqa: E402
import orchestrator  # noqa: E402

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
_PHASE_ETA_DEFAULT = {"RESEARCH": 10, "PROTOTYPE": 6, "IMPLEMENT": 14, "TESTQA": 5}

# Honest-range multipliers (mirror estimate.py's LOW_MULT/HIGH_MULT) so a promised ETA is a RANGE, not a
# false-precision point — "~10-14 min", never a bare "~3 min" that over-runs. When a job overruns we RAISE
# the point (sla_watchdog) so the range tracks reality instead of lying.
_ETA_LOW_MULT, _ETA_HIGH_MULT = 0.6, 1.6
# How long past a prior SLA warning before we WARN + PING AGAIN on a still-overrunning job (#2.6/#5 re-ping
# on continued overrun) — a CEO who left the tab gets a fresh heads-up, not one-and-done silence.
_SLA_REWARN_MIN = 5


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
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS controller_state (
            thread_id BIGINT PRIMARY KEY, tenant_id TEXT, org_id BIGINT,
            phase TEXT NOT NULL DEFAULT 'DISCOVER', brief JSONB, options JSONB, chosen_option JSONB,
            plan JSONB, research_run_id BIGINT, product TEXT, awaiting TEXT, updated_at TIMESTAMPTZ DEFAULT now())""")
        # LIVE-PROGRESS columns (added in-place for existing orgs): the in-flight job's kind, when it
        # started, its ETA (minutes), and a short status string the console renders live.
        cur.execute("""ALTER TABLE controller_state
            ADD COLUMN IF NOT EXISTS job_kind TEXT,
            ADD COLUMN IF NOT EXISTS job_started_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS job_eta_min INTEGER,
            ADD COLUMN IF NOT EXISTS job_status TEXT,
            ADD COLUMN IF NOT EXISTS job_sla_warned BOOLEAN DEFAULT false,
            ADD COLUMN IF NOT EXISTS job_sla_warned_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS pending_intent TEXT""")
        cur.execute("""CREATE TABLE IF NOT EXISTS controller_jobs (
            id BIGSERIAL PRIMARY KEY, thread_id BIGINT, tenant_id TEXT, phase TEXT, kind TEXT,
            status TEXT DEFAULT 'running', result JSONB,
            started_at TIMESTAMPTZ DEFAULT now(), finished_at TIMESTAMPTZ)""")
        # ARCHITECTURE-OVERHAUL Step 1: an OUTPUT-INDEPENDENT liveness heartbeat. The worker ticks heartbeat_at
        # on a fixed timer (NOT when it produces output), so the reaper can tell "worker process dead" (heartbeat
        # stopped) from "claude working quietly" (heartbeat still ticking) — killing the false-positive reap (F8).
        # lease_token is a fencing token: a reap bumps it so a wrongly-reaped-but-alive worker's writes are rejected.
        cur.execute("ALTER TABLE controller_jobs ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ")
        cur.execute("ALTER TABLE controller_jobs ADD COLUMN IF NOT EXISTS lease_token BIGINT DEFAULT 0")
        c.commit()


def _st(thread_id):
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT thread_id, tenant_id, org_id, phase, brief, options, chosen_option, plan,
                              research_run_id, product, awaiting, pending_intent
                       FROM controller_state WHERE thread_id=%s""",
                    (thread_id,))
        r = cur.fetchone()
    if not r:
        return None
    keys = ["thread_id", "tenant_id", "org_id", "phase", "brief", "options", "chosen_option", "plan",
            "research_run_id", "product", "awaiting", "pending_intent"]
    return dict(zip(keys, r))


def _set(thread_id, **kw):
    if not kw:
        return
    cols, vals = [], []
    for k, v in kw.items():
        cols.append(f"{k}=%s")
        vals.append(json.dumps(v) if k in ("brief", "options", "chosen_option", "plan") and v is not None else v)
    vals.append(thread_id)
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute(f"UPDATE controller_state SET {', '.join(cols)}, updated_at=now() WHERE thread_id=%s", vals)
        c.commit()


def _job_begin(thread_id, kind, eta_min, status):
    """Stamp the LIVE-PROGRESS fields when an async job kicks off (kind/start/ETA/status) so the console
    can render a real, ticking 'Researching… (Nm elapsed, ~M min)' bubble instead of a static one."""
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""UPDATE controller_state SET job_kind=%s, job_started_at=now(), job_eta_min=%s,
                       job_status=%s, job_sla_warned=false, updated_at=now() WHERE thread_id=%s""",
                    (kind, eta_min, status, thread_id))
        c.commit()


def _job_progress(thread_id, status):
    """Update the live status string mid-run — only while the thread is genuinely on its fleet gate, so a
    late update can never resurrect a status on an already-finished/cancelled job (NO false 'working')."""
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""UPDATE controller_state SET job_status=%s, updated_at=now()
                       WHERE thread_id=%s AND awaiting='fleet'""", (status, thread_id))
        c.commit()


def _job_clear(thread_id):
    """Clear the LIVE-PROGRESS fields once a job leaves flight (done/failed/cancelled)."""
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""UPDATE controller_state SET job_kind=NULL, job_started_at=NULL, job_eta_min=NULL,
                       job_status=NULL, job_sla_warned=false, updated_at=now() WHERE thread_id=%s""",
                    (thread_id,))
        c.commit()


def _research_history_min(default=None):
    """History-backed ETA (minutes) for a research run — the research analogue of estimate.py's median-of-
    history approach, but research timing lives in `research_runs` (started_at/finished_at), not the build
    `traces` estimate.py reads. We take the MEDIAN wall-clock duration of recent COMPLETED runs so the
    promised ETA matches what we actually deliver. With no history we fall back to a REALISTIC default: a
    research run fans out ~6-8 parallel web agents + a synthesis pass, so it takes ~10-14 min in practice —
    NOT the old unrealistic 3. Always returns a positive int; never raises."""
    try:
        import statistics
        with psycopg.connect(DB) as c, c.cursor() as cur:
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
    'service'); a prototype is only a slice of the full build. QA uses a small sensible default. Always
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
        the taxonomy level whose contract includes a push and makes the feed unmistakable.
      * PUSH (push.send on a daemon thread, high priority for urgent): reaches a closed tab / phone. HONEST
        CAVEAT: on a self-host box this only actually delivers if ntfy is configured AND the tenant has a
        push topic registered (email likewise needs SMTP); otherwise it's a no-op. The in-app feed above is
        what we rely on always reaching the user.
    The daemon thread keeps a slow/down ntfy from ever blocking the control loop. Never raises."""
    try:
        import notifications
        notifications.send(tid, category, title, (body or "")[:300], level=level)
    except Exception:
        pass

    def _p():
        try:
            import push
            push.send(tid, title, (body or "")[:160], priority="high" if level == "urgent" else "default")
        except Exception:
            pass
    threading.Thread(target=_p, daemon=True).start()


def _resume_halts(thread_id):
    """Lift any kill-switch HALT this thread set via cancel(), so a 'retry' actually re-dispatches the
    parked phase instead of immediately re-stopping on the still-set halt. Idempotent + best-effort."""
    try:
        import killswitch
        killswitch.resume(f"thread-{thread_id}")
        s = _st(thread_id)
        if s and s.get("product"):
            killswitch.resume(s["product"])
    except Exception:
        pass


def live_status(thread_id):
    """Truthful live snapshot for the console: what the controller is doing RIGHT NOW. While a durable job
    runs it reports running + elapsed + ETA + a short status (so the UI shows 'Researching… (2m elapsed)');
    on a gate it reports the real awaiting status; and it reports done ONLY when the loop has actually
    reached DELIVER and isn't awaiting anything — never a false 'done' mid-job."""
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT phase, awaiting, job_kind, job_status, job_eta_min,
                              EXTRACT(EPOCH FROM (now()-job_started_at))::int
                       FROM controller_state WHERE thread_id=%s""", (thread_id,))
        r = cur.fetchone()
    if not r:
        return {"error": "no such thread"}
    phase, awaiting, jk, js, eta, elapsed = r
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
        out.update(job_kind=jk, elapsed_s=es, elapsed_min=em, eta_min=eta, eta_lo=lo, eta_hi=hi,
                   progress_pct=pct, overrun=overrun, status=base_status,
                   label=f"{base_status} ({em}m elapsed{eta_txt})")
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


def _dispatch(thread_id, kind, fn, eta_min=None, kickoff=None, status=None):
    """Durable job + daemon worker that runs a real module then advances. Survives via controller_jobs.

    On kickoff it (a) stamps the LIVE-PROGRESS fields (kind/ETA/status) so the console shows a ticking
    bubble, and (b) optionally posts a forward-looking 'working… (~N min)' message so the CEO sees an ETA
    the instant async work starts — never a silent stall."""
    s = _st(thread_id)
    if eta_min is None:
        eta_min = _estimate_runtime(s["phase"], s.get("plan"))
    with psycopg.connect(DB) as c, c.cursor() as cur:
        # SINGLE-WRITER AT DISPATCH (overhaul Step 2, defense-in-depth): never create a SECOND running job for a
        # thread that already has one in flight. In normal flow the awaiting='fleet' gate + the per-thread drive
        # lock already serialize phases, so this is inert; it exists so a duplicate dispatch that somehow slips
        # through (a racing driver, a double advance) can't double-RUN the phase. Dead 'running' rows are flipped
        # to 'failed' by _reap_dead_jobs BEFORE any re-dispatch, so this never wedges a legitimately-crashed job.
        cur.execute("SELECT id FROM controller_jobs WHERE thread_id=%s AND status='running' LIMIT 1", (thread_id,))
        if cur.fetchone():
            return None                              # already an in-flight job for this thread — don't double-run
        cur.execute("""INSERT INTO controller_jobs (thread_id, tenant_id, phase, kind, heartbeat_at)
                       VALUES (%s,%s,%s,%s, now()) RETURNING id""", (thread_id, s["tenant_id"], s["phase"], kind))
        jid = cur.fetchone()[0]; c.commit()
    _set(thread_id, awaiting="fleet")
    _job_begin(thread_id, kind, eta_min, status or _KIND_LABEL.get(kind, "Working…"))
    if kickoff:
        rng = _eta_phrase(eta_min)
        eta_txt = f" ({rng})" if rng else ""
        _report(s["tenant_id"], thread_id, kickoff + eta_txt,
                {"kind": "working", "phase": s["phase"], "job": kind, "eta_min": eta_min})

    # OUTPUT-INDEPENDENT HEARTBEAT (overhaul Step 1): a background timer that ticks heartbeat_at every ~45s while
    # the (possibly long, quiet) phase runs — proving the worker PROCESS is alive regardless of whether `claude`
    # is producing output. If the process dies, this thread dies with it → heartbeat stops → the reaper correctly
    # detects a real death. A quiet-but-alive build keeps beating and is NEVER reaped. Kills F8.
    _beat_stop = threading.Event()

    def _heartbeat():
        while not _beat_stop.wait(HEARTBEAT_S):
            try:
                with psycopg.connect(DB) as c, c.cursor() as cur:
                    cur.execute("UPDATE controller_jobs SET heartbeat_at=now() WHERE id=%s AND status='running'", (jid,))
                    c.commit()
            except Exception:
                pass
    threading.Thread(target=_heartbeat, daemon=True).start()

    def _work():
        result, status = {}, "done"
        try:
            result = fn() or {}
        except Exception as e:
            result, status = {"error": str(e)[:200]}, "failed"
        finally:
            _beat_stop.set()
        # A long-but-healthy async run (e.g. a research fleet still going past the in-worker poll budget)
        # returns a 'pending' sentinel. We must NOT mark it done/failed (that would surface a FALSE timeout
        # and park the thread on a feedback gate, orphaning the eventual completion) nor advance(). Park the
        # job as 'pending' (the crash-reaper ignores it — it only reaps 'running') and leave the thread on
        # its 'fleet' gate so resume_stalled() reconciles it against the REAL run once it terminates.
        if isinstance(result, dict) and result.get("pending"):
            with psycopg.connect(DB) as c, c.cursor() as cur:
                cur.execute("""UPDATE controller_jobs SET status='pending', result=%s
                               WHERE id=%s AND status='running'""", (json.dumps(result), jid))
                c.commit()
            _job_progress(thread_id, "Still working — this one's taking a little longer…")
            return
        # Guard the terminal write to status='running': if cancel() (or a reaper) already moved this job to
        # 'cancelled'/'failed', our UPDATE touches 0 rows and we MUST NOT advance — otherwise a cancelled
        # job would still surface a result and a halted run would double-report.
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("""UPDATE controller_jobs SET status=%s, result=%s, finished_at=now()
                           WHERE id=%s AND status='running'""", (status, json.dumps(result), jid))
            changed = cur.rowcount; c.commit()
        if not changed:
            return
        _set(thread_id, awaiting=None)
        _job_clear(thread_id)
        advance(thread_id, job_result=result)
    threading.Thread(target=_work, daemon=True).start()
    return jid


def start(tid, org_id):
    _ensure()
    thread_id = orchestrator.start_thread(tid)
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO controller_state (thread_id, tenant_id, org_id, phase, awaiting)
                       VALUES (%s,%s,%s,'DISCOVER','user_feedback') ON CONFLICT (thread_id) DO NOTHING""",
                    (thread_id, tid, org_id))
        c.commit()
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
    with psycopg.connect(DB) as c, c.cursor() as cur:
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
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT thread_id FROM controller_state WHERE tenant_id=%s AND org_id=%s ORDER BY thread_id LIMIT 1",
                    (tid, org_id))
        r = cur.fetchone()
    if r:
        return r[0]
    return start(tid, org_id)["thread_id"]


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
    return r if (r.get("key") or r.get("auth_mode") == "subscription") else None


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
        with psycopg.connect(DB) as c, c.cursor() as cur:
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
        with psycopg.connect(DB) as c, c.cursor() as cur:
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


def say(tid, thread_id, msg, api_key=None, on_delta=None):
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
    dup = not _store_user(tid, thread_id, msg)
    phase = s["phase"]
    factory._ctx.api_key = api_key
    factory._ctx.tenant = tid          # lets factory.agent enforce the consent gate as a backstop (defense-in-depth)
    factory._ctx.org = s.get("org_id") # MEMORY SPINE (A3): scope company-memory injection to this org

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
                "end your reply with EXACTLY:\n[[RESEARCH]]\n<the research question to investigate>\n[[/RESEARCH]]")
        reply = _llm(tid, thread_id, sysp, s, on_delta=on_delta)
        rq = _parse_block(reply, "RESEARCH")
        clean = re.sub(r"\[\[RESEARCH\]\].*?\[\[/RESEARCH\]\]", "", reply, flags=re.S | re.I).strip()
        if rq:
            _set(thread_id, brief={"question": rq})
            _report(tid, thread_id, clean or "Got it.")   # the research kickoff (with its ETA) is posted by _dispatch
            _to(thread_id, "RESEARCH"); _set(thread_id, awaiting=None); advance(thread_id)
        else:
            _report(tid, thread_id, reply)
        return {"phase": _st(thread_id)["phase"]}

    if phase == "DEEP_DESIGN" and s["awaiting"] != "user_feedback":
        return {"phase": phase}
    if phase == "DEEP_DESIGN":
        if s["plan"] and _classify_intent(tid, thread_id, msg, phase, "user_feedback",
                                           api_key=api_key)["verdict"] in ("approve", "proceed"):
            _set(thread_id, awaiting=None); _to(thread_id, "PLAN_APPROVAL"); advance(thread_id)
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
                "End with EXACTLY:\n[[PLAN]]\nname: <slug>\nkind: lib|web|service|project\n"
                "plan: <bullets incl. the impact map, invariants/edge cases, parallelization, and done checks; one per line '- '>\n"
                "agentic: <free-text: the agentic feature(s) the CEO wants + how each is invoked (button/event-async/"
                "schedule), or 'none'>\ncharter: <2-4 sentences incl. the team/external surfaces to build in>\n[[/PLAN]]")
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
            _report(tid, thread_id, (clean or "Here's the plan.") + "\n\nDoes this look right? Say \"looks good\" "
                                    "to lock it in, or tell me what to change.", {"kind": "plan", "plan": plan})
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
        intent = _classify_intent(tid, thread_id, msg, phase, s["awaiting"], api_key=api_key)
        if intent["verdict"] in ("approve", "proceed"):
            _resume_halts(thread_id)   # a 'retry' after a cancel() must lift the halt before re-dispatching
            _set(thread_id, awaiting=None); advance(thread_id)
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
        if _classify_intent(tid, thread_id, msg, phase, "credentials", api_key=api_key)["verdict"] in ("approve", "proceed"):
            _resume_halts(thread_id)
            _set(thread_id, awaiting=None); advance(thread_id)
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
    _set(thread_id, chosen_option=chosen, awaiting="user_feedback")
    _to(thread_id, "DEEP_DESIGN")
    _report(tid, thread_id, "Great — going with that direction. Tell me anything specific you want, or say "
                            "\"go ahead\" and I'll draft the technical plan.")
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
    jid = None
    try:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("""INSERT INTO controller_jobs (thread_id, tenant_id, phase, kind)
                           VALUES (%s,%s,'DIRECTIVE','company-directive') RETURNING id""", (thread_id, tid))
            jid = cur.fetchone()[0]; c.commit()
    except Exception:
        pass
    out = company.run_directive(directive, tenant=tid, functions=functions)
    try:
        if jid is not None:
            with psycopg.connect(DB) as c, c.cursor() as cur:
                cur.execute("UPDATE controller_jobs SET status=%s, result=%s WHERE id=%s",
                            (out.get("status", "done"), json.dumps({"run_id": out.get("run_id"),
                             "functions": len(out.get("functions") or [])}), jid))
                c.commit()
    except Exception:
        pass
    return out


def advance(thread_id, job_result=None):
    s = _st(thread_id)
    if not s:
        return
    if s["awaiting"] in ("user_feedback", "user_approval", "credentials", "fleet"):
        return
    tid, phase = s["tenant_id"], s["phase"]

    # A dispatched job came back BROKEN — the worker raised (_work() -> {'error':...}, status='failed')
    # or the underlying module reported failure/timeout. We must NOT fall through to the phase block:
    # that re-runs the SAME failing job, silently re-dispatching it forever (#9). Surface it to the user
    # and park on a feedback gate so they decide (e.g. say "retry" to re-dispatch the phase, or change it).
    if job_result and (job_result.get("error") or job_result.get("status") in ("failed", "timeout")):
        err = job_result.get("error") or job_result.get("status")
        _set(thread_id, awaiting="user_feedback")
        # Map a KNOWN governed-spend block (the gate refused BEFORE any spend) to an ACTIONABLE message so the
        # user can fix the precondition and resume, instead of an opaque 'failed'. Unknown errors keep the
        # generic surface. After fixing it they say "ready"/"retry" -> the user_feedback gate re-dispatches.
        es = str(err).lower()
        if "consent" in es:
            text = ("⚠️ I can't research or build yet because AI-processing consent isn't on file. Please accept "
                    "it in Settings → Privacy (it names the provider your text is sent to), then say \"ready\" "
                    "and I'll pick up right where we left off.")
            meta_kind = "consent_required"
        elif "quota reached" in es or "quota exceeded" in es or "over quota" in es:
            # ONLY a genuine over-quota (research.py emits "quota reached (...)"); an internal spend-gate error
            # ("internal_error: quota check failed …") must NOT be mis-rendered as a billing/upgrade message.
            text = ("⚠️ You've hit your plan's build quota, so I paused before spending anything. Upgrade your "
                    "plan (or wait for it to reset) in Settings → Billing, then say \"ready\" to continue.")
            meta_kind = "quota_reached"
        else:
            text = (f"⚠️ The **{phase}** step hit a problem and stopped: {err}. "
                    f"Tell me how you'd like to proceed, or say \"retry\" to run it again.")
            meta_kind = "job_failed"
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
        _to(thread_id, "OPTIONS"); _set(thread_id, awaiting="user_approval")

        # PRE-AUTHORIZED INTENT (#2.1 follow-up): if the CEO told us mid-research to "go with the recommendation
        # and start building", honour it NOW instead of silently re-parking on the approval gate. Auto-select
        # the RECOMMENDED direction via the SAME path a chip-tap takes (-> DEEP_DESIGN feedback gate), so every
        # downstream guard (plan draft, plan approval, spend gates) still holds — we never blast a human-gated
        # build. If there's no recommended option we can't safely auto-pick, so we fall through and just ask.
        pend = (s.get("pending_intent") or "").strip()
        rec = next((o for o in opts if isinstance(o, dict) and o.get("recommended")), None)
        if pend and rec and _intent_auto_proceed(pend):
            _set(thread_id, pending_intent=None)
            rid = rec.get("id")
            chosen = {"option_id": rid}
            try:
                import research as _research
                chosen = _research.select(tid, job_result["run_id"], rid) or chosen
            except Exception:
                pass
            _set(thread_id, chosen_option=chosen, awaiting="user_feedback")
            _to(thread_id, "DEEP_DESIGN")
            _report(tid, thread_id,
                    "Research is in — and as you asked, I went with my recommendation: "
                    f"“{rec.get('title', 'the recommended direction')}”. I'll turn that into the "
                    "technical plan next; say \"go ahead\" when you want me to draft it and start the build.",
                    {"kind": "option_chosen", "auto_selected": rid, "title": rec.get("title")})
            _ping(tid, "Research done — I picked your recommended direction",
                  "As you asked, I went with the recommended option and I'm teeing up the plan. "
                  "Open the chat to follow along.", level="urgent")
            audit.append(actor="loopcontroller", action="IntentAutoApplied", resource=str(thread_id),
                         decision="auto_select_recommended", payload={"option_id": rid})
            return

        # Otherwise present the options. If the CEO left a (non-directive) note mid-research, acknowledge it up
        # front so it's never silently dropped — its substance also rides along in the transcript the plan reads.
        if pend:
            _set(thread_id, pending_intent=None)
            intro = ("Here's what I found — pick a direction. (I saw the note you sent while I was working; "
                     "I'll carry it into the plan once you choose.)")
        else:
            intro = "Here's what I found — pick a direction:"
        _report(tid, thread_id, intro, {"kind": "options", "options": opts,
                                        "research_run_id": job_result["run_id"], "report_available": True})
        # PING: research RESULTS landed — heads-up the CEO now (not only on failure/final ship). level=urgent
        # so the in-app bell/feed lights up unmistakably AND a push fires (if ntfy/email are configured).
        _ping(tid, "Your options are ready — review them",
              "I finished researching and brought back a few directions. Open the chat to pick one.",
              level="urgent")
        return
    if job_result and job_result.get("screens") is not None:        # prototype finished -> gate at IMPLEMENT
        _job_clear(thread_id)
        # SURFACE THE DESIGN (#4): reference the design artifact in the meta (org + a flag + the product +
        # the surfaces) so the console can link straight to "Review the design" instead of a dead-end bubble.
        _report(tid, thread_id, f"I've drafted {job_result.get('screens', 0)} prototype screens "
                                f"(cockpit / team / external) — review them in Design. Say \"approve\" to build it.",
                {"kind": "prototype", "design_ready": True, "org": s.get("org_id"),
                 "product": s.get("product"), "screens": job_result.get("screens", 0),
                 "surfaces": job_result.get("surfaces")})
        _to(thread_id, "IMPLEMENT"); _set(thread_id, awaiting="user_feedback")
        # PING: a PROTOTYPE landed — heads-up the CEO to review + approve. level=urgent so the bell/feed lights
        # up unmistakably AND a push fires (if ntfy/email are configured).
        _ping(tid, "Your prototype is ready — review it",
              f"{job_result.get('screens', 0)} screens are ready to review. Approve in the chat to build it.",
              level="urgent")
        return
    if job_result and (job_result.get("shipped") is not None or job_result.get("result")):  # build done
        product = _st(thread_id).get("product")
        # BOUNDARY CONTRACT (root-cause fix): record the build outcome to the single-source-of-truth registry,
        # then VALIDATE that QA is even allowed to run — build genuinely succeeded AND its artifact exists at the
        # REGISTERED path. A failed/empty build must NOT advance to a QA that can't find it (F6/F7); it auto-loops
        # back to the builder instead. Fail-open: a registry hiccup never blocks the pipeline.
        build_ok = bool(job_result.get("shipped")) or (job_result.get("result") not in (None, "", "error")
                                                        and job_result.get("status") != "error")
        proceed, why = True, ""
        try:
            import productregistry as _preg
            _preg.record_phase(product, "build", ok=build_ok, artifact=_preg.path(product),
                               verdict=str(job_result.get("status") or job_result.get("result") or "")[:200])
            proceed, why = _preg.precondition(product, "qa")
        except Exception:
            proceed = True
        if proceed:
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
            _to(thread_id, "DELIVER"); advance(thread_id)
        else:
            # A failed/unverifiable build must NOT reach DELIVER — but the CEO is NOT the first responder.
            # Auto-loop back to the builder (bounded); escalate to the human ONLY when the loop is exhausted.
            _autoloop_build(thread_id, tid, product,
                            reason=(job_result.get("verdict") or job_result.get("error") or "QA did not pass"))
        return

    if phase == "RESEARCH":
        q = (s["brief"] or {}).get("question", "build my product")
        def _do_research():
            import research as _r, time
            # AOS_ORCHESTRA (default ON): dispatch the research as a durable ORCHESTRA org run —
            # controller actor -> research supervisor -> N child researchers as Postgres rows
            # (identity/tenure/heartbeats) with events on the persisted bus (REBUILD-PLAN A1:
            # orchestra IS the engine, this is its production callsite). Flag off -> the legacy
            # in-process fleet. Same research_runs/report/options contract either way, so the
            # console UX below is engine-agnostic.
            eng = "orchestra" if _r.orchestra_on() else "fleet"
            started = _r.start(tid, s["org_id"], thread_id, q, engine=eng)
            rid = started.get("run_id")
            if started.get("error"):
                # The governed-spend gate (consent/quota) refused the fan-out up front — no thread was
                # started. Carry the REAL reason into the job result so advance() renders an actionable
                # message ("accept consent / upgrade plan, then say ready") instead of an opaque 'failed'.
                return {"run_id": rid, "status": "failed", "error": started["error"], "options": []}
            # Persist the run id up front so resume_stalled() can reconcile this thread against the REAL
            # research run even if this worker — or the whole process — dies before the run finishes.
            _set(thread_id, research_run_id=rid)
            _job_progress(thread_id, "Researching directions…")
            # Poll in-worker for the common (fast) case, but bound the wait to the same window the
            # crash-sweeper uses (RUNNING_TIMEOUT_MIN) instead of a hard 300s cap that falsely declared a
            # still-healthy fleet 'timeout'. The research fleet runs in its OWN daemon, so if it outlives
            # this budget we hand off (pending) rather than killing a live run.
            deadline = time.time() + RUNNING_TIMEOUT_MIN * 60
            while time.time() < deadline:
                st = _r.run_state(tid, rid)
                if st["status"] in ("done", "failed"):
                    return {"run_id": rid, "status": st["status"], "options": st.get("options", [])}
                time.sleep(2)
            # Still running and healthy — hand off to resume_stalled() instead of declaring a false timeout.
            return {"run_id": rid, "status": "pending", "pending": True}
        _dispatch(thread_id, "research", _do_research, eta_min=_estimate_runtime("RESEARCH"),
                  kickoff="On it — I'm researching this now and will bring back a few directions.",
                  status="Researching directions…")
        return

    if phase == "PLAN_APPROVAL":
        try:
            import tenantproviders
            r = tenantproviders.resolve(tid)
            if not r.get("key") and r.get("auth_mode") != "subscription":
                import agent_request
                agent_request.ask(tid, "To build this I need a model provider connected (Anthropic or Codex) — "
                                       "add one in Settings → Providers, then say \"ready\".",
                                  kind="credential", org_id=s["org_id"], thread_id=thread_id)
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
        def _do_proto():
            import design_fleet
            return design_fleet.prototype(tid, str(s["org_id"]), product, plan)
        _dispatch(thread_id, "design", _do_proto, eta_min=_estimate_runtime("PROTOTYPE", plan),
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
            if plan.get("kind") == "project":
                import project
                log = project.build_complex(product, charter)
                return {"product": product, "result": (log or {}).get("result")}
            # SCAFFOLD-THEN-IMPROVE (root-cause fix): qualityloop/verify/improve only IMPROVE an EXISTING product
            # (they return "no such product" otherwise) — so the product must be BUILT first. Nothing did that,
            # which is why the build errored and QA found nothing. Build it from the charter at the REGISTERED
            # path, THEN run the quality loop to raise it to the bar.
            import pathlib
            import productregistry as _preg
            repo = pathlib.Path(_preg.path(product))
            if not repo.exists() or not any(repo.iterdir()):
                factory.build_product(product, charter, kind=plan.get("kind", "web"))
            import qualityloop
            return qualityloop.run(product, bar="high")
        _dispatch(thread_id, "build", _do_build, eta_min=_estimate_runtime("IMPLEMENT", plan),
                  kickoff="Building it now — I'll ping you the moment it's ready.",
                  status="Building…")
        return

    if phase == "TESTQA":
        product = s.get("product")
        _dispatch(thread_id, "qa", lambda: qa_gate(product), eta_min=_estimate_runtime("TESTQA", s.get("plan")),
                  kickoff="Running QA on the build…", status="Testing…")
        return

    if phase == "DELIVER":
        product = s.get("product")
        try:
            import orgs
            orgs.record_artifact(s["org_id"], "product_repo", f"Shipped {product}", product=product)
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


def qa_gate(product) -> dict:
    """TESTQA's verification body (REBUILD-PLAN C1): run the AGENTIC QA stack against the RUNNING build
    and consume the SAME machine verdict the LAUNCH gate reads (docs/QA-VERDICT.json: passed==true,
    blocking_open==0, stories>0). The build is brought up via devserve (web/service get a stable dev
    URL; non-servable kinds fall through to factory's independent qa-security verification inside
    run_grounded_qa). The builder never grades its own homework, and verify.verify's static tiers are
    no longer the ship gate. FAIL-CLOSED: an unverifiable build is a QA failure — DELIVER stays blocked."""
    try:
        target = None
        try:
            import devserve
            up = devserve.up(product)                 # idempotent: reuses a live instance + stable port
            target = up.get("url")
        except Exception:
            target = None                             # not servable / didn't come up -> independent path
        v = factory.run_grounded_qa(product, target_url=target)
        # ONE ship condition, shared with the factory QA stage and gate_check's LAUNCH validator:
        # passed==true AND blocking_open==0 AND stories>0 (fail-closed on missing/garbled facts).
        qa_ok = factory.qa_verdict_ok(v)
        return {"qa_ok": qa_ok, "stories": (v or {}).get("stories"),
                "blocking_open": (v or {}).get("blocking_open"),
                "verdict": (v or {}).get("verdict"), "verdict_json": (v or {}).get("verdict_json")}
    except Exception as e:
        # FAIL-CLOSED: if verification cannot run, we have NO evidence the build is good, so we
        # must not let it ship. Treat an unverifiable build as a QA failure (gate blocks DELIVER).
        return {"qa_ok": False, "error": str(e)[:200]}


RUNNING_TIMEOUT_MIN = 30   # (legacy constant kept for callers/tests; superseded by heartbeat liveness below)
RUNNING_FLOOR_MIN = int(os.environ.get("AOS_JOB_FLOOR_MIN", "20"))       # never reap younger than this
STALL_SILENT_MIN = int(os.environ.get("AOS_JOB_SILENT_MIN", "12"))       # (legacy; retained for callers)
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


def _reap_dead_jobs():
    """Reap ONLY jobs whose worker is genuinely dead — its heartbeat lapsed (process gone) OR it blew the hard
    ceiling (runaway) — and NEVER on output silence (overhaul Step 1; the correct Temporal/Step-Functions liveness
    model). A long, quiet-but-alive build keeps beating heartbeat_at on its background timer, so it is never
    falsely reaped (F8). Bumps lease_token to fence a wrongly-reaped-but-alive worker. Returns rows reaped."""
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""
            UPDATE controller_jobs cj SET status='failed', lease_token = cj.lease_token + 1,
                result = COALESCE(cj.result,'{}'::jsonb)
                         || '{"error":"worker died (heartbeat lapsed or hard ceiling)","status":"failed"}'::jsonb,
                finished_at = now()
            WHERE cj.status='running'
              AND cj.started_at < now() - make_interval(mins => %s)               -- past the generous floor
              AND (
                    COALESCE(cj.heartbeat_at, cj.started_at) < now() - make_interval(secs => %s)  -- heartbeat lapsed
                 OR cj.started_at < now() - make_interval(mins => %s)             -- OR hard ceiling (runaway)
              )
        """, (RUNNING_FLOOR_MIN, HEARTBEAT_TIMEOUT_S, HARD_CEILING_MIN))
        n = cur.rowcount; c.commit()
        return n


def liveness_selftest():
    """Prove the overhaul Step-1 liveness model: a quiet-but-beating job is NEVER reaped; a job whose heartbeat
    lapsed IS reaped; a job past the hard ceiling IS reaped. This is the fix for F8 (healthy build killed for
    going quiet). Offline, DB-only."""
    _ensure()
    import psycopg as _pg
    def mk(started_min_ago, beat_secs_ago):
        with _pg.connect(DB) as c, c.cursor() as cur:
            cur.execute("""INSERT INTO controller_jobs (thread_id, tenant_id, phase, kind, status, started_at, heartbeat_at)
                           VALUES (0,'live-selftest','IMPLEMENT','build','running',
                                   now() - make_interval(mins => %s),
                                   CASE WHEN %s IS NULL THEN NULL ELSE now() - make_interval(secs => %s) END)
                           RETURNING id""",
                        (started_min_ago, beat_secs_ago, beat_secs_ago or 0))
            jid = cur.fetchone()[0]; c.commit(); return jid
    def status(jid):
        with _pg.connect(DB) as c, c.cursor() as cur:
            cur.execute("SELECT status FROM controller_jobs WHERE id=%s", (jid,)); return cur.fetchone()[0]
    ok = True
    try:
        alive = mk(120, 20)                # 2h old but beat 20s ago → ALIVE (slow Opus build), must NOT reap
        dead = mk(40, 600)                 # 40 min old, last beat 10 min ago → worker dead, MUST reap
        runaway = mk(HARD_CEILING_MIN + 30, 10)   # beating, but past the liberal hard ceiling → MUST reap
        young = mk(5, 600)                 # heartbeat lapsed but under the floor → too young, must NOT reap
        _reap_dead_jobs()
        checks = [(status(alive) == "running", "quiet-but-beating build is NOT reaped (F8 fixed)"),
                  (status(dead) == "failed", "heartbeat-lapsed worker IS reaped"),
                  (status(runaway) == "failed", "past-hard-ceiling runaway IS reaped"),
                  (status(young) == "running", "job under the floor is NOT reaped")]
        for cond, label in checks:
            print(("PASS" if cond else "FAIL") + f": {label}"); ok = ok and cond
        print("liveness_selftest: PASS (output-independent heartbeat liveness; no false reap of a quiet build)"
              if ok else "liveness_selftest: FAIL")
        return 0 if ok else 1
    finally:
        with _pg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM controller_jobs WHERE tenant_id='live-selftest'"); c.commit()


# ARCHITECTURE-OVERHAUL Step 2 — SINGLE OWNER PER BUILD. Every path that ADVANCES a thread (jobd's
# runnable loop, a resume-sweep in ANY process, a live worker completion) first takes this per-thread
# Postgres advisory lock. At most one driver advances a given thread at a time, so two sweepers — or a
# sweeper racing jobd — can never double-advance the same build. jobd imports this so the lock NAMESPACE
# is defined in exactly one place (no drifting magic numbers).
_DRIVE_LOCK_NS = 841000


def _close_quietly(conn):
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


def _latest_job_status(thread_id):
    """Status of the thread's most recent controller_job (None if it has none). Used to re-check UNDER the
    drive lock that a pre-lock read isn't stale — another owner may have advanced the thread and dispatched
    the next phase (a fresh 'running' job) between our read and our lock acquisition."""
    try:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("SELECT status FROM controller_jobs WHERE thread_id=%s ORDER BY id DESC LIMIT 1",
                        (thread_id,))
            r = cur.fetchone()
            return r[0] if r else None
    except Exception:
        return None


@contextlib.contextmanager
def thread_drive_lock(thread_id):
    """Yield True if THIS caller now owns the right to advance `thread_id` (lock acquired), False if another
    driver already holds it (caller must skip and let the owner proceed). Session-scoped advisory lock held
    on a dedicated connection for the whole block. FAIL-OPEN (yields True) on a DB hiccup — a lock-server
    blip must never wedge all forward progress; the terminal-write guards remain the backstop.

    Structured so the generator yields EXACTLY once on every path (setup error, not-owned, owned) and an
    exception raised inside the caller's block propagates normally after the lock is released."""
    conn = None
    got = False
    try:                                              # SETUP: acquire the lock (fail-open on infra error)
        conn = psycopg.connect(DB)
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s, %s)", (_DRIVE_LOCK_NS, int(thread_id)))
            got = cur.fetchone()[0]
    except Exception:
        _close_quietly(conn)
        yield True                                    # lock infra down -> own it ungated rather than stall
        return
    if not got:
        _close_quietly(conn)
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
        _close_quietly(conn)


def resume_stalled():
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
    _ensure()
    advanced = 0
    # USER-FACING SLA first (#2.6): surface "taking longer than usual" the moment a job overruns its ETA —
    # well before the 30-min crash-reap below — so the same scheduler tick that recovers dead workers also
    # keeps live-but-slow jobs honest. Best-effort: a watchdog hiccup must never block crash recovery.
    try:
        sla_watchdog()
    except Exception:
        pass
    # 0) RESEARCH threads are reconciled against the REAL research run (research_runs) — NOT the dispatch
    #    poll. A fleet run that outlived the in-worker poll budget (-> 'pending'), or whose worker/process
    #    died, still reaches a terminal state in its own daemon; pull its result through so the extracted
    #    options are never orphaned — even if an older build already parked the thread on a feedback gate.
    #    This OWNS RESEARCH recovery; the generic fleet sweep below skips RESEARCH to avoid a double-advance
    #    or a spurious failure from a controller_job the reaper marked 'failed' while the run was healthy.
    try:
        import research as _r
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("""SELECT thread_id, tenant_id, research_run_id, awaiting FROM controller_state
                           WHERE phase='RESEARCH' AND research_run_id IS NOT NULL""")
            rrows = cur.fetchall()
        for thread_id, rtid, rid, awaiting in rrows:
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
                with psycopg.connect(DB) as c, c.cursor() as cur:
                    cur.execute("""UPDATE controller_jobs SET status=%s, finished_at=COALESCE(finished_at, now())
                                   WHERE thread_id=%s AND kind='research' AND status IN ('running','pending')""",
                                (rstatus, thread_id))
                    claimed = cur.rowcount; c.commit()
                if not claimed:
                    continue                      # a prior owner already reconciled this run — don't re-advance
                _set(thread_id, awaiting=None)
                advance(thread_id, job_result=jr)  # done -> OPTIONS; failed -> surfaces failure
                advanced += 1
    except Exception:
        pass
    _reap_dead_jobs()                       # overhaul Step 1: reap by heartbeat/ceiling, NEVER by output silence
    with psycopg.connect(DB) as c, c.cursor() as cur:
        # 2) For every NON-research thread parked on 'fleet', take its most recent job (any status).
        #    RESEARCH is reconciled above against its real run, so exclude it here.
        cur.execute("""SELECT DISTINCT ON (cj.thread_id) cj.thread_id, cj.result, cj.status
                       FROM controller_jobs cj
                       JOIN controller_state cs ON cs.thread_id=cj.thread_id
                       WHERE cs.awaiting='fleet' AND cs.phase <> 'RESEARCH'
                       ORDER BY cj.thread_id, cj.id DESC""")
        rows = cur.fetchall()
    for thread_id, result, status in rows:
        if status in ("running", "pending"):
            continue                              # newest job still genuinely in flight — leave it alone
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
    return {"resumed": advanced}


def sla_watchdog():
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
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT thread_id, tenant_id, phase, job_kind, job_eta_min,
                              EXTRACT(EPOCH FROM (now()-job_started_at))::int
                       FROM controller_state
                       WHERE awaiting='fleet' AND job_started_at IS NOT NULL AND job_eta_min IS NOT NULL
                         AND now() - job_started_at > make_interval(mins => job_eta_min)
                         AND (job_sla_warned_at IS NULL
                              OR now() - job_sla_warned_at > make_interval(mins => %s))""",
                    (_SLA_REWARN_MIN,))
        rows = cur.fetchall()
    warned = 0
    for thread_id, tid, phase, jk, eta, elapsed in rows:
        # Claim the warning atomically (still on its fleet gate + still outside the re-warn throttle) so a
        # concurrent sweep or a job that just finished can't also post. In the SAME write, RAISE the stored
        # ETA so the console's live range stops lying about "N min left". 0 rows -> beaten to it; skip.
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("""UPDATE controller_state
                           SET job_sla_warned=true, job_sla_warned_at=now(), updated_at=now(),
                               job_eta_min = GREATEST(
                                   COALESCE(job_eta_min, 0) + 1,
                                   CEIL(EXTRACT(EPOCH FROM (now()-job_started_at)) / 60.0)::int + 2)
                           WHERE thread_id=%s AND awaiting='fleet'
                             AND (job_sla_warned_at IS NULL
                                  OR now() - job_sla_warned_at > make_interval(mins => %s))
                           RETURNING job_eta_min""", (thread_id, _SLA_REWARN_MIN))
            claim = cur.fetchone(); c.commit()
        if not claim:
            continue
        new_eta = claim[0]
        em = int((elapsed or 0) // 60)
        again = eta and em >= eta + _SLA_REWARN_MIN     # a follow-up re-ping vs. the first overrun warning
        lead = "It's still running" if again else "This is taking longer than usual — still running"
        _report(tid, thread_id,
                f"⏳ {lead} ({em}m elapsed; now expecting up to ~{new_eta} min total). It may just need a "
                f"little more time; say \"retry\" to start it over or \"cancel\" to stop.",
                {"kind": "sla_warning", "phase": phase, "job": jk, "elapsed_min": em, "eta_min": new_eta,
                 "reping": bool(again), "actions": ["retry", "cancel"]}, urgent=True)
        # The _report above only reaches an OPEN chat tab. Also light up the always-available in-app feed (and
        # a push, if ntfy/email are configured) so a CEO who LEFT is told their run is overrunning — not left
        # wondering whether it died. level=urgent -> the bell/feed is unmistakable + push fires high-priority.
        _ping(tid, "Still working — taking longer than usual",
              f"Your {(phase or 'current').lower()} step is still running ({em}m elapsed). I'll post the "
              f"results in the chat the moment it's done.", level="urgent")
        audit.append(actor="loopcontroller", action="SLAWarn", resource=str(thread_id), decision=phase,
                     payload={"elapsed_min": em, "eta_min": new_eta, "reping": bool(again)})
        warned += 1
    return {"warned": warned}


MAX_BUILD_RETRY = int(os.environ.get("AOS_MAX_BUILD_RETRY", "3"))
BUILD_BUDGET_USD = float(os.environ.get("AOS_BUILD_BUDGET_USD", "40"))   # hard $ ceiling for ONE product's build


def _spend_usd(product):
    """Real cumulative $ this product's build has spent (from the same traces ledger appguard reads)."""
    try:
        import appguard
        return float(appguard.economics(product).get("spend") or 0.0)
    except Exception:
        return 0.0


def _autoloop_build(thread_id, tid, product, reason=""):
    """A failed/unverifiable build routes back to the DEV/builder AUTOMATICALLY (bounded), and escalates to the
    CEO ONLY when the autonomous loop is exhausted — never bothering the human before then (North Star: the
    human is the last resort, not the first responder). Leaves the thread runnable so jobd/advance re-dispatches
    the build in a long-lived process. HARD BUDGET GATE: a non-converging build must not bleed money — if
    cumulative spend has crossed BUILD_BUDGET_USD, STOP retrying and escalate regardless of the retry count."""
    spent = _spend_usd(product)
    if spent >= BUILD_BUDGET_USD:                   # hard money stop — stop spawning, hand it to the human
        _set(thread_id, awaiting="user_feedback"); _to(thread_id, "IMPLEMENT")
        _report(tid, thread_id,
                f"🛑 I paused this build — it's spent ${spent:.0f} (budget ${BUILD_BUDGET_USD:.0f}) and still "
                f"isn't passing verification ({str(reason)[:120]}). I stopped before spending more. Say "
                f"\"keep going\" to raise the budget and continue, or tell me how you'd like to proceed.",
                {"kind": "budget_stop", "spent": round(spent, 2)}, urgent=True)
        audit.append(actor="loopcontroller", action="BuildBudgetStop", resource=str(product),
                     decision="halted", payload={"spent": round(spent, 2), "budget": BUILD_BUDGET_USD})
        return
    try:
        import productregistry as _preg
        n = _preg.attempt(product, "build_retry")
    except Exception:
        n = MAX_BUILD_RETRY + 1                     # registry unavailable -> be conservative, escalate
    if n <= MAX_BUILD_RETRY:
        _to(thread_id, "IMPLEMENT"); _set(thread_id, awaiting=None)     # runnable -> re-dispatch the build
        _report(tid, thread_id,
                f"🔧 Verification didn't pass (attempt {n}/{MAX_BUILD_RETRY}: {str(reason)[:150]}). Handing it "
                f"back to the builder to fix and re-test — nothing needed from you.",
                {"kind": "auto_rebuild", "attempt": n}, urgent=False)
        audit.append(actor="loopcontroller", action="AutoRebuild", resource=str(thread_id), decision=f"attempt-{n}")
        advance(thread_id)
    else:
        _set(thread_id, awaiting="user_feedback"); _to(thread_id, "IMPLEMENT")
        _report(tid, thread_id,
                f"⚠️ I tried to fix and re-test this {MAX_BUILD_RETRY}× but it still isn't passing "
                f"({str(reason)[:150]}). I've held it back from delivery — tell me how you'd like to proceed, or "
                f"say \"retry\" to keep trying.", {"kind": "qa_failed_escalate"}, urgent=True)
        audit.append(actor="loopcontroller", action="QAGate", resource=str(thread_id), decision="ESCALATE")


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


def _store_user(tid, thread_id, msg):
    """Persist a user turn, SUPPRESSING a consecutive identical duplicate (a double-tapped send / client
    retry). Returns True if it stored a new turn, False if it suppressed an exact repeat of the last user
    message — so callers can also skip re-posting a duplicate reply."""
    with psycopg.connect(DB) as c, c.cursor() as cur:
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
        # exhausted — surface an actionable, resumable message (NEVER a bare "timeout"); phase stays put so the
        # CEO can just say "retry" and pick up exactly here.
        return "⚠️ The model call kept timing out for a moment — say \"retry\" and I'll pick right back up."
    # Use the COMPLETE output (out_full) — never the tail-truncated 'out'. The controller's reply carries
    # leading control blocks ([[RESEARCH]]/[[PLAN]]); a >1500-char plan would lose its OPENING tag under
    # front-truncation, so _parse_block fails (plan never persists) and a dangling [[/PLAN]] leaks to chat.
    return (r.get("out_full") or r.get("out") or "").strip() or "Tell me a bit more."


def _parse_plan(body):
    def f(name, d=""):
        m = re.search(rf"{name}\s*:\s*(.+?)(?:\n[a-z]+\s*:|\Z)", body, re.S | re.I)
        return m.group(1).strip() if m else d
    kind = f("kind", "service").lower()
    return {"name": (f("name", "app").split()[0][:24] or "app"),
            "kind": kind if kind in ("lib", "web", "service", "project") else "service",
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
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""UPDATE controller_jobs SET status='cancelled',
                          result = COALESCE(result, '{}'::jsonb) || '{"cancelled":true}'::jsonb,
                          finished_at = now()
                       WHERE thread_id=%s AND status IN ('running', 'pending')""", (thread_id,))
        jobs = cur.rowcount; c.commit()
    _set(thread_id, awaiting="user_feedback")
    _job_clear(thread_id)
    _report(tid, thread_id,
            f"⏹️ Stopped the **{phase}** step — nothing more will run until you say so. Say \"retry\" to "
            f"start it again, or tell me what to change.", {"kind": "cancelled", "phase": phase})
    audit.append(actor="loopcontroller", action="JobCancelled", resource=str(thread_id), decision=phase,
                 payload={"scopes": scopes, "jobs": jobs, "reason": str(reason)[:200]})
    return {"cancelled": True, "phase": phase, "scopes": scopes, "jobs_cancelled": jobs}


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
    tid = billing.signup("loopctl-selftest", "free")["tenant_id"]
    org = _orgs.create(tid, "Test Org", "a test")["org_id"]
    real_agent = factory.agent
    real_build = getattr(factory, "build_product", None)
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
    try:
        import tenantproviders; tenantproviders.connect(tid, "anthropic", "subscription")
    except Exception:
        pass
    import consent

    def wait(th, target, gate=None, tmax=14):
        for _ in range(tmax * 5):
            s = _st(th)
            if s["phase"] == target and (gate is None or s["awaiting"] == gate):
                return True
            time.sleep(0.2)
        return False
    try:
        th = start(tid, org)["thread_id"]
        # CONSENT GATE: pre-consent, say() must REFUSE before touching the LLM (no phase change, no spend) and
        # tell the CEO to accept consent — not silently send their text to the provider.
        pre = say(tid, th, "I want a YouTube competitor")
        consent_gate_ok = (pre.get("blocked") == "consent_required" and _st(th)["phase"] == "DISCOVER")
        consent.record(tid)                                       # CEO accepts AI-processing consent in Settings
        say(tid, th, "I want a YouTube competitor")               # DISCOVER->RESEARCH->OPTIONS
        opt = wait(th, "OPTIONS", "user_approval")
        # (#1) OPTIONS ELABORATES, doesn't force: a typed QUESTION about the options runs an LLM answer (a new
        # task) and the gate is HELD (still OPTIONS/user_approval) — never advances, never a bare "tap one".
        n_opt = len(tasks)
        say(tid, th, "which option is cheaper and why?")
        options_elaborate_ok = (len(tasks) > n_opt and _st(th)["phase"] == "OPTIONS"
                                and _st(th)["awaiting"] == "user_approval")
        # an EMPTY/whitespace message just NUDGES to pick — no LLM turn, gate still held.
        n_opt2 = len(tasks)
        say(tid, th, "   ")
        options_nudge_ok = (len(tasks) == n_opt2 and _st(th)["phase"] == "OPTIONS")
        # (#2) EXPOSE THE RESEARCH DOC + option summaries: research_report() returns the report+options for the
        # org, and every presented option card carries a non-empty summary (not a bare title).
        rr = research_report(tid, org)
        opts_now = _st(th).get("options") or []
        research_report_ok = (isinstance(rr, dict) and "report" in rr and isinstance(rr.get("options"), list)
                              and bool(opts_now)
                              and all((o.get("summary") or "").strip() for o in opts_now if isinstance(o, dict)))
        gate_held = _st(th)["phase"] == "OPTIONS"                  # OPTIONS only moves via choose()/an ordinal
        choose(tid, th, 1)                                         # ->DEEP_DESIGN
        in_design = _st(th)["phase"] == "DEEP_DESIGN"
        say(tid, th, "go ahead")                                  # draft PLAN (awaiting feedback)
        # PLAN must parse from the (front-truncatable) LLM reply: persisted to state, rendered as a plan
        # card (meta.kind='plan'), and NO dangling control tag leaked into the user-visible chat.
        plan_persisted = bool((_st(th).get("plan") or {}).get("name"))
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("""SELECT content, meta FROM chat_messages WHERE thread_id=%s AND role='assistant'
                           ORDER BY id DESC LIMIT 1""", (th,))
            pc, pm = cur.fetchone()
        plan_card = isinstance(pm, dict) and pm.get("kind") == "plan"
        no_tag_leak = "[[" not in (pc or "")
        # (#3) EXPOSE THE FULL PLAN: the plan meta carries the COMPLETE plan text (meta.plan.full), not just a
        # one-line charter — persisted to state AND on the plan card — so the console can show & explain it.
        plan_full_ok = (bool((_st(th).get("plan") or {}).get("full"))
                        and isinstance(pm, dict) and bool(((pm.get("plan") or {}).get("full")))
                        and "Plan:" in ((_st(th).get("plan") or {}).get("full") or ""))
        say(tid, th, "looks good")                                # approve plan -> PLAN_APPROVAL -> PROTOTYPE
        proto = wait(th, "IMPLEMENT", "user_feedback")            # prototype done -> gated at IMPLEMENT for approval
        # (#4) SURFACE THE DESIGN: the prototype message meta references the design artifact (org + a flag) so
        # the console can link "Review the design".
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("""SELECT meta FROM chat_messages WHERE thread_id=%s AND role='assistant'
                           AND meta->>'kind'='prototype' ORDER BY id DESC LIMIT 1""", (th,))
            _pr = cur.fetchone()
        design_surface_ok = (bool(_pr) and isinstance(_pr[0], dict) and _pr[0].get("design_ready") is True
                             and _pr[0].get("org") == org)
        say(tid, th, "approve")                                   # -> build -> TESTQA -> DELIVER
        deliver = wait(th, "DELIVER")
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("SELECT count(*) FROM controller_jobs WHERE thread_id=%s AND status='done'", (th,))
            jobs = cur.fetchone()[0]

        # (1) ETA on kickoff: estimate.py-backed, positive, and surfaced as an HONEST RANGE "~lo-hi min" in a
        # kickoff message — never a false-precision point (#2). The _eta_range helper must bracket the point.
        eta_min = _estimate_runtime("IMPLEMENT", {"kind": "service", "charter": "auth billing dashboard api"})
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("""SELECT count(*) FROM chat_messages WHERE thread_id=%s AND role='assistant'
                           AND content ~ '~[0-9]+-[0-9]+ min'""", (th,))
            eta_msg = cur.fetchone()[0]
        r_lo, r_hi = _eta_range(eta_min)
        eta_range_ok = r_lo is not None and r_lo < eta_min < r_hi and _eta_phrase(eta_min) == f"~{r_lo}-{r_hi} min"
        eta_ok = isinstance(eta_min, int) and eta_min > 0 and eta_msg >= 1 and eta_range_ok
        # (1c) CONSTANTS RECONCILED with console.py:163 — the controller's coarse fallbacks equal the console's,
        # so the two live-progress bubbles can never promise different numbers.
        consts_ok = _PHASE_ETA_DEFAULT == {"RESEARCH": 10, "PROTOTYPE": 6, "IMPLEMENT": 14, "TESTQA": 5}
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
        th3 = start(tid, org)["thread_id"]
        _set(th3, awaiting="fleet")
        _job_begin(th3, "build", 5, "Building…")
        with psycopg.connect(DB) as c, c.cursor() as cur:    # backdate start so it has clearly overrun ~5m ETA
            cur.execute("UPDATE controller_state SET job_started_at=now()-interval '9 min' WHERE thread_id=%s",
                        (th3,))
            c.commit()
        w1 = sla_watchdog().get("warned", 0)
        with psycopg.connect(DB) as c, c.cursor() as cur:   # ETA must be RAISED past the elapsed time (#2)
            cur.execute("SELECT job_eta_min FROM controller_state WHERE thread_id=%s", (th3,))
            eta_after_warn = cur.fetchone()[0]
        w2 = sla_watchdog().get("warned", 0)                 # immediate re-sweep must NOT re-warn (throttled)
        # (5) RE-PING ON CONTINUED OVERRUN: after the re-warn window elapses and the job is STILL overrunning,
        # the watchdog warns + pings AGAIN — not one-and-done silence. Backdate the last-warn + start so both
        # the throttle and the (now-raised) ETA are exceeded, then a fresh sweep must re-warn exactly once.
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("""UPDATE controller_state
                           SET job_sla_warned_at=now()-interval '9 min',
                               job_started_at=now()-interval '40 min' WHERE thread_id=%s""", (th3,))
            c.commit()
        w3 = sla_watchdog().get("warned", 0)                 # continued overrun -> re-ping fires again
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("""SELECT count(*) FROM chat_messages WHERE thread_id=%s AND role='assistant'
                           AND meta->>'kind'='sla_warning'""", (th3,))
            sla_msgs = cur.fetchone()[0]
        sla_ok = (w1 >= 1 and w2 == 0 and w3 >= 1 and sla_msgs == 2
                  and isinstance(eta_after_warn, int) and eta_after_warn >= 9)

        # (2.1) STATUS HONESTY: a message typed WHILE a durable job is in flight must return the TRUE running
        # status (kind='working') and run NO free-form LLM turn — never a hallucinated "Done".
        th4 = start(tid, org)["thread_id"]
        _to(th4, "RESEARCH"); _set(th4, awaiting="fleet")
        _job_begin(th4, "research", 6, "Researching directions…")
        n_before = len(tasks)
        r4 = say(tid, th4, "is it done yet?")
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("""SELECT meta->>'kind' FROM chat_messages WHERE thread_id=%s AND role='assistant'
                           ORDER BY id DESC LIMIT 1""", (th4,))
            last_kind = cur.fetchone()[0]
        status_honest_ok = (r4.get("running") is True and len(tasks) == n_before and last_kind == "working")

        # (2.1b) MID-FLIGHT PRE-AUTHORIZED INTENT: a real directive typed WHILE research runs must be QUEUED
        # (not dropped, no LLM turn, gate held) and then APPLIED when results land — auto-selecting the
        # recommended option into DEEP_DESIGN instead of silently re-parking on the user_approval gate.
        th5 = start(tid, org)["thread_id"]
        _to(th5, "RESEARCH"); _set(th5, awaiting="fleet", research_run_id=777)
        _job_begin(th5, "research", 6, "Researching…")
        n5 = len(tasks)
        r5 = say(tid, th5, "go with your recommendation and start building it now")
        intent_queued = (r5.get("queued_intent") is True and len(tasks) == n5
                         and bool((_st(th5).get("pending_intent") or "")) and _st(th5)["awaiting"] == "fleet")
        _set(th5, awaiting=None)                                   # worker clears the gate before advancing
        advance(th5, {"run_id": 777, "options": [{"id": 1, "title": "A", "recommended": True}]})
        s5 = _st(th5)
        intent_applied = (s5["phase"] == "DEEP_DESIGN" and s5["awaiting"] == "user_feedback"
                          and bool(s5.get("chosen_option")) and not (s5.get("pending_intent") or ""))
        # a pure status ping must NOT be queued as an intent (stays a status reply, no pending_intent)
        th5b = start(tid, org)["thread_id"]
        _to(th5b, "RESEARCH"); _set(th5b, awaiting="fleet")
        _job_begin(th5b, "research", 6, "Researching…")
        rq = say(tid, th5b, "is it done yet?")
        status_not_queued = (rq.get("queued_intent") is False and not (_st(th5b).get("pending_intent") or ""))
        midflight_intent_ok = intent_queued and intent_applied and status_not_queued

        # (2.1c) DUPLICATE SUPPRESSION: a double-tapped identical message must NOT store a second user turn nor
        # post a second identical status bubble — the first reply stands (flagged duplicate on the retry).
        th6 = start(tid, org)["thread_id"]
        _to(th6, "RESEARCH"); _set(th6, awaiting="fleet")
        _job_begin(th6, "research", 6, "Researching…")
        d1 = say(tid, th6, "how's it going?")
        d2 = say(tid, th6, "how's it going?")                       # exact repeat -> suppressed
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("SELECT count(*) FROM chat_messages WHERE thread_id=%s AND role='user' "
                        "AND content='how''s it going?'", (th6,))
            dup_user_rows = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM chat_messages WHERE thread_id=%s AND role='assistant' "
                        "AND meta->>'kind'='working'", (th6,))
            dup_status_bubbles = cur.fetchone()[0]
        dedupe_ok = (d1.get("duplicate") is not True and d2.get("duplicate") is True
                     and dup_user_rows == 1 and dup_status_bubbles == 1)

        # (2) LIVE PROGRESS + (4) NO FALSE DONE + (5) CANCEL — on a fresh thread with a stamped in-flight job:
        th2 = start(tid, org)["thread_id"]
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
              and design_surface_ok and orchestra_wired_ok)
        print(f"consent_gate={consent_gate_ok} options={opt} gate_held={gate_held} design={in_design} "
              f"plan_persisted={plan_persisted} plan_card={plan_card} no_tag_leak={no_tag_leak} "
              f"prototype={proto} deliver={deliver} jobs_done={jobs}")
        print(f"eta(kickoff range)={eta_ok}(est={eta_min}m,range={_eta_phrase(eta_min)}) "
              f"consts_reconciled={consts_ok} research_eta_realistic={research_eta_ok}"
              f"(={research_eta}m) ping(results-land)={ping_ok} "
              f"live_progress={live_ok} no_false_done={no_false_done_running and no_false_done_after} "
              f"cancel(killswitch)={cancel_ok}")
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
        factory.agent = real_agent
        if real_build is not None:
            factory.build_product = real_build
        _r.start, _r.run_state, _r.select, _d.prototype, _q.run, factory.run_grounded_qa = real
        with psycopg.connect(DB) as c, c.cursor() as cur:
            for t in ("controller_jobs", "controller_state", "chat_messages", "chat_threads", "orgs",
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
        print(json.dumps(resume_stalled()))
    elif a[0] == "watchdog":
        print(json.dumps(sla_watchdog()))
    elif a[0] == "liveness":
        sys.exit(liveness_selftest())
    else:
        sys.exit("usage: loopcontroller.py "
                 "start|say|choose|state|research|live|cancel|resume|watchdog|liveness|selftest ...")


if __name__ == "__main__":
    _main(sys.argv[1:])
