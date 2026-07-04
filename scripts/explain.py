#!/usr/bin/env python3
"""explain.py — "why did the AI do X?" (REBUILD-PLAN C4 explainability).

A skeptical CEO trusts AI less than a human and needs to SEE why the fleet did what it did. This
reconstructs the real decision trail for a product from the durable traces (which role, which stage,
what it actually did + what it cost) and composes a plain-language, GROUNDED explanation — no
fabrication, only what the traces record. Surfaced in the console so every action is accountable.

  python explain.py <product>
  python explain.py selftest
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import psycopg  # noqa: E402
import trace as _trace  # noqa: E402

DB = _trace.DB


def _gist(text, n=160):
    t = " ".join((text or "").split())
    return (t[:n] + "…") if len(t) > n else t


def trail(product, limit=20):
    """The real decision steps for a product, oldest first: who (role), what stage, a gist of the output,
    and the cost — straight from traces (the ground truth of what the fleet actually did)."""
    if not product:
        return []
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT role, stage, output, coalesce(cost_usd,0), ts
                       FROM traces WHERE product=%s AND kind='agent'
                       ORDER BY ts ASC LIMIT %s""", (product, limit))
        return [{"role": r, "stage": s, "did": _gist(o), "cost_usd": round(float(cost), 4),
                 "at": ts.isoformat() if ts else None} for r, s, o, cost, ts in cur.fetchall()]


def explain(product, api_key=None):
    """Grounded plain-language 'why' over the trail. Model composes it FROM the steps only; a deterministic
    fallback (still grounded) is used if the model call fails — never blank, never invented."""
    steps = trail(product)
    if not steps:
        return {"product": product, "steps": [], "summary": "No fleet activity is recorded for this product yet."}
    total = round(sum(s["cost_usd"] for s in steps), 4)
    facts = [{"role": s["role"], "stage": s["stage"], "did": s["did"]} for s in steps]
    try:
        import factory
        if api_key is not None:
            try:
                factory._ctx.api_key = api_key
            except Exception:
                pass
        prompt = ("Explain to a non-technical CEO, in 2-4 short sentences, WHY their AI team did what it "
                  "did to build this product — a clear, trustworthy account. Use ONLY these recorded steps; "
                  "invent nothing. Reply with plain text, no preamble.\nSTEPS:\n" + json.dumps(facts)[:2500])
        r = factory.agent("classifier", ".", prompt, light=True, model=factory.CHEAP_MODEL)
        text = ((r or {}).get("out_full") or (r or {}).get("out") or "").strip()
        if text and len(text) > 20:
            return {"product": product, "steps": steps, "total_cost_usd": total, "summary": text}
    except Exception:
        pass
    roles = list(dict.fromkeys(s["role"] for s in steps))
    stages = list(dict.fromkeys(s["stage"] for s in steps))
    return {"product": product, "steps": steps, "total_cost_usd": total, "_fallback": True,
            "summary": f"{len(steps)} steps by {len(roles)} roles ({', '.join(roles[:5])}) across "
                       f"{', '.join(stages[:6])} — total AI cost ${total}. Each step's action is listed below."}


def _selftest():
    import uuid
    import factory
    real = factory.agent
    prod = f"explain-selftest-{uuid.uuid4().hex[:8]}"
    ok = True

    def chk(cond, label):
        nonlocal ok
        print(("PASS" if cond else "FAIL") + f": {label}")
        ok = ok and bool(cond)

    try:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            for role, stage, out, cost in [("staff-engineer", "PLAN", "Decomposed into 3 modules", 0.04),
                                           ("backend-engineer", "BUILD", "Implemented the money engine with Decimal", 0.21),
                                           ("qa-security", "TESTQA", "Ran 40 tests, all passed", 0.07)]:
                cur.execute("""INSERT INTO traces (product,stage,role,kind,rc,output,prompt,run_id,cost_usd,ts)
                               VALUES (%s,%s,%s,'agent',0,%s,'p',555001,%s,now())""", (prod, stage, role, out, cost))
            c.commit()

        t = trail(prod)
        chk(len(t) == 3 and t[0]["role"] == "staff-engineer" and "Decomposed" in t[0]["did"]
            and t[0]["cost_usd"] == 0.04, f"trail reconstructs the real ordered decision steps ({len(t)})")

        # model explanation
        factory.agent = lambda *a, **k: {"rc": 0, "out_full": "Your team planned the app, built the money "
                                         "engine carefully with Decimal, and QA'd it with 40 passing tests."}
        e = explain(prod)
        chk("Decimal" in e["summary"] and e["total_cost_usd"] == 0.32 and len(e["steps"]) == 3
            and not e.get("_fallback"), "explain() composes a grounded plain-language 'why' + total cost")

        # grounded fallback (model fails) — still real, never blank
        factory.agent = lambda *a, **k: {"rc": 1, "out": "x"}
        f = explain(prod)
        chk(f.get("_fallback") and "staff-engineer" in f["summary"] and f["steps"],
            "fallback explanation is grounded in the real roles/stages (never blank/invented)")

        chk(explain(f"empty-{uuid.uuid4().hex[:6]}")["summary"].startswith("No fleet activity"),
            "unknown product -> honest 'no activity', not a fabricated story")
        print("PASS: explain — grounded 'why did the AI do X' from the real trace trail ✅" if ok else "FAIL")
    finally:
        factory.agent = real
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM traces WHERE product=%s", (prod,))
            c.commit()
    return ok


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "selftest"
    if cmd == "selftest":
        sys.exit(0 if _selftest() else 1)
    else:
        print(json.dumps(explain(cmd), indent=2, default=str))
