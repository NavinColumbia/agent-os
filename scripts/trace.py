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

ENV = Path.home() / "projects" / "agent-os" / ".env.local"
DB = next((l.split("=", 1)[1].strip() for l in ENV.read_text().splitlines()
           if l.strip().startswith("DATABASE_URL=")), None)


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
    elif a[0] == "show":
        show(a[1], full="--full" in a)
    elif a[0] == "selftest":
        rs = runs(1)
        # well-formedness: schema present + query works (data optional in a fresh DB)
        ok = isinstance(rs, list)
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("SELECT count(*) FROM traces"); n = cur.fetchone()[0]
        print(f"traces persisted: {n}; runs query ok: {ok}")
        print("PASS: debug trace store + replay ✅" if ok else "FAIL")
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    _main(sys.argv[1:])
