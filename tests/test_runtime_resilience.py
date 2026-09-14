"""Focused crash/concurrency harnesses for the live controller→orchestra path.

Every database fixture is isolated by a random tenant/run/thread and explicitly
deleted. These tests never call a global resume sweep or touch product files.
"""
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
ORCHESTRA = SCRIPTS / "orchestra"
for path in (str(ORCHESTRA), str(SCRIPTS)):
    if path not in sys.path:
        sys.path.insert(0, path)

import jobrunner
import jobd
import clauded
import loopcontroller as lc
import runtime
import store
from dbpool import connection


def _tenant(label):
    return f"resilience-{label}-{uuid.uuid4().hex}"


def _cleanup_run(run_id):
    with connection() as c, c.cursor() as cur:
        cur.execute("DELETE FROM orchestra_tool_leases WHERE run_id=%s", (run_id,))
        cur.execute("DELETE FROM orchestra_events WHERE run_id=%s", (run_id,))
        cur.execute("DELETE FROM orchestra_actors WHERE run_id=%s", (run_id,))
        cur.execute("DELETE FROM orchestra_runs WHERE run_id=%s", (run_id,))


def test_runtime_dispatch_query_returns_only_actors_with_pending_inbox_work():
    tenant = _tenant("pending-dispatch")
    run = store.start_run(tenant, "indexed pending dispatch")
    try:
        target = store.spawn_actor(run["run_id"], tenant, "target", "engineer")
        idle = store.spawn_actor(run["run_id"], tenant, "historical-idle", "engineer")
        terminal = store.spawn_actor(run["run_id"], tenant, "terminal", "engineer")
        store.update_actor(terminal["actor_id"], tenant, status="done")

        event = store.emit(run["run_id"], tenant, None, target["actor_id"], "task", {"task": "work"})
        assert [item["actor_id"] for item in
                store.actors_with_pending_events(run["run_id"], tenant)] == [target["actor_id"]]
        claimed = store.claim_events(target["actor_id"], tenant, claimed_by="dispatch-test")
        store.complete_event(claimed[0]["id"], tenant)
        assert store.actors_with_pending_events(run["run_id"], tenant) == []

        # Terminal recipients remain visible so runtime can drain stale mail instead of leaking it forever.
        store.emit(run["run_id"], tenant, None, terminal["actor_id"], "task", {"task": "stale"})
        pending = store.actors_with_pending_events(run["run_id"], tenant)
        assert [item["actor_id"] for item in pending] == [terminal["actor_id"]]
        assert idle["actor_id"] not in {item["actor_id"] for item in pending}
        assert event["id"] > 0
    finally:
        _cleanup_run(run["run_id"])


def test_live_tool_lease_heartbeats_child_and_waiting_supervisor_chain():
    parents = {33: 22, 22: 11, 11: None}
    beats = []

    class Store:
        @staticmethod
        def heartbeat(actor_id, tenant):
            beats.append((actor_id, tenant))

        @staticmethod
        def actor(actor_id, _tenant):
            return {"actor_id": actor_id, "supervisor_id": parents[actor_id]}

    jobrunner._heartbeat_actor_chain(Store, {"actor_id": 33, "tenant": "tenant-a"})

    assert beats == [(33, "tenant-a"), (22, "tenant-a"), (11, "tenant-a")]


def test_expired_actor_claim_takeover_fences_stale_persist():
    """Concurrent resume may take over, but the obsolete runtime cannot commit afterward."""
    ctx_a = runtime._Ctx(1, "t", ".", None, 60, None)
    ctx_b = runtime._Ctx(1, "t", ".", None, 60, None)
    assert ctx_a.worker_name(0) != ctx_b.worker_name(0)
    tenant = _tenant("claim")
    run = store.start_run(tenant, "claim fencing")
    try:
        actor = store.spawn_actor(run["run_id"], tenant, "worker", "engineer")
        assert store.claim_actor_step(actor["actor_id"], tenant, claimed_by="runtime-a", lease_s=60)
        with store._conn(tenant) as c, c.cursor() as cur:
            cur.execute("""UPDATE orchestra_actors
                           SET step_claimed_at=now()-interval '2 minutes'
                           WHERE actor_id=%s""", (actor["actor_id"],))
        assert store.claim_actor_step(actor["actor_id"], tenant, claimed_by="runtime-b", lease_s=60)

        stale = store.persist_step(run["run_id"], tenant, actor["actor_id"],
                                   status="done", claimed_by="runtime-a")
        assert stale.get("error") == "actor step lease is no longer owned"
        assert store.actor(actor["actor_id"], tenant)["status"] != "done"

        winner = store.persist_step(run["run_id"], tenant, actor["actor_id"],
                                    status="working", claimed_by="runtime-b")
        assert winner["ok"] and store.actor(actor["actor_id"], tenant)["status"] == "working"
    finally:
        _cleanup_run(run["run_id"])


def test_controlled_handoff_probe_sees_in_process_orchestra_claims():
    """A child-free coordinator step is still active work and must fence a rolling handoff."""
    tenant = _tenant("handoff-claim")
    run = store.start_run(tenant, "handoff claim fencing")
    owner_pid = 987654321
    owner = f"{owner_pid}-test-runtime:orgw-0"
    try:
        actor = store.spawn_actor(run["run_id"], tenant, "coordinator", "qa-coordinator")
        store.emit(run["run_id"], tenant, None, actor["actor_id"], "task", {"task": "decide"})
        assert store.claim_actor_step(actor["actor_id"], tenant, claimed_by=owner)
        claimed = store.claim_events(actor["actor_id"], tenant, claimed_by=owner)
        assert claimed

        active = lc._worker_orchestra_claims(owner_pid)

        assert [item["actor_id"] for item in active["actor_steps"]] == [actor["actor_id"]]
        assert [item["event_id"] for item in active["events"]] == [claimed[0]["id"]]
        assert active["events"][0]["claimed_by"] == owner
    finally:
        _cleanup_run(run["run_id"])


def test_persist_failure_between_actor_update_and_emit_rolls_back_everything():
    """A crash-shaped emit failure cannot leave actor state ahead of its handoff event."""
    tenant = _tenant("atomic")
    run = store.start_run(tenant, "atomic failure")
    try:
        supervisor = store.spawn_actor(run["run_id"], tenant, "lead", "lead")
        actor = store.spawn_actor(run["run_id"], tenant, "worker", "engineer",
                                  supervisor_id=supervisor["actor_id"])
        event = store.emit(run["run_id"], tenant, supervisor["actor_id"], actor["actor_id"],
                           "task", {"task": "work"})
        claimed = store.claim_events(actor["actor_id"], tenant, claimed_by="runtime-a")
        before = store.actor(actor["actor_id"], tenant)["status"]
        with pytest.raises(TypeError):
            store.persist_step(
                run["run_id"], tenant, actor["actor_id"], status="done",
                emits=[(actor["actor_id"], supervisor["actor_id"], "done",
                        {"not_json": object()}, "bad-emit")],
                complete_ids=[e["id"] for e in claimed])
        assert store.actor(actor["actor_id"], tenant)["status"] == before
        row = next(e for e in store.events(run["run_id"], tenant) if e["id"] == event["id"])
        assert row["processed_at"] is None
        assert not any(e["corr_id"] == "bad-emit" for e in store.events(run["run_id"], tenant))
    finally:
        _cleanup_run(run["run_id"])


def test_replayed_parent_hire_reuses_child_and_kickoff_event():
    """Crash after hire but before parent persist replays to exactly one child and one task."""
    tenant = _tenant("hire")
    run = store.start_run(tenant, "idempotent hire")
    try:
        lead = store.spawn_actor(run["run_id"], tenant, "lead", "lead", kind="supervisor")
        ctx = type("Ctx", (), {"run_id": run["run_id"], "tenant": tenant, "repo": "."})()
        spec = {"name": "worker-1", "role": "engineer", "kind": "worker", "task": "build x"}
        key = runtime._hire_key(lead["actor_id"], "parent-event-1", spec, 0)
        first = runtime._hire(ctx, lead["actor_id"], spec, hire_key=key)
        second = runtime._hire(ctx, lead["actor_id"], spec, hire_key=key)
        assert first == second
        children = [a for a in store.actors(run["run_id"], tenant)
                    if a["supervisor_id"] == lead["actor_id"]]
        kickoffs = [e for e in store.events(run["run_id"], tenant)
                    if e["corr_id"] == f"hire-{key}-task"]
        assert len(children) == 1 and children[0]["hire_key"] == key
        assert len(kickoffs) == 1 and kickoffs[0]["to_actor"] == first
    finally:
        _cleanup_run(run["run_id"])


def test_safety_slice_rollover_persists_attempt_then_reconciles_same_actor(monkeypatch):
    """A QA deadline is a durable handoff, not completion or a fresh assignment."""
    captured = {}
    actor = {
        "actor_id": 81, "supervisor_id": 80, "name": "qa-1", "role": "qa-explorer",
        "status": "blocked", "assignment": "US-1", "hired_at": "now",
        "memory": {"context": {"tool": "qa_explore", "tool_dispatched": True,
                                 "tool_attempt": 0, "tool_args": {"story": {"id": "US-1"}}}},
    }
    event = {"id": 9, "kind": "tool_result", "corr_id": "job-9", "frm": 81,
             "payload": {"tool": "qa_explore", "status": "failed", "findings": [],
                         "result": {"error": "tool run cancelled by QA safety deadline",
                                    "story": "US-1"}}}
    ctx = type("Ctx", (), {"run_id": 8, "tenant": "t", "repo": "."})()

    def capture(_ctx, _actor, step, _events):
        captured["step"] = step

    monkeypatch.setattr(runtime, "_persist", capture)
    runtime._worker_step(ctx, actor, [event])
    step = captured["step"]
    assert step.status == "blocked" and step.result["checkpointed"] is True
    assert step.memory["context"]["tool_attempt"] == 1

    resumed_actor = {**actor, "memory": step.memory, "status": "blocked"}
    jobs = []
    fake = type("Store", (), {
        "actors": staticmethod(lambda *_: [resumed_actor]),
        "events": staticmethod(lambda *_: [{"to_actor": 81, "kind": "tool_result",
                                             "processed_at": "done", "corr_id": "job-8:81:qa_explore:0-result"}]),
    })
    monkeypatch.setattr(jobrunner, "reconcile", lambda pending, *_a, **_k: jobs.extend(pending) or len(pending))
    assert jobrunner.reconcile_parked(fake, 8, "t") == 1
    assert len(jobs) == 1 and jobs[0]["actor_id"] == 81
    assert jobs[0]["attempt"] == 1 and jobs[0]["resumed"] is True


def test_browser_capacity_wait_checkpoints_same_actor_without_gapfill(monkeypatch):
    """Normal queue pressure must not finish a story or manufacture a replacement employee."""
    captured = {}
    actor = {
        "actor_id": 82, "supervisor_id": 80, "name": "qa-capacity", "role": "qa-explorer",
        "status": "blocked", "assignment": "US-7", "hired_at": "now",
        "memory": {"context": {"tool": "qa_explore", "tool_dispatched": True,
                                "tool_attempt": 0, "tool_args": {"story": {"id": "US-7"}}}},
    }
    event = {"id": 10, "kind": "tool_result", "corr_id": "job-capacity", "frm": 82,
             "payload": {"tool": "qa_explore", "status": "failed", "findings": [],
                         "result": {"error": "RuntimeError: browser capacity exhausted — no QA browser slot available",
                                    "stop_reason": "capacity-wait-checkpoint",
                                    "checkpoint_required": True, "capacity_wait": True,
                                    "story": "US-7"}}}
    ctx = type("Ctx", (), {"run_id": 8, "tenant": "t", "repo": "."})()

    monkeypatch.setattr(runtime, "_persist",
                        lambda _ctx, _actor, step, _events: captured.setdefault("step", step))
    returned = runtime._worker_step(ctx, actor, [event])
    step = captured["step"]
    assert returned is step
    assert step.status == "blocked" and step.result["checkpointed"] is True
    assert step.memory["context"]["tool_attempt"] == 1
    assert step.emits == [], "the supervisor must receive neither done nor finding for a capacity wait"


def test_stale_heartbeat_reaper_is_scoped_and_preserves_fresh_worker():
    """Silence, not age alone, reaps a worker; the harness cannot inspect other live threads."""
    lc._ensure()
    tenant = _tenant("heartbeat")
    base = 980000000 + int(uuid.uuid4().hex[:6], 16)
    tids = [base, base + 1]
    try:
        with connection() as c, c.cursor() as cur:
            for tid in tids:
                cur.execute("""INSERT INTO controller_state
                               (thread_id,tenant_id,org_id,phase,awaiting,updated_at,execution_scope)
                               VALUES (%s,%s,1,'PROTOTYPE','fleet',now(),'test')""", (tid, tenant))
            cur.execute("""INSERT INTO controller_jobs
                           (thread_id,tenant_id,phase,kind,status,started_at,heartbeat_at,execution_scope)
                           VALUES (%s,%s,'PROTOTYPE','design','running',now()-interval '40 min',now(),'test'),
                                  (%s,%s,'PROTOTYPE','design','running',now()-interval '40 min',
                                   now()-interval '10 min','test')""", (tids[0], tenant, tids[1], tenant))
        assert lc._reap_dead_jobs(thread_ids=tids, execution_scope="test") == 1
        with connection() as c, c.cursor() as cur:
            cur.execute("SELECT thread_id,status FROM controller_jobs WHERE thread_id=ANY(%s)", (tids,))
            statuses = dict(cur.fetchall())
        assert statuses == {tids[0]: "running", tids[1]: "failed"}
    finally:
        with connection() as c, c.cursor() as cur:
            cur.execute("DELETE FROM controller_jobs WHERE thread_id=ANY(%s)", (tids,))
            cur.execute("DELETE FROM controller_state WHERE thread_id=ANY(%s)", (tids,))


def test_concurrent_dispatch_creates_one_job_and_runs_one_phase(monkeypatch):
    """The inner SELECT→INSERT guard remains single-writer without an outer drive lock."""
    lc._ensure()
    tenant = _tenant("dispatch")
    tid = 990000000 + int(uuid.uuid4().hex[:6], 16)
    calls = []
    barrier = threading.Barrier(3)

    def caller(results):
        barrier.wait()
        results.append(lc._dispatch(tid, "design", fn=lambda: {}, eta_min=1))

    try:
        with connection() as c, c.cursor() as cur:
            cur.execute("""INSERT INTO controller_state
                           (thread_id,tenant_id,org_id,phase,awaiting,updated_at,execution_scope)
                           VALUES (%s,%s,1,'OPTIONS',NULL,now(),'test')""", (tid, tenant))
        monkeypatch.setattr(lc, "_spawn_parked_worker",
                            lambda thread_id, kind, jid: calls.append((thread_id, kind, jid)) or True)
        results = []
        threads = [threading.Thread(target=caller, args=(results,)) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)
        with connection() as c, c.cursor() as cur:
            cur.execute("SELECT count(*) FROM controller_jobs WHERE thread_id=%s", (tid,))
            assert cur.fetchone()[0] == 1
        assert len(calls) == 1 and sum(r is not None for r in results) == 1
    finally:
        with connection() as c, c.cursor() as cur:
            cur.execute("DELETE FROM controller_jobs WHERE thread_id=%s", (tid,))
            cur.execute("DELETE FROM controller_state WHERE thread_id=%s", (tid,))


def test_global_dispatch_capacity_queues_competing_threads(monkeypatch):
    """Direct completion paths obey the global cap and leave excess work runnable."""
    lc._ensure()
    tenant = _tenant("capacity")
    base = 992000000 + int(uuid.uuid4().hex[:6], 16)
    tids = [base, base + 1]
    barrier = threading.Barrier(3)
    calls = []
    results = []

    with connection() as c, c.cursor() as cur:
        # Capacity is isolated by execution scope so production work cannot make a test fixture flaky (and
        # conversely, tests cannot consume the production budget).
        cur.execute("""SELECT count(*) FROM controller_jobs
                        WHERE status IN ('running','pending') AND execution_scope='test'""")
        baseline = int(cur.fetchone()[0])

    def caller(tid):
        barrier.wait()
        results.append(lc._dispatch(tid, "design", fn=lambda: {}, eta_min=1))

    try:
        with connection() as c, c.cursor() as cur:
            for tid in tids:
                cur.execute("""INSERT INTO controller_state
                               (thread_id,tenant_id,org_id,phase,awaiting,updated_at,execution_scope)
                               VALUES (%s,%s,1,'OPTIONS',NULL,now(),'test')""", (tid, tenant))
        monkeypatch.setattr(lc, "_spawn_parked_worker",
                            lambda thread_id, kind, jid: calls.append((thread_id, kind, jid)) or True)
        monkeypatch.setattr(lc, "_MAX_ACTIVE_CONTROLLER_JOBS", baseline + 1)
        workers = [threading.Thread(target=caller, args=(tid,)) for tid in tids]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join(timeout=5)

        with connection() as c, c.cursor() as cur:
            cur.execute("SELECT thread_id FROM controller_jobs WHERE thread_id=ANY(%s)", (tids,))
            dispatched = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT thread_id,awaiting FROM controller_state WHERE thread_id=ANY(%s)", (tids,))
            states = dict(cur.fetchall())
        assert len(dispatched) == 1 and len(calls) == 1
        queued = next(tid for tid in tids if tid not in dispatched)
        assert states[queued] is None, "capacity pressure must remain runnable, never become a gate"
        assert sum(result is not None for result in results) == 1
    finally:
        with connection() as c, c.cursor() as cur:
            cur.execute("DELETE FROM controller_jobs WHERE thread_id=ANY(%s)", (tids,))
            cur.execute("DELETE FROM controller_state WHERE thread_id=ANY(%s)", (tids,))


def test_concurrent_jobd_ticks_have_one_thread_driver(monkeypatch):
    """Two daemon instances may discover one runnable, but only one advances it."""
    tid = 995000000 + int(uuid.uuid4().hex[:6], 16)
    entered = threading.Event()
    release = threading.Event()
    barrier = threading.Barrier(3)
    advances = []
    outputs = []

    monkeypatch.setattr(jobd, "runnable_threads", lambda *_a, **_k: [tid])
    monkeypatch.setattr(jobd, "queued_runnables", lambda *_a, **_k: [])
    monkeypatch.setattr(lc, "resume_stalled", lambda *_a, **_k: {"resumed": 0})
    monkeypatch.setattr(clauded, "reap", lambda: {"reaped": 0})

    def advance(thread_id):
        advances.append(thread_id)
        entered.set()
        release.wait(5)

    monkeypatch.setattr(lc, "advance", advance)

    def tick():
        barrier.wait()
        outputs.append(jobd.tick())

    workers = [threading.Thread(target=tick) for _ in range(2)]
    for worker in workers:
        worker.start()
    barrier.wait()
    assert entered.wait(2)
    time.sleep(0.1)  # let the competing tick observe the held advisory lock
    release.set()
    for worker in workers:
        worker.join(timeout=5)
    assert advances == [tid]
    assert sum(item["driven"] for item in outputs) == 1


def test_completion_handoff_recovers_without_rerunning_tool(monkeypatch):
    """Lost event-delivery acknowledgements retry the cheap handoff, never the browser work."""
    attempts = {"tool": 0, "emit": 0}

    class FlakyStore:
        def emit_once(self, *args):
            attempts["emit"] += 1
            if attempts["emit"] <= 5:
                raise ConnectionError("lost acknowledgement")
            return {"id": 1}

    def tool(_name, _args):
        attempts["tool"] += 1
        return {"status": "done", "findings": [], "result": {"story": "US-1"}}

    job = {"run_id": 71, "tenant": "t", "actor_id": 72, "tool": "qa_explore",
           "attempt": 0, "args": {"story": {"id": "US-1"}}}
    jid = jobrunner.dispatch(job, FlakyStore(), run_tool=tool, sync=True)
    assert jobrunner._JOBS[jid]["state"] == "completion_pending"
    assert jobrunner.reconcile([job], FlakyStore(), run_tool=tool) == 0
    assert jobrunner._JOBS[jid]["state"] == "done"
    assert attempts["tool"] == 1 and attempts["emit"] == 6
    with jobrunner._LOCK:
        jobrunner._JOBS.pop(jid, None)
