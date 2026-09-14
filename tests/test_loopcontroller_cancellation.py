import sys
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "orchestra"))

import loopcontroller
import store
from dbpool import connection


def test_research_cancel_terminalizes_exact_linked_durable_run():
    tenant = f"research-cancel-{uuid.uuid4().hex[:10]}"
    thread_id = 990_000_000 + uuid.uuid4().int % 1_000_000
    research_id = None
    run_id = None
    try:
        with connection() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO tenants (tenant_id,name,api_token)
                            VALUES (%s,'cancel test',%s)
                            ON CONFLICT (tenant_id) DO NOTHING""", (tenant, uuid.uuid4().hex))
            cur.execute("""INSERT INTO research_runs
                              (tenant_id,org_id,thread_id,question,status)
                            VALUES (%s,'1',%s,'test question','running') RETURNING id""",
                        (tenant, thread_id))
            research_id = cur.fetchone()[0]
            conn.commit()
        run = store.start_run(tenant, "test question", org_id=1)
        run_id = run["run_id"]
        coordinator = store.spawn_actor(
            run_id, tenant, "research-coordinator", "research-coordinator", kind="supervisor",
            memory={"research_run_id": research_id, "context": {"thread_id": thread_id}})
        worker = store.spawn_actor(
            run_id, tenant, "researcher", "research-growth", kind="worker",
            supervisor_id=coordinator["actor_id"])
        assert store.claim_actor_step(worker["actor_id"], tenant, claimed_by="cancel-test")

        halted = loopcontroller._halt_linked_orchestra_runs(
            tenant, thread_id, research_run_id=research_id, reason="user changed scope")

        assert halted == 1
        assert store.run(run_id, tenant)["status"] == "halted"
        actors = {actor["actor_id"]: actor for actor in store.actors(run_id, tenant)}
        assert actors[coordinator["actor_id"]]["status"] == "dead"
        assert actors[worker["actor_id"]]["status"] == "dead"
        with connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT status,finished_at FROM research_runs WHERE id=%s", (research_id,))
            status, finished = cur.fetchone()
            assert status == "cancelled" and finished is not None
            cur.execute("SELECT step_claimed_at,step_claimed_by FROM orchestra_actors WHERE actor_id=%s",
                        (worker["actor_id"],))
            assert cur.fetchone() == (None, None)
    finally:
        with connection() as conn, conn.cursor() as cur:
            if run_id is not None:
                cur.execute("DELETE FROM orchestra_tool_leases WHERE run_id=%s", (run_id,))
                cur.execute("DELETE FROM orchestra_events WHERE run_id=%s", (run_id,))
                cur.execute("DELETE FROM orchestra_actors WHERE run_id=%s", (run_id,))
                cur.execute("DELETE FROM orchestra_runs WHERE run_id=%s", (run_id,))
            if research_id is not None:
                cur.execute("DELETE FROM research_options WHERE run_id=%s", (research_id,))
                cur.execute("DELETE FROM research_runs WHERE id=%s", (research_id,))
            cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tenant,))
            conn.commit()
