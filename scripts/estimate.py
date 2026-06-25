#!/usr/bin/env python3
"""estimate.py — PRE-COMMIT cost/time estimate ("this build ≈ $X, ~N min") shown BEFORE a CEO spends.

Every existing surface is descriptive (what a build cost AFTER the fact) or pre-emptive about budget burn.
This is the missing PRE-COMMIT piece: a plain-language "what will this cost me?" answer the moment a
non-technical CEO is about to greenlight a build — so the spend is a decision, not a surprise.

We estimate from HISTORICAL data. Each completed build (a `product`) is grouped by its build kind
(lib/web/service) via app_registry.kind, and we take the MEDIAN total cost_usd / elapsed_s / tokens across
similar past builds. That median is the point estimate, surrounded by an honest range (0.6x–1.6x). With no
history for a kind we fall back to sensible defaults. A charter (the spec) bumps the estimate a little by
its complexity (length / feature-word count) — a more detailed spec tends to cost a bit more.

    estimate.py json lib|web|service     # the estimate payload for a kind
    estimate.py selftest
Run with the agent-os venv python.
"""
import statistics
import sys
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit  # noqa: E402  (kept consistent with sibling scripts; available for future use)

ENV = Path.home() / "projects" / "agent-os" / ".env.local"
DB = next((l.split("=", 1)[1].strip() for l in ENV.read_text().splitlines()
           if l.strip().startswith("DATABASE_URL=")), None)

# Sensible point-estimate fallbacks when there's no history for a kind: (cost_usd, minutes, tokens).
DEFAULTS = {
    "lib":     {"cost_usd": 1.5, "minutes": 8,  "tokens": 900_000},
    "web":     {"cost_usd": 2.5, "minutes": 12, "tokens": 1_500_000},
    "service": {"cost_usd": 4.0, "minutes": 18, "tokens": 2_500_000},
}
LOW_MULT, HIGH_MULT = 0.6, 1.6      # honest range around the point estimate

# Feature-y words: a charter packed with these implies more surface area -> a bit more cost/time.
FEATURE_WORDS = ("auth", "login", "billing", "payment", "dashboard", "api", "database", "db",
                 "admin", "search", "upload", "notification", "email", "report", "export",
                 "integration", "webhook", "test", "deploy", "user", "role", "permission",
                 "analytics", "chart", "queue", "cache", "schedule", "oauth", "stripe")


def _history(kind):
    """Per-product totals for completed builds of this kind -> (costs[], minutes[], tokens[]).

    A 'product' is one build; we sum its trace rows, then collect the totals across products so we
    can take a median. Only completed builds (cost > 0) count, so half-finished runs don't drag it down.
    """
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute(
            """SELECT t.product,
                      sum(t.cost_usd)::float            AS cost,
                      sum(t.elapsed_s)::float           AS elapsed_s,
                      sum(t.tokens_in + t.tokens_out)   AS tokens
               FROM traces t
               JOIN app_registry r ON r.name = t.product
               WHERE r.kind = %s
               GROUP BY t.product
               HAVING sum(t.cost_usd) > 0""",
            (kind,),
        )
        rows = cur.fetchall()
    costs = [r[1] for r in rows]
    minutes = [r[2] / 60.0 for r in rows]
    tokens = [int(r[3] or 0) for r in rows]
    return costs, minutes, tokens


def _range(point):
    return round(point * LOW_MULT, 2), round(point * HIGH_MULT, 2)


def _money(x):
    return f"${x:.0f}" if x >= 10 else f"${x:.2f}".rstrip("0").rstrip(".")


def estimate(kind="lib"):
    """A pre-commit estimate for a build of `kind`, from history when we have it, else defaults."""
    kind = (kind or "lib").lower()
    costs, minutes, tokens = _history(kind)
    n = len(costs)
    if n >= 1:
        cost = round(statistics.median(costs), 2)
        mins = round(statistics.median(minutes), 1)
        toks = int(statistics.median(tokens))
        basis = f"history(n={n})"
        confidence = "high" if n >= 5 else ("med" if n >= 2 else "low")
    else:
        d = DEFAULTS.get(kind, DEFAULTS["lib"])
        cost, mins, toks = float(d["cost_usd"]), float(d["minutes"]), int(d["tokens"])
        basis, confidence = "default", "low"

    cost_lo, cost_hi = _range(cost)
    min_lo, min_hi = max(1, round(mins * LOW_MULT)), round(mins * HIGH_MULT)
    if basis.startswith("history"):
        note = (f"About {_money(cost_lo)}–{_money(cost_hi)} and ~{round(mins)} minutes, "
                f"based on {n} similar build{'s' if n != 1 else ''}.")
    else:
        note = (f"About {_money(cost_lo)}–{_money(cost_hi)} and ~{round(mins)} minutes "
                f"(rough estimate — no past {kind} builds yet).")
    return {
        "kind": kind,
        "cost_usd_estimate": cost,
        "cost_usd_range": [cost_lo, cost_hi],
        "minutes_estimate": mins,
        "minutes_range": [min_lo, min_hi],
        "tokens_estimate": toks,
        "basis": basis,
        "confidence": confidence,
        "note": note,
    }


def _complexity_factor(charter):
    """A small, honest bump from how detailed the spec is: longer + more feature-words -> a bit higher.

    Bounded to roughly 1.0x–1.45x so it nudges, never dominates.
    """
    text = (charter or "").lower()
    words = len(text.split())
    feats = sum(text.count(w) for w in FEATURE_WORDS)
    length_bump = min(0.25, words / 400.0 * 0.25)   # up to +25% for a long spec (~400+ words)
    feature_bump = min(0.20, feats * 0.025)         # up to +20% for many feature words (~8+)
    return round(1.0 + length_bump + feature_bump, 3)


def estimate_for_charter(charter, kind="lib"):
    """Base estimate for `kind`, bumped a little by the charter's complexity. Honest + rough."""
    base = estimate(kind)
    factor = _complexity_factor(charter)
    cost = round(base["cost_usd_estimate"] * factor, 2)
    mins = round(base["minutes_estimate"] * factor, 1)
    toks = int(base["tokens_estimate"] * factor)
    cost_lo, cost_hi = _range(cost)
    min_lo, min_hi = max(1, round(mins * LOW_MULT)), round(mins * HIGH_MULT)
    extra = "" if factor <= 1.0 else f" (a bit higher for the detail in your spec)"
    note = (f"About {_money(cost_lo)}–{_money(cost_hi)} and ~{round(mins)} minutes for this {kind}"
            f"{extra} — a rough, pre-commit estimate based on {base['basis']}.")
    return {
        "kind": kind,
        "cost_usd_estimate": cost,
        "cost_usd_range": [cost_lo, cost_hi],
        "minutes_estimate": mins,
        "minutes_range": [min_lo, min_hi],
        "tokens_estimate": toks,
        "complexity_factor": factor,
        "basis": base["basis"],
        "confidence": base["confidence"],
        "note": note,
    }


def _selftest():
    e = estimate("lib")
    assert isinstance(e, dict), "estimate must return a dict"
    assert e["cost_usd_estimate"] > 0, "cost_usd_estimate must be > 0"
    assert e["minutes_estimate"] > 0, "minutes_estimate must be > 0"
    assert e["note"] and isinstance(e["note"], str), "note must be a non-empty string"
    assert len(e["cost_usd_range"]) == 2 and e["cost_usd_range"][0] < e["cost_usd_range"][1], "range must be lo<hi"

    base_service = estimate("service")["cost_usd_estimate"]
    c = estimate_for_charter("a big app with auth, billing, dashboards, API, and tests", "service")
    assert c["cost_usd_estimate"] >= base_service, "more complex charter -> cost >= base"
    assert c["complexity_factor"] >= 1.0, "complexity factor must not shrink the estimate"

    # a richer charter should not estimate below a trivial one for the same kind
    trivial = estimate_for_charter("tiny thing", "service")["cost_usd_estimate"]
    assert c["cost_usd_estimate"] >= trivial, "detailed spec >= trivial spec"

    print(f"lib={e['note']!r} basis={e['basis']} conf={e['confidence']}")
    print(f"service base=${base_service} charter=${c['cost_usd_estimate']} factor={c['complexity_factor']}")
    print("PASS: estimate gives a pre-commit cost/time range from history (charter bumps it honestly) ✅")
    sys.exit(0)


def _main(a):
    import json
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "json" and len(a) > 1:
        print(json.dumps(estimate(a[1]), indent=2))
    else:
        sys.exit("usage: estimate.py json <lib|web|service> | selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
