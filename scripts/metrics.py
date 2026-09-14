#!/usr/bin/env python3
"""metrics.py — org self-measurement (ADR 0002/0004 §5).

Append-only event ledger in Postgres -> KPIs surfaced in the daily digest / monthly retro.
Maturity = rework-rate down + feedback-loop-latency down at flat cost, month over month.

    metrics.py demo    # record synthetic events, print KPIs
    from metrics import record, kpis
Run with the agent-os venv python.
"""
import sys
from pathlib import Path

from dbpool import connection  # noqa: E402


def record(event, product=None, task_id=None, from_state=None, to_state=None,
           tokens_in=0, tokens_out=0, model=None, wall_clock_s=None, outcome=None):
    with connection() as c, c.cursor() as cur:
        cur.execute("""INSERT INTO org_metrics
            (event,product,task_id,from_state,to_state,tokens_in,tokens_out,model,wall_clock_s,outcome)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (event, product, task_id, from_state, to_state, tokens_in, tokens_out, model, wall_clock_s, outcome))


def kpis(product=None):
    where = "WHERE product=%s" if product else ""
    args = (product,) if product else ()
    with connection() as c, c.cursor() as cur:
        cur.execute(f"SELECT count(*) FILTER (WHERE event='state_change' AND to_state='done') FROM org_metrics {where}", args)
        throughput = cur.fetchone()[0]
        # rework = state_changes that moved BACKWARD (to an earlier-than-current stage) or outcome='rework'
        cur.execute(f"SELECT count(*) FILTER (WHERE outcome='rework' OR event='cr_filed') FROM org_metrics {where}", args)
        rework = cur.fetchone()[0]
        cur.execute(f"SELECT coalesce(sum(tokens_in+tokens_out),0) FROM org_metrics {where}", args)
        tokens = cur.fetchone()[0]
        cur.execute(f"SELECT count(*) FILTER (WHERE event='cr_filed'), count(*) FILTER (WHERE event='cr_decided') FROM org_metrics {where}", args)
        cr_filed, cr_decided = cur.fetchone()
        total_tasks = throughput + rework
        return {
            "throughput_done": throughput,
            "rework_events": rework,
            "rework_rate": round(rework / total_tasks, 3) if total_tasks else 0.0,
            "total_tokens": tokens,
            "change_requests": cr_filed,
            "crs_resolved": cr_decided,
        }


def _demo():
    import os
    p = f"metricsdemo-{os.urandom(3).hex()}"  # unique product so counts are deterministic across re-runs
    record("state_change", product=p, task_id="t1", to_state="done", tokens_in=1200, tokens_out=300, outcome="success")
    record("state_change", product=p, task_id="t2", to_state="done", tokens_in=900, tokens_out=250, outcome="success")
    record("cr_filed", product=p, task_id="t3", outcome="rework")     # a spec-change loop
    record("cr_decided", product=p, task_id="t3")
    record("state_change", product=p, task_id="t3", to_state="done", tokens_in=600, tokens_out=200, outcome="success")
    k = kpis(p)
    print("KPIs for", p, ":")
    for key, val in k.items():
        print(f"  {key:18} = {val}")
    ok = k["throughput_done"] >= 3 and k["change_requests"] == 1 and k["total_tokens"] > 0
    print("PASS: org self-metrics ledger + KPI rollup working ✅" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    _demo()
