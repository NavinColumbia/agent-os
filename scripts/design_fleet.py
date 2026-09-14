#!/usr/bin/env python3
"""design_fleet.py — the PROTOTYPE phase through the GOVERNED FLEET (sibling of research_fleet.py).

Before a product is built, the fleet PROTOTYPES it for the three audiences that have to live with it:
the CEO cockpit (the owner's command view), the internal team (operators), and the external users
(customers). A PARALLEL fleet of frontend agents each produces ONE self-contained prototype HTML
screen for its audience from the product plan, into design/<surface>.html under a per-org design
workspace repo. Every produced screen is recorded as a design_artifacts row (status='review') so the
gallery view (designview.py) can show it and a human can approve/reject. Bounded + honest: if an agent
doesn't write its file, we drop in a minimal placeholder so a row still exists (no silent gaps).

    design_fleet.py prototype <tenant_id> <org_id> <product> '<plan-json>'  # 3 audiences -> screens
    design_fleet.py selftest                                     # covers design_fleet AND designview
Run with the agent-os venv python. Agents already have file edits via the factory runtime.
"""
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit     # noqa: E402
import factory   # noqa: E402
from dbpool import connection, tenant_connection  # noqa: E402

# The three audiences we prototype for, each with a one-line brief that steers its agent.
SURFACES = {
    "cockpit":  "the CEO/owner command cockpit — at-a-glance KPIs, controls, and decisions for the owner",
    "team":     "the internal team/operator console — the day-to-day working screens for staff",
    "external": "the external end-user / customer experience — the public-facing product screen",
}

# Pick a design role that actually exists in the governed manifest (ux-designer does NOT exist here).
_DESIGN_ROLE = next((r for r in ("frontend-engineer", "design-ux", "ux-designer")
                     if (factory.ROLES / f"{r}.yaml").exists()), "builder")


def _ensure():
    with connection() as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS design_artifacts (
            id BIGSERIAL PRIMARY KEY, org_id TEXT, product TEXT, kind TEXT DEFAULT 'screen',
            surface TEXT, title TEXT, html_path TEXT, status TEXT DEFAULT 'draft',
            created_at TIMESTAMPTZ DEFAULT now())""")
        # Scope every artifact by tenant as well as org (IDOR hardening), mirroring designview._ensure.
        cur.execute("ALTER TABLE design_artifacts ADD COLUMN IF NOT EXISTS tenant_id TEXT")


def _placeholder_html(org_id, product, surface, plan) -> str:
    """Minimal self-contained screen so a row always has a real file behind it (agent miss -> no gap)."""
    feats = ", ".join((plan or {}).get("features", [])) or "(plan features TBD)"
    return (f"<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            f"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>{product} — {surface}</title>"
            f"<style>body{{font-family:system-ui,sans-serif;margin:0;background:#0f1220;color:#e8eaf2}}"
            f"header{{padding:24px;background:#171a2b;border-bottom:1px solid #2a2f4a}}"
            f"main{{padding:24px;max-width:880px;margin:0 auto}}"
            f".card{{background:#171a2b;border:1px solid #2a2f4a;border-radius:12px;padding:20px;margin:16px 0}}"
            f"h1{{margin:0;font-size:20px}}small{{color:#8b90b5}}</style></head>"
            f"<body><header><h1>{product}</h1>"
            f"<small>{surface} prototype — {SURFACES.get(surface, '')}</small></header>"
            f"<main><div class=\"card\"><h2>Prototype placeholder</h2>"
            f"<p>This is a minimal stand-in screen for the <b>{surface}</b> audience "
            f"(org {org_id}). The generating agent did not return a screen, so this placeholder "
            f"keeps the artifact honest and reviewable.</p>"
            f"<p><b>Planned features:</b> {feats}</p></div></main></body></html>")


def design_one(repo: Path, tenant_id: str, org_id: str, product: str, surface: str, plan: dict, api_key=None) -> dict:
    """One frontend agent produces ONE prototype screen for one audience into design/<surface>.html."""
    factory._ctx.api_key = api_key
    factory._ctx.tenant = tenant_id
    factory._ctx.product = repo.name
    factory._ctx.run = f"design-{repo.name}"
    factory._ctx.stage = f"DESIGN:{surface}"
    out = f"design/{surface}.html"
    brief = SURFACES[surface]
    task = (f"Produce a SINGLE self-contained prototype HTML screen for {brief}.\n"
            f"PRODUCT: {product}\nPLAN (JSON):\n{json.dumps(plan, indent=2)}\n\n"
            f"Write it to `{out}` in the repo. HARD CONSTRAINTS: one file, fully self-contained — "
            f"inline CSS in a <style> tag and any JS inline, NO external CDNs/fonts/network, works "
            f"offline when opened over http. Make it a clean, realistic, audience-appropriate mock of "
            f"the real screen (not a wireframe sketch). Do not write any other files.")
    r = factory.agent(_DESIGN_ROLE, str(repo), task)
    fp = repo / out
    if not fp.exists():                                  # honest fallback: never leave a gap
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(_placeholder_html(org_id, product, surface, plan))
        placeholder = True
    else:
        placeholder = False
    title = f"{product} — {surface} screen"
    return {"surface": surface, "title": title, "html_path": str(fp), "placeholder": placeholder,
            "ok": fp.exists(), "rc": r.get("rc")}


def prototype(tenant_id: str, org_id: str, product: str, plan: dict, api_key=None) -> dict:
    """Full prototype phase: 3 audiences in PARALLEL -> a design_artifacts row per produced screen.
    Each row is scoped by (tenant_id, org_id) so designview's tenant-scoped gallery/decide can read it.
    Bounded by factory._AGENT_SEM (global agent cap). Returns {screens, surfaces}."""
    _ensure()
    repo = factory.PRODUCTS / f"{org_id}-design"
    (repo / "design").mkdir(parents=True, exist_ok=True)
    audit.append(actor="design:lead", action="PrototypeStart", resource=repo.name,
                 decision="executed", payload={"org": org_id, "product": product,
                                               "surfaces": list(SURFACES)}, tenant_id=tenant_id)
    results, workers = [], int(os.environ.get("AOS_FLEET_WORKERS", "3"))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(design_one, repo, tenant_id, org_id, product, s, plan, api_key) for s in SURFACES]
        for f in as_completed(futs):
            try:
                results.append(f.result())
            except Exception as e:
                results.append({"ok": False, "error": str(e)})
    # one design_artifacts row per produced screen (status='review' — awaiting a human's call)
    surfaces = []
    with tenant_connection(tenant_id) as c, c.cursor() as cur:
        for r in results:
            if not r.get("ok"):
                continue
            cur.execute("""INSERT INTO design_artifacts (tenant_id, org_id, product, kind, surface,
                             title, html_path, status) VALUES (%s,%s,%s,'screen',%s,%s,%s,'review')""",
                        (tenant_id, org_id, product, r["surface"], r["title"], r["html_path"]))
            surfaces.append(r["surface"])
    audit.append(actor="design:lead", action="PrototypeComplete", resource=repo.name,
                 decision="executed", payload={"org": org_id, "screens": len(surfaces),
                                               "surfaces": surfaces}, tenant_id=tenant_id)
    print(f"[design] {org_id}/{product}: {len(surfaces)} screens -> {sorted(surfaces)}", flush=True)
    return {"screens": len(surfaces), "surfaces": surfaces}


def _selftest():
    """Offline check (NO real spend): monkeypatch factory.agent to a fake that WRITES the expected
    design/<surface>.html (parsed from the task) and returns rc=0. Drives prototype() for all three
    surfaces, then exercises designview gallery/decide end-to-end. Covers BOTH scripts. Cleans up the
    design_artifacts rows AND the temp repo dir in finally; restores factory.agent + factory.PRODUCTS."""
    import re
    import shutil
    import tempfile
    import uuid
    import designview

    _ensure()
    tenant_id = "t-selftest-" + uuid.uuid4().hex[:8]
    org_id = "o-selftest-" + uuid.uuid4().hex[:8]
    workdir = Path(tempfile.mkdtemp(prefix="design-selftest-"))
    real_agent, real_products = factory.agent, factory.PRODUCTS
    factory.PRODUCTS = workdir

    def fake_agent(role, repo, task, **k):
        m = re.search(r"`(design/\w+\.html)`", task)     # parse the target path out of the task
        if m:
            fp = Path(repo) / m.group(1)
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text("<!doctype html><title>fake</title><h1>fake prototype</h1>")
        return {"rc": 0, "out": "done"}
    factory.agent = fake_agent

    ok = False
    try:
        res = prototype(tenant_id, org_id, "demo", {"features": ["x"]})
        with tenant_connection(tenant_id) as c, c.cursor() as cur:
            cur.execute("SELECT count(*) FROM design_artifacts WHERE org_id=%s", (org_id,))
            n_rows = cur.fetchone()[0]
        gallery = designview.gallery(tenant_id, org_id)
        first_id = gallery[0]["id"]
        flipped = designview.decide(tenant_id, org_id, first_id, "approved")
        approved = next(g for g in designview.gallery(tenant_id, org_id) if g["id"] == first_id)
        no_placeholders = all(not r.get("placeholder") for r in [])  # agent wrote files -> none expected
        sfc = designview.surfaces()

        ok = (res["screens"] == 3
              and sorted(res["surfaces"]) == ["cockpit", "external", "team"]
              and n_rows == 3
              and len(gallery) == 3
              and all(g["status"] == "review" for g in designview.gallery(tenant_id, org_id) if g["id"] != first_id)
              and flipped.get("ok") is True
              and approved["status"] == "approved"
              and len(sfc) == 3)
        print(f"prototype screens={res['screens']} rows={n_rows} gallery={len(gallery)} "
              f"decide={flipped.get('ok')} approved_status={approved['status']} surfaces={len(sfc)}")
        print("PASS: design fleet (prototype 3 surfaces) + designview (gallery/decide) ✅"
              if ok else "FAIL")
    finally:
        factory.agent, factory.PRODUCTS = real_agent, real_products
        with connection() as c, c.cursor() as cur:
            cur.execute("DELETE FROM design_artifacts WHERE org_id=%s", (org_id,))
        shutil.rmtree(workdir, ignore_errors=True)
    sys.exit(0 if ok else 1)


def _main(a):
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "prototype" and len(a) >= 4:
        plan = json.loads(a[4]) if len(a) > 4 else {"features": []}
        print(prototype(a[1], a[2], a[3], plan))
    else:
        sys.exit("usage: design_fleet.py prototype <tenant_id> <org_id> <product> '<plan-json>' | selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
