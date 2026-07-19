#!/usr/bin/env python3
"""research_org.py — RESEARCH AS A DURABLE ORG RUN: orchestra's first PRODUCTION caller (REBUILD-PLAN A1).

The arch-review verdict this closes: "the actual AI org engine sits in a demo folder with zero
production callers". loopcontroller's RESEARCH phase (AOS_ORCHESTRA=1, default ON — see
research.orchestra_on) dispatches here via research.start/_run, and the research executes as a REAL
org run on the durable store (scripts/orchestra/store.py / postgres 50-orchestra.sql):

    controller actor ──task──> research supervisor ──task──> N child researchers   (parallel)
                     <──done──                     <──done/blocked──

  - every actor is a Postgres ROW with identity (name + a control-plane role), lineage
    (supervisor_id), assignment, lifecycle status, tenure (hired_at) and liveness (last_active —
    a heartbeat ticker beats every live actor while the run works, so sentinel.stale_working can
    tell "slow" from "silently dead");
  - every inter-actor event (task / done / blocked) is a persisted, claimable row on the durable
    bus (orchestra_events) — the whole run is auditable and crash-surviving;
  - every work unit is an AI call via factory.agent, so ALL factory gates (killswitch, governance
    can_spawn, consent/provider backstops, budget caps) are enforced per actor; store.start_run /
    spawn_actor / emit additionally refuse while the kill-switch is engaged.

OUTPUT CONTRACT (unchanged console UX): the child researchers and the supervisor's synthesis reuse
research_fleet.decompose / research_one / synthesize, so findings/ and REPORT.md land EXACTLY where
the legacy fleet puts them — research.py's report/options distillation and every console surface
(research_runs / report / option cards) work identically with either engine.

    research_org.py selftest      # OFFLINE (factory.agent stubbed — no LLM), REAL local Postgres
Run with the agent-os venv python. Library module — research.py is the caller; binds no server.
"""
from __future__ import annotations

import os
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent          # scripts/
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import audit           # noqa: E402
import factory         # noqa: E402  — THE llm call; every gate lives inside it
import research_fleet  # noqa: E402  — decompose/research_one/synthesize (the proven work units)
import store           # noqa: E402  — the durable org (actors/events/runs as Postgres rows)

# The org shape's roles come from control-plane/roles/*.yaml (factory.role_brief loads them).
CONTROLLER_ROLE = "controller"
RESEARCHER_ROLE = "research-growth"
BEAT_S = int(os.environ.get("AOS_ORCHESTRA_BEAT_S", "45"))       # heartbeat cadence while working
WORKERS = int(os.environ.get("AOS_FLEET_WORKERS", "5"))          # parallel researchers (same as legacy)


def _drain(actor_id, tenant_id):
    """Claim + complete everything in an actor's durable inbox (the act of handling it). Returns
    the drained events. Claiming also bumps last_active — draining your inbox is a sign of life."""
    drained = []
    while True:
        batch = store.claim_events(actor_id, tenant_id, claimed_by=f"research_org:{actor_id}")
        if not batch:
            return drained
        for ev in batch:
            store.complete_event(ev["id"], tenant_id)
        drained.extend(batch)


def _guard(row, what):
    """store mutations refuse (e.g. kill-switch halted) by returning {'error': ...} — surface that
    as a hard stop so research.py records the run failed instead of limping on half-spawned."""
    if isinstance(row, dict) and row.get("error"):
        raise RuntimeError(f"orchestra {what} refused: {row['error']}")
    return row


def run_research(question, out_rel="REPORT.md", api_key=None, research_run_id=None,
                 tenant_id=None, org_id=None):
    """Execute one research question as a durable org run. Same return contract as
    research_fleet.research: {"report": <path>, "subquestions": N, "answered": N} (+ the
    orchestra run id). Raises on a refused/failed run so research._run marks it failed."""
    if os.environ.get("AOS_RESEARCH_RUNORG", "0").lower() not in ("0", "false", "no", ""):
        # CRASH-RESUMABLE engine (docs/RESEARCH-RUNORG-REWIRE.md): route through runtime.run_org. Flagged OFF
        # by default until the offline crash-resume proof AND one live run pass; then this becomes the default.
        return run_research_via_org(question, out_rel, api_key, research_run_id, tenant_id, org_id)
    rid = research_run_id if research_run_id is not None else uuid.uuid4().hex[:12]
    tenant = tenant_id or "platform"
    org_num = int(org_id) if org_id is not None and str(org_id).isdigit() else None
    repo = factory.PRODUCTS / f"research-{rid}-{research_fleet._slug(question)}"
    (repo / "findings").mkdir(parents=True, exist_ok=True)

    r = _guard(store.start_run(tenant, question, org_id=org_num), "start_run")
    orc = r["run_id"]
    corr = f"research-{rid}"
    stop = threading.Event()
    try:
        # --- hire the org: controller -> research supervisor (both real Postgres rows) --------
        ctrl = _guard(store.spawn_actor(orc, tenant, "controller", CONTROLLER_ROLE,
                                        kind="controller", org_id=org_num,
                                        assignment=f"deliver research: {question[:160]}",
                                        memory={"research_run_id": rid, "org_id": org_id}),
                      "spawn controller")
        sup = _guard(store.spawn_actor(orc, tenant, "research-supervisor", RESEARCHER_ROLE,
                                       kind="supervisor", supervisor_id=ctrl["actor_id"],
                                       org_id=org_num, assignment=question), "spawn supervisor")
        _guard(store.emit(orc, tenant, ctrl["actor_id"], sup["actor_id"], "task",
                          {"question": question}, corr_id=corr), "emit task")

        # --- heartbeat ticker: every live actor beats while the run works (sentinel liveness) --
        def _beat():
            while not stop.wait(BEAT_S):
                try:
                    for a in store.actors(orc, tenant):
                        if a["status"] in ("idle", "working", "blocked"):
                            store.heartbeat(a["actor_id"], tenant)
                except Exception:
                    pass                                   # a beat must never kill the run
        threading.Thread(target=_beat, daemon=True, name=f"orchestra-beat-{orc}").start()

        store.update_actor(ctrl["actor_id"], tenant, status="working")
        _drain(sup["actor_id"], tenant)                    # supervisor picks up the controller's task
        store.update_actor(sup["actor_id"], tenant, status="working")

        # --- supervisor decomposes (an AI call) and HIRES one researcher per sub-question ------
        subqs = research_fleet.decompose(repo, question)
        children = []
        for i, sq in enumerate(subqs):
            a = _guard(store.spawn_actor(orc, tenant, f"researcher-{i + 1:02d}", RESEARCHER_ROLE,
                                         kind="worker", supervisor_id=sup["actor_id"],
                                         org_id=org_num, assignment=sq), "spawn researcher")
            _guard(store.emit(orc, tenant, sup["actor_id"], a["actor_id"], "task",
                              {"idx": i, "subq": sq}, corr_id=corr), "emit subtask")
            children.append((a["actor_id"], i, sq))
        audit.append(actor="orchestra:research", action="OrgRunSpawned", resource=str(orc),
                     decision="executed", payload={"tenant": tenant, "researchers": len(children),
                                                   "research_run_id": rid})

        # --- N child researchers in PARALLEL (each work unit = factory.agent, fully gated) ------
        def _child(actor_id, idx, subq):
            _drain(actor_id, tenant)                       # claim my task (sign of life)
            store.update_actor(actor_id, tenant, status="working")
            try:
                res = research_fleet.research_one(repo, idx, subq, api_key)
            except Exception as e:                         # a crashed researcher = a blocked child
                res = {"idx": idx, "subq": subq, "ok": False, "error": str(e)[:300]}
            if res.get("ok"):
                store.update_actor(actor_id, tenant, status="done", result=res)
                store.emit(orc, tenant, actor_id, sup["actor_id"], "done", res, corr_id=corr)
            else:
                store.update_actor(actor_id, tenant, status="blocked", result=res)
                store.emit(orc, tenant, actor_id, sup["actor_id"], "blocked", res, corr_id=corr)
            return res

        results = []
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futs = [ex.submit(_child, aid, i, sq) for aid, i, sq in children]
            for f in as_completed(futs):
                results.append(f.result())

        # --- supervisor handles the children's events, then AGGREGATES (an AI call) ------------
        _drain(sup["actor_id"], tenant)
        report = research_fleet.synthesize(repo, question, out_rel)
        answered = sum(1 for x in results if x.get("ok"))
        store.update_actor(sup["actor_id"], tenant, status="done",
                           result={"report": str(report), "answered": answered,
                                   "subquestions": len(subqs)})
        store.emit(orc, tenant, sup["actor_id"], ctrl["actor_id"], "done",
                   {"report": str(report), "answered": answered}, corr_id=corr)

        # --- controller receives the synthesis and closes the run ------------------------------
        _drain(ctrl["actor_id"], tenant)
        store.update_actor(ctrl["actor_id"], tenant, status="done",
                           result={"report": str(report)})
        summary = {"report": str(report), "subquestions": len(subqs), "answered": answered,
                   "research_run_id": rid}
        store.finish_run(orc, "done", summary, tenant_id=tenant)
        audit.append(actor="orchestra:research", action="OrgRunDone", resource=str(orc),
                     decision="executed", payload=summary)
        return {**summary, "orchestra_run_id": orc}
    except Exception as e:
        try:                                               # durable post-mortem, then propagate
            store.finish_run(orc, "failed", {"error": str(e)[:300]}, tenant_id=tenant)
            audit.append(actor="orchestra:research", action="OrgRunFailed", resource=str(orc),
                         decision="failed", payload={"error": str(e)[:300]})
        except Exception:
            pass
        raise
    finally:
        stop.set()                                         # stop the heartbeat ticker


def run_research_via_org(question, out_rel="REPORT.md", api_key=None, research_run_id=None,
                         tenant_id=None, org_id=None):
    """CRASH-RESUMABLE research: run it as a durable runtime.run_org org instead of the synchronous
    ThreadPoolExecutor. A research-coordinator DECOMPOSES the question and spawns one dispatch-and-parked
    `research_subq` tool-worker per sub-question — each is lease-reclaimable, so a process death mid-run is
    resumed (reconcile_parked re-dispatches only the unfinished ones) rather than lost. Then we synthesize
    REPORT.md deterministically (kept OUT of the org: a mid-synthesize crash just re-runs a single cheap call).
    Same return contract as run_research. See docs/RESEARCH-RUNORG-REWIRE.md."""
    import time as _t
    import runtime as rt
    rid = research_run_id if research_run_id is not None else uuid.uuid4().hex[:12]
    tenant = tenant_id or "platform"
    org_num = int(org_id) if org_id is not None and str(org_id).isdigit() else None
    repo = factory.PRODUCTS / f"research-{rid}-{research_fleet._slug(question)}"
    (repo / "findings").mkdir(parents=True, exist_ok=True)

    run = _guard(store.start_run(tenant, question, org_id=org_num), "start_run")
    orc = run["run_id"]
    coord = _guard(store.spawn_actor(orc, tenant, "research-coordinator", "research-coordinator",
                                     kind="supervisor", org_id=org_num, assignment=question,
                                     memory={"context": {"repo": str(repo), "question": question,
                                                         "tenant": tenant, "org": org_id},
                                             "research_run_id": rid}), "spawn coordinator")
    _guard(store.emit(orc, tenant, None, coord["actor_id"], "task", {"task": question}), "emit task")

    try:
        # DRIVE to completion — tool jobs are async, so re-enter run_org until terminal or fully idle (the
        # proven company.run_company_org loop). run_org's reconcile_parked at startup gives crash-resume.
        budget_s = int(os.environ.get("AOS_RESEARCH_DRIVE_S", "3600"))
        started = _t.time()
        while _t.time() - started < budget_s:
            rt.run_org(orc, tenant, repo=str(repo), workers=WORKERS)
            if (store.run(orc, tenant) or {}).get("status") in ("done", "failed", "halted"):
                break
            try:
                import jobrunner
                active = any(j["state"] == "running" for j in jobrunner._JOBS.values())
            except Exception:
                active = False
            if not active and not sum(store.pending_count(a["actor_id"], tenant)
                                      for a in store.actors(orc, tenant)):
                break
            _t.sleep(0.2)

        # SYNTHESIZE the contract report where research.py expects it (deterministic post-step).
        report = research_fleet.synthesize(repo, question, out_rel)
        workers = [a for a in store.actors(orc, tenant) if a.get("kind") == "worker"]
        answered = sum(1 for w in workers if ((w.get("result") or {}).get("result") or {}).get("ok"))
        summary = {"report": str(report), "subquestions": len(workers), "answered": answered,
                   "research_run_id": rid}
        if (store.run(orc, tenant) or {}).get("status") != "done":
            store.finish_run(orc, "done", summary, tenant_id=tenant)
        audit.append(actor="orchestra:research", action="OrgRunDone", resource=str(orc),
                     decision="executed", payload={**summary, "engine": "run_org"})
        return {**summary, "orchestra_run_id": orc}
    except Exception as e:
        try:
            store.finish_run(orc, "failed", {"error": str(e)[:300]}, tenant_id=tenant)
            audit.append(actor="orchestra:research", action="OrgRunFailed", resource=str(orc),
                         decision="failed", payload={"error": str(e)[:300], "engine": "run_org"})
        except Exception:
            pass
        raise


# ==============================================================================================
# OFFLINE SELFTEST — factory.agent stubbed (no LLM, no web, no spend); REAL local Postgres.
# Proves the full org run: hire tree -> task events -> parallel children (one BLOCKS) ->
# supervisor aggregate -> controller done -> durable rows/events/tenure all correct. Cleans up.
# ==============================================================================================
def _selftest():
    import json
    import tempfile
    import psycopg

    tid = f"research-org-selftest-{uuid.uuid4().hex[:8]}"
    tmp = Path(tempfile.mkdtemp())
    real_agent, real_products = factory.agent, factory.PRODUCTS
    factory.PRODUCTS = tmp

    def fake_agent(role, repo, task, **k):
        repo = Path(repo)
        if "Split this research question" in task:          # supervisor decompose -> 3 sub-questions
            return {"rc": 0, "out": "Q: q1 alpha?\nQ: q2 beta?\nQ: q3 gamma?"}
        if "Write your findings" in task:                   # child researchers; q2 hits a wall
            if "q2" in task:
                return {"rc": -1, "failed": True, "out": "", "blocker": "no web access"}
            f = task.split("Write your findings to ")[1].split(" ")[0]
            (repo / f).parent.mkdir(parents=True, exist_ok=True)
            (repo / f).write_text("finding\nSources: x")
            return {"rc": 0, "out": "ok"}
        if "SYNTHESIZER" in task:                           # supervisor aggregate
            (repo / "REPORT.md").write_text("# report")
            return {"rc": 0, "out": "ok"}
        return {"rc": 0, "out": "ok"}

    factory.agent = fake_agent
    ok = False
    try:
        res = run_research("how should we grow the creator platform",
                           tenant_id=tid, org_id="org-x", research_run_id=777)
        orc = res["orchestra_run_id"]

        # output contract: the report is on disk where research.py expects it; 2/3 answered
        out_ok = (Path(res["report"]).exists() and res["subquestions"] == 3
                  and res["answered"] == 2 and str(res["report"]).endswith("REPORT.md")
                  and f"research-777-" in res["report"])

        # durable org: run done; 5 hired actors with identity/lineage/status all as rows
        r = store.run(orc, tid)
        acts = store.actors(orc, tid)
        by_name = {a["name"]: a for a in acts}
        ctrl, sup = by_name["controller"], by_name["research-supervisor"]
        workers = [a for a in acts if a["kind"] == "worker"]
        rows_ok = (r["status"] == "done" and r["result"]["answered"] == 2
                   and len(acts) == 5 and ctrl["kind"] == "controller"
                   and ctrl["role"] == CONTROLLER_ROLE and ctrl["memory"]["research_run_id"] == 777
                   and sup["supervisor_id"] == ctrl["actor_id"] and sup["role"] == RESEARCHER_ROLE
                   and all(w["supervisor_id"] == sup["actor_id"] for w in workers)
                   and sorted(w["name"] for w in workers) == ["researcher-01", "researcher-02",
                                                              "researcher-03"]
                   and all((w["assignment"] or "").startswith("q") for w in workers))
        st = sorted(w["status"] for w in workers)
        status_ok = (st == ["blocked", "done", "done"] and ctrl["status"] == "done"
                     and sup["status"] == "done"
                     and by_name["researcher-02"]["status"] == "blocked"
                     and by_name["researcher-02"]["result"]["ok"] is False)

        # org chart: controller -> supervisor -> 3 researchers, with tenure fields for rendering
        t = store.org_tree(orc, tid)
        root = t["tree"][0]
        tree_ok = (t["actors"] == 5 and root["name"] == "controller"
                   and root["reports"][0]["name"] == "research-supervisor"
                   and len(root["reports"][0]["reports"]) == 3
                   and isinstance(root["tenure_s"], int)
                   and root["last_active_age_s"] is not None and root["last_active_age_s"] < 120)

        # durable bus: 4 tasks + 3 child verdicts (2 done, 1 blocked) + 1 supervisor done,
        # ALL claimed AND completed (nothing stranded in any inbox)
        evs = store.events(orc, tid)
        kinds = sorted(e["kind"] for e in evs)
        bus_ok = (kinds.count("task") == 4 and kinds.count("done") == 3
                  and kinds.count("blocked") == 1
                  and all(e["processed_at"] and e["claimed_at"] for e in evs)
                  and all(e["corr_id"] == "research-777" for e in evs))

        ok = out_ok and rows_ok and status_ok and tree_ok and bus_ok
        print(f"output_contract={out_ok} durable_rows={rows_ok} statuses(done,done,blocked)="
              f"{status_ok} org_tree_nested+tenure={tree_ok} bus_drained={bus_ok} "
              f"(events={len(evs)} kinds={kinds})")
        print("PASS: research as a durable ORG RUN — controller -> supervisor -> 3 parallel "
              "researchers (one blocked), real Postgres actors/events, report lands where the "
              "console expects it ✅" if ok else "FAIL")
    finally:
        factory.agent, factory.PRODUCTS = real_agent, real_products
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
        with psycopg.connect(store.DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM orchestra_events WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM orchestra_actors WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM orchestra_runs WHERE tenant_id=%s", (tid,))
            c.commit()
    sys.exit(0 if ok else 1)


def _selftest_via_org():
    """OFFLINE proof of the run_org engine path (AOS_RESEARCH_RUNORG): (A) happy path — controller-less
    research-coordinator decomposes → 3 dispatch-and-parked research_subq workers (one fails) → REPORT.md lands
    where research.py expects, answered=2/3; (B) CRASH-RESUME — a worker left parked mid-job (its finding
    deleted, in-process job handle gone) is re-dispatched by run_org's reconcile_parked and re-writes its
    finding losslessly. factory.agent stubbed (no LLM/web/spend); REAL local Postgres. Returns 0/1."""
    import shutil
    import tempfile
    import time as _t
    import psycopg
    import jobrunner
    import runtime as rt

    tid = f"research-viaorg-selftest-{uuid.uuid4().hex[:8]}"
    tmp = Path(tempfile.mkdtemp())
    real_agent, real_products = factory.agent, factory.PRODUCTS
    factory.PRODUCTS = tmp

    def fake_agent(role, repo, task, **k):
        repo = Path(repo)
        if "Split this research question" in task:
            return {"rc": 0, "out": "Q: q1 alpha?\nQ: q2 beta?\nQ: q3 gamma?"}
        if "Write your findings" in task:
            if "q2" in task:
                return {"rc": -1, "failed": True, "out": "", "blocker": "no web access"}
            f = task.split("Write your findings to ")[1].split(" ")[0]
            (repo / f).parent.mkdir(parents=True, exist_ok=True)
            (repo / f).write_text("finding\nSources: x")
            return {"rc": 0, "out": "ok"}
        if "SYNTHESIZER" in task:
            (repo / "REPORT.md").write_text("# report")
            return {"rc": 0, "out": "ok"}
        return {"rc": 0, "out": "ok"}

    factory.agent = fake_agent
    ok = False
    try:
        # (A) happy path through the durable engine
        res = run_research_via_org("how should we grow the platform", tenant_id=tid,
                                   org_id="7", research_run_id=555)
        orc = res["orchestra_run_id"]
        acts = store.actors(orc, tid)
        workers = [a for a in acts if a["kind"] == "worker"]
        happy = (Path(res["report"]).exists() and str(res["report"]).endswith("REPORT.md")
                 and res["subquestions"] == 3 and res["answered"] == 2
                 and any(a["role"] == "research-coordinator" for a in acts)
                 and len(workers) == 3
                 and (store.run(orc, tid) or {}).get("status") == "done")

        # (B) crash-resume: take one DONE worker, wipe its finding + park it as if its job crashed mid-flight,
        # drop the in-process job handle, then let run_org's reconcile_parked re-dispatch it -> re-writes it.
        repo = Path(res["report"]).parent
        victim = next(w for w in workers if (w.get("result") or {}).get("result", {}).get("ok"))
        vidx = victim["result"]["result"]["idx"]
        (repo / f"findings/{int(vidx):02d}.md").unlink(missing_ok=True)
        store.update_actor(victim["actor_id"], tid, status="blocked",
                           memory={"context": {"tool": "research_subq", "tool_dispatched": True,
                                               "tool_args": {"idx": vidx, "subq": victim["assignment"],
                                                             "repo": str(repo), "tenant": tid}}})
        with jobrunner._LOCK:
            jobrunner._JOBS.clear()                               # simulate a fresh process (crash)
        with psycopg.connect(store.DB) as c, c.cursor() as cur:   # a crashed run is 'running', not 'done'
            cur.execute("UPDATE orchestra_runs SET status='running' WHERE run_id=%s AND tenant_id=%s", (orc, tid))
            c.commit()
        deadline = _t.time() + 30
        while _t.time() < deadline:                              # run_org startup reconciles the parked worker
            rt.run_org(orc, tid, repo=str(repo), workers=2, stall_s=2.0)
            if store.actor(victim["actor_id"], tid)["status"] == "done":
                break
            _t.sleep(0.2)
        resumed = (store.actor(victim["actor_id"], tid)["status"] == "done"
                   and (repo / f"findings/{int(vidx):02d}.md").exists())    # finding re-written -> lossless

        ok = happy and resumed
        print(f"via_org: happy_path(contract+answered)={happy} crash_resume(reconcile re-dispatch)={resumed}")
        print("PASS: research via run_org — durable coordinator→parked research_subq workers, contract "
              "REPORT.md preserved, crash-resumed losslessly ✅" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        factory.agent, factory.PRODUCTS = real_agent, real_products
        shutil.rmtree(tmp, ignore_errors=True)
        with psycopg.connect(store.DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM orchestra_events WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM orchestra_actors WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM orchestra_runs WHERE tenant_id=%s", (tid,))
            c.commit()


__all__ = ["run_research", "run_research_via_org", "CONTROLLER_ROLE", "RESEARCHER_ROLE"]


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] not in ("selftest", "selftest-via-org"):
        sys.exit("usage: research_org.py selftest|selftest-via-org")
    if len(sys.argv) > 1 and sys.argv[1] == "selftest-via-org":
        sys.exit(_selftest_via_org())
    _selftest()
