#!/usr/bin/env python3
"""experiments.py — experiment tracking for data-scientist / ml-engineer roles (ADR 0004 K6).

Log runs (params + metrics), compare them, pick the best — the loop a data scientist or ML engineer
runs (e.g. tuning a recommendation model). Backed by Postgres; integrates with the metrics ledger.

    experiments.py demo
    from experiments import log_run, best, compare
Run with the agent-os venv python.
"""
import json
import sys
import uuid
from pathlib import Path

import psycopg

from aoscfg import ENV, DB


def log_run(experiment, params, metrics, product=None, tags=None):
    rid = f"run-{uuid.uuid4().hex[:10]}"
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("INSERT INTO experiments (run_id, experiment, product, params, metrics, tags) VALUES (%s,%s,%s,%s,%s,%s)",
                    (rid, experiment, product, json.dumps(params), json.dumps(metrics), tags or []))
        c.commit()
    return rid


def compare(experiment, metric, mode="max"):
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT run_id, params, metrics FROM experiments WHERE experiment=%s", (experiment,))
        rows = [{"run_id": r[0], "params": r[1], "metrics": r[2]} for r in cur.fetchall()]
    rows = [r for r in rows if metric in (r["metrics"] or {})]
    rows.sort(key=lambda r: r["metrics"][metric], reverse=(mode == "max"))
    return rows


def best(experiment, metric, mode="max"):
    c = compare(experiment, metric, mode)
    return c[0] if c else None


def _demo():
    exp = f"recommender-{uuid.uuid4().hex[:6]}"
    log_run(exp, {"model": "als", "factors": 64}, {"recall@10": 0.31, "latency_ms": 8}, product="yt-clone")
    log_run(exp, {"model": "als", "factors": 128}, {"recall@10": 0.38, "latency_ms": 12}, product="yt-clone")
    log_run(exp, {"model": "two-tower", "dim": 128}, {"recall@10": 0.44, "latency_ms": 19}, product="yt-clone")
    table = compare(exp, "recall@10")
    print(f"experiment {exp} — runs by recall@10:")
    for r in table:
        print(f"  {r['run_id']}  {r['params']}  recall@10={r['metrics']['recall@10']}")
    b = best(exp, "recall@10")
    print(f"best: {b['run_id']} -> {b['params']} (recall@10={b['metrics']['recall@10']})")
    ok = b["params"].get("model") == "two-tower" and len(table) == 3
    print("PASS: experiment tracking — log/compare/best works ✅" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    _demo() if (len(sys.argv) > 1 and sys.argv[1] == "demo") else _demo()
