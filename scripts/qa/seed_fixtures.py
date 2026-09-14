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
from psycopg import sql  # noqa: E402
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
        cur.execute("""INSERT INTO tasks
                          (tenant_id,assignee,requester,role,title,priority,status,attempts,max_retry,last_error)
                       VALUES (%s,%s,'factory','builder',%s,5,'dead',3,3,%s)""",
                    (tid, "builder@" + prod, "Build step failed (seeded)",
                     "seeded: provider error after 3 retries"))
        seeded.append("dead_letter@" + prod)
        # 3) a NOTIFICATION (the Notifications screen + bell)
        cur.execute("""INSERT INTO notifications (tenant_id, channel, category, level, title, body)
                       VALUES (%s,'in_app','build','standard',%s,%s)""",
                    (tid, "Your build needs attention", "A seeded build hit an issue — decide in Approvals."))
        seeded.append("notification")
        c.commit()
    return {"product": prod, "seeded": seeded}


def cleanup(tid):
    """Remove one action-crawl tenant and all of its synthetic rows.

    Fail closed on the fixture name so this helper can never erase a real customer merely because a
    shell variable was wrong.  Tenant-scoped tables are discovered from the schema because the product
    grows new surfaces frequently; leaving old fixture rows behind previously accumulated fake companies,
    workstreams, notifications, and products in the operator's real dashboard.
    """
    prod = "qaseed-" + (tid.replace("t-", "")[:6])
    with psycopg.connect(_t.DB) as c, c.cursor() as cur:
        cur.execute("SELECT name FROM tenants WHERE tenant_id=%s FOR UPDATE", (tid,))
        row = cur.fetchone()
        if not row:
            return {"removed": False, "reason": "already absent"}
        if not str(row[0]).startswith("ActionCrawl"):
            raise ValueError("refusing to clean a tenant that is not an ActionCrawl fixture")

        cur.execute("SELECT product FROM tenant_products WHERE tenant_id=%s", (tid,))
        products = [r[0] for r in cur.fetchall()]
        if prod not in products:
            products.append(prod)

        # Product-keyed rows do not all carry tenant_id.  Older versions of the action crawler could
        # accidentally launch real builds, so clean every product owned by this test tenant, not merely
        # the modern qaseed fixture.
        cur.execute("DELETE FROM tasks WHERE assignee = ANY(%s)", (["builder@" + p for p in products],))
        cur.execute("DELETE FROM traces WHERE product = ANY(%s) OR run_id = ANY(%s)",
                    (products, ["run-" + p for p in products]))
        cur.execute("""SELECT c.table_name FROM information_schema.columns c
                       WHERE c.table_schema='public' AND c.column_name='product'
                         AND NOT EXISTS (
                           SELECT 1 FROM information_schema.columns t
                           WHERE t.table_schema='public' AND t.table_name=c.table_name
                             AND t.column_name='tenant_id')
                       ORDER BY c.table_name""")
        for i, table in enumerate(r[0] for r in cur.fetchall()):
            sp = f"fixture_product_{i}"
            cur.execute(sql.SQL("SAVEPOINT {}").format(sql.Identifier(sp)))
            try:
                cur.execute(sql.SQL("DELETE FROM {} WHERE product = ANY(%s)").format(sql.Identifier(table)),
                            (products,))
            except Exception:
                cur.execute(sql.SQL("ROLLBACK TO SAVEPOINT {}").format(sql.Identifier(sp)))
            else:
                cur.execute(sql.SQL("RELEASE SAVEPOINT {}").format(sql.Identifier(sp)))

        cur.execute("""SELECT DISTINCT table_name FROM information_schema.columns
                       WHERE table_schema='public' AND column_name='tenant_id'
                         AND table_name NOT IN ('tenants', 'audit_log') ORDER BY table_name""")
        tables = [r[0] for r in cur.fetchall()]
        # Foreign-key ordering varies as modules evolve.  Multiple passes make dependency leaves disappear
        # first; an individual failure rolls back only its savepoint and is retried on the next pass.
        pending = tables
        for attempt in range(3):
            failed = []
            for i, table in enumerate(pending):
                sp = f"fixture_cleanup_{attempt}_{i}"
                cur.execute(sql.SQL("SAVEPOINT {}").format(sql.Identifier(sp)))
                try:
                    cur.execute(sql.SQL("DELETE FROM {} WHERE tenant_id=%s").format(sql.Identifier(table)), (tid,))
                except Exception:
                    cur.execute(sql.SQL("ROLLBACK TO SAVEPOINT {}").format(sql.Identifier(sp)))
                    failed.append(table)
                else:
                    cur.execute(sql.SQL("RELEASE SAVEPOINT {}").format(sql.Identifier(sp)))
            pending = failed
            if not pending:
                break
        if pending:
            raise RuntimeError("could not clean tenant-scoped fixture tables: " + ", ".join(pending))
        cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,))
    return {"removed": True, "tenant_id": tid, "products": products}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: seed_fixtures.py <tenant_id> [org_id] | cleanup <tenant_id>")
    try:
        if sys.argv[1] == "cleanup":
            if len(sys.argv) != 3:
                sys.exit("usage: seed_fixtures.py cleanup <tenant_id>")
            print("CLEANED:", cleanup(sys.argv[2]))
        else:
            r = seed(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else 0)
            print("SEEDED:", r)
    except Exception as e:
        print("FIXTURE-ERR:", str(e)[:200])
        sys.exit(1)
