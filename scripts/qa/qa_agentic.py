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
import os
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for p in (str(_HERE), str(_HERE.parent), str(_HERE.parent / "orchestra")):
    if p not in sys.path:
        sys.path.insert(0, p)


def run_agentic_qa(target_url, vision, *, product="app", token=None, org="0", summary="", repo=None,
                   stories=None, artifact_dir=None, restart_cmd=None, health_url=None,
                   tenant="agentic-qa", workers=2, drive_budget_s=1800, stall_s=3.0, file_findings=True):
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
    status = (store.run(rid, tenant) or {}).get("status")

    # PHASE 6 — durable, gate-consumable verdict: map the qa-coordinator's honest verdict onto the procedural
    # pipeline's report shape and reuse its persistence, so an AGENTIC run leaves the SAME artifacts a build's
    # LAUNCH gate binds to: a qa_runs row (durable history) + docs/QA-VERDICT.json (in the product repo).
    # Fail-open — persistence must never crash the run.
    coord = next((a for a in acts if a.get("role") == "qa-coordinator"), None)
    v = (coord or {}).get("result") or {}
    blocking = v.get("blocking_stories") or []

    # EVIDENCE (parity with the procedural loop): a Windows-visible evidence dir with COVERAGE.md
    # (tested-vs-untested per story, from the explorers' coverage ledgers — latest per story) + run-final.json.
    import artifacts
    evidence_dir = artifacts.run_dir(product, started)
    cov_json, lines = [], [f"# QA Coverage (agentic) — {product}", "", f"Verdict: {v.get('result', '')}", ""]
    per_story = {}
    for act in acts:
        if act.get("role") != "qa-explorer":
            continue
        res = ((act.get("result") or {}).get("result")) or {}
        if res.get("story") is not None:
            per_story[str(res["story"])] = res                # a re-test overwrites the initial explore
    for sid, res in per_story.items():
        cov = res.get("coverage") or []
        tested = [c["aspect"] for c in cov if c.get("covered")]
        untested = [c["aspect"] for c in cov if not c.get("covered")]
        cov_json.append({"story": sid, "stop_reason": res.get("stop_reason"),
                         "tested": tested, "yet_to_test": untested})
        lines += [f"## {sid}", f"- stop reason: **{res.get('stop_reason', '?')}**",
                  f"- tested ({len(tested)}): " + ("; ".join(tested) or "(none recorded)"),
                  f"- yet to test ({len(untested)}): " + ("; ".join(untested) or "(none)"), ""]
    try:
        (evidence_dir / "COVERAGE.md").write_text("\n".join(lines))
        (evidence_dir / "coverage.json").write_text(json.dumps(cov_json, indent=2, default=str))
        (evidence_dir / "run-final.json").write_text(json.dumps(
            {"product": product, "verdict": v.get("result"), "passed": bool(v.get("passed")),
             "story_status": v.get("blocking_stories"), "findings": findings}, indent=2, default=str))
    except Exception:
        pass

    report = {"verdict": v.get("result") or f"agentic QA: {status}", "summary": v.get("result") or "",
              "passed": bool(v.get("passed")), "total_stories": v.get("stories") or len(stories or []),
              "total_bugs": len(findings), "open_bugs": len(blocking), "blocking_open": len(blocking),
              "clean": bool(v.get("passed")), "rounds": 1, "evidence_dir": str(evidence_dir),
              "coverage_doc": str(evidence_dir / "COVERAGE.md"),
              "md": str(evidence_dir / "COVERAGE.md"), "json": str(evidence_dir / "run-final.json")}

    # SHIP BAR: any STILL-blocking bug (unfixed after the bounded re-test loop) becomes a governed finding —
    # so nothing broken reaches a human as a footnote, exactly like the procedural loop.
    if file_findings and blocking:
        try:
            import json as _j
            import findings as _findings
            for f in [ff for ff in findings if ff.get("blocking") and str(ff.get("story")) in set(map(str, blocking))]:
                _findings.file(f"qa-agentic:{product}", "builder",
                               f"[{product}] {f.get('title') or 'blocking defect'}",
                               _j.dumps(f, default=str), severity="high", priority=2)
        except Exception:
            pass
    try:
        import qa_run
        report["qa_run_id"] = qa_run._persist_run(report, {
            "product": product, "url": target_url, "vision": vision, "stories": [], "bugs": [],
            "started_at": started, "finished_at": time.time(), "rounds": 1, "clean": report["passed"]})
        if repo:                                    # only write the gate artifact into a real product repo
            report["verdict_json"] = qa_run.write_verdict(repo, report, product=product,
                target_url=target_url, producer="qa_agentic", qa_run_id=report.get("qa_run_id"))
    except Exception as e:
        report["persist_error"] = str(e)

    return {"run_id": rid, "status": status, "findings": findings, "actors": len(acts),
            "explorers": [a for a in acts if a.get("role") == "qa-explorer"],
            "verdict": v, "report": report}


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

    _seen = {}

    def fake_tool(name, args):
        if name == "dev_fix":                     # the dev-fixer tool-worker (spawned via the dev-handoff)
            return {"status": "done", "findings": [], "result": {"fixed": True, "files": ["src/x.js"]}}
        sid = (args.get("story") or {}).get("id")  # qa_explore: first test finds a bug; RE-TEST after fix is clean
        _seen[sid] = _seen.get(sid, 0) + 1
        findings = ([{"kind": "bug", "title": f"bug {sid}", "blocking": True, "story": sid}]
                    if _seen[sid] == 1 else [])
        return {"status": "done", "findings": findings,
                "result": {"story": sid, "stop_reason": "coverage-complete"}}
    tools.run_tool = fake_tool
    jobrunner._default_run_tool = lambda: fake_tool

    import tempfile
    tmprepo = tempfile.mkdtemp(prefix="agentic-qa-")   # a throwaway repo so write_verdict never clobbers ours
    _prev_ev = os.environ.get("AOS_QA_EVIDENCE_DIR")
    os.environ["AOS_QA_EVIDENCE_DIR"] = tempfile.mkdtemp(prefix="agentic-ev-")   # evidence in a throwaway dir
    out = None
    try:
        out = run_agentic_qa("http://app.test", "A console the user signs in to and messages an assistant.",
                             product="agentic-selftest", stories=[{"id": "US1"}, {"id": "US2"}],
                             tenant="agentic-selftest", repo=tmprepo, workers=2, drive_budget_s=60, stall_s=2.0)
        roles = [a.get("role") for a in store.actors(out["run_id"], "agentic-selftest")]
        assert out["status"] == "done", f"the org run must finish; got {out['status']}"
        # the CLOSED LOOP: 2 initial explorers find bugs -> 2 dev-coordinators -> 2 dev-fixers fix them ->
        # qa-coordinator RE-TESTS each story (2 more explorers) -> re-test is CLEAN -> verdict PASSED.
        assert roles.count("dev-coordinator") == 2, f"a dev-coordinator per blocking bug; got {roles.count('dev-coordinator')}"
        assert roles.count("dev-fixer") == 2, f"a dev-fixer per dev-coordinator; got {roles.count('dev-fixer')}"
        assert roles.count("qa-explorer") == 4, f"2 initial + 2 re-test explorers; got {roles.count('qa-explorer')}"
        coord = next(a for a in store.actors(out["run_id"], "agentic-selftest") if a["role"] == "qa-coordinator")
        v = (coord.get("result") or {})
        assert v.get("passed") is True, f"re-test was clean -> verdict must PASS; got {v}"
        assert not v.get("blocking_stories"), v
        # phase 6: the run left a durable qa_runs row + a gate-consumable QA-VERDICT.json in the (temp) repo.
        rep = out.get("report") or {}
        assert rep.get("qa_run_id"), f"agentic run must persist a qa_runs row; got {rep}"
        assert (Path(tmprepo) / "docs" / "QA-VERDICT.json").exists(), "QA-VERDICT.json (the LAUNCH artifact) must be written"
        assert rep.get("coverage_doc") and Path(rep["coverage_doc"]).exists(), "COVERAGE.md evidence must be written"
        print(f"qa_agentic selftest: PASS (CLOSED LOOP find->fix->re-test->passed; "
              f"{roles.count('qa-explorer')} explorers, {roles.count('dev-fixer')} fixers; "
              f"durable verdict persisted qa_run_id={rep.get('qa_run_id')} + QA-VERDICT.json written)")
        return 0
    finally:
        tools.run_tool = _orig_tool
        jobrunner._default_run_tool = lambda: __import__("tools").run_tool
        if _real_factory is not None:
            sys.modules["factory"] = _real_factory
        else:
            sys.modules.pop("factory", None)
        _cleanup(out["run_id"] if isinstance(out, dict) else -1, "agentic-selftest")
        import shutil
        shutil.rmtree(tmprepo, ignore_errors=True)
        shutil.rmtree(os.environ.get("AOS_QA_EVIDENCE_DIR", ""), ignore_errors=True)
        if _prev_ev is None:
            os.environ.pop("AOS_QA_EVIDENCE_DIR", None)
        else:
            os.environ["AOS_QA_EVIDENCE_DIR"] = _prev_ev
        if isinstance(out, dict) and (out.get("report") or {}).get("qa_run_id"):
            try:                                        # drop the selftest's qa_runs row
                import psycopg
                import pulse
                with psycopg.connect(pulse.DB) as c, c.cursor() as cur:
                    cur.execute("DELETE FROM qa_runs WHERE id=%s", (out["report"]["qa_run_id"],))
                    c.commit()
            except Exception:
                pass


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
