"""Seeded accelerated fault schedules over the durable controller/orchestra spine.

The harness uses isolated database fixtures and parked-worker stubs: it exercises
the real transition/claim/persist code without launching daemons, browsers, or
touching any existing workstream.
"""
import random
import sys
import threading
import uuid
from collections import Counter, defaultdict
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
ORCHESTRA = SCRIPTS / "orchestra"
for path in (str(ORCHESTRA), str(SCRIPTS)):
    if path not in sys.path:
        sys.path.insert(0, path)

import jobrunner
import loopcontroller as lc
import runtime
import store
from dbpool import connection


SEEDS = (17, 711, 20260815)


def _tenant(seed):
    return f"fault-soak-{seed}-{uuid.uuid4().hex}"


def _cleanup_run(run_id):
    with connection() as c, c.cursor() as cur:
        # The real duty-manager scheduler may observe the intentionally stale
        # pulse fixtures while this soak is running. Retire the complete test
        # control-plane footprint, not only the orchestra rows, so an offline
        # regression cannot manufacture live manager work minutes later.
        cur.execute("SELECT tenant_id FROM orchestra_runs WHERE run_id=%s", (run_id,))
        row = cur.fetchone()
        tenant = row[0] if row else None
        if tenant:
            cur.execute("SELECT case_id FROM management_cases WHERE tenant_id=%s", (tenant,))
            case_ids = [r[0] for r in cur.fetchall()]
            if case_ids:
                cur.execute("DELETE FROM management_questions WHERE case_id=ANY(%s)", (case_ids,))
                cur.execute("DELETE FROM management_decisions WHERE case_id=ANY(%s)", (case_ids,))
                cur.execute("DELETE FROM management_events WHERE case_id=ANY(%s)", (case_ids,))
                cur.execute("DELETE FROM management_cases WHERE case_id=ANY(%s)", (case_ids,))
        cur.execute("DELETE FROM agent_pulse WHERE work_id LIKE %s", (f"{run_id}:%",))
        cur.execute("DELETE FROM orchestra_tool_leases WHERE run_id=%s", (run_id,))
        cur.execute("DELETE FROM orchestra_events WHERE run_id=%s", (run_id,))
        cur.execute("DELETE FROM orchestra_actors WHERE run_id=%s", (run_id,))
        cur.execute("DELETE FROM orchestra_runs WHERE run_id=%s", (run_id,))


def _expire_claims(actor_id):
    with connection() as c, c.cursor() as cur:
        cur.execute("""UPDATE orchestra_actors
                       SET step_claimed_at=now()-interval '10 minutes'
                       WHERE actor_id=%s""", (actor_id,))
        cur.execute("""UPDATE orchestra_events
                       SET claimed_at=now()-interval '10 minutes'
                       WHERE to_actor=%s AND processed_at IS NULL""", (actor_id,))


@pytest.mark.parametrize("seed", SEEDS)
def test_seeded_orchestra_fault_matrix_converges(seed):
    """Seeded virtual slices preserve exactly-once hires/results and drain all work."""
    rng = random.Random(seed)
    tenant = _tenant(seed)
    run = store.start_run(tenant, f"seeded fault soak {seed}")
    tool_handles = []
    try:
        root = store.spawn_actor(run["run_id"], tenant, "manager", "manager", kind="supervisor")
        ctx = type("Ctx", (), {"run_id": run["run_id"], "tenant": tenant, "repo": "."})()
        items = []
        for index in range(10):
            spec = {"name": f"worker-{index}", "role": "engineer", "kind": "worker",
                    "task": f"work-{index}"}
            key = runtime._hire_key(root["actor_id"], f"assignment-{index}", spec, index)
            actor_id = runtime._hire(ctx, root["actor_id"], spec, hire_key=key)
            # Crash after the child commit but before the parent checkpoint: replay the hire.
            assert runtime._hire(ctx, root["actor_id"], spec, hire_key=key) == actor_id
            items.append({"actor": actor_id, "key": key, "owner": None, "generation": 0})

        # Exercise real tool completion idempotency. Each wrapper commits the result and then
        # loses five acknowledgements, forcing completion_pending + reconcile without rerunning.
        tool_calls = Counter()

        class LostAckStore:
            def __init__(self):
                self.attempts = defaultdict(int)

            def emit_once(self, *args):
                corr = args[-1]
                row = store.emit_once(*args)
                self.attempts[corr] += 1
                if self.attempts[corr] <= 5:
                    raise ConnectionError("seeded acknowledgement loss")
                return row

        flaky = LostAckStore()
        for item in items[:3]:
            job = {"run_id": run["run_id"], "tenant": tenant, "actor_id": item["actor"],
                   "tool": "soak_tool", "attempt": 0, "args": {},
                   "assignment": item["key"]}

            def run_tool(_name, _args, actor=item["actor"]):
                tool_calls[actor] += 1
                return {"status": "done", "findings": [], "result": {"ok": True}}

            jid = jobrunner.dispatch(job, flaky, run_tool=run_tool, sync=True)
            assert jobrunner._JOBS[jid]["state"] == "completion_pending"
            tool_handles.append((jid, job, run_tool))

        for jid, job, run_tool in tool_handles:
            assert jobrunner.reconcile([job], flaky, run_tool=run_tool) == 0
            assert jobrunner._JOBS[jid]["state"] == "done"

        # Seeded interleavings inject abandoned claims, stale owners, transactional
        # persist failures, duplicate hire replay, and ordinary progress.
        for virtual_slice in range(400):
            item = rng.choice(items)
            actor_id = item["actor"]
            action = rng.randrange(20)
            if action >= 7:  # crash before a transition: no durable state changes
                continue
            actor = store.actor(actor_id, tenant)

            if action == 0:  # replay after an uncertain hire acknowledgement
                spec = {"name": f"worker-{items.index(item)}", "role": "engineer",
                        "kind": "worker", "task": f"work-{items.index(item)}"}
                assert runtime._hire(ctx, root["actor_id"], spec,
                                     hire_key=item["key"]) == actor_id
            elif action in (1, 2) and actor["status"] != "done":
                item["generation"] += 1
                owner = f"seed-{seed}-v{item['generation']}"
                if store.claim_actor_step(actor_id, tenant, claimed_by=owner, lease_s=0):
                    item["owner"] = owner
                    store.claim_events(actor_id, tenant, claimed_by=owner, lease_s=0)
            elif action == 3 and item["owner"] and actor["status"] != "done":
                # A stale process tries to commit after a deterministic takeover.
                stale = item["owner"]
                _expire_claims(actor_id)
                item["generation"] += 1
                winner = f"seed-{seed}-v{item['generation']}"
                assert store.claim_actor_step(actor_id, tenant, claimed_by=winner, lease_s=1)
                rejected = store.persist_step(run["run_id"], tenant, actor_id,
                                              status="done", claimed_by=stale)
                assert rejected.get("error") == "actor step lease is no longer owned"
                item["owner"] = winner
            elif action == 4 and item["owner"] and actor["status"] != "done":
                # JSON encoding fails after the actor UPDATE statement; the transaction
                # must roll back both state and inbox completion.
                before = store.actor(actor_id, tenant)["status"]
                with pytest.raises(TypeError):
                    store.persist_step(
                        run["run_id"], tenant, actor_id, status="done",
                        emits=[(actor_id, root["actor_id"], "done",
                                {"bad": object()}, f"bad-{seed}-{virtual_slice}")],
                        claimed_by=item["owner"])
                assert store.actor(actor_id, tenant)["status"] == before
            elif action == 5 and item["owner"] and actor["status"] != "done":
                claimed = store.claim_events(actor_id, tenant, claimed_by=item["owner"], lease_s=0)
                task_ids = [event["id"] for event in claimed if event["kind"] == "task"]
                if task_ids:
                    result = store.persist_step(
                        run["run_id"], tenant, actor_id, status="done", result={"ok": True},
                        emits=[(actor_id, root["actor_id"], "done", {"ok": True},
                                f"done-{item['key']}")], complete_ids=task_ids,
                        claimed_by=item["owner"])
                    assert result["ok"]
                    store.release_actor_step(actor_id, tenant, claimed_by=item["owner"])
            elif action == 6 and item["owner"] and actor["status"] != "done":
                # Simulated worker cancellation: abandon the lease; expiry makes the
                # same durable assignment available to a later virtual slice.
                _expire_claims(actor_id)
                item["owner"] = None

        # Fair recovery tail: regardless of the adversarial prefix, every durable
        # runnable gets a fresh owner and converges without a human-facing gate.
        for item in items:
            actor_id = item["actor"]
            if store.actor(actor_id, tenant)["status"] == "done":
                continue
            _expire_claims(actor_id)
            owner = f"seed-{seed}-drain-{actor_id}"
            assert store.claim_actor_step(actor_id, tenant, claimed_by=owner, lease_s=1)
            claimed = store.claim_events(actor_id, tenant, claimed_by=owner, lease_s=0)
            task_ids = [event["id"] for event in claimed if event["kind"] == "task"]
            assert task_ids, "a cancelled/taken-over worker must retain its runnable assignment"
            result = store.persist_step(
                run["run_id"], tenant, actor_id, status="done", result={"ok": True},
                emits=[(actor_id, root["actor_id"], "done", {"ok": True},
                        f"done-{item['key']}")], complete_ids=task_ids, claimed_by=owner)
            assert result["ok"]

        actors = store.actors(run["run_id"], tenant)
        events = store.events(run["run_id"], tenant)
        children = [actor for actor in actors if actor["supervisor_id"] == root["actor_id"]]
        assert len(children) == len(items)
        assert len({actor["hire_key"] for actor in children}) == len(items)
        assert all(actor["status"] == "done" for actor in children)
        for item in items:
            assert sum(event["corr_id"] == f"hire-{item['key']}-task" for event in events) == 1
            assert sum(event["corr_id"] == f"done-{item['key']}" for event in events) == 1
        for actor_id in tool_calls:
            corr = f"job-{jobrunner.job_id_for(next(job for _, job, _ in tool_handles if job['actor_id'] == actor_id))}-result"
            assert tool_calls[actor_id] == 1
            assert sum(event["corr_id"] == corr for event in events) == 1
    finally:
        with jobrunner._LOCK:
            for jid, _job, _tool in tool_handles:
                jobrunner._JOBS.pop(jid, None)
        _cleanup_run(run["run_id"])


@pytest.mark.parametrize("seed", SEEDS)
def test_seeded_controller_dispatch_soak_is_bounded_and_converges(seed, monkeypatch):
    """Seeded scheduler slices never duplicate active work or lose a runnable."""
    rng = random.Random(seed)
    tenant = _tenant(seed)
    base = 996000000 + int(uuid.uuid4().hex[:6], 16)
    tids = [base + index for index in range(12)]
    capacity = 3
    dispatches = Counter()

    lc._ensure()
    monkeypatch.setattr(lc, "_PARK", True)
    monkeypatch.setattr(lc, "_MAX_ACTIVE_CONTROLLER_JOBS", capacity)
    monkeypatch.setattr(lc, "_spawn_parked_worker", lambda *_args, **_kwargs: True)

    def active_rows():
        with connection() as c, c.cursor() as cur:
            cur.execute("""SELECT thread_id,status FROM controller_jobs
                           WHERE thread_id=ANY(%s) AND status IN ('running','pending')""", (tids,))
            return cur.fetchall()

    def dispatch(tid):
        jid = lc._dispatch(tid, "design", fn=lambda: {}, eta_min=1)
        if jid is not None:
            dispatches[tid] += 1
        return jid

    def finish_one(status="done"):
        rows = active_rows()
        if not rows:
            return
        tid, _ = rng.choice(rows)
        with connection() as c, c.cursor() as cur:
            cur.execute("""UPDATE controller_jobs SET status=%s,finished_at=now()
                           WHERE thread_id=%s AND status IN ('running','pending')""", (status, tid))
            cur.execute("""UPDATE controller_state SET awaiting=NULL,
                           phase=CASE WHEN %s='done' THEN 'DELIVER' ELSE phase END,
                           updated_at=now()-interval '10 seconds' WHERE thread_id=%s""", (status, tid))

    try:
        with connection() as c, c.cursor() as cur:
            for tid in tids:
                cur.execute("""INSERT INTO controller_state
                               (thread_id,tenant_id,org_id,phase,awaiting,updated_at,execution_scope)
                               VALUES (%s,%s,1,'OPTIONS',NULL,now()-interval '10 seconds','test')""",
                            (tid, tenant))

        for virtual_slice in range(400):
            action = rng.randrange(20)
            tid = rng.choice(tids)
            if action in (0, 1):
                state = lc._st(tid)
                if state["phase"] != "DELIVER" and state["awaiting"] is None:
                    dispatch(tid)
            elif action == 2:  # duplicate daemon tick / direct completion callback
                state = lc._st(tid)
                if state["phase"] != "DELIVER" and state["awaiting"] is None:
                    before = len(active_rows())
                    dispatch(tid)
                    dispatch(tid)
                    assert len(active_rows()) <= max(before + 1, capacity)
            elif action == 3:
                finish_one("done")
            elif action == 4:  # cancellation/rollover: retry the same durable phase
                finish_one("cancelled")
            elif action == 5:
                state = lc._st(tid)
                # Crash after job commit but before/after fleet-gate delivery. A pending
                # row is still active and must fence the replaying dispatcher.
                if state["phase"] != "DELIVER" and state["awaiting"] is None:
                    jid = dispatch(tid)
                else:
                    jid = None
                if jid is not None:
                    with connection() as c, c.cursor() as cur:
                        cur.execute("""UPDATE controller_jobs SET status='pending',
                                       started_at=now()-interval '2 days',
                                       heartbeat_at=now()-interval '2 days' WHERE id=%s""", (jid,))
                        cur.execute("UPDATE controller_state SET awaiting=NULL WHERE thread_id=%s", (tid,))
                    assert dispatch(tid) is None
                    assert lc._reap_dead_jobs(thread_ids=[tid], execution_scope="test") == 1
                    assert lc._st(tid)["awaiting"] is None
            # actions 6..19 are crashes before dispatch: durable state is untouched.

            if virtual_slice % 10 == 0:
                rows = active_rows()
                assert len(rows) <= capacity
                assert max(Counter(row_tid for row_tid, _status in rows).values(), default=0) <= 1
                with connection() as c, c.cursor() as cur:
                    cur.execute("""SELECT count(*) FROM controller_state
                                   WHERE thread_id=ANY(%s) AND awaiting NOT IN ('fleet')""", (tids,))
                    assert cur.fetchone()[0] == 0, "fault recovery invented a silent human gate"

        # Fair scheduler tail: release capacity, retry every ungated phase, and
        # complete it. This proves backpressure is a queue, not lost work.
        while any(lc._st(tid)["phase"] != "DELIVER" for tid in tids):
            while active_rows():
                finish_one("done")
            for tid in tids:
                state = lc._st(tid)
                if state["phase"] != "DELIVER" and state["awaiting"] is None:
                    dispatch(tid)
            assert len(active_rows()) <= capacity

        assert not active_rows()
        assert all(lc._st(tid)["phase"] == "DELIVER" and lc._st(tid)["awaiting"] is None
                   for tid in tids)
        assert all(dispatches[tid] >= 1 for tid in tids)
    finally:
        with connection() as c, c.cursor() as cur:
            cur.execute("DELETE FROM controller_jobs WHERE thread_id=ANY(%s)", (tids,))
            cur.execute("DELETE FROM controller_state WHERE thread_id=ANY(%s)", (tids,))
