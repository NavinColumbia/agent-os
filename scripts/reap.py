#!/usr/bin/env python3
"""reap.py — bounded resiliency cleanup for exact-owned processes, stale DB rows, and scratch.

When an orchestrator (a build or research run) is killed, its detached `claude -p` / `codex exec` children
can survive as orphans and keep burning tokens + RAM. Destructive process cleanup requires an exact durable
registry identity (boot ID, PID, and birth ticks) plus proof that its owner generation is gone. Generic
`claude -p` / `codex exec` and browser command matches are diagnostics only. The same bounded duty also
reconciles stale database rows and old agent-os scratch. Component failures are explicit and nonzero.

    reap.py run        # reap orphans/stuck + clean scratch
    reap.py status     # show what WOULD be reaped (dry run)
    reap.py selftest
Run with the agent-os venv python.
"""
import json
import fnmatch
import os
import signal
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit  # noqa: E402
import process_assurance  # noqa: E402
from dbpool import connection  # noqa: E402

def _bounded_int(name, default, minimum, maximum):
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = int(default)
    return max(int(minimum), min(int(maximum), value))


MAX_RUNTIME_S = _bounded_int("AOS_AGENT_MAX_RUNTIME", 2400, 60, 86400)
SCRATCH_MAX_AGE_S = _bounded_int("AOS_SCRATCH_MAX_AGE", 3600, 60, 86400 * 30)
SCRATCH_GLOBS = ["codexrun-*", "improve-*", "research-*", "webqa-*.png"]
BROWSER_STALE_S = _bounded_int("AOS_BROWSER_STALE_MIN", 60, 5, 1440) * 60
BUILD_ABANDON_H = _bounded_int("AOS_BUILD_ABANDON_H", 12, 1, 168)
# A research run legitimately takes ~10-30 min and the controller stretches its own estimate to ~92 before
# complaining. 2h therefore only ever catches a run whose worker actually died.
RESEARCH_ABANDON_MIN = _bounded_int("AOS_RESEARCH_ABANDON_MIN", 120, 10, 10080)
# The worker beats every ~45s and the generic job reaper calls a worker dead after 180s. 10 min is far
# beyond both, so a lapse this long means the process is genuinely gone — not merely slow.
RESEARCH_HEARTBEAT_LAPSE_MIN = _bounded_int("AOS_RESEARCH_HB_LAPSE_MIN", 10, 2, 1440)
PROCESS_SCAN_LIMIT = _bounded_int("AOS_REAP_PROCESS_SCAN_LIMIT", 4096, 64, 65536)
REGISTRY_SCAN_LIMIT = _bounded_int("AOS_REAP_REGISTRY_SCAN_LIMIT", 200, 1, 5000)
# Inventory and mutation are separate bounds. A busy shared /tmp can contain thousands of
# unrelated entries; letting the first N unrelated names consume the cleanup batch permanently
# starves agent-os scratch later in the directory. Inventory remains hard-bounded, while at most
# SCRATCH_SCAN_LIMIT matching entries are inspected/deleted per sweep.
SCRATCH_VISIT_LIMIT = _bounded_int("AOS_REAP_SCRATCH_VISIT_LIMIT", 10000, 200, 100000)
SCRATCH_SCAN_LIMIT = _bounded_int("AOS_REAP_SCRATCH_SCAN_LIMIT", 200, 1, 5000)
SCRATCH_TREE_LIMIT = _bounded_int("AOS_REAP_SCRATCH_TREE_LIMIT", 1000, 1, 20000)
DB_BATCH_LIMIT = _bounded_int("AOS_REAP_DB_BATCH_LIMIT", 50, 1, 500)
DB_LOCK_TIMEOUT_MS = _bounded_int("AOS_REAP_DB_LOCK_TIMEOUT_MS", 500, 50, 5000)
DB_STATEMENT_TIMEOUT_MS = max(
    DB_LOCK_TIMEOUT_MS, _bounded_int("AOS_REAP_DB_STATEMENT_TIMEOUT_MS", 5000, 100, 30000))


def _set_db_timeouts(cur):
    cur.execute("SELECT set_config('lock_timeout', %s, true)", (f"{DB_LOCK_TIMEOUT_MS}ms",))
    cur.execute("SELECT set_config('statement_timeout', %s, true)",
                (f"{DB_STATEMENT_TIMEOUT_MS}ms",))


def _append_audit(action, resource, decision, payload):
    """Append a platform audit row without an unbounded global-chain lock wait."""
    try:
        _db, key = audit._cfg()
    except SystemExit as exc:
        raise RuntimeError(str(exc)) from exc
    event = dict(payload or {})
    if os.environ.get("AOS_SELFTEST", "").strip().lower() in {"1", "true", "yes", "on"}:
        event["_selftest"] = True
    with connection() as conn, conn.cursor() as cur:
        _set_db_timeouts(cur)
        role = audit._audit_role()
        if role:
            cur.execute(audit.sql.SQL("SET LOCAL ROLE {}").format(audit.sql.Identifier(role)))
        cur.execute("SELECT pg_try_advisory_xact_lock(742042)")
        if not cur.fetchone()[0]:
            raise RuntimeError("audit chain is busy; bounded append refused")
        cur.execute("SELECT entry_hash FROM audit_log ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        prev_hash = row[0] if row else ""
        canonical = audit._canonical("reap", action, resource, decision, event, prev_hash)
        entry_hash = audit._chain_hash(key, canonical)
        cur.execute("""INSERT INTO audit_log
                         (actor,action,resource,decision,payload,prev_hash,entry_hash,
                          tenant_id,t_prev_hash,t_entry_hash)
                       VALUES ('reap',%s,%s,%s,%s,%s,%s,NULL,NULL,NULL)
                       RETURNING id""",
                    (action, resource, decision, json.dumps(event), prev_hash, entry_hash))
        return cur.fetchone()[0], entry_hash


def _reap_reason(ppid, etimes, max_s=MAX_RUNTIME_S):
    """Pure decision (unit-tested): why this agent process should be reaped, or None to keep it."""
    if ppid == 1:
        return "orphaned (parent dead)"
    if etimes > max_s:
        return f"stuck ({etimes}s > {max_s}s)"
    return None


def _scan_process_table(limit=PROCESS_SCAN_LIMIT, proc_root=Path("/proc")):
    """Read at most ``limit`` /proc entries. Returns exact snapshots plus age and truncation evidence."""
    try:
        uptime_s = float((proc_root / "uptime").read_text().split()[0])
        ticks_s = float(os.sysconf("SC_CLK_TCK"))
        iterator = os.scandir(proc_root)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"process table unavailable: {exc}") from exc
    rows = {}
    seen = 0
    truncated = False
    with iterator:
        for entry in iterator:
            if not entry.name.isdigit():
                continue
            seen += 1
            if seen > int(limit):
                truncated = True
                break
            snap = process_assurance.read_snapshot(int(entry.name), proc_root)
            if snap is not None:
                age_s = max(0, int(uptime_s - (snap.identity.start_ticks / ticks_s)))
                rows[snap.identity.pid] = (snap, age_s)
    return {"rows": rows, "scanned": min(seen, int(limit)), "truncated": truncated,
            "limit": int(limit)}


def _agent_procs(process_rows=None):
    """Bounded diagnostic matches only; command text is never destructive authority."""
    if process_rows is None:
        process_rows = _scan_process_table()["rows"]
    procs = []
    for pid, (snap, etimes) in process_rows.items():
        args = snap.cmdline
        if ("claude -p " in args or "claude -p\t" in args or args.rstrip().endswith("claude -p")
                or "codex exec" in args):
            procs.append((int(pid), int(snap.ppid), int(etimes), args))
    return procs


def _reap_browser_decision(ppid, etimes, max_s=BROWSER_STALE_S):
    """Pure decision (unit-tested): reap a playwright browser only if ORPHANED (parent dead) or very OLD.
    A live QA run's browser is young + parented -> kept."""
    return ppid == 1 or etimes > max_s


def _browser_procs(process_rows=None):
    """(pid, ppid, etimes) for QA-leaked PROCESSES: playwright browser ROOTS (chrome-headless-shell AND the
    full 'chromium'/'chrome' channels — a leaked run left 42 of the latter that the old headless-only match
    missed) plus the per-session FFMPEG video recorders (each QA session = a Chromium + an ffmpeg; nothing
    reaped ffmpeg, so they leaked forever -> slow OOM). Renderer/gpu children (--type=) are skipped: they die
    with their root. Only ROOTS + recorders are returned; the ORPHANED/very-OLD safety in the caller ensures a
    live QA run is never touched."""
    if process_rows is None:
        process_rows = _scan_process_table()["rows"]
    procs = []
    for pid, (snap, etimes) in process_rows.items():
        args = snap.cmdline
        low = args.lower()
        is_browser_root = (("chrome-headless-shell" in low or "/chromium" in low or "/chrome " in low
                            or low.endswith("/chrome") or "headless_shell" in low)
                           and "--type=" not in args)
        # playwright records video via an ffmpeg child; a leaked session leaves the recorder writing forever
        is_ffmpeg_recorder = ("ffmpeg" in low and ("agent-os-qa-evidence" in low or "image2pipe" in low
                              or ".webm" in low))
        if is_browser_root or is_ffmpeg_recorder:
            procs.append((int(pid), int(snap.ppid), int(etimes)))
    return procs


def _owned_browser_command(args):
    """Narrow proof that a browser/ffmpeg root belongs to agent-os Playwright QA."""
    low = str(args or "").lower()
    if "ffmpeg" in low:
        return "agent-os-qa-evidence" in low or "image2pipe" in low or ".webm" in low
    playwright_binary = ("ms-playwright" in low or "chrome-headless-shell" in low or
                          "headless_shell" in low)
    automation = "--remote-debugging-pipe" in low or "--headless" in low
    return playwright_binary and automation and "--type=" not in low


def _identity_safe_signal(pid, expected_ppid, expected_args, sig):
    """Signal only if PID birth, parent, and the agent-os ownership marker survive revalidation."""
    first = process_assurance.read_snapshot(pid)
    if first is None or first.ppid != int(expected_ppid) or first.cmdline != str(expected_args).strip():
        return False
    if not _owned_browser_command(first.cmdline):
        return False
    second = process_assurance.read_snapshot(pid)
    if not process_assurance.same_process(first.identity, second):
        return False
    try:
        os.kill(pid, sig)
        return True
    except ProcessLookupError:
        return False


def _sweep_browsers(dry=False, process_rows=None):
    """Observe suspicious unregistered Playwright roots; never use a name match as kill authority.

    BrowserBridge roots now carry an exact boot-id/PID/start-ticks registry record and are reaped through
    ``clauded.reap_owned_orphans``.  This legacy scanner remains useful as diagnostics for installations that
    predate the registry, but even a Playwright-looking command line cannot prove that the process belongs to
    this repo or that its owner generation is gone.  ``dry`` is retained for call-site compatibility.
    """
    del dry
    observed = 0
    procs = _browser_procs() if process_rows is None else _browser_procs(process_rows)
    for _pid, ppid, etimes in procs:
        if _reap_browser_decision(ppid, etimes):
            observed += 1
    return observed


def _owner_generation_gone(data, root_snapshot, proc_root=Path("/proc")):
    """Prove the registered spawning generation is gone; legacy rows require orphan PPID evidence."""
    fields = ("owner_pid", "owner_start_ticks", "owner_boot_id")
    if all(data.get(k) is not None for k in fields):
        expected = process_assurance.ProcessIdentity(
            int(data["owner_pid"]), int(data["owner_start_ticks"]), str(data["owner_boot_id"]))
        observed = process_assurance.read_snapshot(expected.pid, proc_root)
        if observed is not None:
            return not process_assurance.same_process(expected, observed)
        try:
            return not (proc_root / str(expected.pid)).exists()
        except OSError:
            return False
    return bool(root_snapshot is not None and root_snapshot.ppid == 1)


def _registered_browser_records(registry, limit=REGISTRY_SCAN_LIMIT):
    """Read a globally bounded registry prefix. Invalid/unverifiable records are left untouched."""
    records, errors = [], []
    try:
        iterator = os.scandir(registry)
    except FileNotFoundError:
        return {"records": [], "scanned": 0, "truncated": False, "errors": []}
    except OSError as exc:
        raise RuntimeError(f"owned process registry unavailable: {exc}") from exc
    seen = 0
    truncated = False
    with iterator:
        for entry in iterator:
            if not entry.name.endswith(".json"):
                continue
            seen += 1
            if seen > int(limit):
                truncated = True
                break
            path = Path(entry.path)
            try:
                data = json.loads(path.read_text())
                if not str(data.get("owner") or "").startswith("qa-browser:"):
                    continue
                identity = process_assurance.ProcessIdentity(
                    int(data["pid"]), int(data["start_ticks"]), str(data["boot_id"]))
                records.append((identity, path, data))
            except Exception as exc:
                errors.append({"record": entry.name, "error": str(exc)[:200]})
    return {"records": records, "scanned": min(seen, int(limit)), "truncated": truncated,
            "errors": errors}


def _reap_owned_browser_trees(process_scan, *, dry=False, registry=None):
    """Reap only exact registered roots whose exact owner generation is provably gone.

    A complete bounded host snapshot is required before signaling because a truncated ancestry graph could
    leave descendants behind. Every PID is birth-revalidated immediately before its signal.
    """
    if registry is None:
        import clauded
        registry = clauded.REGISTRY
    registry_scan = _registered_browser_records(Path(registry))
    out = {"reaped": 0, "pids": [], "signaled_pids": [], "dry_run": bool(dry),
           "registry_scanned": registry_scan["scanned"],
           "registry_truncated": registry_scan["truncated"],
           "errors": list(registry_scan["errors"])}
    if process_scan.get("truncated"):
        out["errors"].append({"error": "process snapshot truncated; destructive tree cleanup refused"})
        return out
    snapshots = {pid: pair[0] for pid, pair in process_scan["rows"].items()}
    for identity, record, data in registry_scan["records"]:
        observed = process_assurance.read_snapshot(identity.pid)
        if (not process_assurance.same_process(identity, observed)
                or not _owner_generation_gone(data, observed)):
            continue
        out["pids"].append(identity.pid)
        if dry:
            out["reaped"] += 1
            continue
        signaled = []
        plan = process_assurance.cleanup_plan(identity, snapshots) + [identity]
        for expected in plan:
            if not process_assurance.same_process(expected, process_assurance.read_snapshot(expected.pid)):
                continue
            try:
                os.kill(expected.pid, signal.SIGKILL)
                signaled.append(expected.pid)
            except ProcessLookupError:
                continue
            except (PermissionError, OSError) as exc:
                out["errors"].append({"pid": expected.pid, "error": str(exc)[:200]})
        if identity.pid in signaled:
            out["reaped"] += 1
            out["signaled_pids"].extend(signaled)
            try:
                record.unlink(missing_ok=True)
            except OSError as exc:
                out["errors"].append({"record": record.name, "error": str(exc)[:200]})
    return out


def _sweep_stuck_builds(dry=False):
    """Auto-RESOLVE (not just detect) a build stuck with NO terminal outcome for BUILD_ABANDON_H+ hours: mark
    it ABANDONED (a recoverable terminal state) so the loop can stop, the sentinel stuck-build flag clears,
    and the projects view shows the truth instead of an eternal 'building'. The statement and result page are
    bounded; failures propagate to the per-component degraded result."""
    with connection() as c, c.cursor() as cur:
            _set_db_timeouts(cur)
            # Scoped to BUILD runs (run_id 'build-%', the same key factory's stuck-run query uses). A
            # standalone QA run traces under 'qa-<product>-<ts>'; without this scope a long QA sweep would
            # keep max(ts) fresh on a product whose first build trace is >12h old and get it marked
            # ABANDONED mid-verification. A build genuinely stuck IN its QA stage still traces under
            # 'build-%' and is still caught.
            cur.execute("""SELECT t.product FROM traces t
                           WHERE t.kind='agent' AND t.run_id LIKE 'build-%%'
                           GROUP BY t.product
                           HAVING max(t.ts) > now() - interval '20 minutes'
                              AND min(t.ts) < now() - make_interval(hours => %s)
                              AND NOT EXISTS (SELECT 1 FROM audit_log a
                                              WHERE a.resource=t.product AND a.action='ProductComplete')
                           LIMIT %s""", (BUILD_ABANDON_H, DB_BATCH_LIMIT))
            stuck = [r[0] for r in cur.fetchall()]
    if dry or not stuck:
        return len(stuck)
    for prod in stuck:
        _append_audit("ProductComplete", prod, "ABANDONED",
                      {"reason": f"stuck build: agents for >{BUILD_ABANDON_H}h with no terminal outcome"})
    return len(stuck)


def _component(result, name, fn, default):
    """Run one independent reaper component and retain typed failure evidence."""
    try:
        value = fn()
        status = "ok"
        errors = value.get("errors", []) if isinstance(value, dict) else []
        truncated = bool(value.get("truncated") or value.get("registry_truncated")) \
            if isinstance(value, dict) else False
        if errors or truncated:
            status = "degraded"
            result["degraded"].append({"component": name,
                                       "error": (errors[0].get("error") if errors else "scan truncated")})
        result["components"][name] = {"status": status, "result": value}
        return value
    except Exception as exc:
        result["components"][name] = {"status": "degraded", "error": str(exc)[:300]}
        result["degraded"].append({"component": name, "error": str(exc)[:300]})
        return default


def _stale_agent_diagnostics(process_rows):
    observed = []
    for pid, ppid, etimes, _args in _agent_procs(process_rows):
        reason = _reap_reason(ppid, etimes)
        if reason:
            observed.append({"pid": pid, "reason": reason})
    return observed


def reap(dry=False):
    """Run every bounded component independently and never convert a failure into a healthy zero."""
    result = {"status": "ok", "dry": bool(dry), "components": {}, "degraded": []}
    process_scan = _component(result, "process_scan", _scan_process_table,
                              {"rows": {}, "scanned": 0, "truncated": True,
                               "limit": PROCESS_SCAN_LIMIT})
    # Exact snapshots are internal kill-fencing evidence, not JSON/report payload.
    if result["components"].get("process_scan", {}).get("result") is process_scan:
        result["components"]["process_scan"]["result"] = {
            key: value for key, value in process_scan.items() if key != "rows"}
    unowned_agents = _component(
        result, "unowned_agent_diagnostics",
        lambda: _stale_agent_diagnostics(process_scan.get("rows", {})), [])
    owned_browser_trees = _component(
        result, "owned_browser_trees",
        lambda: _reap_owned_browser_trees(process_scan, dry=dry),
        {"reaped": 0, "pids": [], "errors": []})
    scratch = _component(result, "scratch", lambda: _clean_scratch(dry),
                         {"removed": [], "errors": [], "truncated": False})
    stale_dir = _component(result, "directory", lambda: _sweep_directory(dry), 0)
    # Command-line matches are diagnostics only. Exact registry identity is the sole browser kill authority.
    unowned_browsers = _component(
        result, "unowned_browser_diagnostics",
        lambda: _sweep_browsers(dry=True, process_rows=process_scan.get("rows", {})), 0)
    stale_runs = _component(result, "stale_runs", lambda: _sweep_stale_runs(dry), 0)
    terminal_leases = _component(result, "terminal_step_claims",
                                 lambda: _sweep_terminal_step_claims(dry), 0)
    terminal_event_claims = _component(result, "terminal_event_claims",
                                       lambda: _sweep_terminal_event_claims(dry), 0)
    stuck_builds = _component(result, "stuck_builds", lambda: _sweep_stuck_builds(dry), 0)
    dead_research = _component(result, "stale_research", lambda: _sweep_stale_research(dry), 0)
    result.update({"reaped": [], "unowned_agents_observed": unowned_agents,
                   "scratch_cleaned": scratch.get("removed", []),
                   "stale_directory_released": stale_dir,
                   "owned_browser_trees_reaped": int(owned_browser_trees.get("reaped") or 0),
                   "unowned_browsers_observed": unowned_browsers,
                   "stale_browsers_reaped": 0, "stale_runs_abandoned": stale_runs,
                   "terminal_step_claims_released": terminal_leases,
                   "terminal_event_claims_released": terminal_event_claims,
                   "stuck_builds_abandoned": stuck_builds, "dead_research_failed": dead_research})
    if result["degraded"]:
        result["status"] = "degraded"
    return result


def _sweep_stale_runs(dry=False):
    """Abandon orchestra runs stuck 'running' after an orchestrator crash (no actor sign-of-life in the
    window) — they inflate the running count + mislead dashboards. Claims a bounded batch with SKIP LOCKED;
    dry-run locks nothing and mutates nothing. Failures propagate to the component result."""
    secs = float(os.environ.get("AOS_RUN_STALE_H", "2")) * 3600
    with connection() as c, c.cursor() as cur:
        _set_db_timeouts(cur)
        if dry:
            cur.execute("""SELECT r.run_id FROM orchestra_runs r
                            WHERE r.status='running'
                              AND r.created_at<now()-make_interval(secs => %s)
                              AND NOT EXISTS (SELECT 1 FROM orchestra_actors a
                                               WHERE a.run_id=r.run_id
                                                 AND a.last_active>now()-make_interval(secs => %s))
                            ORDER BY r.created_at,r.run_id LIMIT %s""",
                        (secs, secs, DB_BATCH_LIMIT))
        else:
            cur.execute("""WITH candidates AS MATERIALIZED (
                               SELECT r.run_id FROM orchestra_runs r
                                WHERE r.status='running'
                                  AND r.created_at<now()-make_interval(secs => %s)
                                  AND NOT EXISTS (SELECT 1 FROM orchestra_actors a
                                                   WHERE a.run_id=r.run_id
                                                     AND a.last_active>now()-make_interval(secs => %s))
                                ORDER BY r.created_at,r.run_id
                                FOR UPDATE OF r SKIP LOCKED LIMIT %s
                           )
                           UPDATE orchestra_runs r SET status='abandoned',finished_at=now()
                             FROM candidates c WHERE r.run_id=c.run_id RETURNING r.run_id""",
                        (secs, secs, DB_BATCH_LIMIT))
        return len(cur.fetchall())


def _sweep_terminal_step_claims(dry=False):
    """Release leases owned by terminal actors/runs; never touch live actors in running runs."""
    with connection() as c, c.cursor() as cur:
        _set_db_timeouts(cur)
        if dry:
            cur.execute("""SELECT a.actor_id
                             FROM orchestra_actors a JOIN orchestra_runs r ON r.run_id=a.run_id
                            WHERE a.step_claimed_at IS NOT NULL
                              AND (r.status<>'running' OR a.status IN ('done','dead'))
                            ORDER BY a.step_claimed_at,a.actor_id LIMIT %s""", (DB_BATCH_LIMIT,))
        else:
            cur.execute("""WITH candidates AS MATERIALIZED (
                               SELECT a.actor_id
                                 FROM orchestra_actors a JOIN orchestra_runs r ON r.run_id=a.run_id
                                WHERE a.step_claimed_at IS NOT NULL
                                  AND (r.status<>'running' OR a.status IN ('done','dead'))
                                ORDER BY a.step_claimed_at,a.actor_id
                                FOR UPDATE OF a SKIP LOCKED LIMIT %s
                           )
                           UPDATE orchestra_actors a
                              SET step_claimed_at=NULL,step_claimed_by=NULL
                             FROM candidates c WHERE a.actor_id=c.actor_id RETURNING a.actor_id""",
                        (DB_BATCH_LIMIT,))
        return len(cur.fetchall())


def _sweep_terminal_event_claims(dry=False):
    """Release inbox claims from terminal runs without falsifying unprocessed forensic events."""
    with connection() as c, c.cursor() as cur:
        _set_db_timeouts(cur)
        if dry:
            cur.execute("""SELECT e.id
                             FROM orchestra_events e
                             JOIN orchestra_runs r ON r.run_id=e.run_id AND r.tenant_id=e.tenant_id
                            WHERE e.processed_at IS NULL AND e.claimed_at IS NOT NULL
                              AND r.status<>'running'
                            ORDER BY e.claimed_at,e.id LIMIT %s""", (DB_BATCH_LIMIT,))
        else:
            cur.execute("""WITH candidates AS MATERIALIZED (
                               SELECT e.id
                                 FROM orchestra_events e
                                 JOIN orchestra_runs r ON r.run_id=e.run_id AND r.tenant_id=e.tenant_id
                                WHERE e.processed_at IS NULL AND e.claimed_at IS NOT NULL
                                  AND r.status<>'running'
                                ORDER BY e.claimed_at,e.id
                                FOR UPDATE OF e SKIP LOCKED LIMIT %s
                           )
                           UPDATE orchestra_events e SET claimed_at=NULL,claimed_by=NULL
                             FROM candidates c WHERE e.id=c.id RETURNING e.id""",
                        (DB_BATCH_LIMIT,))
        return len(cur.fetchall())


def _sweep_stale_research(dry=False):
    """Fail a research run whose worker is gone, so the CEO stops being told it is still running.

    THE GAP THIS CLOSES (finding #752, reproduced 2026-08-10): research_runs is only ever updated from
    INSIDE the worker (research.py) — it marks itself done. When the worker dies, nothing flips the row, so
    it stays 'running' forever. loopcontroller's recovery loop reconciles a RESEARCH thread against that
    status and handles 'done' and 'failed' correctly, but a permanently-'running' row falls into its
    `else: continue  # still running — leave it` branch, so the recovery machinery that already exists
    never fires. Observed: run 532 sat 'running' for 94 minutes with NO worker process, NO claude CLI and
    NO trace for an hour, while the controller kept telling the CEO "still running (89m elapsed; expecting
    up to ~92 min)" and simply extended the estimate. That is the "silent loss of directed work with a
    false-green cockpit" bug: the work was gone and every surface reported healthy.

    Marking it 'failed' is all that is needed — the controller's existing reconcile then surfaces the
    failure to the thread on its next tick. The candidate page and mutation are bounded; rows already owned
    by another recovery pass are skipped rather than waited on."""
    with connection() as c, c.cursor() as cur:
        _set_db_timeouts(cur)
        cur.execute("""SELECT DISTINCT r.id,r.thread_id
                         FROM research_runs r
                         LEFT JOIN LATERAL (
                              SELECT j.heartbeat_at,j.status
                                FROM controller_jobs j
                               WHERE j.thread_id=r.thread_id AND j.kind='research'
                               ORDER BY j.id DESC LIMIT 1) j ON TRUE
                        WHERE r.status='running' AND r.finished_at IS NULL
                          AND ((j.heartbeat_at IS NOT NULL
                                AND j.heartbeat_at<now()-make_interval(mins => %s))
                               OR r.started_at<now()-make_interval(mins => %s))
                        ORDER BY r.id LIMIT %s""",
                    (RESEARCH_HEARTBEAT_LAPSE_MIN, RESEARCH_ABANDON_MIN, DB_BATCH_LIMIT))
        stale = cur.fetchall()
        if dry or not stale:
            return len(stale)
        cur.execute("""WITH candidates AS MATERIALIZED (
                           SELECT id FROM research_runs
                            WHERE id=ANY(%s) AND status='running' AND finished_at IS NULL
                            ORDER BY id FOR UPDATE SKIP LOCKED LIMIT %s
                       )
                       UPDATE research_runs r SET status='failed',finished_at=now()
                         FROM candidates c WHERE r.id=c.id RETURNING r.id""",
                    ([r[0] for r in stale], DB_BATCH_LIMIT))
        changed = [r[0] for r in cur.fetchall()]
    if changed:
        _append_audit("ResearchAbandoned", "research_runs", "failed",
                      {"ids": changed, "threads": [r[1] for r in stale if r[0] in changed],
                       "after_min": RESEARCH_ABANDON_MIN,
                       "reason": "worker gone; run never reached a terminal state"})
    return len(changed)


def _sweep_directory(dry=False, stale_min=None):
    """Release STALE agent presence in the directory: an 'active' entry whose updated_at is older than the
    threshold is a dead agent that never released (a finished build, a crashed worker). Left alone they
    accumulate (we found 66) and MISLEAD orchestrate.request_collaborator into reusing a non-existent agent
    instead of hiring — a correctness bug at scale, not just test cruft. Real live agents update far more
    often than this floor, so releasing stale presence is safe. Returns the bounded count released."""
    mins = stale_min if stale_min is not None else _bounded_int("AOS_DIRECTORY_STALE_MIN", 30, 5, 10080)
    with connection() as c, c.cursor() as cur:
        _set_db_timeouts(cur)
        if dry:
            cur.execute("""SELECT agent_id FROM directory
                            WHERE status='active' AND updated_at<now()-make_interval(mins=>%s)
                            ORDER BY updated_at,agent_id LIMIT %s""", (mins, DB_BATCH_LIMIT))
        else:
            cur.execute("""WITH candidates AS MATERIALIZED (
                               SELECT agent_id FROM directory
                                WHERE status='active' AND updated_at<now()-make_interval(mins=>%s)
                                ORDER BY updated_at,agent_id
                                FOR UPDATE SKIP LOCKED LIMIT %s
                           )
                           UPDATE directory d SET status='released'
                             FROM candidates c WHERE d.agent_id=c.agent_id RETURNING d.agent_id""",
                        (mins, DB_BATCH_LIMIT))
        return len(cur.fetchall())


def _bounded_tree_inventory(root, limit=SCRATCH_TREE_LIMIT):
    """Inventory a scratch tree without following symlinks; refuse deletion if the cap is exceeded."""
    stack, entries = [Path(root)], []
    while stack:
        current = stack.pop()
        try:
            children = os.scandir(current)
        except NotADirectoryError:
            continue
        with children:
            for child in children:
                entries.append(Path(child.path))
                if len(entries) > int(limit):
                    return None
                if child.is_dir(follow_symlinks=False):
                    stack.append(Path(child.path))
    return entries


def _remove_bounded_tree(root, limit=SCRATCH_TREE_LIMIT):
    entries = _bounded_tree_inventory(root, limit=limit)
    if entries is None:
        raise RuntimeError(f"scratch tree exceeds {limit} entries")
    for path in sorted(entries, key=lambda p: len(p.parts), reverse=True):
        if path.is_dir() and not path.is_symlink():
            path.rmdir()
        else:
            path.unlink(missing_ok=True)
    Path(root).rmdir()


def _clean_scratch(dry=False, tmp=Path("/tmp")):
    now = time.time()
    removed, errors = [], []
    scanned = 0
    truncated = False
    candidates = []
    try:
        entries = os.scandir(tmp)
    except OSError as exc:
        raise RuntimeError(f"scratch root unavailable: {exc}") from exc
    with entries:
        for entry in entries:
            scanned += 1
            if scanned > SCRATCH_VISIT_LIMIT:
                truncated = True
                break
            if not any(fnmatch.fnmatch(entry.name, pattern) for pattern in SCRATCH_GLOBS):
                continue
            path = Path(entry.path)
            try:
                candidates.append((path.lstat().st_mtime, path))
            except Exception as exc:
                errors.append({"path": path.name, "error": str(exc)[:200]})
    # Oldest first ensures a bounded cleanup pass always makes progress even when a large
    # stream of young matching scratch is continually added.
    candidates.sort(key=lambda item: (item[0], item[1].name))
    deferred = max(0, len(candidates) - SCRATCH_SCAN_LIMIT)
    for mtime, path in candidates[:SCRATCH_SCAN_LIMIT]:
        try:
            if now - mtime <= SCRATCH_MAX_AGE_S:
                continue
            if not dry:
                if path.is_dir() and not path.is_symlink():
                    _remove_bounded_tree(path)
                else:
                    path.unlink(missing_ok=True)
            removed.append(path.name)
        except Exception as exc:
            errors.append({"path": path.name, "error": str(exc)[:200]})
    return {"removed": removed, "errors": errors, "truncated": truncated,
            "scanned": min(scanned, SCRATCH_VISIT_LIMIT), "visit_limit": SCRATCH_VISIT_LIMIT,
            "matched": len(candidates), "cleanup_limit": SCRATCH_SCAN_LIMIT,
            "deferred": deferred}


def _selftest():
    keep = _reap_reason(ppid=12345, etimes=60) is None                      # healthy, parented -> keep
    orphan = _reap_reason(ppid=1, etimes=60) == "orphaned (parent dead)"    # PPID 1 -> reap
    stuck = _reap_reason(ppid=999, etimes=99999) is not None                # too old -> reap
    interactive_excluded = not any("claude --continue" in a for _, _, _, a in _agent_procs())  # never our session
    # directory-staleness sweep is wired + released count is an int (dry-run against the live DB)
    dir_ok = isinstance(_sweep_directory(dry=True), int) and "stale_directory_released" in reap(dry=True)
    # Browser name matches are observable only. Exact registered BrowserBridge trees own all destructive cleanup.
    browser_status = reap(dry=True)
    browser_wired = (isinstance(_sweep_browsers(dry=True), int)
                     and "owned_browser_trees_reaped" in browser_status
                     and "unowned_browsers_observed" in browser_status
                     and browser_status["stale_browsers_reaped"] == 0)
    browser_safe = _reap_browser_decision(ppid=12345, etimes=60) is False \
        and _reap_browser_decision(ppid=1, etimes=60) is True \
        and _reap_browser_decision(ppid=999, etimes=BROWSER_STALE_S + 1) is True
    # stale-run + stuck-build sweeps are wired into reap() (crashed runs; builds looping w/o outcome)
    runs_wired = "stale_runs_abandoned" in reap(dry=True)
    leases_wired = ("terminal_step_claims_released" in reap(dry=True)
                    and isinstance(_sweep_terminal_step_claims(dry=True), int))
    event_claims_wired = ("terminal_event_claims_released" in reap(dry=True)
                          and isinstance(_sweep_terminal_event_claims(dry=True), int))
    builds_wired = "stuck_builds_abandoned" in reap(dry=True) and isinstance(_sweep_stuck_builds(dry=True), int)
    ok = (keep and orphan and stuck and interactive_excluded and dir_ok and browser_wired
          and browser_safe and runs_wired and leases_wired and event_claims_wired and builds_wired)
    print(f"keep-healthy={keep} reap-orphan={orphan} reap-stuck={stuck} session-excluded={interactive_excluded} "
          f"directory-sweep={dir_ok} browser-sweep={browser_wired} browser-safe={browser_safe} "
          f"stale-run-sweep={runs_wired} terminal-lease-sweep={leases_wired} "
          f"terminal-event-claim-sweep={event_claims_wired} "
          f"stuck-build-sweep={builds_wired}")
    print("PASS: orphan/stuck reaper (decision + session-safe) ✅" if ok else "FAIL")
    sys.exit(0 if ok else 1)


def _main(a):
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "run":
        import json
        result = reap(dry=False)
        print(json.dumps(result, indent=2))
        return 2 if result.get("status") != "ok" else 0
    elif a[0] == "status":
        import json
        result = reap(dry=True)
        print(json.dumps(result, indent=2))
        return 2 if result.get("status") != "ok" else 0
    else:
        print("usage: reap.py run | status | selftest", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
