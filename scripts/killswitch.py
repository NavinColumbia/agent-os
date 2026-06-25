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
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit   # noqa: E402

ENV = Path.home() / "projects" / "agent-os" / ".env.local"
DB = next((l.split("=", 1)[1].strip() for l in ENV.read_text().splitlines()
           if l.strip().startswith("DATABASE_URL=")), None)


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


def is_halted(scope="global"):
    """True if the fleet is halted globally OR this specific scope is halted. factory.agent() calls this
    before every spawn. Fail-OPEN on a DB error (don't let an outage wedge the whole fleet shut)."""
    try:
        _ensure()
        with psycopg.connect(DB, connect_timeout=3) as c, c.cursor() as cur:
            cur.execute("SELECT scope, reason FROM kill_switch WHERE scope IN ('global', %s)", (scope,))
            row = cur.fetchone()
        return {"halted": True, "scope": row[0], "reason": row[1]} if row else {"halted": False}
    except Exception:
        return {"halted": False}


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
