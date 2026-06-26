#!/usr/bin/env python3
"""designview.py — the prototype GALLERY + approval, for an org's design artifacts (sibling of the
other *view.py read/decide surfaces). The design fleet (design_fleet.py) prototypes three audience
screens (cockpit / team / external); designview lists them for the org and lets a human approve or
send one back to draft. Everything is org-scoped: you only ever see/decide YOUR org's artifacts.

    designview.py gallery <org_id>                       # [{id, surface, title, html_path, status}]
    designview.py decide <org_id> <artifact_id> <status> # status -> approved | draft | review
    designview.py surfaces                               # the three audiences + one-liners
    designview.py selftest
Run with the agent-os venv python. NO web server — read-only listing + a single ownership-checked write.
"""
import json
import sys
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit  # noqa: E402

DB = next((l.split("=", 1)[1].strip()
           for l in (Path.home() / "projects" / "agent-os" / ".env.local").read_text().splitlines()
           if l.strip().startswith("DATABASE_URL=")), None)

STATUSES = ("draft", "review", "approved")
_SURFACES = {
    "cockpit":  "the CEO/owner command cockpit — KPIs, controls, and decisions for the owner",
    "team":     "the internal team/operator console — the day-to-day working screens for staff",
    "external": "the external end-user / customer experience — the public-facing product screen",
}


def _ensure():
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS design_artifacts (
            id BIGSERIAL PRIMARY KEY, org_id TEXT, product TEXT, kind TEXT DEFAULT 'screen',
            surface TEXT, title TEXT, html_path TEXT, status TEXT DEFAULT 'draft',
            created_at TIMESTAMPTZ DEFAULT now())""")
        c.commit()


def gallery(org_id: str) -> list:
    """Every design artifact for the org, newest first: [{id, surface, title, html_path, status}]."""
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT id, surface, title, html_path, status FROM design_artifacts
                       WHERE org_id=%s ORDER BY id DESC""", (org_id,))
        return [{"id": r[0], "surface": r[1], "title": r[2], "html_path": r[3], "status": r[4]}
                for r in cur.fetchall()]


def decide(org_id: str, artifact_id, status: str) -> dict:
    """Set an artifact's status (e.g. review -> approved, or back to draft). Ownership via org match:
    the UPDATE only touches a row whose org_id matches, so one org can't decide another's artifact."""
    _ensure()
    if status not in STATUSES:
        return {"ok": False, "error": f"status must be one of {STATUSES}"}
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("UPDATE design_artifacts SET status=%s WHERE id=%s AND org_id=%s",
                    (status, artifact_id, org_id))
        changed = cur.rowcount
        c.commit()
    if not changed:
        return {"ok": False, "error": "not found or not your artifact"}
    audit.append(actor="designview", action="DesignDecide", resource=str(artifact_id),
                 decision=status, payload={"org": org_id, "artifact_id": artifact_id, "status": status})
    return {"ok": True}


def surfaces() -> list:
    """The three audiences we prototype for, each with a one-line description."""
    return [{"surface": s, "description": d} for s, d in _SURFACES.items()]


def _selftest():
    """Insert three fake artifacts for a throwaway org (design_artifacts.org_id is just text — no real
    org row needed), then prove gallery() lists them, decide() flips one to approved with ownership
    enforced, and surfaces() returns the three audiences. Cleans up the rows in finally."""
    import uuid
    _ensure()
    org_id = "o-selftest-" + uuid.uuid4().hex[:8]
    ok = False
    try:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            for s in ("cockpit", "team", "external"):
                cur.execute("""INSERT INTO design_artifacts (org_id, product, surface, title,
                                 html_path, status) VALUES (%s,'demo',%s,%s,%s,'review')""",
                            (org_id, s, f"demo — {s} screen", f"/tmp/{org_id}-{s}.html"))
            c.commit()

        g = gallery(org_id)
        target = g[0]["id"]
        flip = decide(org_id, target, "approved")
        guard = decide("o-not-mine", target, "draft")          # ownership: other org can't decide it
        bad = decide(org_id, target, "nonsense")               # invalid status rejected
        after = next(x for x in gallery(org_id) if x["id"] == target)
        sfc = surfaces()

        ok = (len(g) == 3
              and all(set(x) == {"id", "surface", "title", "html_path", "status"} for x in g)
              and flip.get("ok") is True
              and after["status"] == "approved"
              and guard.get("ok") is False
              and bad.get("ok") is False
              and len(sfc) == 3
              and {x["surface"] for x in sfc} == {"cockpit", "team", "external"})
        print(f"gallery={len(g)} decide={flip.get('ok')} approved={after['status']} "
              f"guard={guard.get('ok')} bad_status={bad.get('ok')} surfaces={len(sfc)}")
        print("PASS: designview gallery + ownership-checked decide + surfaces ✅" if ok else "FAIL")
    finally:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM design_artifacts WHERE org_id=%s", (org_id,))
            c.commit()
    sys.exit(0 if ok else 1)


def _main(a):
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "gallery" and len(a) > 1:
        print(json.dumps(gallery(a[1]), indent=2))
    elif a[0] == "decide" and len(a) > 3:
        print(decide(a[1], int(a[2]), a[3]))
    elif a[0] == "surfaces":
        print(json.dumps(surfaces(), indent=2))
    else:
        sys.exit("usage: designview.py gallery <org_id> | decide <org_id> <id> <status> | "
                 "surfaces | selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
