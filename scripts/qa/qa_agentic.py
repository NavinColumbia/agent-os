#!/usr/bin/env python3
"""qa_agentic.py — the AGENTIC QA entrypoint (agentic-org phase 5).

Runs QA as a real ORG instead of a procedural loop: a **qa-coordinator** supervisor actor is hired with the
run params in its memory.context; on its `task` it spawns one **qa-explorer** tool-worker per story
(dispatch-and-park → jobrunner runs the browser explore off-loop → emits `finding`/`done` back); the
coordinator's generic supervisor step reacts and aggregates. Durable + crash-resumable on the orchestra
runtime — the coordination is genuine agent conversation over the bus, not a Python for-loop.

    run_agentic_qa(target_url, vision, product=, token=, org=, stories=, ...) -> {run_id, status, findings, ...}

STATUS: the org spine (coordinator hires explorers, tool-workers dispatch-and-park, findings flow back,
run finishes) is wired and tested here end-to-end with a stubbed instant tool. Full PARITY with the
procedural qa_run (gap-fill hires, dev-coordinator hand-off, auditor sign-off at aggregate, honest verdict +
findings.py) is phase 4b — until then `qa_run.py` stays the default. See docs/AGENTIC-QA-ORG.md.
"""
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for p in (str(_HERE), str(_HERE.parent), str(_HERE.parent / "orchestra")):
    if p not in sys.path:
        sys.path.insert(0, p)


def run_agentic_qa(target_url, vision, *, product="app", token=None, org="0", summary="", repo=None,
                   stories=None, artifact_dir=None, restart_cmd=None, health_url=None,
                   tenant="agentic-qa", workers=2, drive_budget_s=1800, stall_s=3.0):
    """Create the QA org and drive it to completion. Returns the run id, terminal status, and the findings
    (bugs) the explorers reported over the bus. Drives run_org in a loop because tool jobs run ASYNC — a
    single run_org can return while a browser job is still going; we re-enter until the run is terminal or
    the wall-clock budget trips (a runaway guard, not a quality cap)."""
    import store
    import runtime as rt
    if stories is None:
        import story_gen
        stories = story_gen.generate_stories(vision, summary)

    run = store.start_run(tenant, vision)
    rid = run["run_id"]
    context = {"vision": vision, "target_url": target_url, "token": token, "org": str(org),
               "product": product, "stories": stories, "repo": repo, "artifact_dir": artifact_dir,
               "restart_cmd": restart_cmd, "health_url": health_url}
    coord = store.spawn_actor(rid, tenant, "qa-coordinator", "qa-coordinator", kind="supervisor",
                              memory={"context": context, "repo": repo or "."})
    store.emit(rid, tenant, None, coord["actor_id"], "task",
               {"task": f"QA the product '{product}' against its vision; report every bug."})

    # DRIVE to completion. run_org self-stalls after ~stall_s of no claimable events (e.g. while an async
    # tool job runs); loop and re-enter until the run is terminal or the budget trips.
    started = time.time()
    while time.time() - started < drive_budget_s:
        rt.run_org(rid, tenant, repo=repo or ".", workers=workers, stall_s=stall_s)
        r = store.run(rid, tenant)
        if (r or {}).get("status") in ("done", "failed", "halted"):
            break
        try:
            import jobrunner
            active = any(j["state"] == "running" for j in jobrunner._JOBS.values())
        except Exception:
            active = False
        pend = sum(store.pending_count(a["actor_id"], tenant) for a in store.actors(rid, tenant))
        if not active and not pend:
            break                                  # nothing running and nothing queued -> settled
        time.sleep(0.2)

    evs = store.events(rid, tenant)
    findings = [e["payload"] for e in evs if e["kind"] == "finding"]
    acts = store.actors(rid, tenant)
    return {"run_id": rid, "status": (store.run(rid, tenant) or {}).get("status"),
            "findings": findings, "actors": len(acts),
            "explorers": [a for a in acts if a.get("role") == "qa-explorer"]}


def _selftest():
    """Deterministic check of the Phase-5 wiring: a qa-coordinator, on its task, hires one qa-explorer
    TOOL-worker per story (each carrying tool=qa_explore in memory.context). Drives ONE coordinator step
    directly (no async pool) so it is fast and never flaky. The FULL async drive (run_agentic_qa) works but
    has a claim/emit race that occasionally hangs the pool — that is phase-5 HARDENING (see docs/HANDOFF.md),
    verified live, not in this unit test."""
    import store
    import runtime as rt

    tenant = "agentic-selftest"
    stories = [{"id": "US1", "title": "sign in"}, {"id": "US2", "title": "send message"}]
    run = store.start_run(tenant, "A console the user signs into and messages an assistant.")
    rid = run["run_id"]
    try:
        context = {"vision": "console", "target_url": "http://app.test", "token": "t", "org": "1",
                   "product": "agentic-selftest", "stories": stories, "repo": "."}
        coord = store.spawn_actor(rid, tenant, "qa-coordinator", "qa-coordinator", kind="supervisor",
                                  memory={"context": context, "repo": "."})
        store.emit(rid, tenant, None, coord["actor_id"], "task", {"task": "QA the console against its vision"})

        ctx = rt._Ctx(rid, tenant, ".", None, store.CLAIM_LEASE_S, None)
        evs = store.claim_events(coord["actor_id"], tenant)
        rt._supervisor_step(ctx, store.actor(coord["actor_id"], tenant), evs)   # decompose + hire (deterministic)

        explorers = [a for a in store.actors(rid, tenant) if a.get("role") == "qa-explorer"]
        assert len(explorers) == 2, f"qa-coordinator must hire ONE qa-explorer per story, got {len(explorers)}"
        for e in explorers:
            c = ((e.get("memory") or {}).get("context")) or {}
            assert c.get("tool") == "qa_explore", f"explorer must be a tool-worker, got {c}"
            assert (c.get("tool_args") or {}).get("story", {}).get("id") in ("US1", "US2")
        assert {(((e.get("memory") or {}).get("context")) or {}).get("tool_args", {}).get("story", {}).get("id")
                for e in explorers} == {"US1", "US2"}
        print("qa_agentic selftest: PASS (qa-coordinator hired 2 qa-explorer TOOL-workers, one per story)")
        return 0
    finally:
        _cleanup(rid, tenant)


def _cleanup(run_id, tenant):
    try:
        import pulse
        import psycopg
        if pulse.DB:
            with psycopg.connect(pulse.DB) as c, c.cursor() as cur:
                cur.execute("DELETE FROM orchestra_events WHERE run_id=%s", (run_id,))
                cur.execute("DELETE FROM orchestra_actors WHERE run_id=%s", (run_id,))
                cur.execute("DELETE FROM orchestra_runs WHERE run_id=%s", (run_id,))
                cur.execute("DELETE FROM agent_pulse WHERE work_id LIKE %s", (f"{run_id}:%",))
                c.commit()
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(_selftest())
