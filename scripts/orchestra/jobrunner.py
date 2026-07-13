#!/usr/bin/env python3
"""jobrunner.py — dispatch-and-park executor for tool-worker actors (agentic-org phase 3a).

See docs/AGENTIC-QA-ORG.md. A tool-worker must NOT run a 10-30 min browser job inside a lease-bound
decide-step (the 900s event lease would reclaim it -> a duplicate browser). Instead its step calls
`dispatch(job)`: the job runs in a BACKGROUND thread (the browser lives there), and on completion writes the
outcome straight onto the org bus — a `finding` per bug + a `done` to the worker's supervisor — and flips the
parked worker actor to `done`. Every decide-step stays short.

Crash-safety (reuses the pulse plane, not a new table): each job is a pulse (`kind=tool-job`) that beats
while alive; if the process dies mid-job its pulse goes silent, and `reconcile()` re-dispatches from the
parked actor's persisted job spec — idempotent (a job already tracked in-process is skipped). The done/finding
+ pulse + watchdog machinery is reused, never duplicated.

Isolated + unit-tested: `dispatch`/`reconcile` take the store module and the tool runner injected, so the
selftest drives the full lifecycle with fakes (no runtime, no browser, no DB-required logic).
"""
import sys
import threading
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

_JOBS = {}                        # job_id -> {"state": running|done|failed, "thread": Thread}
_LOCK = threading.Lock()


def _pulse():
    try:
        import pulse
        return pulse
    except Exception:
        return None


def _default_run_tool():
    import tools
    return tools.run_tool


def job_id_for(job) -> str:
    """A STABLE id per (actor, tool) so a re-dispatch of the same parked job is the same pulse/handle —
    this is what makes reconcile idempotent."""
    return f"{job.get('run_id')}:{job.get('actor_id')}:{job.get('tool')}"


def dispatch(job: dict, store, run_tool=None, sync: bool = False) -> str:
    """Start a tool job for a parked tool-worker. Returns the job id immediately (the actor is already parked
    by its decide-step). `sync=True` runs inline (tests). Idempotent: a job already running is not re-started."""
    jid = job_id_for(job)
    with _LOCK:
        if jid in _JOBS and _JOBS[jid]["state"] == "running":
            return jid
        _JOBS[jid] = {"state": "running", "thread": None}
    run_tool = run_tool or _default_run_tool()
    if sync:
        _run(jid, job, store, run_tool)
        return jid
    t = threading.Thread(target=_run, args=(jid, job, store, run_tool), daemon=True)
    with _LOCK:
        _JOBS[jid]["thread"] = t
    t.start()
    return jid


def _run(jid, job, store, run_tool):
    p = _pulse()
    if p:
        p.beat(jid, kind="tool-job", tenant_id=job.get("tenant"),
               label=f"{job.get('tool')} · {job.get('actor_name') or job.get('actor_id')}",
               stage="running", progress=f"running {job.get('tool')}", expected_cadence_s=300)
    try:
        out = run_tool(job["tool"], job.get("args") or {})
    except Exception as e:                                # a tool must never take the org down
        out = {"status": "failed", "findings": [], "result": {"error": f"{type(e).__name__}: {e}"}}
    ok = _complete(jid, job, out, store)
    with _LOCK:
        _JOBS[jid]["state"] = "done" if ok else "failed"
    if p:
        p.finish(jid, status=("done" if out.get("status") == "done" else "failed"),
                 result={"findings": len(out.get("findings") or [])})


def _complete(jid, job, out, store) -> bool:
    """Write the tool's outcome onto the bus: a `finding` per bug to the supervisor, then a `done` that flips
    the parked worker terminal so the supervisor's normal interrupt-driven step reacts. Fail-soft."""
    aid, sup = job.get("actor_id"), job.get("supervisor_id")
    run_id, tenant = job.get("run_id"), job.get("tenant")
    try:
        for i, f in enumerate(out.get("findings") or []):
            if sup is not None:
                store.emit(run_id, tenant, aid, sup, "finding", f, f"job-{jid}-{i}")
        status = out.get("status", "done")
        if sup is not None:
            store.emit(run_id, tenant, aid, sup, "done",
                       {"task": job.get("assignment"), "tool": job.get("tool"),
                        "status": status, "result": out.get("result")}, f"job-{jid}-done")
        store.update_actor(aid, tenant, status="done",
                           result={"tool": job.get("tool"), "status": status, "result": out.get("result")})
        return True
    except Exception:
        return False


def reconcile(stale_jobs, store, run_tool=None) -> int:
    """Re-dispatch tool jobs whose pulse went silent (the process running them died). `stale_jobs` are the
    parked tool-workers' persisted job specs (from actor memory). Idempotent: a job still running in THIS
    process is skipped. Returns how many were re-dispatched."""
    n = 0
    for job in (stale_jobs or []):
        jid = job_id_for(job)
        with _LOCK:
            live = jid in _JOBS and _JOBS[jid]["state"] == "running"
        if not live:
            dispatch(job, store, run_tool=run_tool)
            n += 1
    return n


def _selftest():
    import time

    class FakeStore:
        def __init__(self):
            self.emits, self.updates = [], []

        def emit(self, run_id, tenant, frm, to, kind, payload, corr):
            self.emits.append({"kind": kind, "to": to, "payload": payload, "corr": corr})

        def update_actor(self, aid, tenant, status=None, result=None):
            self.updates.append({"aid": aid, "status": status, "result": result})

    # a tool that finds one blocking bug -> the job must emit finding + done and flip the actor done.
    def fake_run_tool(name, args):
        assert name == "qa_explore"
        return {"status": "done",
                "findings": [{"kind": "bug", "title": "blank panel", "blocking": True}],
                "result": {"coverage": [], "stop_reason": "coverage-complete"}}

    st = FakeStore()
    job = {"run_id": 1, "tenant": "t1", "actor_id": 10, "supervisor_id": 5, "tool": "qa_explore",
           "assignment": "verify story US1", "args": {"story": {"id": "US1"}}}
    jid = dispatch(job, st, run_tool=fake_run_tool, sync=True)

    kinds = [e["kind"] for e in st.emits]
    assert kinds == ["finding", "done"], kinds
    assert st.emits[0]["to"] == 5 and st.emits[0]["payload"]["blocking"] is True
    assert st.emits[1]["payload"]["status"] == "done" and st.emits[1]["payload"]["tool"] == "qa_explore"
    assert st.updates and st.updates[-1]["status"] == "done", st.updates
    assert _JOBS[jid]["state"] == "done"

    # a raising tool -> failed result, still emits a done (never leaves the worker parked forever), no crash.
    def boom(name, args):
        raise RuntimeError("browser died")
    st2 = FakeStore()
    dispatch({**job, "actor_id": 11, "tool": "qa_explore"}, st2, run_tool=boom, sync=True)
    assert [e["kind"] for e in st2.emits] == ["done"] and st2.emits[0]["payload"]["status"] == "failed"

    # reconcile re-dispatches only jobs NOT currently running here (idempotent).
    with _LOCK:
        _JOBS[job_id_for(job)]["state"] = "done"      # simulate the earlier one finished (crashed process)
    st3 = FakeStore()
    n = reconcile([job], st3, run_tool=fake_run_tool)
    assert n == 1, n
    # give the re-dispatched (async) job a moment, then confirm it completed
    for _ in range(50):
        if [e["kind"] for e in st3.emits] == ["finding", "done"]:
            break
        time.sleep(0.02)
    assert [e["kind"] for e in st3.emits] == ["finding", "done"], st3.emits

    # a job already running is NOT re-dispatched.
    with _LOCK:
        _JOBS[job_id_for(job)]["state"] = "running"
    assert reconcile([job], FakeStore(), run_tool=fake_run_tool) == 0

    print("jobrunner selftest: PASS (dispatch->finding+done+flip; tool-crash still done; reconcile idempotent)")
    return 0


if __name__ == "__main__":
    sys.exit(_selftest())
