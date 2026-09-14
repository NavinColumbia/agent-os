"""Focused deadline, contention, and late-commit contracts for the durable orchestra.

All database rows use a random tenant/run and are deleted explicitly. No controller or live QA state is read or
mutated.
"""
import sys
import threading
import time
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
ORCHESTRA = SCRIPTS / "orchestra"
for path in (str(ORCHESTRA), str(SCRIPTS)):
    if path not in sys.path:
        sys.path.insert(0, path)

import runtime
import store
from qa import qa_agentic
from dbpool import connection


def _tenant(label):
    return f"deadline-fence-{label}-{uuid.uuid4().hex}"


def _cleanup(run_id):
    with connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM orchestra_tool_leases WHERE run_id=%s", (run_id,))
        cur.execute("DELETE FROM orchestra_events WHERE run_id=%s", (run_id,))
        cur.execute("DELETE FROM orchestra_actors WHERE run_id=%s", (run_id,))
        cur.execute("DELETE FROM orchestra_runs WHERE run_id=%s", (run_id,))


def test_claim_lock_timeout_retries_then_converges_once(monkeypatch):
    """A short actor lock crosses one bounded attempt; rollback + retry claims the event exactly once."""
    tenant = _tenant("claim")
    run = store.start_run(tenant, "bounded claim")
    release = threading.Event()
    locked = threading.Event()
    holder = None
    try:
        actor = store.spawn_actor(run["run_id"], tenant, "worker", "engineer")
        event = store.emit(run["run_id"], tenant, None, actor["actor_id"], "task", {"task": "x"})

        def hold_actor_row():
            with store._conn(tenant) as conn, conn.cursor() as cur:
                cur.execute("SELECT actor_id FROM orchestra_actors WHERE actor_id=%s FOR UPDATE",
                            (actor["actor_id"],))
                locked.set()
                release.wait(2)
                conn.rollback()

        holder = threading.Thread(target=hold_actor_row, daemon=True)
        holder.start()
        assert locked.wait(2)
        monkeypatch.setenv("AOS_ORCHESTRA_DB_LOCK_TIMEOUT_MS", "40")
        monkeypatch.setenv("AOS_ORCHESTRA_DB_STATEMENT_TIMEOUT_MS", "500")
        monkeypatch.setenv("AOS_ORCHESTRA_DB_RETRIES", "3")
        monkeypatch.setattr(store.random, "random", lambda: 0.0)
        timer = threading.Timer(0.075, release.set)
        timer.start()
        claimed = store.claim_events(actor["actor_id"], tenant, claimed_by="bounded-owner")
        timer.join(1)
        holder.join(1)

        assert [item["id"] for item in claimed] == [event["id"]]
        assert store.claim_events(actor["actor_id"], tenant, claimed_by="other-owner") == []
        rows = [item for item in store.events(run["run_id"], tenant) if item["id"] == event["id"]]
        assert len(rows) == 1 and rows[0]["claimed_by"] == "bounded-owner"
    finally:
        release.set()
        if holder:
            holder.join(1)
        _cleanup(run["run_id"])


def test_persist_query_timeout_rolls_back_then_replay_commits_exactly_once(monkeypatch):
    """Every timed-out attempt is empty; the same durable step can then land one actor/event transition."""
    tenant = _tenant("statement")
    run = store.start_run(tenant, "bounded persist")
    try:
        supervisor = store.spawn_actor(run["run_id"], tenant, "lead", "lead")
        actor = store.spawn_actor(run["run_id"], tenant, "worker", "engineer",
                                  supervisor_id=supervisor["actor_id"])
        source = store.emit(run["run_id"], tenant, supervisor["actor_id"], actor["actor_id"],
                            "task", {"task": "x"})
        claimed = store.claim_events(actor["actor_id"], tenant, claimed_by="runtime-a")
        original_timeouts = store._set_step_timeouts

        monkeypatch.setenv("AOS_ORCHESTRA_DB_STATEMENT_TIMEOUT_MS", "25")
        monkeypatch.setenv("AOS_ORCHESTRA_DB_RETRIES", "2")
        monkeypatch.setattr(store.random, "random", lambda: 0.0)

        def force_statement_timeout(cur):
            original_timeouts(cur)
            cur.execute("SELECT pg_sleep(0.06)")

        monkeypatch.setattr(store, "_set_step_timeouts", force_statement_timeout)
        refused = store.persist_step(
            run["run_id"], tenant, actor["actor_id"], status="done",
            emits=[(actor["actor_id"], supervisor["actor_id"], "done", {"ok": True}, "one-result")],
            complete_ids=[source["id"]])
        assert refused["retryable"] is True and refused["attempts"] == 2
        monkeypatch.setattr(store, "_set_step_timeouts", original_timeouts)

        assert store.actor(actor["actor_id"], tenant)["status"] != "done"
        assert next(item for item in store.events(run["run_id"], tenant)
                    if item["id"] == source["id"])["processed_at"] is None
        assert not [item for item in store.events(run["run_id"], tenant)
                    if item["corr_id"] == "one-result"]

        committed = store.persist_step(
            run["run_id"], tenant, actor["actor_id"], status="done",
            emits=[(actor["actor_id"], supervisor["actor_id"], "done", {"ok": True}, "one-result")],
            complete_ids=[item["id"] for item in claimed])
        assert committed["ok"] and committed["emitted"] == 1 and committed["completed"] == 1
        assert len([item for item in store.events(run["run_id"], tenant)
                    if item["corr_id"] == "one-result"]) == 1
    finally:
        _cleanup(run["run_id"])


def test_terminal_run_fence_rejects_late_step_without_partial_state():
    """A worker returning after halt cannot mutate its actor, emit, or consume its inbox."""
    tenant = _tenant("halt")
    run = store.start_run(tenant, "late commit")
    try:
        actor = store.spawn_actor(run["run_id"], tenant, "worker", "engineer")
        source = store.emit(run["run_id"], tenant, None, actor["actor_id"], "task", {"task": "x"})
        store.claim_events(actor["actor_id"], tenant, claimed_by="late-worker")
        assert store.finish_run(run["run_id"], "halted", {"reason": "deadline"}, tenant)["status"] == "halted"

        late = store.persist_step(
            run["run_id"], tenant, actor["actor_id"], status="done",
            emits=[(actor["actor_id"], actor["actor_id"], "done", {}, "late-result")],
            complete_ids=[source["id"]], claimed_by="late-worker")
        assert late["halted"] is True and late["run_status"] == "halted"
        assert store.actor(actor["actor_id"], tenant)["status"] != "done"
        events = store.events(run["run_id"], tenant)
        assert next(item for item in events if item["id"] == source["id"])["processed_at"] is None
        assert not [item for item in events if item["corr_id"] == "late-result"]
    finally:
        _cleanup(run["run_id"])


def test_root_completion_and_run_terminal_transition_are_one_commit():
    """Correct fencing does not create a gap between the root's final checkpoint and run completion."""
    tenant = _tenant("root")
    run = store.start_run(tenant, "atomic root finish")
    try:
        actor = store.spawn_actor(run["run_id"], tenant, "root", "controller", kind="controller")
        source = store.emit(run["run_id"], tenant, None, actor["actor_id"], "task", {"task": "x"})
        assert store.claim_actor_step(actor["actor_id"], tenant, claimed_by="root-worker")
        claimed = store.claim_events(actor["actor_id"], tenant, claimed_by="root-worker")
        result = {"ok": True, "result": "complete"}
        saved = store.persist_step(run["run_id"], tenant, actor["actor_id"], status="done",
                                   result=result, complete_ids=[item["id"] for item in claimed],
                                   claimed_by="root-worker", finish_status="done")
        assert saved["ok"] and saved["run_status"] == "done"
        assert store.run(run["run_id"], tenant)["status"] == "done"
        assert store.actor(actor["actor_id"], tenant)["status"] == "done"
        assert next(item for item in store.events(run["run_id"], tenant)
                    if item["id"] == source["id"])["processed_at"] is not None
    finally:
        _cleanup(run["run_id"])


def test_step_exception_immediately_releases_only_its_event_claims(monkeypatch):
    released = []
    monkeypatch.setattr(runtime, "_worker_step", lambda *_: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(runtime.store, "release_event_claims",
                        lambda ids, tenant, claimed_by=None: released.append((ids, tenant, claimed_by)))
    ctx = runtime._Ctx(1, "tenant-a", ".", None, 60, None)
    runtime._execute_step(ctx, {"actor_id": 3, "kind": "worker", "name": "worker"},
                          [{"id": 7}], "owner-a")
    assert released == [([7], "tenant-a", "owner-a")]
    assert len(ctx.errors) == 1 and "boom" in ctx.errors[0]


def test_run_org_deadline_cooperatively_stops_and_joins_pool(monkeypatch):
    import jobrunner

    monkeypatch.setattr(runtime.store, "ensure", lambda: None)
    monkeypatch.setattr(runtime.store, "release_stale_claims", lambda *a, **k: 0)
    monkeypatch.setattr(runtime.store, "run", lambda *a, **k: {"status": "running"})
    monkeypatch.setattr(jobrunner, "reconcile_parked", lambda *a, **k: 0)
    monkeypatch.setattr(runtime, "_pool_loop", lambda ctx, *_: ctx.stop.wait(2))

    started = time.time()
    out = runtime.run_org(1, "deadline-test", workers=2, deadline=started + 0.03,
                          stop_join_s=0.2)
    assert time.time() - started < 0.35
    assert out["deadline_exceeded"] is True and out["threads_alive"] == 0


def test_runtime_survivors_are_explicit_cleanup_telemetry():
    facts = qa_agentic._cleanup_facts(3, 0, runtime_threads_alive=2)
    assert facts == {"cleanup_threads_incomplete": 5,
                     "cleanup_runtime_threads_incomplete": 2,
                     "cleanup_processes_incomplete": 0,
                     "cleanup_process_contained": True,
                     "cleanup_incomplete": 5}
