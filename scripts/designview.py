#!/usr/bin/env python3
"""designview.py — the prototype GALLERY + approval, for an org's design artifacts (sibling of the
other *view.py read/decide surfaces). The design fleet (design_fleet.py) prototypes three audience
screens (cockpit / team / external); designview lists them for the org and lets a human approve or
send one back to draft. Everything is org-scoped: you only ever see/decide YOUR org's artifacts.

    designview.py gallery <tenant_id> <org_id>                       # [{id, surface, title, html_path, status}]
    designview.py decide <tenant_id> <org_id> <artifact_id> <status> # status -> approved | draft | review
    designview.py surfaces                                           # the three audiences + one-liners
    designview.py selftest
Run with the agent-os venv python. NO web server — read-only listing + a single ownership-checked write.
Both read and write are scoped by (tenant_id, org_id): defense-in-depth so a tenant can never see or
decide another tenant's artifacts even if the routing-layer/org check is bypassed (IDOR hardening).
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
        # Defense-in-depth: scope every artifact by tenant as well as org (IDOR hardening). Older
        # rows predate the column, so add it idempotently rather than only on first create.
        cur.execute("ALTER TABLE design_artifacts ADD COLUMN IF NOT EXISTS tenant_id TEXT")
        c.commit()


def gallery(tenant_id: str, org_id: str) -> list:
    """Every design artifact for the (tenant, org), newest first: [{id, surface, title, html_path,
    status}]. Scoped by tenant_id AND org_id so a tenant never reads another tenant's artifacts."""
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT id, surface, title, html_path, status FROM design_artifacts
                       WHERE tenant_id=%s AND org_id=%s ORDER BY id DESC""", (tenant_id, org_id))
        return [{"id": r[0], "surface": r[1], "title": r[2], "html_path": r[3], "status": r[4]}
                for r in cur.fetchall()]


def decide(tenant_id: str, org_id: str, artifact_id, status: str) -> dict:
    """Set an artifact's status (e.g. review -> approved, or back to draft). Ownership via tenant+org
    match: the UPDATE only touches a row whose tenant_id AND org_id match, so neither another tenant
    nor another org can decide an artifact that isn't theirs."""
    _ensure()
    if status not in STATUSES:
        return {"ok": False, "error": f"status must be one of {STATUSES}"}
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("UPDATE design_artifacts SET status=%s WHERE id=%s AND tenant_id=%s AND org_id=%s",
                    (status, artifact_id, tenant_id, org_id))
        changed = cur.rowcount
        c.commit()
    if not changed:
        return {"ok": False, "error": "not found or not your artifact"}
    audit.append(actor="designview", action="DesignDecide", resource=str(artifact_id),
                 decision=status, payload={"tenant": tenant_id, "org": org_id,
                                           "artifact_id": artifact_id, "status": status})
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
    tenant_id = "t-selftest-" + uuid.uuid4().hex[:8]
    org_id = "o-selftest-" + uuid.uuid4().hex[:8]
    ok = False
    try:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            for s in ("cockpit", "team", "external"):
                cur.execute("""INSERT INTO design_artifacts (tenant_id, org_id, product, surface,
                                 title, html_path, status) VALUES (%s,%s,'demo',%s,%s,%s,'review')""",
                            (tenant_id, org_id, s, f"demo — {s} screen", f"/tmp/{org_id}-{s}.html"))
            c.commit()

        g = gallery(tenant_id, org_id)
        target = g[0]["id"]
        flip = decide(tenant_id, org_id, target, "approved")
        guard = decide(tenant_id, "o-not-mine", target, "draft")   # other org can't decide it
        xtenant = decide("t-not-mine", org_id, target, "draft")    # other tenant can't decide it
        xtenant_read = gallery("t-not-mine", org_id)               # other tenant can't read it
        bad = decide(tenant_id, org_id, target, "nonsense")        # invalid status rejected
        after = next(x for x in gallery(tenant_id, org_id) if x["id"] == target)
        sfc = surfaces()

        ok = (len(g) == 3
              and all(set(x) == {"id", "surface", "title", "html_path", "status"} for x in g)
              and flip.get("ok") is True
              and after["status"] == "approved"
              and guard.get("ok") is False
              and xtenant.get("ok") is False
              and xtenant_read == []
              and bad.get("ok") is False
              and len(sfc) == 3
              and {x["surface"] for x in sfc} == {"cockpit", "team", "external"})
        print(f"gallery={len(g)} decide={flip.get('ok')} approved={after['status']} "
              f"guard={guard.get('ok')} xtenant={xtenant.get('ok')} xtenant_read={len(xtenant_read)} "
              f"bad_status={bad.get('ok')} surfaces={len(sfc)}")
        print("PASS: designview gallery + tenant/org-scoped decide + surfaces ✅" if ok else "FAIL")
    finally:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM design_artifacts WHERE tenant_id=%s AND org_id=%s",
                        (tenant_id, org_id))
            c.commit()
    sys.exit(0 if ok else 1)


def _main(a):
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "gallery" and len(a) > 2:
        print(json.dumps(gallery(a[1], a[2]), indent=2))
    elif a[0] == "decide" and len(a) > 4:
        print(decide(a[1], a[2], int(a[3]), a[4]))
    elif a[0] == "surfaces":
        print(json.dumps(surfaces(), indent=2))
    else:
        sys.exit("usage: designview.py gallery <tenant_id> <org_id> | "
                 "decide <tenant_id> <org_id> <id> <status> | surfaces | selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
