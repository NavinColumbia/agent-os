#!/usr/bin/env python3
"""designview.py — the prototype GALLERY + approval, for an org's design artifacts (sibling of the
other *view.py read/decide surfaces). The design fleet (design_fleet.py) prototypes three audience
screens (cockpit / team / external); designview lists them for the org and lets a human approve or
send one back to draft. Everything is org-scoped: you only ever see/decide YOUR org's artifacts.

    designview.py gallery <tenant_id> <org_id>                       # reviewable artifacts (html + preview + desc)
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

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit  # noqa: E402

from dbpool import connection, tenant_connection  # noqa: E402

STATUSES = ("draft", "review", "approved")
_SURFACES = {
    "cockpit":  "the CEO/owner command cockpit — KPIs, controls, and decisions for the owner",
    "team":     "the internal team/operator console — the day-to-day working screens for staff",
    "external": "the external end-user / customer experience — the public-facing product screen",
}


def _ensure():
    with connection() as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS design_artifacts (
            id BIGSERIAL PRIMARY KEY, org_id TEXT, product TEXT, kind TEXT DEFAULT 'screen',
            surface TEXT, title TEXT, html_path TEXT, status TEXT DEFAULT 'draft',
            created_at TIMESTAMPTZ DEFAULT now())""")
        # Defense-in-depth: scope every artifact by tenant as well as org (IDOR hardening). Older
        # rows predate the column, so add it idempotently rather than only on first create.
        cur.execute("ALTER TABLE design_artifacts ADD COLUMN IF NOT EXISTS tenant_id TEXT")


_PREVIEW_CHARS = 4000   # cap the inlined content so the gallery payload stays sane for many artifacts


def _load_artifact(html_path, surface, title, product):
    """Turn a stored html_path into something a human can actually REVIEW. The design fleet writes each
    prototype as a self-contained HTML file on disk (inline CSS/JS, no network), so the reviewable
    content IS that file. Read it back and return the renderable body plus a plain-text preview; if the
    file is missing/unreadable, fall back to a description of the surface so the row is never a dead
    link the user 'can't click to review'."""
    surface_desc = _SURFACES.get(surface, "")
    p = Path(html_path) if html_path else None
    if p and p.exists() and p.is_file():
        try:
            html = p.read_text(errors="replace")
        except OSError as e:
            html = None
            err = str(e)
        else:
            err = None
        if html is not None:
            # strip tags for a quick text preview the console can show even without an HTML renderer
            import re
            text = re.sub(r"<[^>]+>", " ", html)
            text = re.sub(r"\s+", " ", text).strip()
            return {
                "reviewable": True,
                "render": "html",
                "html": html,                                   # full self-contained doc -> iframe/srcdoc
                "preview_text": text[:600],                     # tag-stripped snippet for text-only views
                "truncated_html": len(html) > _PREVIEW_CHARS,
                "html_excerpt": html[:_PREVIEW_CHARS],          # bounded slice for cheap previews
                "bytes": len(html),
                "surface_description": surface_desc,
                "description": (f"{title} — a self-contained prototype screen for the {surface} audience "
                                f"({surface_desc}). Open/render the html field to review the design."),
            }
        # file exists but couldn't be read
        return {
            "reviewable": False, "render": "none", "html": None,
            "surface_description": surface_desc,
            "description": (f"{title} — prototype for the {surface} audience ({surface_desc}), but its "
                            f"file could not be read: {err}. Path: {html_path}"),
        }
    # no file on disk (e.g. metadata-only row): return the fullest description we can so it's reviewable
    return {
        "reviewable": False, "render": "none", "html": None,
        "surface_description": surface_desc,
        "description": (f"{title} — a planned prototype screen for the {surface} audience "
                        f"({surface_desc}). No rendered file is available at {html_path or '(none)'}; "
                        f"this artifact describes the surface to review rather than a rendered mock."),
    }


def gallery(tenant_id: str, org_id: str) -> list:
    """Every design artifact for the (tenant, org), newest first — each entry carries enough to actually
    REVIEW the design, not just a title + status. Alongside {id, surface, title, html_path, status} we
    include the renderable prototype content (html), a tag-stripped preview_text, a human description of
    the surface, and a `reviewable` flag, by reading the self-contained HTML the design fleet wrote to
    disk. Scoped by tenant_id AND org_id so a tenant never reads another tenant's artifacts."""
    _ensure()
    with tenant_connection(tenant_id) as c, c.cursor() as cur:
        cur.execute("""SELECT id, surface, title, html_path, status, product FROM design_artifacts
                       WHERE tenant_id=%s AND org_id=%s ORDER BY id DESC""", (tenant_id, org_id))
        rows = cur.fetchall()
    out = []
    for r in rows:
        art = {"id": r[0], "surface": r[1], "title": r[2], "html_path": r[3], "status": r[4]}
        art.update(_load_artifact(r[3], r[1], r[2], r[5]))
        out.append(art)
    return out


def decide(tenant_id: str, org_id: str, artifact_id, status: str) -> dict:
    """Set an artifact's status (e.g. review -> approved, or back to draft). Ownership via tenant+org
    match: the UPDATE only touches a row whose tenant_id AND org_id match, so neither another tenant
    nor another org can decide an artifact that isn't theirs."""
    _ensure()
    if status not in STATUSES:
        return {"ok": False, "error": f"status must be one of {STATUSES}"}
    with tenant_connection(tenant_id) as c, c.cursor() as cur:
        cur.execute("UPDATE design_artifacts SET status=%s WHERE id=%s AND tenant_id=%s AND org_id=%s",
                    (status, artifact_id, tenant_id, org_id))
        changed = cur.rowcount
    if not changed:
        return {"ok": False, "error": "not found or not your artifact"}
    audit.append(actor="designview", action="DesignDecide", resource=str(artifact_id),
                 decision=status, payload={"tenant": tenant_id, "org": org_id,
                                           "artifact_id": artifact_id, "status": status},
                 tenant_id=tenant_id)
    return {"ok": True}


def surfaces() -> list:
    """The three audiences we prototype for, each with a one-line description."""
    return [{"surface": s, "description": d} for s, d in _SURFACES.items()]


def _selftest():
    """Insert three fake artifacts for a throwaway org (design_artifacts.org_id is just text — no real
    org row needed), then prove gallery() lists them, decide() flips one to approved with ownership
    enforced, and surfaces() returns the three audiences. Cleans up the rows in finally."""
    import shutil
    import tempfile
    import uuid
    _ensure()
    tenant_id = "t-selftest-" + uuid.uuid4().hex[:8]
    org_id = "o-selftest-" + uuid.uuid4().hex[:8]
    workdir = Path(tempfile.mkdtemp(prefix="designview-selftest-"))
    ok = False
    try:
        # Two artifacts with a REAL self-contained html file on disk (the reviewable prototype), plus
        # one metadata-only row whose file is missing — to prove gallery() returns reviewable content
        # for real prototypes AND a full description (never a dead link) when no file exists.
        real = {}
        with connection() as c, c.cursor() as cur:
            for s in ("cockpit", "team"):
                fp = workdir / f"{s}.html"
                fp.write_text(f"<!doctype html><html><head><title>demo {s}</title></head>"
                              f"<body><h1>{s} prototype</h1><p>reviewable content for {s}</p>"
                              f"</body></html>")
                real[s] = str(fp)
                cur.execute("""INSERT INTO design_artifacts (tenant_id, org_id, product, surface,
                                 title, html_path, status) VALUES (%s,%s,'demo',%s,%s,%s,'review')""",
                            (tenant_id, org_id, s, f"demo — {s} screen", str(fp)))
            # metadata-only: file intentionally absent
            cur.execute("""INSERT INTO design_artifacts (tenant_id, org_id, product, surface,
                             title, html_path, status) VALUES (%s,%s,'demo',%s,%s,%s,'review')""",
                        (tenant_id, org_id, "external", "demo — external screen",
                         str(workdir / "missing.html")))

        g = gallery(tenant_id, org_id)
        target = g[0]["id"]
        flip = decide(tenant_id, org_id, target, "approved")
        guard = decide(tenant_id, "o-not-mine", target, "draft")   # other org can't decide it
        xtenant = decide("t-not-mine", org_id, target, "draft")    # other tenant can't decide it
        xtenant_read = gallery("t-not-mine", org_id)               # other tenant can't read it
        bad = decide(tenant_id, org_id, target, "nonsense")        # invalid status rejected
        after = next(x for x in gallery(tenant_id, org_id) if x["id"] == target)
        sfc = surfaces()

        base_keys = {"id", "surface", "title", "html_path", "status"}
        review_keys = {"reviewable", "render", "html", "surface_description", "description"}
        # every entry is reviewable: renderable html for real files, full description for the missing one
        rendered = [x for x in g if x["surface"] in ("cockpit", "team")]
        metaonly = next(x for x in g if x["surface"] == "external")
        ok = (len(g) == 3
              and all(base_keys | review_keys <= set(x) for x in g)               # base + review payload
              and len(rendered) == 2
              and all(x["reviewable"] is True and x["render"] == "html"
                      and "prototype" in (x["html"] or "") and x["preview_text"]
                      and x["surface_description"] and x["description"] for x in rendered)
              and metaonly["reviewable"] is False and metaonly["html"] is None
              and metaonly["surface_description"] and metaonly["description"]     # still reviewable text
              and flip.get("ok") is True
              and after["status"] == "approved"
              and guard.get("ok") is False
              and xtenant.get("ok") is False
              and xtenant_read == []
              and bad.get("ok") is False
              and len(sfc) == 3
              and {x["surface"] for x in sfc} == {"cockpit", "team", "external"})
        print(f"gallery={len(g)} rendered={len(rendered)} metaonly_reviewable={metaonly['reviewable']} "
              f"decide={flip.get('ok')} approved={after['status']} "
              f"guard={guard.get('ok')} xtenant={xtenant.get('ok')} xtenant_read={len(xtenant_read)} "
              f"bad_status={bad.get('ok')} surfaces={len(sfc)}")
        print("PASS: designview reviewable gallery + tenant/org-scoped decide + surfaces ✅"
              if ok else "FAIL")
    finally:
        with connection() as c, c.cursor() as cur:
            cur.execute("DELETE FROM design_artifacts WHERE tenant_id=%s AND org_id=%s",
                        (tenant_id, org_id))
        shutil.rmtree(workdir, ignore_errors=True)
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
