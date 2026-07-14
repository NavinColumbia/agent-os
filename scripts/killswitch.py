#!/usr/bin/env python3
"""killswitch.py — runtime human-oversight control: STOP / OVERRIDE the agent fleet on demand.

EU AI Act Art. 14 (human oversight, enforced 2026-08-02) and the launch research both require the operator
to be able to halt autonomous agents at runtime — not just at deploy time. Agents here are turn-based
(each step is a fresh `claude -p`), so a control-plane HALT flag that factory.agent() checks before every
spawn cleanly stops a runaway build/fleet at the next step boundary, without killing in-flight work
mid-write. Scope is 'global' (whole fleet) or any string (a product/tenant id) for targeted halts.

    killswitch.py halt [scope] ["reason"]   # default scope 'global'
    killswitch.py resume [scope]
    killswitch.py status [scope]
    killswitch.py selftest
Run with the agent-os venv python.
"""
import sys
import time
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit   # noqa: E402

from aoscfg import ENV, DB


def _ensure():
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS kill_switch (
            scope TEXT PRIMARY KEY, reason TEXT, set_by TEXT,
            set_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        c.commit()


def halt(scope="global", reason="operator halt", set_by="operator"):
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO kill_switch (scope, reason, set_by) VALUES (%s,%s,%s)
                       ON CONFLICT (scope) DO UPDATE SET reason=EXCLUDED.reason, set_by=EXCLUDED.set_by,
                       set_at=now()""", (scope, reason, set_by))
        c.commit()
    audit.append(actor="killswitch", action="FleetHalted", resource=scope, decision="halted",
                 payload={"reason": reason, "by": set_by})
    return True


def resume(scope="global"):
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("DELETE FROM kill_switch WHERE scope=%s", (scope,))
        c.commit()
    audit.append(actor="killswitch", action="FleetResumed", resource=scope, decision="resumed")
    return True


_HALT_RETRIES = 3        # attempts to reach the control-plane before giving up on THIS call
_HALT_BACKOFF = 0.2      # base seconds between retries (0.2s, 0.4s) — short, this is the per-spawn hot path
_HALT_CACHE_TTL = 45     # grace window (s): reuse last-known-good halt state during a transient DB blip
_halt_cache: dict = {}   # scope -> (monotonic_ts, result_dict) of the last SUCCESSFUL read


def _alert(msg):
    """Best-effort operator alert for a control-plane reachability problem. MUST never raise on the hot
    path, so the DB-backed audit is best-effort and stderr is the always-available fallback."""
    print(f"[killswitch] {msg}", file=sys.stderr)
    try:
        audit.append(actor="killswitch", action="ControlPlaneUnreachable", resource="control-plane",
                     decision="degraded", payload={"detail": msg})
    except Exception:
        pass


def _read_halt(scope):
    """One authoritative read of the halt state from the control-plane. Raises on any DB error."""
    _ensure()
    with psycopg.connect(DB, connect_timeout=3) as c, c.cursor() as cur:
        cur.execute("SELECT scope, reason FROM kill_switch WHERE scope IN ('global', %s)", (scope,))
        row = cur.fetchone()
    return {"halted": True, "scope": row[0], "reason": row[1]} if row else {"halted": False}


def is_halted(scope="global"):
    """True if the fleet is halted globally OR this specific scope is halted. factory.agent() calls this
    before EVERY spawn, so a wrong answer here brakes (or bricks) the whole line.

    Failure handling is deliberately tiered so a momentary DB blip can NOT masquerade as an operator STOP:
      1. Retry the read a few times with short backoff — most blips (a 2s hiccup, a pool exhaustion, a PG
         restart, a max_connections spike) clear within a second or two.
      2. If still unreachable, reuse the LAST successfully-read halt state for this scope while it is within
         a short grace window (TTL). A transient outage thus reuses the last known-good answer instead of
         inventing a fleet-wide halt — preserving liveness, exactly as the token-budget governor downstream
         in factory.agent() expects of a shared-Postgres hiccup.
      3. Only fail CLOSED if the control-plane is DURABLY unreachable (retries exhausted AND no fresh
         known-good state). This honours the human-oversight HALT (EU AI Act Art. 14): once we genuinely
         cannot prove the operator did NOT press stop, a recoverable pause beats silently running a fleet
         someone tried to STOP. Both the stale-reuse and the durable fail-closed paths emit an alert."""
    last_err = None
    for attempt in range(_HALT_RETRIES):
        try:
            res = _read_halt(scope)
            _halt_cache[scope] = (time.monotonic(), res)   # remember this KNOWN-GOOD answer (per scope)
            return res
        except Exception as e:
            last_err = e
            if attempt < _HALT_RETRIES - 1:
                time.sleep(_HALT_BACKOFF * (attempt + 1))  # short backoff, then retry

    # Retries exhausted: the control-plane is unreachable right now. Prefer the last known-good state.
    cached = _halt_cache.get(scope)
    if cached and (time.monotonic() - cached[0]) < _HALT_CACHE_TTL:
        ts, res = cached
        out = dict(res)
        out["stale"] = True
        out["stale_age_s"] = round(time.monotonic() - ts, 1)
        _alert(f"control-plane unreachable ({type(last_err).__name__}); reusing last-good state for "
               f"scope={scope!r} (halted={out['halted']}, age={out['stale_age_s']}s) — NOT inventing a halt")
        return out

    # Durably unreachable (or no known-good state ever): only NOW do we fail CLOSED, scoped to the caller.
    _alert(f"control-plane DURABLY unreachable for scope={scope!r} "
           f"({type(last_err).__name__}, {_HALT_RETRIES} tries, no fresh cache); failing CLOSED")
    return {"halted": True, "scope": scope,
            "reason": f"failsafe: control-plane unreachable ({type(last_err).__name__}); failing CLOSED"}


def _selftest():
    s = "selftest-" + __import__("os").urandom(3).hex()
    clean0 = not is_halted(s)["halted"]                 # nothing halted initially
    halt(s, "test halt")
    scoped = is_halted(s)["halted"] and not is_halted("other-" + s)["halted"]   # targeted, not global
    resume(s)
    cleared = not is_halted(s)["halted"]
    halt("global", "test global"); glob = is_halted("anything")["halted"]       # global halts everything
    resume("global"); glob_cleared = not is_halted("anything")["halted"]
    ok = clean0 and scoped and cleared and glob and glob_cleared
    print(f"clean={clean0} scoped-halt={scoped} resume={cleared} global-halts-all={glob} global-resume={glob_cleared}")
    print("PASS: runtime kill-switch (scoped + global halt/resume) ✅" if ok else "FAIL")
    sys.exit(0 if ok else 1)


def _main(a):
    import json
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "halt":
        print(json.dumps({"halted": halt(a[1] if len(a) > 1 else "global",
                                         a[2] if len(a) > 2 else "operator halt")}))
    elif a[0] == "resume":
        print(json.dumps({"resumed": resume(a[1] if len(a) > 1 else "global")}))
    elif a[0] == "status":
        print(json.dumps(is_halted(a[1] if len(a) > 1 else "global"), indent=2))
    else:
        sys.exit("usage: killswitch.py halt [scope] [reason] | resume [scope] | status [scope] | selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
