#!/usr/bin/env python3
"""traceview.py — tenant Monitoring/Observability + trace explorer (Area 6).

The owner-facing observability surface: a tenant gets aggregate health over the products it owns
(runs, error rate, spend, tokens, per-stage breakdown, recent failures), the list of its runs, and a
secret-redacted, ownership-checked step-by-step replay of any single run. Data/logic module only — no
web server; pair it with a UI surface (cockpit/dashboard) that renders these payloads.

Everything is scoped to the caller's products via tenant_products; replay() refuses runs the tenant
doesn't own. Anything surfaced from prompt/output is run through redact.scrub so persisted secrets
(api keys, tokens, db urls) never reach a UI or a customer.

    traceview.py json <tenant_id>     # overview + runs (CLI dump)
    traceview.py selftest
Run with the agent-os venv python.
"""
import json
import sys
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit  # noqa: E402,F401  (factory convention: audit trail module on path)

from aoscfg import ENV, DB

# Redaction: reuse the canonical scrubber if present, else a basic fallback for common secret shapes.
try:
    import redact  # noqa: E402

    def _scrub(text):
        return redact.scrub(text)
except Exception:  # pragma: no cover - fallback only if redact.py is missing
    import re
    _MASK = "‹REDACTED›"
    _PATS = [
        re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),
        re.compile(r"\baos_[A-Fa-f0-9]{16,}\b"),
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{10,}"),
    ]

    def _scrub(text):
        if not text:
            return text
        out = text
        for p in _PATS:
            out = p.sub(_MASK, out)
        return out


def _snip(text, n):
    """Truncate + redact a prompt/output blob for safe display."""
    if not text:
        return ""
    return _scrub(text[:n])


def overview(tid):
    """Aggregate observability across the tenant's owned products."""
    with psycopg.connect(DB) as c, c.cursor() as cur:
        # headline aggregates (scoped to owned products via the join)
        cur.execute("""SELECT count(DISTINCT t.run_id), count(*),
                          count(*) FILTER (WHERE t.rc IS NOT NULL AND t.rc<>0),
                          sum(COALESCE(t.cost_usd,0)),
                          sum(COALESCE(t.tokens_in,0)+COALESCE(t.tokens_out,0))
                       FROM traces t JOIN tenant_products tp ON tp.product=t.product
                       WHERE tp.tenant_id=%s""", (tid,))
        runs_n, steps_n, errs, cost, toks = cur.fetchone()
        # per-stage breakdown
        cur.execute("""SELECT t.stage, count(*),
                          count(*) FILTER (WHERE t.rc IS NOT NULL AND t.rc<>0),
                          sum(COALESCE(t.cost_usd,0)), avg(COALESCE(t.elapsed_s,0))
                       FROM traces t JOIN tenant_products tp ON tp.product=t.product
                       WHERE tp.tenant_id=%s GROUP BY t.stage ORDER BY count(*) DESC""", (tid,))
        by_stage = [{"stage": s, "steps": n, "errors": e,
                     "cost_usd": round(float(co or 0), 4), "avg_elapsed_s": round(float(av or 0), 2)}
                    for s, n, e, co, av in cur.fetchall()]
        # latest failures with a redacted snippet of their output
        cur.execute("""SELECT t.product, t.stage, t.run_id, t.ts, t.output
                       FROM traces t JOIN tenant_products tp ON tp.product=t.product
                       WHERE tp.tenant_id=%s AND t.rc IS NOT NULL AND t.rc<>0
                       ORDER BY t.ts DESC LIMIT 10""", (tid,))
        recent_errors = [{"product": p, "stage": st, "run_id": rid,
                          "ts": ts.isoformat() if ts else None, "snippet": _snip(o, 160)}
                         for p, st, rid, ts, o in cur.fetchall()]
        # recent activity TIMELINE — the fleet's real work as it happens (which agent did what), success
        # AND fail, so the CEO can WATCH their company work, not just read error rollups. Tenant-scoped.
        cur.execute("""SELECT t.product, t.stage, t.role, t.ts, t.rc
                       FROM traces t JOIN tenant_products tp ON tp.product=t.product
                       WHERE tp.tenant_id=%s AND t.kind='agent'
                       ORDER BY t.ts DESC LIMIT 15""", (tid,))
        recent_activity = [{"product": p, "stage": st, "role": role,
                            "ts": ts.isoformat() if ts else None, "ok": (rc == 0 or rc is None)}
                           for p, st, role, ts, rc in cur.fetchall()]
    return {
        "runs": runs_n or 0,
        "steps": steps_n or 0,
        "errors": errs or 0,
        "cost_usd": round(float(cost or 0), 4),
        "tokens": int(toks or 0),
        "by_stage": by_stage,
        "recent_errors": recent_errors,
        "recent_activity": recent_activity,
    }


def runs(tid):
    """The tenant's runs grouped by run_id (owned products only), newest first, limit 50."""
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT t.run_id, min(t.product), count(DISTINCT t.stage),
                          sum(COALESCE(t.cost_usd,0)),
                          sum(COALESCE(t.tokens_in,0)+COALESCE(t.tokens_out,0)),
                          min(t.ts), max(t.ts),
                          bool_and(t.rc IS NULL OR t.rc=0)
                       FROM traces t JOIN tenant_products tp ON tp.product=t.product
                       WHERE tp.tenant_id=%s
                       GROUP BY t.run_id ORDER BY max(t.ts) DESC LIMIT 50""", (tid,))
        return [{"run_id": rid, "product": prod, "stages": stages,
                 "cost_usd": round(float(co or 0), 4), "tokens": int(tk or 0),
                 "started": a.isoformat() if a else None, "ended": b.isoformat() if b else None,
                 "ok": bool(ok)}
                for rid, prod, stages, co, tk, a, b, ok in cur.fetchall()]


def replay(tid, run_id):
    """Ownership-checked step-by-step timeline for one run, with redacted prompts/outputs."""
    with psycopg.connect(DB) as c, c.cursor() as cur:
        # ownership: the run's product(s) must belong to this tenant
        cur.execute("""SELECT 1 FROM traces t
                       JOIN tenant_products tp ON tp.product=t.product
                       WHERE tp.tenant_id=%s AND t.run_id=%s LIMIT 1""", (tid, run_id))
        if not cur.fetchone():
            return {"error": "not your run"}
        cur.execute("""SELECT stage, role, rc, cost_usd, tokens_in, tokens_out, elapsed_s, ts,
                          model, prompt, output
                       FROM traces WHERE run_id=%s ORDER BY ts""", (run_id,))
        return [{"stage": st, "role": r, "rc": rc,
                 "cost_usd": round(float(co or 0), 4) if co is not None else None,
                 "tokens_in": ti, "tokens_out": to, "elapsed_s": e,
                 "ts": ts.isoformat() if ts else None, "model": m,
                 "prompt": _snip(pr, 400), "output": _snip(o, 800)}
                for st, r, rc, co, ti, to, e, ts, m, pr, o in cur.fetchall()]


def _selftest():
    """Real tenant + product + a one-run mix of ok/error traces; prove counts, listing, replay, redaction, isolation."""
    import billing
    reg = billing.signup("trace-selftest", "free")   # REAL tenant (tenant_products FKs to tenants)
    tid = reg["tenant_id"]
    prod = tid.replace("t-", "")[:6] + "-obs"
    run = f"run-{prod}"
    foreign_run = f"foreign-{prod}"
    foreign_prod = "not-" + prod
    ok = False
    try:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("INSERT INTO tenant_products (product, tenant_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                        (prod, tid))
            # two healthy stages + one failure whose output embeds a fake secret to prove redaction
            cur.execute("""INSERT INTO traces (run_id,product,stage,role,kind,rc,cost_usd,tokens_in,tokens_out,elapsed_s,prompt,output,model)
                           VALUES (%s,%s,'SPEC','planner','agent',0,0.10,500,800,12,'plan it','spec ok','m')""",
                        (run, prod))
            cur.execute("""INSERT INTO traces (run_id,product,stage,role,kind,rc,cost_usd,tokens_in,tokens_out,elapsed_s,prompt,output,model)
                           VALUES (%s,%s,'BUILD','builder','agent',0,0.50,1000,2000,40,'build it','built','m')""",
                        (run, prod))
            cur.execute("""INSERT INTO traces (run_id,product,stage,role,kind,rc,cost_usd,tokens_in,tokens_out,elapsed_s,prompt,output,model)
                           VALUES (%s,%s,'QA','tester','test',1,0.00,0,0,5,'run tests',%s,'m')""",
                        (run, prod, "FAILED: leaked key sk-SECRET123456789012 in config"))
            # a run owned by NOBODY this tenant owns — for the isolation check
            cur.execute("""INSERT INTO traces (run_id,product,stage,role,kind,rc,prompt,output)
                           VALUES (%s,%s,'SPEC','planner','agent',0,'p','o')""", (foreign_run, foreign_prod))
            c.commit()

        ov = overview(tid)
        rs = runs(tid)
        tl = replay(tid, run)
        foreign = replay(tid, foreign_run)

        secret_in_replay = any("sk-SECRET123" in (s.get("output") or "") for s in tl)
        error_seen = any("sk-SECRET123" in (e["snippet"] or "") for e in ov["recent_errors"])

        checks = {
            "overview_counts_error": ov["errors"] == 1,
            "overview_runs": ov["runs"] == 1,
            "overview_cost": ov["cost_usd"] == 0.60,
            "overview_by_stage": len(ov["by_stage"]) == 3,
            "runs_lists_run": any(r["run_id"] == run and r["ok"] is False for r in rs),
            "replay_timeline_len": len(tl) == 3,
            "replay_ordered": [s["stage"] for s in tl] == ["SPEC", "BUILD", "QA"],
            "secret_masked_in_replay": not secret_in_replay,
            "secret_masked_in_overview": not error_seen,
            "foreign_run_blocked": foreign == {"error": "not your run"},
        }
        ok = all(checks.values())
        for k, v in checks.items():
            print(f"  {'ok ' if v else 'FAIL'} {k}")
        print(f"overview={ {kk: ov[kk] for kk in ('runs','steps','errors','cost_usd','tokens')} }")
        print(f"replay QA output: {next((s['output'] for s in tl if s['stage']=='QA'), '')!r}")
        print("PASS: tenant observability overview+runs+replay+redaction+isolation ✅" if ok else "FAIL")
    finally:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM traces WHERE product IN (%s,%s)", (prod, foreign_prod))
            cur.execute("DELETE FROM tenant_products WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,))
            c.commit()
    sys.exit(0 if ok else 1)


def _main(a):
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "json" and len(a) > 1:
        print(json.dumps({"overview": overview(a[1]), "runs": runs(a[1])}, indent=2))
    else:
        sys.exit("usage: traceview.py json <tenant_id> | selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
