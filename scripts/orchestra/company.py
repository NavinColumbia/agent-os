#!/usr/bin/env python3
"""company.py — run a FULL multi-function org under CEO direction (the North Star shape).

A CEO-coordinator (top supervisor) is directed with a vision + a set of FUNCTIONS. It spawns one function
COORDINATOR per function (research, finance, legal, product, qa, …); each function coordinator staffs a team
of tool-workers (dispatch-and-park → real work: research/finance_report/qa_explore/…); every worker reports
up to its coordinator, and every coordinator's result aggregates up to the CEO-coordinator — a durable,
crash-resumable, multi-level org where the CEO gives direction and gets one report back. See
docs/NORTH-STAR-ROADMAP.md. Built entirely on the proven orchestra runtime + tool-worker pattern — no new
engine, just the top-level config.

    run_company_org(vision, functions) -> {run_id, status, ceo_report, functions:[{role, report}], ...}
      functions = [{"role": "research-coordinator", "tool": "research",
                    "items": [{"topic": "the market"}, {"topic": "competitors"}]},
                   {"role": "finance-coordinator", "tool": "finance_report",
                    "worker_role": "finance-cost-controller", "items": [{"task": "Q3 report", "data": {...}}]}]
"""
import os
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for p in (str(_HERE), str(_HERE.parent), str(_HERE.parent / "qa")):
    if p not in sys.path:
        sys.path.insert(0, p)


def run_company_org(vision, functions, *, tenant="company", workers=2, drive_budget_s=1800, stall_s=3.0):
    """Direct a full org: spawn a CEO-coordinator with the functions, drive it to completion, and collect the
    reports that flowed up. Drive loop mirrors qa_agentic (tool jobs are ASYNC, so re-enter run_org)."""
    import store
    import runtime as rt

    run = store.start_run(tenant, vision)
    rid = run["run_id"]
    ceo = store.spawn_actor(rid, tenant, "CEO-Coordinator", "ceo-coordinator", kind="supervisor",
                            memory={"context": {"functions": functions}, "repo": "."})
    store.emit(rid, tenant, None, ceo["actor_id"], "task", {"task": vision})

    started = time.time()
    while time.time() - started < drive_budget_s:
        rt.run_org(rid, tenant, repo=".", workers=workers, stall_s=stall_s)
        if (store.run(rid, tenant) or {}).get("status") in ("done", "failed", "halted"):
            break
        try:
            import jobrunner
            active = any(j["state"] == "running" for j in jobrunner._JOBS.values())
        except Exception:
            active = False
        if not active and not sum(store.pending_count(a["actor_id"], tenant) for a in store.actors(rid, tenant)):
            break
        time.sleep(0.2)

    acts = store.actors(rid, tenant)
    ceo_id = ceo["actor_id"]
    func_reports = []
    for a in acts:
        if a.get("kind") == "supervisor" and a["actor_id"] != ceo_id:
            # gather this function's worker reports (the actual outputs)
            outputs = [((w.get("result") or {}).get("result") or {}).get("report")
                       for w in acts if w.get("supervisor_id") == a["actor_id"]]
            func_reports.append({"role": a.get("role"), "result": a.get("result"),
                                 "outputs": [o for o in outputs if o]})
    return {"run_id": rid, "status": (store.run(rid, tenant) or {}).get("status"),
            "ceo_report": next((a.get("result") for a in acts if a["actor_id"] == ceo_id), None),
            "functions": func_reports, "actors": len(acts)}


def _cleanup(run_id, tenant):
    try:
        import psycopg
        import pulse
        if pulse.DB:
            with psycopg.connect(pulse.DB) as c, c.cursor() as cur:
                for t in ("orchestra_events", "orchestra_actors", "orchestra_runs"):
                    cur.execute(f"DELETE FROM {t} WHERE run_id=%s", (run_id,))
                cur.execute("DELETE FROM agent_pulse WHERE work_id LIKE %s", (f"{run_id}:%",))
                c.commit()
    except Exception:
        pass


def _selftest():
    import types
    import store

    _real_factory = sys.modules.get("factory")
    fake = types.ModuleType("factory")
    fake.PRODUCTS = "/tmp"
    fake.agent = lambda role, repo, task, **k: {"rc": 0, "out_full": '{"action":"ack","result":"function delivered","ok":true}'}
    sys.modules["factory"] = fake

    import tools
    import jobrunner
    _orig = tools.run_tool
    tools.run_tool = lambda name, args: {"status": "done", "findings": [],
                                         "result": {"report": f"[{name}] report for {args.get('topic') or args.get('task') or 'item'}"}}
    jobrunner._default_run_tool = lambda: tools.run_tool

    out = None
    try:
        out = run_company_org(
            "Assess whether to launch an AI-QA product.",
            [{"role": "research-coordinator", "tool": "research",
              "items": [{"topic": "the AI-QA market"}, {"topic": "top competitors"}]},
             {"role": "finance-coordinator", "tool": "finance_report", "worker_role": "finance-cost-controller",
              "items": [{"task": "unit economics", "data": {"price": 99, "cogs": 12}}]}],
            tenant="company-selftest", workers=2, drive_budget_s=45, stall_s=1.5)

        roles = [a.get("role") for a in store.actors(out["run_id"], "company-selftest")]
        assert out["status"] == "done", f"the company run must finish; got {out['status']}"
        assert "ceo-coordinator" in roles, "a CEO-coordinator leads the org"
        assert roles.count("research-coordinator") == 1 and roles.count("finance-coordinator") == 1, roles
        assert roles.count("research") == 2, f"research team = 1 worker per topic; got {roles.count('research')}"
        assert roles.count("finance-cost-controller") == 1, roles
        # reports flowed UP: each function produced worker outputs and the CEO got an aggregate.
        research_fn = next(f for f in out["functions"] if f["role"] == "research-coordinator")
        assert len(research_fn["outputs"]) == 2 and all("report for" in o for o in research_fn["outputs"]), research_fn
        assert out["ceo_report"] and out["ceo_report"].get("ok"), out["ceo_report"]
        print(f"company_org selftest: PASS (CEO-coordinator -> 2 function coordinators -> "
              f"{roles.count('research') + roles.count('finance-cost-controller')} tool-workers did real work, "
              f"reports aggregated up to the CEO; run={out['status']})")
        return 0
    finally:
        tools.run_tool = _orig
        jobrunner._default_run_tool = lambda: __import__("tools").run_tool
        if _real_factory is not None:
            sys.modules["factory"] = _real_factory
        else:
            sys.modules.pop("factory", None)
        if isinstance(out, dict):
            _cleanup(out["run_id"], "company-selftest")


if __name__ == "__main__":
    sys.exit(_selftest())
