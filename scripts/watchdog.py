#!/usr/bin/env python3
"""watchdog.py — constant health checks + proactive paging for the whole fleet.

Pull-visibility (the dashboard) tells you what's happening when you look. This is the PUSH side: it
runs on a short loop and pings your phone the moment something needs you — a daemon died, a build went
silent/stalled, a component is down, a wait blew its SLA, a deadlock formed, disk/backup pressure, or a
spike in policy denials. It dedupes with a cooldown so you get one ping per incident, plus a "recovered"
ping when it clears.

Liveness: long-running loops call beat(<component>) each cycle; the watchdog flags stale heartbeats.

    watchdog.py tick               # one pass: detect issues, page on new ones, mark recoveries
    watchdog.py check              # print current issues, don't page
    watchdog.py beat <component>   # record a liveness heartbeat (called by the loops)
    watchdog.py selftest
Run with the agent-os venv python.
"""
import json
import math
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import incident   # noqa: E402
import notify     # noqa: E402
import responder  # noqa: E402
import service_recovery  # noqa: E402

from aoscfg import ENV, DB
from dbpool import connection  # noqa: E402

# daemons recover.sh keeps alive — if one is missing, that's an incident
EXPECTED = dict(responder.DAEMONS)
STALL_MIN = 8          # a running factory build with no audit activity for this long = stalled
COOLDOWN_S = 1800      # re-ping an unresolved issue at most every 30 min
PRIO = {"crit": "urgent", "warn": "high"}
_ensured = False
_ensure_lock = threading.Lock()


def _finite_number(name, default, *, minimum, maximum):
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = float(default)
    if not math.isfinite(value):
        value = float(default)
    return min(maximum, max(minimum, value))


DB_LOCK_TIMEOUT_MS = int(_finite_number(
    "AOS_WATCHDOG_DB_LOCK_TIMEOUT_MS", 500, minimum=50, maximum=5000))
DB_STATEMENT_TIMEOUT_MS = int(_finite_number(
    "AOS_WATCHDOG_DB_STATEMENT_TIMEOUT_MS", 5000, minimum=250, maximum=15000))
INCIDENT_TIMEOUT_S = int(_finite_number(
    "AOS_WATCHDOG_INCIDENT_TIMEOUT_S", 25, minimum=1, maximum=35))


def _bounded_transaction(cur, *, schema=False):
    """Bound every watchdog transaction, especially runtime DDL behind a concurrent migration.

    ``set_config(..., true)`` is transaction-local and therefore safe with pooled connections. Schema setup
    gets the same finite statement ceiling as ordinary reads/writes; on contention it fails promptly and the
    process-cached ensure retries on the next tick instead of freezing the recurring control path.
    """
    statement_ms = min(DB_STATEMENT_TIMEOUT_MS, 3000) if schema else DB_STATEMENT_TIMEOUT_MS
    cur.execute("SELECT set_config('lock_timeout', %s, true)", (f"{DB_LOCK_TIMEOUT_MS}ms",))
    cur.execute("SELECT set_config('statement_timeout', %s, true)", (f"{statement_ms}ms",))


def _ensure():
    global _ensured
    if _ensured:
        return
    with _ensure_lock:
        if _ensured:
            return
        with connection() as c, c.cursor() as cur:
            _bounded_transaction(cur, schema=True)
            cur.execute("""CREATE TABLE IF NOT EXISTS heartbeats (component TEXT PRIMARY KEY,
                           ts TIMESTAMPTZ NOT NULL DEFAULT now(), meta JSONB NOT NULL DEFAULT '{}')""")
            cur.execute("""CREATE TABLE IF NOT EXISTS watchdog_alerts (signature TEXT PRIMARY KEY,
                           level TEXT NOT NULL, first_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
                           last_sent TIMESTAMPTZ)""")
            cur.execute("ALTER TABLE watchdog_alerts ADD COLUMN IF NOT EXISTS fix_attempts INT NOT NULL DEFAULT 0")
            cur.execute("ALTER TABLE watchdog_alerts ADD COLUMN IF NOT EXISTS last_attempt TIMESTAMPTZ")
            cur.execute("ALTER TABLE watchdog_alerts ADD COLUMN IF NOT EXISTS delivery_status TEXT")
        _ensured = True


def beat(component, meta=None):
    _ensure()
    with connection() as c, c.cursor() as cur:
        _bounded_transaction(cur)
        cur.execute("""INSERT INTO heartbeats (component, ts, meta) VALUES (%s, now(), %s)
                       ON CONFLICT (component) DO UPDATE SET ts=now(), meta=EXCLUDED.meta""",
                    (component, json.dumps(meta or {})))


# THE DB-OUTAGE BLIND SPOT: every detector, the dedup table, and self-heal all live in Postgres. If Postgres
# itself is down (container died / OS shutdown — exactly the North-Star failure list), a normal tick() throws
# before it can page — the ONE failure that blinds the whole plane would also MUTE the pager. So tick() probes
# the DB first (DB-free) and, if it's gone, pages out-of-band via ntfy (which needs no DB), file-deduped
# because our watchdog_alerts dedup lives in the very DB that's unreachable.
_DB_DOWN_MARK = Path("/tmp/agentos-watchdog-dbdown.mark")


def _db_reachable():
    try:
        with psycopg.connect(DB, connect_timeout=3) as c, c.cursor() as cur:
            cur.execute("SELECT 1"); cur.fetchone()
        return True
    except Exception:
        return False


def _page_db_down():
    """One urgent page per COOLDOWN_S while Postgres is unreachable (file-deduped)."""
    try:
        fresh = (not _DB_DOWN_MARK.exists()) or (time.time() - _DB_DOWN_MARK.stat().st_mtime > COOLDOWN_S)
    except Exception:
        fresh = True
    if fresh:
        accepted = notify.send(
            "■ Postgres is UNREACHABLE — the whole control plane (builds, QA, pulse, watchdog dedup) "
            "is blind until it is back. Check the postgres container.",
            title="agent-os watchdog", priority="urgent", tags="rotating_light")
        # The file is a SENT cooldown, not an attempt cooldown. If ntfy rejected/unavailable, leave it absent
        # so the next watchdog tick retries instead of muting the only DB-independent alarm for 30 minutes.
        if accepted:
            try:
                _DB_DOWN_MARK.touch()
            except Exception:
                pass


def _pgrep(pat):
    """Count matching processes without mistaking this monitor's parent shell command for the workload.

    `pgrep -f` searches an entire command line. Automation wrappers commonly contain the literal probe text
    (for example `watchdog.py check; pgrep ... 'factory.py build'`), which used to manufacture a fake live
    build and page on old audit rows. Ancestors are orchestration, never child workloads, so exclude them.
    """
    ancestors = set()
    pid = os.getpid()
    while pid > 1 and pid not in ancestors:
        ancestors.add(pid)
        try:
            pid = int((Path("/proc") / str(pid) / "stat").read_text().split()[3])
        except Exception:
            break
    try:
        ps = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True, text=True, timeout=10)
        count = 0
        for line in ps.stdout.splitlines():
            fields = line.strip().split(None, 1)
            if len(fields) == 2 and int(fields[0]) not in ancestors and pat in fields[1]:
                count += 1
        return count
    except Exception:
        return 0


def _pressure_issues(mem_available_kb, mem_total_kb, dev_servers, browser_roots,
                     mem_warn_pct=15.0, mem_crit_pct=8.0, dev_warn=8, browser_warn=4,
                     swap_used_kb=0, swap_total_kb=0, host_pids=0, pid_warn=4096,
                     memory_psi_avg10=0.0, cpu_psi_avg10=0.0):
    """Pure host-pressure policy, separated for deterministic tests."""
    out = []
    pct = (100.0 * mem_available_kb / mem_total_kb) if mem_total_kb else 100.0
    if pct < mem_crit_pct:
        out.append({"sig": "host:memory", "level": "crit",
                    "msg": f"host memory critically low: {pct:.1f}% available"})
    elif pct < mem_warn_pct:
        out.append({"sig": "host:memory", "level": "warn",
                    "msg": f"host memory pressure: {pct:.1f}% available"})
    if dev_servers > dev_warn:
        out.append({"sig": "host:devservers", "level": "warn",
                    "msg": f"{dev_servers} generated dev servers alive (safe ceiling {dev_warn})"})
    if browser_roots > browser_warn:
        out.append({"sig": "host:browsers", "level": "crit",
                    "msg": f"{browser_roots} Chromium/ffmpeg QA roots alive (safe ceiling {browser_warn})"})
    swap_pct = (100.0 * swap_used_kb / swap_total_kb) if swap_total_kb else 0.0
    if pct < 25 and swap_pct >= 50:
        out.append({"sig": "host:swap", "level": "crit" if swap_pct >= 80 else "warn",
                    "msg": f"host swap pressure: {swap_pct:.1f}% used with only {pct:.1f}% RAM available"})
    if int(host_pids or 0) >= int(pid_warn):
        out.append({"sig": "host:pids", "level": "warn",
                    "msg": f"host process/thread pressure: {int(host_pids)} tasks (warning {int(pid_warn)})"})
    if float(memory_psi_avg10 or 0) >= 5:
        out.append({"sig": "host:memory-psi", "level": "crit" if memory_psi_avg10 >= 20 else "warn",
                    "msg": f"sustained memory stalls: PSI some avg10={float(memory_psi_avg10):.1f}%"})
    if float(cpu_psi_avg10 or 0) >= 50:
        out.append({"sig": "host:cpu-psi", "level": "warn",
                    "msg": f"sustained CPU contention: PSI some avg10={float(cpu_psi_avg10):.1f}%"})
    return out


def _db_connection_issues(used, maximum, reserved=3, waiting=0, warn_pct=75.0, crit_pct=90.0):
    """Pure connection-pressure policy; reserve is removed from application capacity."""
    capacity = max(1, int(maximum or 0) - max(0, int(reserved or 0)))
    pct = 100.0 * max(0, int(used or 0)) / capacity
    issues = []
    if pct >= crit_pct:
        issues.append({"sig": "postgres:connections", "level": "crit",
                       "msg": f"Postgres connection capacity critical: {used}/{capacity} ({pct:.1f}%)"})
    elif pct >= warn_pct:
        issues.append({"sig": "postgres:connections", "level": "warn",
                       "msg": f"Postgres connection pressure: {used}/{capacity} ({pct:.1f}%)"})
    if int(waiting or 0) > 0:
        issues.append({"sig": "postgres:connection-waits", "level": "warn",
                       "msg": f"{int(waiting)} Postgres session(s) waiting on Client/connection capacity"})
    return issues


def _host_pressure():
    """Read-only WSL/Linux pressure snapshot. Fail-soft so monitoring never harms the control plane."""
    try:
        vals = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith(("MemTotal:", "MemAvailable:", "SwapTotal:", "SwapFree:")):
                key, value, *_ = line.split()
                vals[key.rstrip(":")] = int(value)
        ps_result = subprocess.run(["ps", "-eo", "args="], capture_output=True, text=True, timeout=10)
        if ps_result.returncode != 0:
            raise RuntimeError(f"ps exited {ps_result.returncode}")
        dev = sum("/agent-os/.venv/bin/python -m src." in line for line in ps_result.stdout.splitlines())
        browsers = sum((("chrome-headless-shell" in line or "/chromium" in line or "ffmpeg" in line)
                        and "--type=" not in line) for line in ps_result.stdout.splitlines())
        def psi(kind):
            try:
                some = next(x for x in Path(f"/proc/pressure/{kind}").read_text().splitlines()
                            if x.startswith("some "))
                return float(next(x.split("=", 1)[1] for x in some.split() if x.startswith("avg10=")))
            except Exception:
                return 0.0
        try:
            load_tasks = int(Path("/proc/loadavg").read_text().split()[3].split("/")[1])
        except Exception:
            load_tasks = 0
        return _pressure_issues(vals.get("MemAvailable", 0), vals.get("MemTotal", 0), dev, browsers,
                                mem_warn_pct=float(os.environ.get("AOS_WATCHDOG_MEM_WARN_PCT", "15")),
                                mem_crit_pct=float(os.environ.get("AOS_WATCHDOG_MEM_CRIT_PCT", "8")),
                                dev_warn=int(os.environ.get("AOS_WATCHDOG_DEVSERVE_WARN", "8")),
                                browser_warn=int(os.environ.get("AOS_WATCHDOG_BROWSER_WARN", "4")),
                                swap_used_kb=max(0, vals.get("SwapTotal", 0)-vals.get("SwapFree", 0)),
                                swap_total_kb=vals.get("SwapTotal", 0), host_pids=load_tasks,
                                pid_warn=int(os.environ.get("AOS_WATCHDOG_PID_WARN", "4096")),
                                memory_psi_avg10=psi("memory"), cpu_psi_avg10=psi("cpu"))
    except Exception as exc:
        return [{"sig": "watchdog:host-pressure-read", "level": "warn",
                 "msg": f"host pressure check failed: {str(exc)[:120]}"}]


def check():
    """Return a list of current issues: {sig, level, msg}. Pure detection, no paging."""
    issues = []
    issues.extend(_host_pressure())
    try:
        _ensure()
    except Exception as e:
        issues.append({"sig": "watchdog:schema", "level": "warn",
                       "msg": f"watchdog schema unavailable or lock-contended: {str(e)[:120]}"})
    try:
        with connection() as c, c.cursor() as cur:
            _bounded_transaction(cur)
            cur.execute("""SELECT count(*),current_setting('max_connections')::int,
                                  current_setting('superuser_reserved_connections')::int
                           FROM pg_stat_activity""")
            import dbpool
            pool_waiting = int(dbpool.stats().get("requests_waiting") or 0)
            issues.extend(_db_connection_issues(*cur.fetchone(), waiting=pool_waiting,
                warn_pct=float(os.environ.get("AOS_WATCHDOG_DB_CONN_WARN_PCT", "75")),
                crit_pct=float(os.environ.get("AOS_WATCHDOG_DB_CONN_CRIT_PCT", "90"))))
    except Exception as e:
        issues.append({"sig": "postgres:connection-pressure-unavailable", "level": "warn",
                       "msg": f"Postgres connection-pressure check failed: {str(e)[:120]}"})
    # 1) reuse the dashboard's derived alerts (health/disk/backup/deadlock/overdue-waits/denials)
    try:
        import contextlib
        import io
        import dashboard
        with contextlib.redirect_stdout(io.StringIO()):
            st = dashboard.state()
        for a in st.get("alerts", []):
            if a["level"] in ("crit", "warn"):
                issues.append({"sig": "alert:" + a["msg"][:40], "level": a["level"], "msg": a["msg"]})
    except Exception as e:
        issues.append({"sig": "watchdog:self", "level": "warn", "msg": f"state read failed: {e}"})
    # 2) expected daemons that died
    for name, service in EXPECTED.items():
        try:
            service_state = service_recovery.status(service)
        except Exception as exc:
            issues.append({"sig": f"daemon:{name}", "level": "crit",
                           "msg": f"{name} ownership/readiness check failed: {str(exc)[:120]}"})
            continue
        if service_state.get("state") != "healthy":
            issues.append({"sig": f"daemon:{name}", "level": "crit",
                           "msg": f"{name} is not exact-and-ready ({service_state.get('state')})"})
    # 3) a factory build that's running but has gone silent (stalled mid-build)
    if _pgrep("factory.py build") > 0:
        try:
            with connection() as c, c.cursor() as cur:
                _bounded_transaction(cur)
                cur.execute("""SELECT resource, EXTRACT(EPOCH FROM now()-max(ts))/60
                               FROM audit_log WHERE actor LIKE 'factory:%%' AND ts > now()-interval '1 hour'
                                 AND COALESCE(payload->>'_selftest','false') <> 'true'
                               GROUP BY resource""")
                rows = cur.fetchall()
            for res, idle_min in rows:
                if idle_min and idle_min > STALL_MIN:
                    issues.append({"sig": f"stall:{res}", "level": "warn",
                                   "msg": f"build '{res}' silent {round(idle_min)}m — possible stall"})
        except Exception as e:
            issues.append({"sig": "watchdog:build-stall-read", "level": "warn",
                           "msg": f"build stall check failed: {str(e)[:120]}"})
    # 3b) SILENT failures (sentinel): hung agent work, dead workflows, provider 529-storms — plus its
    # proactive side-channels (progress ping while long work runs healthy, boot-recovery notice). A broken
    # sentinel must never take the watchdog down with it.
    try:
        import sentinel
        issues.extend(sentinel.observe())
    except Exception as e:
        issues.append({"sig": "sentinel:self", "level": "warn", "msg": f"sentinel observe failed: {e}"})
    # 4) stale heartbeats (a loop that should be beating went quiet)
    try:
        with connection() as c, c.cursor() as cur:
            _bounded_transaction(cur)
            cur.execute("SELECT component, EXTRACT(EPOCH FROM now()-ts) FROM heartbeats")
            rows = cur.fetchall()
        for comp, age in rows:
            if comp.startswith("selftest"):
                continue                      # one-off probes never beat again
            if age and age > 1800:            # >30 min (ticker beats every 15m; a dead loop is caught by pgrep too)
                issues.append({"sig": f"heartbeat:{comp}", "level": "warn",
                               "msg": f"{comp} heartbeat stale ({round(age/60)}m)"})
    except Exception as e:
        issues.append({"sig": "watchdog:heartbeat-read", "level": "warn",
                       "msg": f"heartbeat check failed: {str(e)[:120]}"})
    # 5) IN-FLIGHT AGENTIC WORK gone silent (the pulse plane): a QA run / build / fleet task that stopped
    # beating past its promised cadence — the North Star's "silence is itself a failure signal". This is the
    # gap the old watchdog couldn't see: agentic work in progress, not just daemons. sweep() marks them so
    # the live view agrees; each becomes an incident (deduped by watchdog_alerts like everything else).
    try:
        import pulse
        pulse.reap_orphans()   # first reconcile dead-process ghosts to terminal (reboot/crash) so we don't
        pulse.sweep()          # page forever on work that no longer exists; then flag the genuinely-silent live ones
        for w in pulse.stalled():
            issues.append({"sig": f"pulse:{w['work_id']}", "level": "warn",
                           "msg": f"{w['kind']} '{w.get('label') or w['work_id']}' silent "
                                  f"{round(w['beat_age_s'] / 60)}m at stage '{w.get('stage') or '?'}' "
                                  f"({w.get('progress') or 'no progress reported'})"})
    except Exception as e:
        issues.append({"sig": "pulse:self", "level": "warn", "msg": f"pulse check failed: {e}"})
    return issues


def _known_alerts():
    """One short MVCC read; callers perform no network/model/remediation work until it is closed."""
    with connection() as c, c.cursor() as cur:
        _bounded_transaction(cur)
        cur.execute("SELECT signature, last_sent, fix_attempts FROM watchdog_alerts")
        return {signature: (last_sent, fix_attempts)
                for signature, last_sent, fix_attempts in cur.fetchall()}


def _record_alert(issue, *, accepted, fix_attempted=False, delivery_status=None):
    """Persist one observed attempt in its own bounded transaction."""
    status = delivery_status or ("accepted" if accepted else "failed")
    with connection() as c, c.cursor() as cur:
        _bounded_transaction(cur)
        cur.execute("""INSERT INTO watchdog_alerts
                         (signature,level,last_sent,last_attempt,delivery_status,fix_attempts)
                       VALUES (%s,%s,CASE WHEN %s THEN now() END,now(),%s,%s)
                       ON CONFLICT (signature) DO UPDATE SET level=EXCLUDED.level,
                         last_sent=CASE WHEN %s THEN now() ELSE watchdog_alerts.last_sent END,
                         last_attempt=now(), delivery_status=%s,
                         fix_attempts=watchdog_alerts.fix_attempts+%s""",
                    (issue["sig"], issue["level"], bool(accepted), status,
                     1 if fix_attempted else 0, bool(accepted), status,
                     1 if fix_attempted else 0))


def _forget_alert(signature):
    with connection() as c, c.cursor() as cur:
        _bounded_transaction(cur)
        cur.execute("DELETE FROM watchdog_alerts WHERE signature=%s", (signature,))


def _record_recovery_failure(signature):
    with connection() as c, c.cursor() as cur:
        _bounded_transaction(cur)
        cur.execute("""UPDATE watchdog_alerts SET last_attempt=now(),delivery_status='recovery_failed'
                       WHERE signature=%s""", (signature,))


def _safe_notify(*args, **kwargs):
    """Transport exceptions are failed attempts, never acknowledged delivery."""
    try:
        return bool(notify.send(*args, **kwargs))
    except Exception:
        return False


def tick(auto_heal=True):
    """Detect → try a bounded auto-fix (responder) → page only what can't be auto-fixed or needs
    judgement. Recoveries announced for things a human was paged about."""
    if not _db_reachable():                # DB-OUTAGE GUARD: page out-of-band before any DB touch (see above),
        _page_db_down()                    # so the failure that blinds the plane can't also mute the pager
        return {"issues": 1, "healed": [], "paged": ["postgres unreachable"], "recovered": 0, "db_down": True}
    if _DB_DOWN_MARK.exists():             # Postgres just came back — announce recovery and clear the marker
        try:
            notify.send("✓ recovered: Postgres reachable again", title="agent-os watchdog", tags="white_check_mark")
            _DB_DOWN_MARK.unlink()
        except Exception:
            pass
    try:
        _ensure()
        beat("watchdog")  # liveness proof is committed before any external callback
    except Exception as exc:
        _safe_notify(f"■ watchdog database path is degraded: {str(exc)[:180]}",
                     title="agent-os watchdog", priority="urgent", tags="rotating_light")
        return {"issues": 1, "healed": [], "paged": [], "delivery_failed": ["watchdog database path"],
                "recovered": 0, "degraded": str(exc)[:240]}
    issues = check()
    now_sigs = {i["sig"] for i in issues}
    paged, healed, delivery_failed, recovery_delivery_failed = [], [], [], []
    degraded = []
    try:
        known = _known_alerts()
    except Exception as exc:
        _safe_notify(f"■ watchdog alert ledger is unavailable: {str(exc)[:180]}",
                     title="agent-os watchdog", priority="urgent", tags="rotating_light")
        return {"issues": len(issues) or 1, "healed": [], "paged": [],
                "delivery_failed": ["watchdog alert ledger"], "recovered": 0,
                "degraded": str(exc)[:240]}

    # Every remediation, model investigation and HTTP notification runs after the known-alert read committed.
    for i in issues:
        sig = i["sig"]
        ls, fa = known.get(sig, (None, 0))
        try:
            rem = responder.remediate(i) if (auto_heal and fa < 3) else None
        except Exception as exc:
            rem = {"ok": False, "action": f"responder failed safely: {str(exc)[:160]}"}
        if rem is not None:
            if rem["ok"]:
                accepted = _safe_notify(f"🛠 auto-healed: {i['msg']} — {rem['action']}",
                                        title="agent-os watchdog", tags="wrench")
                healed.append(i["msg"])
                if not accepted:
                    delivery_failed.append(i["msg"])
            else:
                accepted = _safe_notify(
                    f"■ auto-fix FAILED: {i['msg']} ({rem['action']}) — needs you",
                    title="agent-os watchdog", priority="urgent", tags="rotating_light")
                (paged if accepted else delivery_failed).append(i["msg"])
            try:
                _record_alert(i, accepted=accepted, fix_attempted=True)
            except Exception as exc:
                degraded.append(f"alert write {sig}: {str(exc)[:120]}")
            continue

        fresh = ls is None
        cooled = ls is not None and (time.time() - ls.timestamp()) > COOLDOWN_S
        if not (fresh or cooled):
            continue
        rca_note = ""
        if fresh and i["level"] == "crit" and responder.classify(i) == "unknown":
            try:
                rca_note = " · RCA: " + incident.investigate(i, timeout=INCIDENT_TIMEOUT_S)["summary"]
            except Exception:
                pass
        extra = " (auto-fix gave up — flapping)" if fa >= 3 else ""
        accepted = _safe_notify(
            f"{'■' if i['level']=='crit' else '▲'} {i['msg']}{extra}{rca_note}",
            title="agent-os watchdog", priority=PRIO[i["level"]], tags="rotating_light")
        (paged if accepted else delivery_failed).append(i["msg"])
        try:
            _record_alert(i, accepted=accepted, fix_attempted=False)
        except Exception as exc:
            degraded.append(f"alert write {sig}: {str(exc)[:120]}")

    recovered = [signature for signature in known if signature not in now_sigs]
    for signature in recovered:
        ls, fa = known[signature]
        if ls is not None and fa == 0:
            accepted = _safe_notify(f"✓ recovered: {signature}", title="agent-os watchdog",
                                    tags="white_check_mark")
            try:
                if accepted:
                    _forget_alert(signature)
                else:
                    _record_recovery_failure(signature)
                    recovery_delivery_failed.append(signature)
            except Exception as exc:
                degraded.append(f"recovery write {signature}: {str(exc)[:120]}")
        else:
            try:
                _forget_alert(signature)
            except Exception as exc:
                degraded.append(f"recovery delete {signature}: {str(exc)[:120]}")
    try:
        beat("watchdog")
    except Exception as exc:
        degraded.append(f"heartbeat write: {str(exc)[:120]}")
    return {"issues": len(issues), "healed": healed, "paged": paged,
            "delivery_failed": delivery_failed, "recovered": len(recovered),
            **({"recovery_delivery_failed": recovery_delivery_failed}
               if recovery_delivery_failed else {}),
            **({"degraded": degraded} if degraded else {})}


def _main(a):
    if not a or a[0] == "tick":
        print(tick())
    elif a[0] == "check":
        for i in check():
            print(f"  [{i['level']}] {i['msg']}")
    elif a[0] == "beat":
        beat(a[1] if len(a) > 1 else "manual"); print("beat ok")
    elif a[0] == "selftest":
        beat("selftest-probe")
        iss = check()
        ok = isinstance(iss, list)
        # clean up the probe row — a lingering 'selftest-probe' heartbeat would itself become a permanent
        # stale-heartbeat false alarm (exactly what we just had to reconcile by hand).
        try:
            with connection() as c, c.cursor() as cur:
                _bounded_transaction(cur)
                cur.execute("DELETE FROM heartbeats WHERE component='selftest-probe'")
        except Exception:
            pass
        print(f"watchdog check returned {len(iss)} issue(s); heartbeat write ok")
        print("PASS: watchdog detect + heartbeat ✅" if ok else "FAIL")
        sys.exit(0 if ok else 1)
    else:
        sys.exit("usage: watchdog.py tick|check|beat|selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
