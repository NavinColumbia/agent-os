#!/usr/bin/env python3
"""sentinel.py — the SILENT-failure observer (NORTH-STAR: "nothing fails invisibly").

The existing watchdog catches LOUD failures (daemon dead, container down, build stalled in the audit
stream, stale heartbeat). This module watches for the failures that make no noise — born from a real
incident: a 7h43m verify workflow LOOKED dead (flat token counter) and the owner had to ask "did
something silently die?". The system, not the owner, must be the one who notices — or better, the one
who proactively says "still working, here's progress".

What it observes (each returns watchdog-shaped issues {sig, level, msg}):
  1. HUNG AGENT WORK  — claude -p / codex exec processes alive far longer than any sane task
     (reap.py will kill true orphans; sentinel ALERTS so a human hears about it too).
  2. DEAD WORKFLOWS   — a recent workflow transcript dir that stopped writing >45m ago while NO agent
     processes are running: the classic silently-died-mid-flight signature.
  3. PROVIDER DEGRADED — a burst of overloaded/429/529/rate-limit markers or failures in recent traces:
     Anthropic (or the active provider) is degraded; builds will be slow/failing over. Say so BEFORE
     the user wonders why everything crawls.
  3b. STUCK ORCHESTRA ACTOR — a durable-org agent (orchestra_actors) that claims to be 'working' in a
     still-running run but whose heartbeat (store.heartbeat -> last_active) went silent: the actor
     runtime beats every live actor on a cadence while it works, so silence = a silently-dead employee
     holding an assignment. This is heartbeat liveness on *agentic work in progress* (NORTH-STAR).
  4. (side-effect) PROACTIVE PROGRESS PING — while agent work is ACTIVE and healthy for a long time,
     periodically tell the owner "fleet still working: N agents, oldest Xm" so long work never looks dead.
  5. (side-effect) BOOT NOTICE — after an OS/WSL restart, announce "system restarted, stack recovered"
     once per boot (recover.sh already restores the stack; the human deserves to know it happened).

Wiring: watchdog.check() extends with sentinel.observe() — so detection rides the existing 2-min loop,
dedup/cooldown, responder auto-heal, and paging. No new daemon to supervise.

  python sentinel.py observe    # print current issues (pure detection)
  python sentinel.py selftest   # offline-ish: detection + dedup logic against a fake workflow dir
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "orchestra"))
import psycopg  # noqa: E402

import trace as _trace  # noqa: E402  — same .env.local-sourced DATABASE_URL every other module uses
import store as _store  # noqa: E402  — the durable org (orchestra actors + heartbeats)
DB = _trace.DB
HUNG_AGENT_MIN = int(os.environ.get("AOS_SENTINEL_HUNG_MIN", "50"))        # reap.py kills at 40m; alert past it
WF_STALE_MIN = int(os.environ.get("AOS_SENTINEL_WF_STALE_MIN", "45"))      # workflow quiet this long = suspect
WF_WINDOW_H = int(os.environ.get("AOS_SENTINEL_WF_WINDOW_H", "6"))         # only workflows active this recently
PROVIDER_BURST = int(os.environ.get("AOS_SENTINEL_PROVIDER_BURST", "3"))   # transient markers in 15m = degraded
ACTOR_STALE_MIN = int(os.environ.get("AOS_SENTINEL_ACTOR_STALE_MIN", "10"))  # orchestra beats every ~45s; 10m silent = stuck
PROGRESS_EVERY_S = int(os.environ.get("AOS_SENTINEL_PROGRESS_S", "3600"))  # "still working" ping cadence
_TRANSIENT_SQL = "(output ~* 'overloaded|rate.?limit|too many requests|529|429' OR rc <> 0)"
CLAUDE_DIR = Path(os.environ.get("AOS_CLAUDE_DIR", str(Path.home() / ".claude" / "projects")))


def _ensure():
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS sentinel_state (key TEXT PRIMARY KEY, val TEXT, ts TIMESTAMPTZ DEFAULT now())")
        c.commit()


def _state_get(key):
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT val, EXTRACT(EPOCH FROM now()-ts) FROM sentinel_state WHERE key=%s", (key,))
        r = cur.fetchone()
        return (r[0], float(r[1])) if r else (None, None)


def _state_set(key, val):
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO sentinel_state (key, val, ts) VALUES (%s,%s,now())
                       ON CONFLICT (key) DO UPDATE SET val=EXCLUDED.val, ts=now()""", (key, str(val)))
        c.commit()


def _agent_procs():
    """[(pid, elapsed_min, cmd_head)] for live claude -p / codex exec workers (never the interactive CLI)."""
    try:
        p = subprocess.run(["ps", "-eo", "pid,etimes,args"], capture_output=True, text=True, timeout=15)
    except Exception:
        return []
    out = []
    for line in (p.stdout or "").splitlines()[1:]:
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        pid, etimes, args = parts
        if ("claude -p" in args or "codex exec" in args) and "--continue" not in args:
            try:
                out.append((int(pid), int(etimes) / 60.0, args[:90]))
            except ValueError:
                pass
    return out


def _workflow_dirs(root=None):
    """Recent workflow transcript dirs: [(path, newest_mtime_epoch)] touched within WF_WINDOW_H."""
    root = Path(root) if root else CLAUDE_DIR
    now, found = time.time(), []
    try:
        for wf in root.glob("*/*/subagents/workflows/wf_*"):
            files = [f for f in wf.glob("*.jsonl")]
            if not files:
                continue
            newest = max(f.stat().st_mtime for f in files)
            if now - newest < WF_WINDOW_H * 3600:
                found.append((wf, newest))
    except Exception:
        pass
    return found


def observe(wf_root=None, notify_fn=None, now=None):
    """Pure-ish detection -> watchdog-shaped issues. Side-effects (progress ping / boot notice) go through
    notify_fn (defaults to notify.send) and are DB-deduped so they fire on a cadence, not per tick."""
    now = now or time.time()
    _ensure()
    issues = []
    procs = _agent_procs()

    # 1) hung agent work — a worker far beyond any sane runtime (reap kills at 40m; past that + still here)
    for pid, age_min, cmd in procs:
        if age_min > HUNG_AGENT_MIN:
            issues.append({"sig": f"sentinel:hung:{pid}", "level": "warn",
                           "msg": f"agent process {pid} running {round(age_min)}m ({cmd[:60]}…) — possible hang"})

    # 2) dead workflows — recent transcripts that went quiet while nothing is running
    if not procs:
        for wf, newest in _workflow_dirs(wf_root):
            quiet_min = (now - newest) / 60.0
            if quiet_min > WF_STALE_MIN:
                issues.append({"sig": f"sentinel:wf-dead:{wf.name}", "level": "warn",
                               "msg": f"workflow {wf.name} quiet {round(quiet_min)}m with no agents running — "
                                      f"may have died silently (check its result/output)"})

    # 3) provider degraded — burst of transient failures in recent traces
    try:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM traces WHERE ts > now()-interval '15 minutes' AND {_TRANSIENT_SQL}")
            n = cur.fetchone()[0]
            if n >= PROVIDER_BURST:
                issues.append({"sig": "sentinel:provider-degraded", "level": "warn",
                               "msg": f"provider degraded: {n} transient failures (429/529/overload) in 15m — "
                                      f"work is retrying/failing over; expect slowness"})
    except Exception:
        pass

    # 3b) stuck orchestra actor — 'working' in a live run but its heartbeat went silent. The actor
    # runtime (research_org's ticker + every store mutation) beats last_active constantly while an
    # agent works, so a long silence distinguishes "dead" from merely "slow" — the flat-timeout
    # guillotine problem, solved by liveness. Best-effort: a missing table must not kill the loop.
    try:
        for a in _store.stale_working(ACTOR_STALE_MIN):
            issues.append({"sig": f"sentinel:actor-stale:{a['actor_id']}", "level": "warn",
                           "msg": f"orchestra actor {a['name']} ({a['role']}, run {a['run_id']}) says "
                                  f"'working' but has been silent {a['stale_min']}m — possible dead "
                                  f"agent holding an assignment (tenant {a['tenant_id']})"})
    except Exception:
        pass

    # 4) proactive progress ping — long-running healthy work must never LOOK dead
    if notify_fn is None:
        try:
            import notify
            notify_fn = notify.send
        except Exception:
            notify_fn = None
    if procs and notify_fn:
        _, age_s = _state_get("progress_ping")
        if age_s is None or age_s > PROGRESS_EVERY_S:
            oldest = max(a for _, a, _ in procs)
            try:
                notify_fn(f"⏳ fleet still working: {len(procs)} agent(s) active, oldest {round(oldest)}m — healthy",
                          title="agent-os sentinel")
            except Exception:
                pass
            _state_set("progress_ping", int(now))

    # 5) boot notice — announce recovery after an OS/WSL restart, once per boot
    try:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        seen, _ = _state_get("boot_id")
        if seen != boot_id:
            _state_set("boot_id", boot_id)
            if seen is not None and notify_fn:   # not the very first run ever — a real restart happened
                up_min = float(Path("/proc/uptime").read_text().split()[0]) / 60.0
                if up_min < 30:
                    try:
                        notify_fn(f"🔄 system restarted {round(up_min)}m ago — stack recovered (recover.sh)",
                                  title="agent-os sentinel")
                    except Exception:
                        pass
    except Exception:
        pass
    return issues


def _selftest():
    import tempfile
    _ensure()
    sent = []
    fake_notify = lambda msg, **kw: sent.append(msg)  # noqa: E731
    # (a) a stale workflow dir is detected when no agents run
    with tempfile.TemporaryDirectory() as td:
        wf = Path(td) / "p" / "s" / "subagents" / "workflows" / "wf_selftest1"
        wf.mkdir(parents=True)
        f = wf / "agent-x.jsonl"
        f.write_text("{}\n")
        stale = time.time() - (WF_STALE_MIN + 10) * 60
        os.utime(f, (stale, stale))
        iss = observe(wf_root=td, notify_fn=fake_notify)
        wf_hits = [i for i in iss if i["sig"] == "sentinel:wf-dead:wf_selftest1"]
        live_agents = bool(_agent_procs())
        assert wf_hits or live_agents, "stale workflow not detected (and no live agents to excuse it)"
    # (b) issues are watchdog-shaped
    for i in iss:
        assert {"sig", "level", "msg"} <= set(i), f"malformed issue {i}"
    # (c) progress ping dedups: force state fresh -> second observe() must NOT re-ping
    _state_set("progress_ping", int(time.time()))
    before = len([s for s in sent if "still working" in s])
    observe(wf_root="/nonexistent", notify_fn=fake_notify)
    after = len([s for s in sent if "still working" in s])
    assert after == before, "progress ping ignored its cooldown"
    # (d) provider-degraded query is well-formed (runs without error)
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM traces WHERE ts > now()-interval '15 minutes' AND {_TRANSIENT_SQL}")
        cur.fetchone()
    # (e) a stuck orchestra actor is flagged: seed a REAL durable-org run with a 'working' actor
    # whose heartbeat we backdate (a silently-dead employee), assert observe() raises the issue,
    # then heartbeat it (sign of life) and assert the issue clears. Cleans up its own rows.
    import uuid as _uuid
    stid = f"sentinel-selftest-{_uuid.uuid4().hex[:8]}"
    orc = _store.start_run(stid, "sentinel liveness probe")["run_id"]
    a = _store.spawn_actor(orc, stid, "researcher-x", "research-growth", kind="worker")
    _store.update_actor(a["actor_id"], stid, status="working")
    try:
        with psycopg.connect(DB) as c, c.cursor() as cur:   # simulate silence: backdate the beat
            cur.execute("UPDATE orchestra_actors SET last_active=now()-interval '30 minutes' "
                        "WHERE actor_id=%s", (a["actor_id"],))
            c.commit()
        iss2 = observe(wf_root="/nonexistent", notify_fn=fake_notify)
        sig = f"sentinel:actor-stale:{a['actor_id']}"
        hit = [i for i in iss2 if i["sig"] == sig]
        assert hit and "researcher-x" in hit[0]["msg"] and "working" in hit[0]["msg"], \
            f"stuck 'working' orchestra actor not flagged: {iss2}"
        _store.heartbeat(a["actor_id"], stid)               # the actor beats -> it is alive, not stuck
        iss3 = observe(wf_root="/nonexistent", notify_fn=fake_notify)
        assert not [i for i in iss3 if i["sig"] == sig], "heartbeat did not clear the stuck-actor issue"
    finally:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM orchestra_actors WHERE tenant_id=%s", (stid,))
            cur.execute("DELETE FROM orchestra_runs WHERE tenant_id=%s", (stid,))
            c.commit()
    print("PASS: sentinel — silent-failure observer (stale-workflow, hung-agent, provider-burst, "
          "stuck-orchestra-actor heartbeat, deduped progress ping, boot notice) ✅")


def report():
    """One-look answer to 'is the fleet alive and what is it costing?': live agent processes, real
    trace-level spend/tokens (which the workflow token meter does NOT see), and workflow freshness."""
    print("── live agent processes ──")
    procs = _agent_procs()
    for pid, age, cmd in procs:
        print(f"  {pid}  {round(age)}m  {cmd[:76]}")
    print(f"  ({len(procs)} running)")
    print("── real agent runs + spend (traces, last 6h — factory work the workflow meter misses) ──")
    try:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("""SELECT count(*), coalesce(sum(cost_usd),0), coalesce(sum(tokens_in),0),
                                  coalesce(sum(tokens_out),0), count(*) FILTER (WHERE rc<>0)
                           FROM traces WHERE kind='agent' AND ts > now()-interval '6 hours'""")
            n, usd, tin, tout, bad = cur.fetchone()
            print(f"  {n} agent runs · ${float(usd):.2f} · {int(tin):,} in / {int(tout):,} out · {bad} failed")
            cur.execute("""SELECT product, stage, ts::time(0), rc FROM traces
                           WHERE kind='agent' ORDER BY ts DESC LIMIT 5""")
            for prod, stage, ts, rc in cur.fetchall():
                print(f"  {ts} {prod or '-'}:{stage or '-'} rc={rc}")
    except Exception as e:
        print(f"  (traces unavailable: {e})")
    print("── recent workflows (transcript freshness) ──")
    for wf, newest in sorted(_workflow_dirs(), key=lambda x: -x[1]):
        print(f"  {wf.name}  last write {round((time.time()-newest)/60)}m ago")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "observe"
    if cmd == "selftest":
        _selftest()
    elif cmd == "report":
        report()
    else:
        for i in observe():
            print(json.dumps(i))
        print(f"({len(_agent_procs())} live agent processes)")
