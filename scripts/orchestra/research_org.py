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


__all__ = ["run_research", "CONTROLLER_ROLE", "RESEARCHER_ROLE"]


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] != "selftest":
        sys.exit("usage: research_org.py selftest")
    _selftest()
