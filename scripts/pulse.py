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

from aoscfg import ENV, DB

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


def beat(work_id, stage=None, progress=None, meta=None, status="active",
         kind=None, label=None, tenant_id=None, expected_cadence_s=None):
    """Sign of life + current status. Omitted fields keep their prior value (COALESCE). A beat is
    self-sufficient — pass kind/label and it opens the row itself, so a loop can just beat without a separate
    start(). Called on a cadence by any agentic loop. Fail-open: a heartbeat write must NEVER break the work."""
    if not DB:
        return
    try:
        _ensure()
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("""UPDATE agent_pulse SET last_beat=now(), status=%s,
                             kind=COALESCE(%s,kind), label=COALESCE(%s,label), tenant_id=COALESCE(%s,tenant_id),
                             stage=COALESCE(%s,stage), progress=COALESCE(%s,progress),
                             expected_cadence_s=COALESCE(%s,expected_cadence_s), meta = meta || %s::jsonb
                           WHERE work_id=%s""",
                        (status, kind, label, tenant_id, stage, progress, expected_cadence_s,
                         json.dumps(meta or {}), work_id))
            if cur.rowcount == 0:                 # a beat implies live work — open the row itself
                cur.execute("""INSERT INTO agent_pulse (work_id, kind, label, tenant_id, stage, progress,
                                 expected_cadence_s)
                               VALUES (%s, COALESCE(%s,'unknown'), COALESCE(%s,''), %s, %s, %s,
                                 COALESCE(%s, %s)) ON CONFLICT (work_id) DO NOTHING""",
                            (work_id, kind, label, tenant_id, stage, progress,
                             expected_cadence_s, DEFAULT_CADENCE_S))
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


def _fleet_rows():
    """Fleet actors (orchestra) surfaced READ-ONLY from their existing last_active heartbeat — NO write on the
    actor hot path (a synchronous write there perturbs the timing-sensitive supervisor/sibling race). Only
    working/blocked actors in still-running runs. Fail-open if the orchestra tables are absent."""
    try:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("""SELECT a.actor_id, a.role, a.name, a.tenant_id, a.status, a.assignment,
                             EXTRACT(EPOCH FROM now()-a.last_active)::INT AS beat_age_s,
                             EXTRACT(EPOCH FROM now()-a.hired_at)::INT AS age_s
                           FROM orchestra_actors a JOIN orchestra_runs r ON r.run_id = a.run_id
                           WHERE a.status IN ('working', 'blocked') AND r.status = 'running'
                           ORDER BY a.last_active DESC LIMIT 200""")
            out = []
            for aid, role, name, tid, status, assignment, bage, age in cur.fetchall():
                cad = 210                                    # ~matches the fleet's own stale-actor threshold
                out.append({"work_id": f"actor:{aid}", "kind": "fleet-actor",
                            "label": f"{role} · {name}", "tenant_id": tid, "status": status,
                            "stage": status, "progress": assignment or "", "expected_cadence_s": cad,
                            "beat_age_s": bage or 0, "age_s": age or 0,
                            "stalled": (bage or 0) > cad * STALL_MULT})
            return out
    except Exception:
        return []


def live(include_done_s=0):
    """Every in-flight unit of agentic work — pulse rows (QA runs, builds) PLUS fleet actors (read from their
    existing heartbeat) — with beat age + a stalled flag. The unified 'what is every agent doing right now,
    and is anything stuck?' view a human / observer agent / the CEO can glance at in one place."""
    if not DB:
        return []
    try:
        return _rows(include_done_s) + _fleet_rows()
    except Exception:
        return []


def stalled(mult=None):
    """PULSE-tracked work (QA/builds) gone SILENT past cadence*mult — the watchdog signal. Excludes fleet
    actors on purpose: the sentinel already owns fleet stale-actor detection (no double-alerting)."""
    m = mult or STALL_MULT
    if not DB:
        return []
    try:
        return [r for r in _rows() if r["status"] == "active" and r["beat_age_s"] > r["expected_cadence_s"] * m]
    except Exception:
        return []


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


# A pulse silent past cadence*REAP_MULT (with an absolute REAP_MIN_S floor) is not "slow" — its process is
# gone (a WSL/host restart killed it, or it crashed without a terminal write). `stalled` is the right LIVE
# signal, but a stalled row must not linger forever: after a reboot the in-flight view would show week-old
# ghosts and the watchdog would page on work that no longer exists. reap_orphans() reconciles those to a
# terminal 'reaped' state — honest ("it did NOT finish; its process died"), and it clears the plane so
# `live()`/`stalled()`/the watchdog reflect reality. Deliberately conservative so a slow-but-alive beater is
# never reaped; call it from a slow control loop (the watchdog), never the hot beat path.
REAP_MULT = int(os.environ.get("AOS_PULSE_REAP_MULT", "20"))
REAP_MIN_S = int(os.environ.get("AOS_PULSE_REAP_MIN_S", "900"))     # 15 min floor — no live beater is this quiet


def reap_orphans():
    """Finalize non-terminal pulses gone silent past max(cadence*REAP_MULT, REAP_MIN_S) → status='reaped'.
    Their process is provably gone (dead heartbeat). Returns the count reaped. Fail-open."""
    if not DB:
        return 0
    try:
        _ensure()
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("""UPDATE agent_pulse
                             SET status='reaped', finished_at=now(),
                                 result=COALESCE(result,'{}'::jsonb)
                                        || jsonb_build_object('reaped_reason','heartbeat dead (process gone) — reconciled',
                                                              'last_beat_age_s', EXTRACT(EPOCH FROM now()-last_beat)::INT)
                           WHERE status IN ('active','stalled')
                             AND EXTRACT(EPOCH FROM now()-last_beat)
                                 > GREATEST(expected_cadence_s * %s, %s)
                           RETURNING work_id""", (REAP_MULT, REAP_MIN_S))
            n = len(cur.fetchall())
            c.commit()
        return n
    except Exception:
        return 0


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
