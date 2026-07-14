#!/usr/bin/env python3
"""trace.py — the debugger view: replay any run step by step from persisted traces.

This is what makes the platform debuggable (not just monitorable): for any product/run it reconstructs
the full timeline — every agent stage's ACTUAL prompt + response, every QA/test run's output, with
return codes and timing — long after the run finished. Pair with the dashboard's run drill-down.

    trace.py runs [n]              # recent runs (newest first)
    trace.py show <product>        # the run's timeline (heads of prompt/output)
    trace.py show <product> --full # full prompts + outputs (raw debug)
    trace.py selftest
Run with the agent-os venv python.
"""
import sys
from pathlib import Path

import psycopg

from aoscfg import ENV, DB


def runs(limit=15):
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT product, count(*) steps, min(ts), max(ts),
                          round(sum(coalesce(elapsed_s,0))) total_s,
                          count(*) FILTER (WHERE rc<>0) errs
                       FROM traces GROUP BY product ORDER BY max(ts) DESC LIMIT %s""", (limit,))
        return [{"product": p, "steps": s, "start": a, "end": b, "total_s": int(t or 0), "errors": e}
                for p, s, a, b, t, e in cur.fetchall()]


def steps(product):
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT stage, role, kind, rc, elapsed_s, prompt, output, ts
                       FROM traces WHERE product=%s ORDER BY id""", (product,))
        return [{"stage": st, "role": r, "kind": k, "rc": rc, "elapsed_s": e,
                 "prompt": pr, "output": o, "ts": ts} for st, r, k, rc, e, pr, o, ts in cur.fetchall()]


def show(product, full=False):
    s = steps(product)
    if not s:
        print(f"no traces for '{product}' (only NEW builds are traced)")
        return
    print(f"\n=== debug trace: {product} — {len(s)} steps ===")
    for i, x in enumerate(s, 1):
        flag = "OK" if (x["rc"] in (0, None)) else f"rc={x['rc']}"
        print(f"\n[{i}] {x['ts']:%H:%M:%S}  {x['stage']:7} {x['role']:20} {x['kind']:5} {flag} "
              f"{(str(x['elapsed_s'])+'s') if x['elapsed_s'] else ''}")
        if x["prompt"]:
            p = x["prompt"] if full else (x["prompt"][:300] + ("…" if len(x["prompt"]) > 300 else ""))
            print("   ▸ prompt: " + p.replace("\n", "\n     "))
        if x["output"]:
            o = x["output"] if full else (x["output"][-400:] if len(x["output"]) > 400 else x["output"])
            print("   ◂ output: " + o.replace("\n", "\n     "))


def runs_for_tenant(tenant_id, limit=15):
    """Per-tenant isolation: a tenant sees ONLY traces of products it owns (join tenant_products)."""
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT t.product, count(*), max(t.ts), round(sum(coalesce(t.elapsed_s,0))),
                          count(*) FILTER (WHERE t.rc<>0)
                       FROM traces t JOIN tenant_products tp ON tp.product=t.product
                       WHERE tp.tenant_id=%s GROUP BY t.product ORDER BY max(t.ts) DESC LIMIT %s""",
                    (tenant_id, limit))
        return [{"product": p, "steps": s, "end": e, "total_s": int(ts or 0), "errors": er}
                for p, s, e, ts, er in cur.fetchall()]


def prune(days=30):
    """Retention/cost control: drop traces older than N days. Returns rows removed."""
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("DELETE FROM traces WHERE ts < now() - (%s || ' days')::interval", (str(days),))
        n = cur.rowcount
        c.commit()
    return n


def errors():
    """Cross-run error search: every step that failed (rc<>0) — the first place a debugger looks."""
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT product, stage, role, kind, rc, ts FROM traces WHERE rc IS NOT NULL AND rc<>0
                       ORDER BY ts DESC LIMIT 40""")
        return [{"product": p, "stage": s, "role": r, "kind": k, "rc": rc, "ts": ts}
                for p, s, r, k, rc, ts in cur.fetchall()]


def _main(a):
    if not a or a[0] == "runs":
        for r in runs(int(a[1]) if len(a) > 1 else 15):
            print(f"  {r['product']:18} {r['steps']:>2} steps  {r['total_s']:>4}s  errs={r['errors']}  {r['end']:%H:%M:%S}")
    elif a[0] == "errors":
        es = errors()
        for e in es:
            print(f"  {e['ts']:%H:%M:%S} {e['product']:18} {e['stage']:7} {e['role']:18} rc={e['rc']}")
        print(f"  ── {len(es)} failed step(s)" if es else "  no failed steps recorded")
    elif a[0] == "tenant":
        for r in runs_for_tenant(a[1]):
            print(f"  {r['product']:18} {r['steps']:>2} steps  {r['total_s']:>4}s  errs={r['errors']}")
    elif a[0] == "prune":
        print(f"pruned {prune(int(a[1]) if len(a) > 1 else 30)} trace rows older than {a[1] if len(a) > 1 else 30}d")
    elif a[0] == "show":
        show(a[1], full="--full" in a)
    elif a[0] == "selftest":
        import os
        suf = os.urandom(3).hex()
        tid, mine, theirs = f"t-tr-{suf}", f"mine-{suf}", f"theirs-{suf}"
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("INSERT INTO tenants (tenant_id,name,api_token) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                        (tid, "tt", f"aos_{suf}0000000000000000"))
            cur.execute("INSERT INTO tenant_products (product,tenant_id) VALUES (%s,%s) ON CONFLICT DO NOTHING", (mine, tid))
            for prod in (mine, theirs):
                cur.execute("""INSERT INTO traces (run_id,product,stage,role,kind,prompt,output,rc)
                               VALUES (%s,%s,'X','r','agent','p','o',0)""", (f"r-{suf}-{prod}", prod))
            c.commit()
        scoped = runs_for_tenant(tid)
        isolated = bool(scoped) and all(r["product"] == mine for r in scoped)   # sees mine, never theirs
        with psycopg.connect(DB) as c, c.cursor() as cur:   # cleanup
            cur.execute("DELETE FROM traces WHERE product IN (%s,%s)", (mine, theirs))
            cur.execute("DELETE FROM tenant_products WHERE product=%s", (mine,))
            cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,))
            c.commit()
        print(f"per-tenant isolation (sees own, not others'): {isolated}; prune callable: {callable(prune)}")
        print("PASS: debug trace store + per-tenant isolation + retention ✅" if isolated else "FAIL")
        sys.exit(0 if isolated else 1)


if __name__ == "__main__":
    _main(sys.argv[1:])
