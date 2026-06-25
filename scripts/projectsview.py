#!/usr/bin/env python3
"""projectsview.py — the tenant Projects/Products workspace (Area 4 of the factory).

The CEO/owner's portfolio: every product THIS tenant has built or is building, plus a drill-in on
any single one — its governed pipeline run (SPEC/BUILD/QA/REVIEW/LAUNCH), real spend/tokens, and the
actual repo on disk (files, tests, launch kit, lines of code). Strictly tenant-scoped via
tenant_products; project_detail refuses any product the caller doesn't own. Data/logic module only —
no web server. Composes with cockpit.py (fleet view) by zooming into the artifacts the cockpit summarises.

    projectsview.py json <tenant_id>     # list_projects payload on the CLI
    projectsview.py selftest
Run with the agent-os venv python.
"""
import json
import sys
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit    # noqa: E402,F401  (governance convention: every surface imports the audit chain)
import factory  # noqa: E402

ENV = Path.home() / "projects" / "agent-os" / ".env.local"
DB = next((l.split("=", 1)[1].strip() for l in ENV.read_text().splitlines()
           if l.strip().startswith("DATABASE_URL=")), None)
PIPELINE = ["SPEC", "BUILD", "QA", "REVIEW", "LAUNCH"]   # the governed line, for grouping
_SKIP = {".git", "__pycache__", "node_modules", ".venv", ".pytest_cache", ".mypy_cache"}


def _result(cur, product, has_stages):
    """Terminal decision for a product (LAUNCHED / BLOCKED_* / FAILED), or its in-flight status."""
    cur.execute("""SELECT decision FROM audit_log WHERE resource=%s AND action='ProductComplete'
                   ORDER BY id DESC LIMIT 1""", (product,))
    r = cur.fetchone()
    return r[0] if r else ("building" if has_stages else "queued")


def _repo_scan(product):
    """Inspect the on-disk repo: top-level names, tests/launch presence, approx python LOC, runnable."""
    repo = factory.PRODUCTS / product
    info = {"files": [], "has_tests": False, "has_launch_kit": False, "loc": 0, "runnable": False}
    if not repo.is_dir():
        return info
    names = []
    for p in sorted(repo.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
        if p.name in _SKIP:
            continue
        names.append(p.name + ("/" if p.is_dir() else ""))
    info["files"] = names[:40]
    low = {n.rstrip("/").lower() for n in names}
    info["has_tests"] = any(n.startswith("test") or n in ("tests/", "test/") for n in low) \
        or (repo / "tests").is_dir()
    info["has_launch_kit"] = (repo / "launch").is_dir() or "launch/" in [n.lower() for n in names]
    info["runnable"] = any((repo / m).exists() for m in ("main.py", "app.py", "run.py", "__main__.py")) \
        or any(n.lower().startswith("readme") for n in names)
    # approx total python lines across the tree (skip the noise dirs)
    loc = 0
    for py in repo.rglob("*.py"):
        if any(part in _SKIP for part in py.parts):
            continue
        try:
            loc += sum(1 for _ in py.open("r", errors="ignore"))
        except OSError:
            pass
    info["loc"] = loc
    return info


def list_projects(tid):
    """All of a tenant's products, newest first: result + readiness + progress + spend."""
    out = []
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT product FROM tenant_products WHERE tenant_id=%s ORDER BY created_at DESC", (tid,))
        prods = [r[0] for r in cur.fetchall()]
        for product in prods:
            cur.execute("""SELECT count(DISTINCT stage), sum(COALESCE(cost_usd,0)),
                                  sum(COALESCE(tokens_in,0)+COALESCE(tokens_out,0)), max(ts)
                           FROM traces WHERE product=%s AND kind='agent'""", (product,))
            n_stages, cost, toks, last = cur.fetchone()
            n_stages = int(n_stages or 0)
            result = _result(cur, product, n_stages > 0)
            out.append({
                "product": product,
                "result": result,
                "ready": result == "LAUNCHED",
                "failed": result not in ("LAUNCHED", "building", "queued"),
                "stages_done": n_stages,
                "cost_usd": round(float(cost or 0), 4),
                "tokens": int(toks or 0),
                "last_ts": last.strftime("%Y-%m-%d %H:%M:%S") if last else None,
            })
    return out


def project_detail(tid, product):
    """Ownership-checked drill-in: per-stage run, spend, on-disk artifacts. Refuses others' products."""
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT 1 FROM tenant_products WHERE tenant_id=%s AND product=%s", (tid, product))
        if not cur.fetchone():
            return {"error": "not your product"}
        cur.execute("""SELECT stage, role, rc, COALESCE(cost_usd,0), COALESCE(elapsed_s,0), ts,
                              COALESCE(tokens_in,0)+COALESCE(tokens_out,0)
                       FROM traces WHERE product=%s AND kind='agent' ORDER BY ts ASC""", (product,))
        rows = cur.fetchall()
        stages = []
        cost = toks = 0.0
        for stage, role, rc, cu, es, ts, tk in rows:
            grp = next((s for s in PIPELINE if stage and stage.upper().startswith(s)), stage)
            stages.append({"stage": grp, "role": role, "rc": rc,
                           "cost_usd": round(float(cu), 4), "elapsed_s": int(es),
                           "ts": ts.strftime("%Y-%m-%d %H:%M:%S") if ts else None})
            cost += float(cu)
            toks += int(tk)
        result = _result(cur, product, bool(rows))
    scan = _repo_scan(product)
    return {
        "product": product,
        "result": result,
        "stages": stages,
        "cost_usd": round(cost, 4),
        "tokens": int(toks),
        "files": scan["files"],
        "has_tests": scan["has_tests"],
        "has_launch_kit": scan["has_launch_kit"],
        "runnable": scan["runnable"],
        "loc": scan["loc"],
    }


def _selftest():
    """Real tenant + a product with SPEC/BUILD traces + a tiny on-disk repo; prove list + detail + ownership."""
    import shutil
    import billing
    tid = billing.signup("projects-selftest", "free")["tenant_id"]
    prod = tid.replace("t-", "")[:6] + "-projview"
    repo = factory.PRODUCTS / prod
    try:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("INSERT INTO tenant_products (product, tenant_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                        (prod, tid))
            for st, cost in (("SPEC", 0.12), ("BUILD", 0.88)):
                cur.execute("""INSERT INTO traces (run_id, product, stage, role, kind, rc, cost_usd,
                                                   tokens_in, tokens_out, elapsed_s, prompt, output, model)
                               VALUES (%s,%s,%s,'builder','agent',0,%s,1000,2000,20,'p','o','m')""",
                            (f"run-{prod}", prod, st, cost))
            c.commit()
        # tiny on-disk repo so files/loc/detail have something real to scan
        repo.mkdir(parents=True, exist_ok=True)
        (repo / "main.py").write_text("print('hello')\nx = 1\n")
        (repo / "README.md").write_text("# demo\n")

        lst = list_projects(tid)
        mine = next((p for p in lst if p["product"] == prod), None)
        det = project_detail(tid, prod)
        denied = project_detail(tid, "someone-elses-product")

        ok = (mine is not None and mine["stages_done"] == 2
              and abs(mine["cost_usd"] - 1.0) < 1e-6 and mine["tokens"] == 6000
              and len(det["stages"]) == 2
              and {s["stage"] for s in det["stages"]} == {"SPEC", "BUILD"}
              and abs(det["cost_usd"] - 1.0) < 1e-6
              and "main.py" in det["files"] and "README.md" in det["files"]
              and det["loc"] == 2 and det["runnable"] is True
              and denied == {"error": "not your product"})
        print(f"listed={mine is not None} stages_done={mine['stages_done'] if mine else None} "
              f"cost=${mine['cost_usd'] if mine else None} detail_stages={len(det['stages'])} "
              f"files={len(det['files'])} loc={det['loc']} ownership_block={denied.get('error')}")
        print("PASS: projects list + detail (stages/cost/files) + ownership guard ✅" if ok else "FAIL")
    finally:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM traces WHERE product=%s", (prod,))
            cur.execute("DELETE FROM tenant_products WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,))
            c.commit()
        shutil.rmtree(repo, ignore_errors=True)
    sys.exit(0 if ok else 1)


def _main(a):
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "json" and len(a) > 1:
        print(json.dumps(list_projects(a[1]), indent=2))
    elif a[0] == "detail" and len(a) > 2:
        print(json.dumps(project_detail(a[1], a[2]), indent=2))
    else:
        sys.exit("usage: projectsview.py json <tenant_id> | detail <tenant_id> <product> | selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
