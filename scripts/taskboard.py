#!/usr/bin/env python3
"""taskboard.py — the CEO-facing TASK BOARD: what was asked, and the status of each ask.

Distinct from the internal `tasks` work-queue (which dispatches agent work). This is the human record:
every request a CEO/user makes becomes a card with a status (asked → in_progress → blocked → done), so
nothing is ambiguously "I thought we were just discussing it." Both the platform operator AND each tenant
get one; in the product it's the "Requests / Task Board" view. Postgres-backed, self-initialising.

    taskboard.py add "<what was asked>" [--detail "..."] [--source ceo|user]
    taskboard.py status <id> asked|in_progress|blocked|done [--note "..."]
    taskboard.py list [--status ...] [--open]      |     taskboard.py board     (grouped view)
    taskboard.py selftest
Run with the agent-os venv python.
"""
import sys
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit  # noqa: E402

_ENV = Path.home() / "projects" / "agent-os" / ".env.local"
_DB = next((l.split("=", 1)[1].strip() for l in _ENV.read_text().splitlines()
            if l.strip().startswith("DATABASE_URL=")), None)
STATUSES = ("asked", "in_progress", "blocked", "done")

_DDL = """
CREATE TABLE IF NOT EXISTS task_board (
  id          serial PRIMARY KEY,
  tenant      text NOT NULL DEFAULT 'platform',
  title       text NOT NULL,
  detail      text DEFAULT '',
  status      text NOT NULL DEFAULT 'asked',
  source      text NOT NULL DEFAULT 'ceo',      -- ceo | user | auto
  notes       text DEFAULT '',
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);
"""


def _conn():
    c = psycopg.connect(_DB)
    with c.cursor() as cur:
        cur.execute(_DDL)
    c.commit()
    return c


def add(title, detail="", source="ceo", tenant="platform"):
    with _conn() as c, c.cursor() as cur:
        cur.execute("INSERT INTO task_board (tenant,title,detail,source) VALUES (%s,%s,%s,%s) RETURNING id",
                    (tenant, title, detail, source))
        tid = cur.fetchone()[0]
        c.commit()
    audit.append(actor="taskboard", action="TaskAsked", resource=str(tid), decision="asked", payload={"title": title[:120]})
    return tid


def status(tid, new, note=""):
    assert new in STATUSES, f"status must be one of {STATUSES}"
    with _conn() as c, c.cursor() as cur:
        cur.execute("""UPDATE task_board SET status=%s, updated_at=now(),
                       notes = CASE WHEN %s<>'' THEN notes || %s ELSE notes END WHERE id=%s""",
                    (new, note, f"\n[{new}] {note}", tid))
        n = cur.rowcount
        c.commit()
    audit.append(actor="taskboard", action="TaskStatus", resource=str(tid), decision=new, payload={"note": note[:120]})
    return n > 0


def items(tenant=None, status_filter=None, open_only=False):
    q = "SELECT id,tenant,title,status,source,updated_at FROM task_board WHERE 1=1"
    args = []
    if tenant:
        q += " AND tenant=%s"; args.append(tenant)
    if status_filter:
        q += " AND status=%s"; args.append(status_filter)
    if open_only:
        q += " AND status<>'done'"
    q += " ORDER BY (status='done'), array_position(ARRAY['blocked','in_progress','asked','done'], status), id DESC"
    with _conn() as c, c.cursor() as cur:
        cur.execute(q, args)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def board(tenant=None):
    rows = items(tenant)
    by = {s: [r for r in rows if r["status"] == s] for s in STATUSES}
    out = []
    icon = {"asked": "📥", "in_progress": "🔨", "blocked": "⛔", "done": "✅"}
    for s in STATUSES:
        out.append(f"\n{icon[s]} {s.upper()} ({len(by[s])})")
        for r in by[s][:30]:
            out.append(f"  #{r['id']:<4} {r['title'][:78]}")
    return "\n".join(out)


def _selftest():
    tid = add("SELFTEST: build the thing", detail="x", source="ceo")
    ok1 = any(i["id"] == tid and i["status"] == "asked" for i in items(open_only=True))
    status(tid, "in_progress", "started")
    status(tid, "done", "shipped")
    ok2 = not any(i["id"] == tid for i in items(open_only=True))      # done -> not in open list
    with _conn() as c, c.cursor() as cur:                            # cleanup the selftest row
        cur.execute("DELETE FROM task_board WHERE id=%s", (tid,)); c.commit()
    ok = ok1 and ok2
    print(f"asked-appears={ok1} done-closes={ok2}")
    print("PASS: task board (ask -> status -> done lifecycle) ✅" if ok else "FAIL")
    sys.exit(0 if ok else 1)


def _main(a):
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "add":
        detail = a[a.index("--detail") + 1] if "--detail" in a else ""
        src = a[a.index("--source") + 1] if "--source" in a else "ceo"
        print("asked #", add(a[1], detail, src))
    elif a[0] == "status":
        note = a[a.index("--note") + 1] if "--note" in a else ""
        print("ok" if status(int(a[1]), a[2], note) else "not found")
    elif a[0] == "board":
        print(board())
    elif a[0] == "list":
        sf = a[a.index("--status") + 1] if "--status" in a else None
        for r in items(status_filter=sf, open_only="--open" in a):
            print(f"  #{r['id']:<4} [{r['status']:<11}] {r['title'][:72]}")
    else:
        sys.exit("usage: taskboard.py add|status|list|board|selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
