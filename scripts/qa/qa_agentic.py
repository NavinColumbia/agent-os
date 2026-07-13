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
    """FULL async agentic drive, offline: stub BOTH seams — the tool (no browser) AND factory.agent (no real
    CLI, so the coordinator's decide/aggregate steps are instant) — then drive the real org to completion and
    assert it hired one qa-explorer per story, the tool-workers dispatched (dispatch-and-park), their findings
    flowed back over the bus (via `tool_result` -> the worker -> `finding`/`done` up), and the run finished."""
    import types
    import json as _json
    import store

    _real_factory = sys.modules.get("factory")
    fake = types.ModuleType("factory")

    def fake_agent(role, repo, task, **k):        # instant, deterministic coordinator AI (ack / aggregate)
        body = _json.dumps({"action": "ack", "result": "qa complete", "ok": True})
        return {"rc": 0, "out": body, "out_full": body}
    fake.agent = fake_agent
    fake.PRODUCTS = "/tmp"
    sys.modules["factory"] = fake

    import tools
    import jobrunner
    _orig_tool = tools.run_tool

    def fake_tool(name, args):
        if name == "dev_fix":                     # the dev-fixer tool-worker (spawned via the dev-handoff)
            return {"status": "done", "findings": [], "result": {"fixed": True, "files": ["src/x.js"]}}
        sid = (args.get("story") or {}).get("id")
        return {"status": "done", "findings": [{"kind": "bug", "title": f"bug {sid}", "blocking": True,
                "story": sid}], "result": {"story": sid, "stop_reason": "coverage-complete"}}
    tools.run_tool = fake_tool
    jobrunner._default_run_tool = lambda: fake_tool

    out = None
    try:
        out = run_agentic_qa("http://app.test", "A console the user signs in to and messages an assistant.",
                             product="agentic-selftest", stories=[{"id": "US1"}, {"id": "US2"}],
                             tenant="agentic-selftest", repo=".", workers=2, drive_budget_s=60, stall_s=2.0)
        roles = [a.get("role") for a in store.actors(out["run_id"], "agentic-selftest")]
        assert out["status"] == "done", f"the org run must finish; got {out['status']}"
        assert roles.count("qa-explorer") == 2, f"one qa-explorer per story; got {roles.count('qa-explorer')}"
        assert len(out["findings"]) == 2, f"both explorers' findings must flow back over the bus; got {out['findings']}"
        # dev-handoff: each BLOCKING finding -> a dev-coordinator hired, which spawns a dev-fixer.
        assert roles.count("dev-coordinator") == 2, f"a dev-coordinator per blocking bug; got {roles.count('dev-coordinator')}"
        assert roles.count("dev-fixer") == 2, f"a dev-fixer per dev-coordinator; got {roles.count('dev-fixer')}"
        # honest agentic verdict: bugs were found, so NOT passed, and it says the fixes are re-verify-pending.
        coord = next(a for a in store.actors(out["run_id"], "agentic-selftest") if a["role"] == "qa-coordinator")
        v = (coord.get("result") or {})
        assert v.get("passed") is False and v.get("blocking") == 2, f"honest verdict expected, got {v}"
        assert "re-verify pending" in (v.get("result") or ""), v
        print(f"qa_agentic selftest: PASS (full async org -> {out['status']}: 2 explorers found bugs, "
              f"qa-coordinator handed off to 2 dev-coordinators -> 2 dev-fixers; honest verdict: "
              f"passed={v.get('passed')} blocking={v.get('blocking')})")
        return 0
    finally:
        tools.run_tool = _orig_tool
        jobrunner._default_run_tool = lambda: __import__("tools").run_tool
        if _real_factory is not None:
            sys.modules["factory"] = _real_factory
        else:
            sys.modules.pop("factory", None)
        _cleanup(out["run_id"] if isinstance(out, dict) else -1, "agentic-selftest")


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
