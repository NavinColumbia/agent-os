#!/usr/bin/env python3
"""seed_fixtures.py — populate a test tenant with DEEP-STATE data so the action crawler exercises the many
controls that only render when there's real data: a shipped project (Projects/Cockpit + the 'Why?' explain
control), a dead-lettered build (an Approvals decision with approve/deny buttons), and a notification. Without
this, the crawler runs on an empty tenant and never reaches those controls — which is how data-dependent bugs
(a dead decision button, an empty panel, a broken 'Why?') slip past QA.

    python seed_fixtures.py <tenant_id> <org_id>
Best-effort + idempotent-ish; prints what it seeded. Run with the agent-os venv python.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import psycopg  # noqa: E402
import trace as _t  # noqa: E402


def seed(tid, org_id):
    org_id = int(org_id or 0) or None
    prod = "qaseed-" + (tid.replace("t-", "")[:6])
    run = "run-" + prod
    seeded = []
    with psycopg.connect(_t.DB) as c, c.cursor() as cur:
        # 1) a PROJECT the tenant owns (renders on Projects/Cockpit + the 'Why?' explain control)
        cur.execute("INSERT INTO tenant_products (product, tenant_id, org_id) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                    (prod, tid, org_id))
        for stg, role in (("SPEC", "planner"), ("BUILD", "builder"), ("QA", "tester")):
            cur.execute("""INSERT INTO traces (run_id,product,stage,role,kind,rc,cost_usd,tokens_in,tokens_out,elapsed_s,prompt,output,model)
                           VALUES (%s,%s,%s,%s,'agent',0,0.10,400,700,12,'seed','seeded artifact','m')""",
                        (run, prod, stg, role))
        seeded.append("project:" + prod)
        # 2) a DEAD-LETTERED build (an Approvals decision — approve/deny buttons the crawler can exercise).
        #    approvals scopes dead-letters by the assignee's product being one the tenant owns (^ seeded above).
        cur.execute("""INSERT INTO tasks (assignee, requester, role, title, priority, status, attempts, max_retry, last_error)
                       VALUES (%s,'factory','builder',%s,5,'dead',3,3,%s)""",
                    ("builder@" + prod, "Build step failed (seeded)", "seeded: provider error after 3 retries"))
        seeded.append("dead_letter@" + prod)
        # 3) a NOTIFICATION (the Notifications screen + bell)
        cur.execute("""INSERT INTO notifications (tenant_id, channel, category, level, title, body)
                       VALUES (%s,'in_app','build','standard',%s,%s)""",
                    (tid, "Your build needs attention", "A seeded build hit an issue — decide in Approvals."))
        seeded.append("notification")
        c.commit()
    return {"product": prod, "seeded": seeded}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: seed_fixtures.py <tenant_id> [org_id]")
    try:
        r = seed(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else 0)
        print("SEEDED:", r)
    except Exception as e:
        print("SEED-ERR:", str(e)[:200])
