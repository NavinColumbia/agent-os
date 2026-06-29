#!/usr/bin/env python3
"""findings.py — close the loophole: a review finding is a GOVERNED, OWNED, TRACKED work-item, not text.

The bug the CEO caught: review agents produced findings as text back to the orchestrator (a human-like
single point), who manually triaged and could silently DROP one (e.g. "add real signup" got mis-bucketed
as gated). The fix: every finding is filed here, which (1) records it, (2) ROUTES it through the existing
governed fabric — orchestrate.request_collaborator checks the agent directory for an active agent of the
right role and assigns the work to them (or files a hire_request to spawn one), so it has an OWNER; (3)
puts it on the CEO task board for visibility; (4) is tracked to resolution, and accountability/sweep
escalates any finding left open too long. No finding can die in the orchestrator's head anymore.

    findings.py file <source> <need_role> "<title>" ["detail"] [severity]   # file + auto-route to an owner
    findings.py open                       # the accountable backlog (open/routed, unresolved)
    findings.py resolve <id> [by]
    findings.py sweep                      # escalate findings left open past SLA (cron)
    findings.py selftest
Run with the agent-os venv python.
"""
import sys
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit        # noqa: E402
import orchestrate  # noqa: E402
import taskboard    # noqa: E402

ENV = Path.home() / "projects" / "agent-os" / ".env.local"
DB = next((l.split("=", 1)[1].strip() for l in ENV.read_text().splitlines()
           if l.strip().startswith("DATABASE_URL=")), None)
SLA_HOURS = int(__import__("os").environ.get("AOS_FINDING_SLA_HOURS", "24"))


def _ensure():
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS findings (
            id BIGSERIAL PRIMARY KEY, source TEXT NOT NULL, title TEXT NOT NULL, detail TEXT,
            severity TEXT NOT NULL DEFAULT 'med', need_role TEXT NOT NULL DEFAULT 'builder',
            status TEXT NOT NULL DEFAULT 'open',          -- open|routed|hire_pending|no_role|resolved|dropped
            owner TEXT, task_id BIGINT, hire_id BIGINT, board_id BIGINT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(), resolved_at TIMESTAMPTZ, resolved_by TEXT)""")
        c.commit()


def file(source, need_role, title, detail="", severity="med", priority=5):
    """File a finding AND route it to an accountable owner via the governed fabric. Returns the disposition."""
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO findings (source, title, detail, severity, need_role)
                       VALUES (%s,%s,%s,%s,%s) RETURNING id""", (source, title, detail, severity, need_role))
        fid = cur.fetchone()[0]; c.commit()
    # ROUTE through the directory-aware router — assign to an active agent or spawn one (governed)
    _STATUS = {"routed_to_existing": "routed", "hire_requested_overloaded": "hire_pending",
               "hire_requested_spawn": "hire_pending", "no_exact_role_use_nearest": "routed",
               "no_role": "no_role"}
    r = orchestrate.request_collaborator(f"finding:{source}", need_role, title, priority)
    act = r.get("action")
    eff = r  # the disposition we actually persist (the re-routed result wins, if any)
    # if no exact role, re-route to the nearest role so it still gets an owner (don't drop it)
    if act == "no_exact_role_use_nearest" and r.get("suggested_role"):
        eff = orchestrate.request_collaborator(f"finding:{source}", r["suggested_role"], title, priority)
    # derive the real disposition from the EFFECTIVE result so r2's task_id/hire_id/status aren't lost
    eff_act = eff.get("action")
    status = _STATUS.get(eff_act, "open")
    owner = eff.get("assignee") or eff.get("suggested_role")
    task_id = eff.get("task_id")
    hire_id = eff.get("hire_id")
    # CEO visibility on the task board
    bid = None
    try:
        bid = taskboard.add(f"[{severity}] {title}", detail, source=f"review:{source}")
    except Exception:
        pass
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""UPDATE findings SET status=%s, owner=%s, task_id=%s, hire_id=%s, board_id=%s WHERE id=%s""",
                    (status, owner, task_id, hire_id, bid, fid))
        c.commit()
    audit.append(actor="findings", action="FindingFiled", resource=str(fid), decision=status,
                 payload={"source": source, "need_role": need_role, "owner": owner, "routing": eff_act})
    return {"finding_id": fid, "status": status, "owner": owner, "routing": eff_act,
            "task_id": task_id, "hire_id": hire_id, "board_id": bid}


def resolve(fid, by="agent"):
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("UPDATE findings SET status='resolved', resolved_at=now(), resolved_by=%s WHERE id=%s",
                    (by, fid))
        c.commit()
    audit.append(actor="findings", action="FindingResolved", resource=str(fid), decision="resolved",
                 payload={"by": by})
    return {"finding_id": fid, "status": "resolved"}


def open_findings():
    """The accountable backlog: filed but not yet resolved — nothing here can be silently forgotten."""
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT id, source, title, severity, need_role, status, owner,
                              EXTRACT(EPOCH FROM now()-created_at)::int/3600 AS age_h
                       FROM findings WHERE status NOT IN ('resolved','dropped') ORDER BY created_at""")
        return [{"id": i, "source": s, "title": t, "severity": sev, "need_role": nr, "status": st,
                 "owner": o, "age_hours": ah} for i, s, t, sev, nr, st, o, ah in cur.fetchall()]


def sweep():
    """Cron: escalate findings left open past the SLA — a finding cannot quietly rot."""
    overdue = [f for f in open_findings() if (f["age_hours"] or 0) >= SLA_HOURS]
    if overdue:
        audit.append(actor="findings", action="FindingsOverdue", resource="backlog", decision="escalated",
                     payload={"count": len(overdue), "ids": [f["id"] for f in overdue][:20]})
        try:
            import notify
            notify.send(f"{len(overdue)} review finding(s) open past {SLA_HOURS}h with no resolution. "
                        f"Owners: {', '.join(sorted({f['owner'] or '?' for f in overdue}))[:200]}",
                        title="agent-os findings", priority="high", tags="warning")
        except Exception:
            pass
    return {"open": len(open_findings()), "overdue": len(overdue)}


def _selftest():
    import os
    import directory
    suf = os.urandom(3).hex()
    agent_id = f"builder@findings-{suf}"
    directory.register(agent_id, "builder", product=f"findings-{suf}", task="idle")
    try:
        # file a finding needing a 'builder' — must route to the live builder above (an OWNER), not vanish
        r = file(f"reviewer-{suf}", "builder", f"Add X to product {suf}", "details here", "high")
        routed = r["status"] == "routed" and r["owner"] == agent_id and r["task_id"]
        on_board = r["board_id"] is not None
        in_backlog = any(f["id"] == r["finding_id"] for f in open_findings())
        # resolve it -> leaves the accountable backlog
        resolve(r["finding_id"], by=agent_id)
        cleared = not any(f["id"] == r["finding_id"] for f in open_findings())
        ok = routed and on_board and in_backlog and cleared
        print(f"routed_to_owner={routed} on_taskboard={on_board} in_backlog={in_backlog} resolved_clears={cleared}")
        print("PASS: findings are filed -> ROUTED to an owner -> tracked -> resolved (no silent drop) ✅" if ok else "FAIL")
    finally:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM findings WHERE source=%s", (f"reviewer-{suf}",))
            cur.execute("DELETE FROM tasks WHERE assignee=%s", (agent_id,))
            cur.execute("DELETE FROM directory WHERE agent_id=%s", (agent_id,))
            cur.execute("DELETE FROM task_board WHERE source=%s", (f"review:reviewer-{suf}",))
            c.commit()
    sys.exit(0 if ok else 1)


def _main(a):
    import json
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "file" and len(a) >= 4:
        print(json.dumps(file(a[1], a[2], a[3], a[4] if len(a) > 4 else "", a[5] if len(a) > 5 else "med"), indent=2))
    elif a[0] == "open":
        print(json.dumps(open_findings(), indent=2))
    elif a[0] == "resolve" and len(a) > 1:
        print(json.dumps(resolve(int(a[1]), a[2] if len(a) > 2 else "human")))
    elif a[0] == "sweep":
        print(json.dumps(sweep(), indent=2))
    else:
        sys.exit('usage: findings.py file <source> <need_role> "<title>" ["detail"] [sev] | open | resolve <id> | sweep | selftest')


if __name__ == "__main__":
    _main(sys.argv[1:])
