#!/usr/bin/env python3
"""pulse.py — the LIVE PULSE of all in-flight agentic work (the unified observability plane).

North Star (resilience): "heartbeats on everything INCLUDING agentic work in progress; silence is itself a
failure signal; nothing fails invisibly." The fleet's actors already heartbeat (orchestra_actors.last_active)
and daemons beat into `heartbeats` — but NON-fleet agentic work (a QA run, an ad-hoc build, a run's
finalize/report phase) had NO live signal, so a hang there was invisible. To tell "stuck vs slow" you had to
hand-stitch checkpoint files + audit_log + traces across several tables.

`pulse` closes that: every unit of agentic work gets ONE durable row it beats on a cadence — what it is, what
it's doing RIGHT NOW, when it last reported, and (derived) whether it has gone silent. One query answers
"what is every agent doing right now, and is anything stuck?".

    start(work_id, kind, label=...)      open a pulse (idempotent — safe to re-call)
    beat(work_id, stage=, progress=)     sign of life + live status; call on a cadence (fail-open)
    finish(work_id, status=, result=)    terminal transition
    live()                               every non-terminal pulse + beat age + stalled flag  (the glance)
    stalled(mult)                        pulses silent past cadence*mult   (the watchdog/sentinel signal)
    sweep()                              persist status='stalled' on the silent ones (so the view + dedup agree)

    python pulse.py                      print the live pulse table
    python pulse.py selftest             DB round-trip check (no AI, no network)

Complements — does NOT duplicate — existing tables: `heartbeats` (singleton daemons), `orchestra_actors`
(fleet per-actor liveness), `traces` (post-hoc replay). This is the one place that unifies IN-FLIGHT work.
"""
import json
import os
import sys
import time
from pathlib import Path

import psycopg

ENV = Path.home() / "projects" / "agent-os" / ".env.local"
DB = next((l.split("=", 1)[1].strip() for l in ENV.read_text().splitlines()
           if l.strip().startswith("DATABASE_URL=")), None) if ENV.exists() else None

# A pulse is "stalled" once it has been silent for cadence * STALL_MULT. The multiplier tolerates one or two
# slow beats (a heavy model call) before crying stall — silence, not slowness, is the failure signal.
STALL_MULT = int(os.environ.get("AOS_PULSE_STALL_MULT", "3"))
DEFAULT_CADENCE_S = 90


def _ensure():
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS agent_pulse (
                         work_id            TEXT PRIMARY KEY,
                         kind               TEXT NOT NULL,
                         label              TEXT NOT NULL DEFAULT '',
                         tenant_id          TEXT,
                         status             TEXT NOT NULL DEFAULT 'active',  -- active | done | failed | incomplete | stalled
                         stage              TEXT,
                         progress           TEXT,
                         expected_cadence_s INT  NOT NULL DEFAULT 90,
                         meta               JSONB NOT NULL DEFAULT '{}',
                         started_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
                         last_beat          TIMESTAMPTZ NOT NULL DEFAULT now(),
                         finished_at        TIMESTAMPTZ,
                         result             JSONB)""")
        cur.execute("CREATE INDEX IF NOT EXISTS agent_pulse_status_idx ON agent_pulse (status, last_beat DESC)")
        c.commit()


def start(work_id, kind, label="", tenant_id=None, expected_cadence_s=DEFAULT_CADENCE_S, stage=None, meta=None):
    """Open (or re-open) a pulse. Idempotent: re-calling reactivates the same work_id (a resumed run). Returns
    work_id so callers can `wid = pulse.start(...)`. Fail-open — observability must never break real work."""
    if not DB:
        return work_id
    try:
        _ensure()
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("""INSERT INTO agent_pulse
                             (work_id, kind, label, tenant_id, expected_cadence_s, stage, meta,
                              status, started_at, last_beat)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,'active',now(),now())
                           ON CONFLICT (work_id) DO UPDATE SET
                             status='active', last_beat=now(), finished_at=NULL, result=NULL,
                             kind=EXCLUDED.kind, label=EXCLUDED.label,
                             expected_cadence_s=EXCLUDED.expected_cadence_s, stage=EXCLUDED.stage""",
                        (work_id, kind, label, tenant_id, int(expected_cadence_s), stage,
                         json.dumps(meta or {})))
            c.commit()
    except Exception:
        pass
    return work_id


def beat(work_id, stage=None, progress=None, meta=None, status="active"):
    """Sign of life + current status. Omitted stage/progress keep their prior value (COALESCE). Called on a
    cadence by any agentic loop. Fail-open by design: a heartbeat write must NEVER break the work it observes."""
    if not DB:
        return
    try:
        _ensure()
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("""UPDATE agent_pulse SET last_beat=now(), status=%s,
                             stage=COALESCE(%s,stage), progress=COALESCE(%s,progress),
                             meta = meta || %s::jsonb
                           WHERE work_id=%s""",
                        (status, stage, progress, json.dumps(meta or {}), work_id))
            if cur.rowcount == 0:                 # a beat implies live work — auto-open if start() was skipped
                cur.execute("""INSERT INTO agent_pulse (work_id, kind, label, stage, progress)
                               VALUES (%s,'unknown','',%s,%s) ON CONFLICT (work_id) DO NOTHING""",
                            (work_id, stage, progress))
            c.commit()
    except Exception:
        pass


def finish(work_id, status="done", result=None):
    """Terminal transition (done | failed | incomplete). Fail-open."""
    if not DB:
        return
    try:
        _ensure()
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("""UPDATE agent_pulse SET status=%s, last_beat=now(), finished_at=now(), result=%s
                           WHERE work_id=%s""",
                        (status, json.dumps(result) if result is not None else None, work_id))
            c.commit()
    except Exception:
        pass


def _rows(include_done_s=0):
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT work_id, kind, label, tenant_id, status, stage, progress, expected_cadence_s,
                         EXTRACT(EPOCH FROM now()-last_beat)::INT AS beat_age_s,
                         EXTRACT(EPOCH FROM now()-started_at)::INT AS age_s
                       FROM agent_pulse
                       WHERE status IN ('active','stalled')
                          OR (finished_at IS NOT NULL AND finished_at > now() - (%s||' seconds')::interval)
                       ORDER BY (status='stalled') DESC, last_beat DESC""", (str(int(include_done_s)),))
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    for r in rows:
        r["stalled"] = r["status"] == "active" and r["beat_age_s"] > r["expected_cadence_s"] * STALL_MULT
    return rows


def live(include_done_s=0):
    """Every in-flight pulse (+ recently finished if include_done_s>0) with beat age + a stalled flag. This is
    the unified 'what is every agent doing right now' view a human / observer agent / the CEO can glance at."""
    if not DB:
        return []
    try:
        return _rows(include_done_s)
    except Exception:
        return []


def stalled(mult=None):
    """In-flight work that has gone SILENT past cadence*mult — the signal the watchdog/sentinel escalate on."""
    m = mult or STALL_MULT
    return [r for r in live() if r["status"] == "active" and r["beat_age_s"] > r["expected_cadence_s"] * m]


def sweep():
    """Persist status='stalled' on silent pulses so the live view + alert-dedup agree. Returns the count."""
    if not DB:
        return 0
    hits = stalled()
    if not hits:
        return 0
    try:
        _ensure()
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("UPDATE agent_pulse SET status='stalled' WHERE work_id = ANY(%s) AND status='active'",
                        ([h["work_id"] for h in hits],))
            c.commit()
    except Exception:
        pass
    return len(hits)


def _fmt_age(s):
    s = int(s or 0)
    return f"{s}s" if s < 90 else f"{s // 60}m{s % 60:02d}s"


def _print_live():
    rows = live(include_done_s=120)
    if not rows:
        print("pulse: no in-flight agentic work right now.")
        return
    print(f"{'STATUS':9} {'KIND':13} {'STAGE':10} {'LAST BEAT':9} {'WORK / PROGRESS'}")
    print("-" * 92)
    for r in rows:
        flag = "STALLED" if r["stalled"] else r["status"].upper()
        prog = (r.get("progress") or r.get("label") or r["work_id"])[:44]
        print(f"{flag:9} {r['kind']:13.13} {(r.get('stage') or '-'):10.10} "
              f"{_fmt_age(r['beat_age_s']):9} {prog}")
    st = [r for r in rows if r["stalled"]]
    if st:
        print(f"\n⚠ {len(st)} stalled (silent past cadence): " + ", ".join(r["work_id"] for r in st))


def _selftest():
    if not DB:
        print("pulse selftest: SKIP (no DATABASE_URL in .env.local)")
        return 0
    wid = f"selftest:{int(time.time())}"
    start(wid, "selftest", label="pulse self-test", expected_cadence_s=1)
    beat(wid, stage="phase-a", progress="step 1")
    assert any(r["work_id"] == wid for r in live()), "started pulse must appear in live()"
    time.sleep(2.3)                                       # > cadence(1s) once beat_age truncates to int seconds
    assert any(r["work_id"] == wid for r in stalled(mult=1)), "a pulse silent past cadence must be stalled"
    beat(wid, stage="phase-b", progress="step 2")
    assert not any(r["work_id"] == wid for r in stalled(mult=1)), "a fresh beat must clear stalled"
    finish(wid, status="done", result={"ok": True})
    assert not any(r["work_id"] == wid for r in live()), "a finished pulse must leave the live view"
    # cleanup
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("DELETE FROM agent_pulse WHERE work_id=%s", (wid,))
        c.commit()
    print("pulse selftest: PASS (start->beat->live->stall-on-silence->beat-clears->finish->gone)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        sys.exit(_selftest())
    _print_live()
