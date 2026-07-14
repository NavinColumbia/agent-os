#!/usr/bin/env python3
"""recommend.py — a data-driven recommender that gets smarter as builds accumulate.

estimate.py answers "what will this cost?" from history. This generalizes that idea from cost to
STRATEGY and QUALITY: given everything the OS has learned from finished builds (build_outcomes — how many
rounds a build took, whether it shipped, its final score), it answers "how should I build this?" and "what
should I do next?". The signal sharpens over time: every finished build feeds the recommendation, and every
piece of feedback (kept vs dismissed) tells us which advice actually helped.

Two surfaces:
  - recommend_strategy(kind): across all past builds of a kind, which rigor/approach ships cleanest —
    fewer rounds, higher ship rate, higher score. Honest "not enough data yet" default before any history.
  - recommend_next(tenant_id): from a tenant's own recent outcomes, 1-3 concrete next actions (e.g.
    "your last builds keep failing QA — add a security pass"), persisted so they can be kept/dismissed.

It reads build_outcomes, which qualityloop.py owns; if that table doesn't exist yet we treat it as empty
so the recommender is honest (not broken) before there's any data.

    recommend.py json <tenant_id>     # strategy + next-actions payload for a tenant
    recommend.py selftest
Run with the agent-os venv python.
"""
import statistics
import sys
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit  # noqa: E402

from aoscfg import ENV, DB

RECENT_N = 5          # how many of a tenant's latest builds shape "what next"
GOOD_SCORE = 0.8      # a build at/above this is "clean"


def _ensure():
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS recommendations (
            id           BIGSERIAL PRIMARY KEY,
            tenant_id    TEXT,
            org_id       TEXT,
            kind         TEXT,
            title        TEXT,
            body         TEXT,
            score        NUMERIC,
            evidence     JSONB DEFAULT '{}',
            dismissed_at TIMESTAMPTZ,
            created_at   TIMESTAMPTZ DEFAULT now())""")
        c.commit()


def _outcomes_for_kind(kind):
    """Finished builds of this kind -> (rounds[], shipped[bool], scores[]). Empty if no table/no data."""
    try:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("""SELECT rounds, shipped, final_score FROM build_outcomes
                           WHERE kind = %s""", (kind,))
            rows = cur.fetchall()
    except psycopg.Error:
        return [], [], []          # table doesn't exist yet -> honest empty
    rounds = [int(r[0]) for r in rows if r[0] is not None]
    shipped = [bool(r[1]) for r in rows if r[1] is not None]
    scores = [float(r[2]) for r in rows if r[2] is not None]
    return rounds, shipped, scores


def _tenant_products(tid):
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT product FROM tenant_products WHERE tenant_id=%s", (tid,))
        return [r[0] for r in cur.fetchall()]


def _recent_outcomes(products):
    """The tenant's latest finished builds (newest first), across their products. Empty if no table/data."""
    if not products:
        return []
    try:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("""SELECT product, kind, rounds, final_score, shipped, at
                           FROM build_outcomes WHERE product = ANY(%s)
                           ORDER BY at DESC LIMIT %s""", (products, RECENT_N))
            rows = cur.fetchall()
    except psycopg.Error:
        return []
    return [{"product": r[0], "kind": r[1], "rounds": r[2],
             "final_score": float(r[3]) if r[3] is not None else None,
             "shipped": r[4]} for r in rows]


def recommend_strategy(kind="lib"):
    """Across all past builds of `kind`, the rigor/approach that ships cleanest — or an honest default.

    Cleanest = ships reliably, in few rounds, at a high score. With no history we don't pretend: we say so
    and tell the CEO to start at standard rigor. Confidence climbs as more builds accumulate.
    """
    kind = (kind or "lib").lower()
    rounds, shipped, scores = _outcomes_for_kind(kind)
    n = len(scores) or len(rounds) or len(shipped)
    if n == 0:
        return {"kind": kind,
                "recommendation": "not enough data yet — start with standard rigor",
                "confidence": "low",
                "evidence": {"n": 0, "avg_rounds": None, "ship_rate": None, "avg_score": None}}

    avg_rounds = round(statistics.mean(rounds), 2) if rounds else None
    ship_rate = round(sum(shipped) / len(shipped), 3) if shipped else None
    avg_score = round(statistics.mean(scores), 3) if scores else None
    confidence = "high" if n >= 5 else ("med" if n >= 2 else "low")

    # Translate the evidence into plain-language rigor advice. Low ship rate / low score -> climb higher;
    # many rounds to get there -> budget for iteration; clean + cheap -> standard rigor is enough.
    if ship_rate is not None and ship_rate < 0.5:
        rec = (f"raise the quality bar — only {round(ship_rate * 100)}% of past {kind} builds shipped; "
               f"use higher rigor (extra QA + security pass) up front")
    elif avg_score is not None and avg_score < GOOD_SCORE:
        rec = (f"use higher rigor — past {kind} builds averaged {avg_score} (below {GOOD_SCORE}); "
               f"add a review/security pass to lift quality")
    elif avg_rounds is not None and avg_rounds >= 3:
        rec = (f"standard rigor ships, but budget for iteration — {kind} builds took ~{avg_rounds} rounds "
               f"on average to reach the bar")
    else:
        rec = (f"standard rigor is enough — past {kind} builds shipped cleanly "
               f"(~{avg_rounds} rounds, avg score {avg_score})")
    return {"kind": kind, "recommendation": rec, "confidence": confidence,
            "evidence": {"n": n, "avg_rounds": avg_rounds, "ship_rate": ship_rate, "avg_score": avg_score}}


def _next_actions(recent):
    """Turn a tenant's recent outcomes into 1-3 concrete suggestions (title, body, score, evidence)."""
    actions = []
    if not recent:
        actions.append({
            "title": "Ship your first build to get tailored advice",
            "body": ("No finished builds yet. Start one at standard rigor — once a few complete, this "
                     "panel will recommend the rigor and passes that ship cleanest for you."),
            "score": 0.3,
            "evidence": {"reason": "no_history", "n": 0}})
        return actions

    n = len(recent)
    failed = [r for r in recent if r.get("shipped") is False]
    low = [r for r in recent if r.get("final_score") is not None and r["final_score"] < GOOD_SCORE]
    grindy = [r for r in recent if r.get("rounds") is not None and r["rounds"] >= 3]

    if len(failed) >= 2:
        actions.append({
            "title": "Add a security pass — recent builds keep failing QA",
            "body": (f"{len(failed)} of your last {n} builds didn't ship. Turn on a dedicated security + "
                     f"review pass before launch so issues are caught earlier, not at the gate."),
            "score": round(0.7 + 0.05 * len(failed), 3),
            "evidence": {"reason": "repeated_qa_fail", "failed": len(failed), "of": n}})
    if len(low) >= 2:
        avg_low = round(statistics.mean([r["final_score"] for r in low]), 3)
        actions.append({
            "title": "Raise the quality bar",
            "body": (f"{len(low)} recent builds landed below {GOOD_SCORE} (avg {avg_low}). Bump the target "
                     f"bar and allow an extra iteration round so quality climbs before you ship."),
            "score": round(0.6 + 0.05 * len(low), 3),
            "evidence": {"reason": "low_scores", "count": len(low), "avg_score": avg_low}})
    if len(grindy) >= 2:
        avg_r = round(statistics.mean([r["rounds"] for r in grindy]), 2)
        actions.append({
            "title": "Tighten your charter to cut iteration",
            "body": (f"Recent builds needed ~{avg_r} rounds to reach the bar. A more specific, smaller-scope "
                     f"charter usually converges in fewer rounds — split big specs into separate builds."),
            "score": round(0.5 + 0.05 * len(grindy), 3),
            "evidence": {"reason": "many_rounds", "count": len(grindy), "avg_rounds": avg_r}})

    if not actions:
        actions.append({
            "title": "You're on track — keep current settings",
            "body": (f"Your last {n} builds shipped cleanly at standard rigor. No changes recommended; "
                     f"keep building the same way."),
            "score": 0.4,
            "evidence": {"reason": "healthy", "n": n}})
    return actions[:3]


def recommend_next(tenant_id, org_id=None):
    """1-3 concrete next actions from this tenant's recent outcomes, persisted for keep/dismiss feedback."""
    _ensure()
    import json
    recent = _recent_outcomes(_tenant_products(tenant_id))
    actions = _next_actions(recent)
    out = []
    with psycopg.connect(DB) as c, c.cursor() as cur:
        for a in actions:
            cur.execute("""INSERT INTO recommendations
                           (tenant_id, org_id, kind, title, body, score, evidence)
                           VALUES (%s,%s,'next',%s,%s,%s,%s) RETURNING id""",
                        (tenant_id, org_id, a["title"], a["body"], a["score"],
                         json.dumps(a.get("evidence", {}))))
            rec_id = cur.fetchone()[0]
            out.append({"id": rec_id, "title": a["title"], "body": a["body"], "score": a["score"]})
        c.commit()
    audit.append(actor="recommend", action="RecommendNext", resource=tenant_id, decision="created",
                 payload={"count": len(out)})
    return out


def feedback(rec_id, useful):
    """The learning signal: keep (useful=True) clears any dismissal; dismiss (useful=False) hides it."""
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        if useful:
            cur.execute("UPDATE recommendations SET dismissed_at=NULL WHERE id=%s", (rec_id,))
        else:
            cur.execute("UPDATE recommendations SET dismissed_at=now() WHERE id=%s", (rec_id,))
        n = cur.rowcount
        c.commit()
    audit.append(actor="recommend", action="RecommendFeedback", resource=str(rec_id),
                 decision="kept" if useful else "dismissed")
    return {"id": rec_id, "useful": bool(useful), "updated": n}


def recent(tenant_id):
    """A tenant's live (non-dismissed) recommendations, newest first."""
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT id, kind, title, body, score, created_at FROM recommendations
                       WHERE tenant_id=%s AND dismissed_at IS NULL ORDER BY created_at DESC, id DESC""",
                    (tenant_id,))
        rows = cur.fetchall()
    return [{"id": r[0], "kind": r[1], "title": r[2], "body": r[3],
             "score": float(r[4]) if r[4] is not None else None} for r in rows]


def _selftest():
    import billing  # noqa: E402
    import psycopg as pg
    reg = billing.signup("billing.signup", "free")
    tid = reg["tenant_id"]
    prod = tid.replace("t-", "")[:6] + "-rec"
    _ensure()
    ok = False
    try:
        with pg.connect(DB) as c, c.cursor() as cur:
            cur.execute("INSERT INTO tenant_products (product, tenant_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                        (prod, tid))
            # Seed lib outcomes so there IS data: mostly failing / low-score so advice is concrete.
            seed = [(prod, "lib", 4, 0.62, False), (prod, "lib", 3, 0.70, False),
                    (prod, "lib", 2, 0.91, True),  (prod, "lib", 3, 0.55, False)]
            for p, k, rnd, sc, sh in seed:
                cur.execute("""INSERT INTO build_outcomes (product, kind, rounds, final_score, shipped, cost_usd)
                               VALUES (%s,%s,%s,%s,%s,%s)""", (p, k, rnd, sc, sh, 1.5))
            c.commit()

        # 1) strategy from real history
        strat = recommend_strategy("lib")
        assert isinstance(strat, dict), "strategy must be a dict"
        assert strat["evidence"]["n"] >= 1, "strategy evidence.n must be >= 1 with data"
        assert strat["confidence"] in ("low", "med", "high"), "strategy must carry a confidence"

        # 2) next-actions are produced + persisted + visible
        recs = recommend_next(tid)
        assert isinstance(recs, list) and len(recs) >= 1, "recommend_next must return a non-empty list"
        live = recent(tid)
        live_ids = {r["id"] for r in live}
        assert {r["id"] for r in recs} <= live_ids, "persisted recs must appear in recent()"
        before = len(live)

        # 3) feedback dismisses one -> it leaves recent()
        feedback(recs[0]["id"], useful=False)
        live2 = recent(tid)
        assert recs[0]["id"] not in {r["id"] for r in live2}, "dismissed rec must leave recent()"
        assert len(live2) == before - 1, "exactly one rec should drop out"

        # 4) honest default for a kind with NO data
        empty = recommend_strategy("web")
        assert empty["evidence"]["n"] == 0, "web has no data -> evidence.n == 0"
        assert empty["confidence"] == "low", "no-data strategy must be low confidence"
        assert "not enough data" in empty["recommendation"], "no-data strategy must be honest"

        ok = True
        print(f"lib strategy: {strat['recommendation']!r} conf={strat['confidence']} "
              f"evidence={strat['evidence']}")
        print(f"recommend_next -> {len(recs)} actions; recent {before}->{len(live2)} after dismiss; "
              f"web(no data)={empty['recommendation']!r}")
        print("PASS: recommender learns from build_outcomes — strategy + next-actions + feedback ✅")
    finally:
        with pg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM build_outcomes WHERE product=%s", (prod,))
            cur.execute("DELETE FROM recommendations WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM tenant_products WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,))
            c.commit()
    sys.exit(0 if ok else 1)


def _main(a):
    import json
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "json" and len(a) > 1:
        tid = a[1]
        prods = _tenant_products(tid)
        recent_o = _recent_outcomes(prods)
        kinds = sorted({r["kind"] for r in recent_o if r.get("kind")}) or ["lib"]
        print(json.dumps({
            "tenant": tid,
            "strategy": {k: recommend_strategy(k) for k in kinds},
            "next": recommend_next(tid),
        }, indent=2, default=str))
    else:
        sys.exit("usage: recommend.py json <tenant_id> | selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
