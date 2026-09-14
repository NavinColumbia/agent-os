"""Real unit/integration tests for agent-os load-bearing invariants — the correctness properties an
acquirer's engineering due-diligence would actually probe. Run: .venv/bin/python -m pytest tests/ -q
(needs the local Postgres up, as the platform does)."""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "platform"))
# Tests intentionally exercise hundreds of deny paths. Preserve those records in the tamper-evident audit log,
# while allowing production health metrics to distinguish test traffic from real policy pressure.
os.environ.setdefault("AOS_SELFTEST", "1")


def _rid():
    return os.urandom(4).hex()


def test_exhaustive_console_crawl_is_non_mutating_and_bounded():
    """The UI wiring crawler must never turn a regression run into real company work.

    This is a source-level invariant because the safety boundary lives in Playwright routing before any
    request can reach the console.  It guards both hidden model-spending GETs and every mutating API call,
    plus the outer wall-clock deadline.
    """
    crawler = (ROOT / "scripts" / "console_actions_e2e.cjs").read_text()
    wrapper = (ROOT / "scripts" / "console_actions_e2e.sh").read_text()
    assert "req.method() === 'GET' && path === '/api/brief'" in crawler
    assert "req.method() === 'GET' && path === '/api/explain'" in crawler
    assert "if (req.method() === 'GET') return route.continue()" in crawler
    assert "action-crawl-safe" in crawler
    assert "search|answer|respond" in crawler
    assert "timeout --signal=TERM --kill-after=10s 180s" in wrapper
    assert "seed_fixtures.py\" cleanup" in wrapper


def test_factory_selftest_stubs_qa_report_module_agent():
    """Running factory.py as __main__ must not leak a model call through qa_report's module alias."""
    source = (ROOT / "scripts" / "factory.py").read_text()
    assert '_qa_report_factory = _importlib.import_module("factory")' in source
    assert "_qa_report_factory.agent = _good_agent" in source
    assert "_qa_report_factory.agent = _bad_agent" in source
    assert "_qa_report_factory.agent = _orig_report_agent" in source


# ── factory crash-resume: a completed stage is detected from persisted traces ────
def test_factory_stage_resume_checkpoint():
    import psycopg
    import factory
    rid = f"build-resumetest-{_rid()}"
    with psycopg.connect(factory._DB) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO traces (run_id,product,stage,role,kind,rc)
                       VALUES (%s,'rt','SPEC','r','agent',0)""", (rid,))
        c.commit()
    try:
        assert factory._stage_done(rid, "SPEC") is True      # completed stage -> skip on resume
        assert factory._stage_done(rid, "BUILD") is False     # not done -> will run
    finally:
        with psycopg.connect(factory._DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM traces WHERE run_id=%s", (rid,))
            c.commit()


def test_factory_selftest_trace_is_not_live_health_or_resume_evidence():
    """Synthetic provider failures remain debuggable but cannot page, spend-count, or resume a build."""
    import psycopg
    import factory
    import sentinel
    rid = f"build-selftest-trace-{_rid()}"
    factory._ctx.run = rid
    factory._ctx.product = f"selftest-trace-{_rid()}"
    factory._ctx.stage = "BUILD"
    try:
        factory._trace("agent", "builder", "offline probe", "Error: overloaded_error (529)", 1,
                       tokens_out=sentinel.SESSION_BURN_WARN)
        with psycopg.connect(factory._DB) as c, c.cursor() as cur:
            cur.execute("SELECT test_run FROM traces WHERE run_id=%s", (rid,))
            assert cur.fetchone() == (True,)
            cur.execute(f"""SELECT count(*) FROM traces WHERE run_id=%s
                              AND {sentinel._LIVE_TRACE_SQL} AND {sentinel._TRANSIENT_SQL}""", (rid,))
            assert cur.fetchone()[0] == 0
        assert factory._stage_done(rid, "BUILD") is False
    finally:
        factory._ctx.run = factory._ctx.product = factory._ctx.stage = None
        with psycopg.connect(factory._DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM traces WHERE run_id=%s", (rid,))
            c.commit()


# ── self-healing: the resume sweep detects INTERRUPTED builds, not finished ones ─
def test_factory_resume_sweep_detects_interrupted_only():
    """A build that finished BUILD but has no terminal ProductComplete (an outage casualty) must be
    detected for resume; one with a ProductComplete (launched OR blocked) must be EXCLUDED so it is
    never re-resumed in an infinite loop."""
    import psycopg
    import factory
    import audit
    sfx = _rid()
    rid_int = f"build-sweep-int-{sfx}"      # interrupted: BUILD done, no terminal verdict
    rid_done = f"build-sweep-done-{sfx}"    # terminal: BUILD done + ProductComplete
    prod_int, prod_done = rid_int[len("build-"):], rid_done[len("build-"):]
    with psycopg.connect(factory._DB) as c, c.cursor() as cur:
        for rid in (rid_int, rid_done):     # aged 60m so the idle guard passes
            cur.execute("""INSERT INTO traces (run_id,product,stage,role,kind,rc,ts)
                           VALUES (%s,%s,'BUILD','builder','agent',0, now()-interval '60 minutes')""",
                        (rid, rid[len("build-"):]))
        c.commit()
    audit.append(actor="pytest", action="ProductComplete", resource=prod_done, decision="LAUNCHED")
    try:
        found = dict(factory.find_incomplete_builds(max_age_min=20))
        assert prod_int in found, "interrupted build should be detected for resume"
        assert prod_done not in found, "terminal build must NOT be re-resumed"
    finally:
        with psycopg.connect(factory._DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM traces WHERE run_id IN (%s,%s)", (rid_int, rid_done))
            c.commit()


# ── app registry: kind / version / dependency detection from the repo on disk ──
def test_appregistry_detection(tmp_path):
    import json as _json
    import appregistry as ar
    # extension: kind + version from manifest.json, vanilla-JS deps
    ext = tmp_path / "ext"; ext.mkdir()
    (ext / "manifest.json").write_text(_json.dumps({"manifest_version": 3, "version": "2.1.0"}))
    (ext / "popup.js").write_text("//")
    assert ar.detect_kind(ext) == "extension"
    assert ar.detect_version(ext, "extension") == "2.1.0"
    assert "vanilla JS" in ar.detect_dependencies(ext)[0]
    # service: src/<pkg>/__main__.py ; stdlib python deps
    svc = tmp_path / "svc"; (svc / "src" / "p").mkdir(parents=True)
    (svc / "src" / "p" / "__main__.py").write_text("")
    assert ar.detect_kind(svc) == "service"
    assert ar.detect_dependencies(svc) == ["python: stdlib only"]
    # real requirements.txt is parsed
    (svc / "requirements.txt").write_text("# c\nflask==3.0\nrequests>=2\n")
    assert ar.detect_dependencies(svc) == ["flask==3.0", "requests>=2"]
    # web vs project vs lib
    web = tmp_path / "web"; web.mkdir(); (web / "index.html").write_text("<html>")
    assert ar.detect_kind(web) == "web"
    proj = tmp_path / "pr"; (proj / "docs").mkdir(parents=True); (proj / "docs" / "PLAN.json").write_text("{}")
    assert ar.detect_kind(proj) == "project"


# ── QA depth: web/extension QA actually RUNS the builder's functional tests ───
def test_run_js_tests_gates_on_functional_behaviour(tmp_path):
    """The functional-test gate must FAIL when no behaviour tests exist (a smoke-load is not QA), PASS
    when they exist and pass, and FAIL when any fails — this is the hole that let a web app 'pass' QA
    while its real tests were never executed."""
    import factory
    ok, out = factory.run_js_tests(str(tmp_path))
    assert not ok and "NO functional tests" in out, "absence of behaviour tests must FAIL"
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "a.test.js").write_text(
        "const assert=require('assert'); assert.equal(1+1,2); console.log('ok');")
    ok2, _ = factory.run_js_tests(str(tmp_path))
    assert ok2, "a passing behaviour test must PASS"
    (tmp_path / "tests" / "b.test.js").write_text("const assert=require('assert'); assert.equal(1,2);")
    ok3, out3 = factory.run_js_tests(str(tmp_path))
    assert not ok3 and "FAIL" in out3, "a failing behaviour test must FAIL the gate"


# ── runnable systems: the runtime gate boots a real server, runs E2E flows + load ─
def test_run_e2e_qa_boots_real_server_runs_flows_and_load(tmp_path):
    """run_e2e_qa must actually launch a service, drive end-to-end HTTP flows against the LIVE server,
    and load-test it — the capability that turns 'units pass' into 'the system runs'. Uses a hand-written
    trivial stdlib server (no agents) so it's deterministic."""
    import factory
    pkg = "fakesvc"
    (tmp_path / "src" / pkg).mkdir(parents=True)
    (tmp_path / "src" / "__init__.py").write_text("")
    (tmp_path / "src" / pkg / "__init__.py").write_text("")
    (tmp_path / "src" / pkg / "__main__.py").write_text(
        "import os, json\n"
        "from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer\n"
        "class H(BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        body = json.dumps({'ok': True, 'path': self.path}).encode()\n"
        "        self.send_response(200); self.send_header('Content-Type', 'application/json')\n"
        "        self.send_header('Content-Length', str(len(body))); self.end_headers()\n"
        "        self.wfile.write(body)\n"
        "    def log_message(self, *a):\n        pass\n"
        "ThreadingHTTPServer(('127.0.0.1', int(os.environ.get('PORT', '8080'))), H).serve_forever()\n")
    (tmp_path / "tests" / "e2e").mkdir(parents=True)
    (tmp_path / "tests" / "e2e" / "test_flow.py").write_text(
        "import os, json, urllib.request\n"
        "def test_health_then_echo_flow():\n"
        "    base = os.environ['E2E_BASE']\n"
        "    with urllib.request.urlopen(base + '/health') as r:\n"
        "        assert r.status == 200\n"
        "    with urllib.request.urlopen(base + '/echo') as r:\n"
        "        assert json.load(r)['ok'] is True\n")
    ok, out = factory.run_e2e_qa(str(tmp_path), pkg)
    assert ok, f"runtime gate should pass for a working server:\n{out}"
    assert "load test: PASS" in out


# ── complex projects: dependency DAG layering + interdependent build orchestration ─
def test_project_topo_layers_and_validation():
    import project
    comps = [{"id": "store", "deps": []}, {"id": "core", "deps": ["store"]},
             {"id": "query", "deps": ["core"]}, {"id": "cli", "deps": ["core", "query"]}]
    assert project.topo_layers(comps) == [["store"], ["core"], ["query"], ["cli"]]
    # independent components occupy the same (parallel) layer
    assert project.topo_layers([{"id": "a", "deps": []}, {"id": "b", "deps": []},
                                {"id": "c", "deps": ["a", "b"]}]) == [["a", "b"], ["c"]]
    for bad in ([{"id": "x", "deps": ["y"]}],                              # unknown dep
                [{"id": "x", "deps": ["y"]}, {"id": "y", "deps": ["x"]}],  # cycle
                [{"id": "x", "deps": []}, {"id": "x", "deps": []}]):       # duplicate id
        with pytest.raises(ValueError):
            project.validate_dag(bad)


def test_project_build_complex_orchestration(monkeypatch):
    """build_complex must build components in dependency order, hand each dependent its deps' interfaces,
    then integrate — all without touching real agents."""
    import shutil
    import project
    plan_obj = {"components": [
        {"id": "store", "name": "s", "description": "d", "deps": [], "interface": "store.save()"},
        {"id": "core", "name": "c", "description": "d", "deps": ["store"], "interface": "core.do()"},
        {"id": "cli", "name": "l", "description": "d", "deps": ["core", "store"], "interface": "cli.main()"},
    ], "integration_tests": "end to end"}
    monkeypatch.setattr(project, "explore_plan", lambda *a, **k: plan_obj)
    seen = {}

    def fake_build(product, comp, dep_ifaces, api_key=None, ns="", stack=None):
        seen[comp["id"]] = set(dep_ifaces)
        return {"id": comp["id"], "passed": True, "fix_attempts": 0}
    monkeypatch.setattr(project, "build_component", fake_build)
    integrated = {"called": False}
    monkeypatch.setattr(project, "integrate",
                        lambda product, p, ns="", facade=None, stack=None: (integrated.__setitem__("called", True), {"passed": True})[1])
    prod = f"ut-complex-{_rid()}"
    try:
        log = project.build_complex(prod, "goal")
        assert log["result"] == "INTEGRATED"
        assert log["plan"]["layers"] == [["store"], ["core"], ["cli"]]      # dependency-ordered
        assert seen["core"] == {"store"} and seen["cli"] == {"core", "store"}  # dependents got interfaces
        assert integrated["called"]
    finally:
        shutil.rmtree(factory_products_dir() / prod, ignore_errors=True)


def test_project_blocks_integration_when_a_component_fails(monkeypatch):
    """If any component fails to build, the line must NOT integrate — it reports BLOCKED_AT_COMPONENTS
    and surfaces the failing component's blocker."""
    import shutil
    import project
    plan_obj = {"components": [{"id": "a", "name": "a", "description": "d", "deps": [], "interface": "a()"},
                               {"id": "b", "name": "b", "description": "d", "deps": ["a"], "interface": "b()"}],
                "integration_tests": "x"}
    monkeypatch.setattr(project, "explore_plan", lambda *a, **k: plan_obj)
    monkeypatch.setattr(project, "build_component",
                        lambda product, comp, dep_ifaces, api_key=None, ns="", stack=None: {"id": comp["id"],
                        "passed": comp["id"] != "b", "blocker": None if comp["id"] != "b" else "b failed"})
    integrated = {"called": False}
    monkeypatch.setattr(project, "integrate",
                        lambda product, p, ns="", facade=None, stack=None: (integrated.__setitem__("called", True), {"passed": True})[1])
    prod = f"ut-complex-{_rid()}"
    try:
        log = project.build_complex(prod, "goal")
        assert log["result"] == "BLOCKED_AT_COMPONENTS" and "b" in log["failed_components"]
        assert "b failed" in (log.get("blocker") or "")        # blocker propagated up
        assert not integrated["called"], "must not integrate when a component failed"
    finally:
        shutil.rmtree(factory_products_dir() / prod, ignore_errors=True)


def test_project_explore_plan_picks_best(monkeypatch, tmp_path):
    """Parallel design exploration: n>1 generates n candidate architectures and the judge's pick is built."""
    import factory
    import project
    monkeypatch.setattr(factory, "PRODUCTS", tmp_path)
    (tmp_path / "prodx" / "docs").mkdir(parents=True)
    calls = {"n": 0}

    def fake_arch(product, goal, model, ns, depth, variant, out_name, stack=None):
        calls["n"] += 1
        return {"components": [{"id": f"c{calls['n']}", "name": "x", "description": "d", "deps": [], "interface": "i()"}],
                "integration_tests": "t"}
    monkeypatch.setattr(project, "_architect", fake_arch)
    monkeypatch.setattr(project, "_judge_plans", lambda product, goal, cands: cands[max(cands)])  # pick last
    p = project.explore_plan("prodx", "goal", n=3)
    assert calls["n"] == 3                                  # three candidate architectures generated in parallel
    assert p["components"][0]["id"] == "c3"                 # the judged-best one
    assert (tmp_path / "prodx" / "docs" / "PLAN.json").exists()


def test_project_component_role_specialization(monkeypatch, tmp_path):
    """A component's `role` (infra/ml/data/…) is the agent that builds it — not always a generic builder."""
    import factory
    import project
    monkeypatch.setattr(factory, "PRODUCTS", tmp_path)
    (tmp_path / "prodr").mkdir()
    seen = {"role": None}
    monkeypatch.setattr(factory, "agent", lambda role, repo, task, **k: (seen.__setitem__("role", role), {"rc": 0})[1])
    monkeypatch.setattr(factory, "run_tests", lambda *a, **k: (True, "ok"))
    r = project.build_component("prodr", {"id": "trainer", "name": "x", "description": "d", "deps": [],
                                          "interface": "i()", "role": "ml-engineer"}, {})
    assert seen["role"] == "ml-engineer" and r["passed"]


def test_project_recursive_decomposition(monkeypatch):
    """A component the architect marks `decompose` must trigger a RECURSIVE sub-build (its own plan +
    sub-components + facade integrate) — i.e. real multi-level hierarchy, not a flat 2-level build."""
    import shutil
    import project
    monkeypatch.setattr(project, "MAX_DEPTH", 2)
    root = {"components": [
        {"id": "engine", "name": "e", "description": "big subsystem", "deps": [], "interface": "engine.run()",
         "decompose": True, "subgoal": "build the engine"},
        {"id": "api", "name": "a", "description": "thin api", "deps": ["engine"], "interface": "api.serve()"},
    ], "integration_tests": "root e2e"}
    sub = {"components": [
        {"id": "core", "name": "c", "description": "d", "deps": [], "interface": "core.x()"},
        {"id": "io", "name": "i", "description": "d", "deps": ["core"], "interface": "io.y()"},
    ], "integration_tests": "engine parts"}
    plans_for = []
    monkeypatch.setattr(project, "plan",
                        lambda product, goal, model=None, ns="", depth=0, stack=None: (plans_for.append(ns), root if ns == "" else sub)[1])
    leaves = []
    monkeypatch.setattr(project, "build_component",
                        lambda product, comp, dep_ifaces, api_key=None, ns="", stack=None: (leaves.append(ns + comp["id"]), {"id": comp["id"], "passed": True})[1])
    integ_ns = []
    monkeypatch.setattr(project, "integrate",
                        lambda product, p, ns="", facade=None, stack=None: (integ_ns.append(ns), {"passed": True})[1])
    prod = f"ut-recur-{_rid()}"
    try:
        log = project.build_complex(prod, "goal")
        assert log["result"] == "INTEGRATED"
        assert "engine_" in plans_for, "the decomposed component must be planned recursively"   # sub-architect ran
        assert {"engine_core", "engine_io"} <= set(leaves), "sub-components built under the namespace"
        assert "api" in leaves and "engine_core" in leaves                                       # leaf + sub leaves
        assert "engine_" in integ_ns and "" in integ_ns, "facade integrate + root integrate both ran"
    finally:
        shutil.rmtree(factory_products_dir() / prod, ignore_errors=True)


def test_recursive_project_total_component_budget_is_atomic():
    """Concurrency caps bound simultaneous agents; this separate budget bounds recursive total work."""
    import threading
    import project
    budget = {"planned": 0, "limit": 4, "lock": threading.Lock()}
    assert project._reserve_component_budget(budget, 2, "root", 0) == 2
    assert project._reserve_component_budget(budget, 2, "engine_", 1) == 4
    with pytest.raises(project.ScopeBudgetExceeded, match="safe limit is 4"):
        project._reserve_component_budget(budget, 1, "engine_server_", 2)
    assert budget["planned"] == 4, "a refused reservation must not consume budget"


def test_live_project_defaults_bound_recursive_scope_and_wall_time():
    import project
    import loopcontroller as lc
    assert project.MAX_DEPTH == 1
    assert project.MAX_TOTAL_COMPONENTS == 16
    assert lc.BUILD_HARD_CEILING_MIN == 60


def test_project_resume_sweep_detects_interrupted_only(tmp_path, monkeypatch):
    """A complex build with no terminal ProjectComplete (an outage casualty) + a PLAN.json must be
    detected for auto-resume; one with a ProjectComplete must be excluded (no infinite re-resume)."""
    import psycopg
    import factory
    import project
    import audit
    import loopcontroller as lc
    lc._ensure()
    monkeypatch.setattr(factory, "PRODUCTS", tmp_path)
    sfx = _rid()
    p_int, p_done, p_halted, p_active = (f"sweepproj-int-{sfx}", f"sweepproj-done-{sfx}",
                                         f"sweepproj-halted-{sfx}", f"sweepproj-active-{sfx}")
    active_thread = 970000 + int(sfx, 16) % 10000
    for prod in (p_int, p_done, p_halted, p_active):          # all look resumable (have a PLAN.json)
        (tmp_path / prod / "docs").mkdir(parents=True)
        (tmp_path / prod / "docs" / "PLAN.json").write_text('{"components":[],"integration_tests":""}')
    with psycopg.connect(factory._DB) as c, c.cursor() as cur:
        for prod in (p_int, p_done, p_halted, p_active):      # aged 60m so the idle guard passes
            cur.execute("""INSERT INTO traces (run_id,product,stage,role,kind,rc,ts)
                           VALUES (%s,%s,'BUILD:x','builder','agent',0, now()-interval '60 minutes')""",
                        (f"proj-{prod}", prod))
        cur.execute("INSERT INTO kill_switch(scope,reason,set_by) VALUES (%s,'cancelled','pytest')",
                    (p_halted,))
        cur.execute("""INSERT INTO controller_state
                       (thread_id,tenant_id,org_id,phase,product,awaiting,updated_at,execution_scope)
                       VALUES (%s,'pytest',1,'IMPLEMENT',%s,'fleet',now(),'test')""",
                    (active_thread, p_active))
        c.commit()
    audit.append(actor="pytest", action="ProjectComplete", resource=p_done, decision="INTEGRATED")
    try:
        found = factory_find_projects(project, 20)
        assert p_int in found
        assert p_done not in found and p_halted not in found
        assert p_active not in found, "a controller-owned quiet build must never be double-launched"
    finally:
        with psycopg.connect(factory._DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM traces WHERE run_id = ANY(%s)",
                        ([f"proj-{p}" for p in (p_int, p_done, p_halted, p_active)],))
            cur.execute("DELETE FROM kill_switch WHERE scope=%s", (p_halted,))
            cur.execute("DELETE FROM controller_state WHERE thread_id=%s", (active_thread,))
            c.commit()


def test_project_resume_recovers_original_stack_from_audit():
    """A detached resume must not silently turn an existing web product into a Python package."""
    import audit
    import project
    product = f"stack-resume-{_rid()}"
    audit.append(actor="pytest", action="ProjectStart", resource=product, decision="executed",
                 payload={"stack": "web"})
    # A buggy detached retry must not be allowed to redefine an established project's platform.
    audit.append(actor="pytest", action="ProjectStart", resource=product, decision="executed",
                 payload={"stack": "python"})
    assert project._persisted_stack(product) == "web"


def factory_find_projects(project_mod, age):
    return project_mod.find_incomplete_projects(max_age_min=age)


def test_project_plan_resume_reuses_existing_plan(monkeypatch, tmp_path):
    """A re-run must reuse an existing valid docs/PLAN.json instead of re-invoking the architect agent."""
    import factory
    import project
    monkeypatch.setattr(factory, "PRODUCTS", tmp_path)
    prod = "ut-resume"
    docs = tmp_path / prod / "docs"
    docs.mkdir(parents=True)
    plan_obj = {"components": [{"id": "a", "name": "a", "description": "d", "deps": [], "interface": "a()"}],
                "integration_tests": "x"}
    (docs / "PLAN.json").write_text(__import__("json").dumps(plan_obj))
    called = {"agent": False}
    monkeypatch.setattr(factory, "agent", lambda *a, **k: called.__setitem__("agent", True) or {"rc": 0})
    p = project.plan(prod, "any goal")
    assert p["components"][0]["id"] == "a" and not called["agent"], "should reuse plan, not call architect"


def factory_products_dir():
    import factory
    return factory.PRODUCTS


# ── pipeline-as-cycle: the reviewer's verdict is parsed correctly (it gates LAUNCH) ─
def test_review_verdict_parsing(tmp_path):
    import factory
    docs = tmp_path / "docs"
    docs.mkdir()
    rv = docs / "REVIEW.md"

    def verdict(body):
        rv.write_text(body)
        return factory._review_verdict(str(tmp_path))

    # explicit VERDICT line wins
    assert verdict("looks good\n\nVERDICT: APPROVE") == "APPROVE"
    assert verdict("issues found\n\nVERDICT: REQUEST-CHANGES") == "REQUEST-CHANGES"
    # the VERDICT line is authoritative even if risks are discussed above it
    assert verdict("risk: a possible REQUEST-CHANGES situation if X\n\nVERDICT: APPROVE") == "APPROVE"
    # whole-doc fallback when no explicit VERDICT line
    assert verdict("Overall this REQUEST-CHANGES because of a bug") == "REQUEST-CHANGES"
    # review prose with no explicit verdict and no change request -> APPROVE (QA stays the hard gate)
    assert verdict("Looks fine to me, nice work.") == "APPROVE"
    # NO signal at all (no reply text, no file) -> FAIL CLOSED: a silent review gate must escalate
    # (REQUEST-CHANGES -> BLOCKED_AT_REVIEW), never silently auto-pass an unreviewed build.
    rv.unlink()
    assert factory._review_verdict(str(tmp_path)) == "REQUEST-CHANGES"


# ── budget control: a spend cap halts new agent spawning (per-factory tunable) ─
def test_factory_budget_cap_halts_spawning(monkeypatch):
    import factory
    factory._ctx.api_key = None
    monkeypatch.setattr(factory, "BUDGET_USD", 1.0)
    monkeypatch.setattr(factory.time, "sleep", lambda *a, **k: None)
    factory._SPENT[0] = 1.5                                   # already over budget
    ran = {"v": False}
    monkeypatch.setattr(factory, "_run_once",
                        lambda *a, **k: (ran.__setitem__("v", True), (0, "x", 0.0, 0, 0, "m"))[1])
    try:
        r = factory.agent("builder", "/tmp", "do a thing", timeout=5, retries=0)
        assert r.get("failed") and "budget" in (r.get("blocker") or "").lower()
        assert not ran["v"], "must NOT spawn an agent once budget is exhausted"
    finally:
        factory._SPENT[0] = 0.0


# ── resilience: a sustained Anthropic outage fails over to the Codex engine ───
def test_factory_codex_failover_on_anthropic_outage(monkeypatch):
    """When Claude exhausts its retries on transient/overload errors (a provider outage), the SAME task
    runs on Codex and that result is returned — the factory keeps moving instead of just escalating."""
    import factory
    factory._ctx.api_key = None                                  # platform run (BYO would NOT fail over)
    factory._ctx.engine = "claude"                               # exercise Claude outage -> Codex failover
    monkeypatch.setattr(factory, "FALLBACK_ENGINE", "codex")
    monkeypatch.setattr(factory.shutil, "which", lambda _: "/usr/bin/codex")  # pretend codex installed
    monkeypatch.setattr(factory.time, "sleep", lambda *a, **k: None)          # no real backoff waits
    # Claude always returns an overload error; Codex fallback succeeds.
    monkeypatch.setattr(factory, "_run_once",
                        lambda *a, **k: (1, "Error: overloaded_error (529)", 0.0, 0, 0, "claude-opus-4-8"))
    monkeypatch.setattr(factory, "_run_once_codex",
                        lambda *a, **k: (0, "implemented via codex", 0.0, 120, 40, "codex"))
    r = factory.agent("builder", "/tmp", "build the thing", timeout=5, retries=1)
    assert r["rc"] == 0 and r.get("engine") == "codex", f"expected codex failover, got {r}"


def test_factory_byo_key_does_not_failover_to_codex(monkeypatch):
    """A BYO-key tenant must NOT be silently failed over to platform-funded Codex — it escalates."""
    import factory
    factory._ctx.api_key = "sk-ant-tenant-key"
    factory._ctx.engine = "claude"                               # BYO Anthropic outage must not bill platform Codex
    monkeypatch.setattr(factory, "FALLBACK_ENGINE", "codex")
    monkeypatch.setattr(factory.shutil, "which", lambda _: "/usr/bin/codex")
    monkeypatch.setattr(factory.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(factory, "_run_once",
                        lambda *a, **k: (1, "Error: overloaded_error (529)", 0.0, 0, 0, "claude-opus-4-8"))
    called = {"codex": False}
    monkeypatch.setattr(factory, "_run_once_codex",
                        lambda *a, **k: called.__setitem__("codex", True) or (0, "x", 0.0, 1, 1, "codex"))
    try:
        r = factory.agent("builder", "/tmp", "build the thing", timeout=5, retries=0)
        assert r.get("failed") and not called["codex"], "BYO key must not trigger Codex failover"
    finally:
        factory._ctx.api_key = None


# ── governance: the audit log is tamper-evident ──────────────────────────────
def test_audit_chain_intact_after_append():
    import audit
    audit.append(actor="pytest", action="UnitTest", resource=f"r-{_rid()}", decision="executed")
    ok, reason = audit.verify()
    assert ok, f"audit chain broken: {reason}"


def test_selftest_denials_are_audited_but_excluded_from_live_health():
    """Expected deny-path probes belong in the immutable trail, not in production incident rates."""
    import psycopg
    import audit
    resource = f"selftest-denial-{_rid()}"
    audit.append(actor="pytest", action="DenyProbe", resource=resource, decision="deny")
    with psycopg.connect(audit._cfg()[0]) as c, c.cursor() as cur:
        cur.execute("SELECT payload->>'_selftest' FROM audit_log WHERE resource=%s ORDER BY id DESC LIMIT 1",
                    (resource,))
        assert cur.fetchone()[0] == "true"
    for path in (ROOT / "scripts" / "dashboard.py", ROOT / "scripts" / "fleet.py",
                 ROOT / "scripts" / "watchdog.py"):
        source = path.read_text()
        assert "COALESCE(payload->>'_selftest','false') <> 'true'" in source


def test_monitor_process_probes_exclude_their_invoking_shell():
    """A wrapper containing probe text must not impersonate the workload being monitored."""
    import watchdog
    import sentinel
    assert watchdog._pgrep("definitely-not-a-real-agent-os-process") == 0
    # This pytest command line contains neither worker form; the observer must not invent one.
    assert all("pytest" not in cmd for _, _, cmd in sentinel._agent_procs())
    source = (ROOT / "scripts" / "sentinel.py").read_text()
    assert "t.run_id LIKE 'build-%' OR t.run_id LIKE 'proj-%'" in source
    db_hang_guard = source.split("# 3c) DB HANG", 1)[1].split("# 4) proactive", 1)[0]
    assert "state_change < now()" in db_hang_guard
    assert "query_start < now()" not in db_hang_guard


# ── self-healing: the responder routes safely (auto vs escalate vs unknown) ───
def test_responder_classifies_safe_vs_judgement_vs_novel():
    import responder
    assert responder.classify({"sig": "daemon:dashboard", "msg": "dashboard process is DOWN"}) == "auto"
    assert responder.classify({"sig": "alert:postgres is DOWN", "msg": "postgres is DOWN"}) == "auto"
    assert responder.classify({"sig": "alert:disk", "msg": "disk 95% — critical"}) == "auto"
    # judgement / approval calls must NEVER auto-act
    assert responder.classify({"sig": "alert:DEADLOCK", "msg": "DEADLOCK: 1 cycle(s)"}) == "escalate"
    assert responder.classify({"sig": "stall:x", "msg": "build x silent 9m"}) == "escalate"
    assert responder.classify({"sig": "alert:conflict", "msg": "conflict on src/**"}) == "escalate"
    # genuinely novel -> hand to the reasoning agent, not a blind action
    assert responder.classify({"sig": "unknown:weird", "msg": "never seen this"}) == "unknown"


# ── directory: conflict detection is correct and has no false positives ───────
def test_directory_detects_real_conflict_only():
    import directory
    p = f"prod-{_rid()}"
    directory.register(f"a@{p}", "builder", p, "BUILD", ["src/**", "tests/**"])
    directory.register(f"b@{p}", "staff-engineer", p, "REFACTOR", ["src/**"])
    directory.register(f"c@{p}", "design-ux", p, "DESIGN", ["design/**"])
    try:
        pairs = {frozenset(c["agents"]) for c in directory.conflicts() if c["product"] == p}
        assert frozenset({f"a@{p}", f"b@{p}"}) in pairs           # overlapping src/** -> conflict
        assert frozenset({f"a@{p}", f"c@{p}"}) not in pairs       # disjoint paths -> NO false conflict
    finally:
        # These are test-only identities, not real idle staff. Releasing them left three durable directory
        # rows on every suite run and eventually polluted fleet/accountability scans.
        with directory.connection() as c, c.cursor() as cur:
            cur.execute("DELETE FROM directory WHERE product=%s", (p,))


def test_directory_contact_is_brokered_not_socket():
    import directory
    suffix = _rid()
    frm, to = f"x-{suffix}@p", f"y-{suffix}@p"
    m = directory.contact(frm, to, "ask", "hello")
    try:
        assert m["message_id"].startswith("dm-") and m["to"] == to  # durable message id, no connection
    finally:
        # This contract test deliberately uses unregistered endpoints to prove offline broker delivery. Its
        # transcript is not company work and must not accumulate into the production accountability sweep.
        with directory.connection() as c, c.cursor() as cur:
            cur.execute("DELETE FROM inbox WHERE message_id=%s", (m["message_id"],))
            cur.execute("DELETE FROM conversations WHERE message_id=%s", (m["message_id"],))


# ── billing: invoice math (base, overage, quota) is correct ──────────────────
def test_billing_base_invoice_with_no_usage():
    import billing
    t = billing.signup(f"acme-{_rid()}", "pro")
    inv = billing.invoice(t["tenant_id"])
    assert inv["base"] == 49 and inv["overage"]["cost"] == 0 and inv["total"] == 49


def test_billing_unknown_plan_rejected():
    import billing
    with pytest.raises(ValueError):
        billing.signup("x", "platinum-unicorn")


def test_paid_plan_change_requires_stripe_checkout_not_column_flip():
    import billing
    import billingview
    t = billing.signup(f"billing-checkout-{_rid()}", "free")
    tid = t["tenant_id"]
    try:
        r = billingview.change_plan(tid, "pro")
        assert r["ok"] is False and r["error"] == "stripe_not_configured"
        assert billing._plan_of(tid)[0] == "free", "paid plan changed without processor confirmation"
    finally:
        import stripebilling
        with stripebilling.connection() as c, c.cursor() as cur:
            cur.execute("DELETE FROM billing_subscriptions WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM stripe_events WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,))


def test_stripe_webhook_activates_idempotently_and_dunning_preserves_admin_hold(monkeypatch):
    import hashlib
    import hmac
    import json
    import time
    import billing
    import stripebilling
    t = billing.signup(f"stripe-webhook-{_rid()}", "free")
    tid = t["tenant_id"]
    secret = "whsec_pytest"
    monkeypatch.setattr(stripebilling, "get", lambda k, default=None: secret if k == "STRIPE_WEBHOOK_SECRET" else default)

    def signed(event):
        raw = json.dumps(event, separators=(",", ":")).encode()
        ts = int(time.time())
        sig = hmac.new(secret.encode(), f"{ts}.{raw.decode()}".encode(), hashlib.sha256).hexdigest()
        return raw, f"t={ts},v1={sig}"

    try:
        checkout = {"id": f"evt_{_rid()}", "type": "checkout.session.completed",
                    "data": {"object": {"id": "cs_pytest", "customer": "cus_pytest",
                                         "subscription": "sub_pytest", "client_reference_id": tid,
                                         "metadata": {"tenant_id": tid, "plan": "pro"}}}}
        raw, sig = signed(checkout)
        assert stripebilling.handle_webhook(raw, sig)["ok"] is True
        assert stripebilling.handle_webhook(raw, sig)["duplicate"] is True
        assert billing._plan_of(tid)[0] == "pro"

        for i in range(stripebilling._DUNNING_FAILURE_LIMIT):
            assert stripebilling.handle_event({"id": f"evt_fail_{i}_{_rid()}",
                                               "type": "invoice.payment_failed",
                                               "data": {"object": {"id": f"in_{i}",
                                                                   "subscription": "sub_pytest"}}})["ok"]
        assert stripebilling.status(tid)["tenant_suspended"] is True
        assert stripebilling.status(tid)["dunning_suspended"] is True

        assert stripebilling.handle_event({"id": f"evt_paid_{_rid()}", "type": "invoice.paid",
                                           "data": {"object": {"id": "in_paid",
                                                               "subscription": "sub_pytest"}}})["ok"]
        assert stripebilling.status(tid)["tenant_suspended"] is False

        billing.suspend(tid, reason="admin hold", actor="billing:admin")
        assert stripebilling.handle_event({"id": f"evt_admin_paid_{_rid()}", "type": "invoice.paid",
                                           "data": {"object": {"id": "in_admin",
                                                               "subscription": "sub_pytest"}}})["ok"]
        assert stripebilling.status(tid)["tenant_suspended"] is True
    finally:
        with stripebilling.connection() as c, c.cursor() as cur:
            cur.execute("DELETE FROM notifications WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM billing_subscriptions WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM stripe_events WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,))


def test_stripe_live_preflight_reports_required_config_without_spend(monkeypatch):
    import billing
    import stripebilling
    t = billing.signup(f"stripe-preflight-{_rid()}", "free")
    tid = t["tenant_id"]
    try:
        monkeypatch.setattr(stripebilling, "get", lambda _k, default=None: default)
        blocked = stripebilling.preflight("pro", tenant=tid)
        assert blocked["ok"] is False
        assert blocked["checks"]["db_reachable"] is True
        assert blocked["checks"]["schema_ready"] is True
        assert blocked["checks"]["stripe_secret_configured"] is False
        assert blocked["checks"]["all_paid_prices_configured"] is False
        assert "checkout.session.completed" in " ".join(blocked["required_events"])
        assert "plan changes before" in " ".join(blocked["stop_conditions"])

        cfg = {
            "STRIPE_SECRET_KEY": "sk_test_x",
            "STRIPE_WEBHOOK_SECRET": "whsec_x",
            "AOS_STRIPE_PRICE_PRO": "price_pro",
            "AOS_STRIPE_PRICE_ENTERPRISE": "price_ent",
        }
        monkeypatch.setattr(stripebilling, "get", lambda k, default=None: cfg.get(k, default))
        ready = stripebilling.preflight("pro", tenant=tid)
        assert ready["ok"] is True
        assert ready["checkout_start"].startswith("POST /api/billing/checkout")
    finally:
        with stripebilling.connection() as c, c.cursor() as cur:
            cur.execute("DELETE FROM billing_subscriptions WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM stripe_events WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,))


# ── snapshots: encryption round-trips and rejects the wrong passphrase ────────
def test_snapshot_crypto_roundtrip_and_wrong_pass():
    from cryptography.fernet import Fernet
    import snapshot
    salt = os.urandom(16)
    tok = Fernet(snapshot._key(b"right-pass", salt)).encrypt(b"secret-state")
    assert Fernet(snapshot._key(b"right-pass", salt)).decrypt(tok) == b"secret-state"
    with pytest.raises(Exception):
        Fernet(snapshot._key(b"wrong-pass", salt)).decrypt(tok)


# ── skills registry: catalog integrity (engines/statuses are valid) ──────────
def test_skills_catalog_is_well_formed():
    import skills
    valid_engine = {"claude", "tool", "device", "gpu", "paid"}
    valid_status = {"ready", "needs_setup", "needs_key"}
    names = set()
    for name, cat, desc, roles, tools, status, engine in skills.CATALOG:
        assert engine in valid_engine, f"{name}: bad engine {engine}"
        assert status in valid_status, f"{name}: bad status {status}"
        assert roles and tools, f"{name}: missing roles/tools"
        assert name not in names, f"duplicate skill {name}"
        names.add(name)
    assert len(names) >= 90


# ── reliability invariants (regression guards for the crash-resume / concurrency / safety fixes) ──

def test_aoscfg_resolves_config_portably(monkeypatch):
    """The shared resolver must locate a real .env.local, expose DATABASE_URL + a venv python, and let a
    real environment variable override the file — the property that lets CI/dev run outside ~/projects."""
    import aoscfg
    assert aoscfg.ENV.exists(), "resolver did not locate a real .env.local"
    assert aoscfg.DB and "postgres" in aoscfg.DB, "DATABASE_URL not resolved"
    assert aoscfg.VENV_PY.endswith("python"), f"unexpected venv python: {aoscfg.VENV_PY}"
    monkeypatch.setenv("AOS_UT_KEY_XYZ", "sentinel")
    assert aoscfg.get("AOS_UT_KEY_XYZ") == "sentinel", "real env var must win over the file"
    assert aoscfg.get("AOS_UT_DEFINITELY_MISSING", "dflt") == "dflt"


def test_clauded_only_reaps_headless_never_interactive():
    """The reaper must kill fire-and-forget headless agent calls but NEVER a human's interactive Claude Code
    session — the single most dangerous mistake it could make. Staleness is a pure ceiling test."""
    import clauded
    assert clauded._is_headless_agent(
        'claude -p "do x" --output-format json --model claude-opus-4-8'), "must match a headless agent call"
    assert not clauded._is_headless_agent("claude"), "bare interactive REPL must be spared"
    assert not clauded._is_headless_agent("claude --continue"), "interactive --continue must be spared"
    assert not clauded._is_headless_agent("claude --resume sess-123"), "interactive --resume must be spared"
    assert not clauded._is_headless_agent("node /home/x/claude-helper.js"), "unrelated proc must be spared"
    assert clauded.stale([(1, 100), (2, 2000), (3, 5000)], 900) == [2, 3], "only past-ceiling pids are stale"
    assert clauded.stale([(1, 100)], 900) == [], "a fresh pid is never stale"


def test_qa_browser_bridge_teardown_kills_process_group(monkeypatch):
    """QA browser sessions must reap their whole process group, not only the direct Node bridge child."""
    import signal
    from qa import qa_explorer

    class FakeProc:
        pid = 424242

        def __init__(self):
            self.wait_calls = 0

        def poll(self):
            return None

        def wait(self, timeout=None):
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise TimeoutError("still alive")

        def terminate(self):
            raise AssertionError("process-group termination should be preferred")

        def kill(self):
            raise AssertionError("process-group kill should be preferred")

    b = qa_explorer.BrowserBridge.__new__(qa_explorer.BrowserBridge)
    b.proc = FakeProc()
    b._gate_slot = None
    b.video_path = None
    b._send = lambda _msg: {"ok": False, "error": "close timeout"}

    sent = []
    monkeypatch.setattr(qa_explorer.os, "killpg", lambda pid, sig: sent.append((pid, sig)))

    b.close()

    assert sent == [(424242, signal.SIGTERM), (424242, signal.SIGKILL)]


def test_loopcontroller_cancel_kills_descendant_process_groups():
    """A cancelled phase worker must also reap children that created their own process group."""
    import loopcontroller
    import subprocess
    import time

    proc = subprocess.Popen([
        sys.executable, "-c",
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)\n"
        "time.sleep(60)\n",
    ], start_new_session=True)
    try:
        deadline = time.time() + 5
        child = None
        while time.time() < deadline:
            kids = loopcontroller._descendant_pids(proc.pid)
            if kids:
                child = kids[0]
                break
            time.sleep(0.05)
        assert child, "test setup did not create a child process"
        assert loopcontroller._terminate_worker_group(proc.pid, grace_s=1.0) is True
        for _ in range(20):
            if loopcontroller._pid_alive(proc.pid) is False and loopcontroller._pid_alive(child) is False:
                break
            time.sleep(0.05)
        assert loopcontroller._pid_alive(proc.pid) is False
        assert loopcontroller._pid_alive(child) is False
    finally:
        for pid in [proc.pid] + loopcontroller._descendant_pids(proc.pid):
            try:
                os.kill(pid, 9)
            except Exception:
                pass


def test_loopcontroller_cancel_halts_agentic_qa_org_runs():
    """Cancelling product QA must not leave its durable QA org rows falsely running."""
    import loopcontroller
    sys.path.insert(0, str(ROOT / "scripts" / "orchestra"))
    import store

    product = f"qa-cancel-{_rid()}"
    run = store.start_run("agentic-qa", "qa cleanup test")
    coord = store.spawn_actor(run["run_id"], "agentic-qa", "qa-coordinator", "qa-coordinator",
                              kind="supervisor", memory={"context": {"product": product}})
    child = store.spawn_actor(run["run_id"], "agentic-qa", "qa-coordinator.explorer0", "qa-explorer",
                              kind="worker", supervisor_id=coord["actor_id"])
    try:
        assert store.claim_actor_step(child["actor_id"], "agentic-qa", claimed_by="test-worker")
        assert loopcontroller._halt_agentic_qa_runs(product, "test cancel") == 1
        assert store.run(run["run_id"], "agentic-qa")["status"] == "halted"
        actors = {a["actor_id"]: a for a in store.actors(run["run_id"], "agentic-qa")}
        assert actors[coord["actor_id"]]["status"] == "dead"
        assert actors[child["actor_id"]]["status"] == "dead"
        from dbpool import connection
        with connection() as c, c.cursor() as cur:
            cur.execute("SELECT step_claimed_at, step_claimed_by FROM orchestra_actors WHERE actor_id=%s",
                        (child["actor_id"],))
            assert cur.fetchone() == (None, None)
    finally:
        from dbpool import connection
        with connection() as c, c.cursor() as cur:
            cur.execute("DELETE FROM orchestra_events WHERE run_id=%s", (run["run_id"],))
            cur.execute("DELETE FROM orchestra_actors WHERE run_id=%s", (run["run_id"],))
            cur.execute("DELETE FROM orchestra_runs WHERE run_id=%s", (run["run_id"],))
            c.commit()


def test_governance_fails_loud_without_control_plane(monkeypatch):
    """A missing control-plane must FAIL LOUD at the boot guard (not silently allow), unless explicitly
    acknowledged via the env flag (CI/selftest)."""
    import governance
    monkeypatch.setattr(governance, "CONTROL_PLANE_OK", False)
    monkeypatch.delenv("AOS_ALLOW_MISSING_CONTROL_PLANE", raising=False)
    with pytest.raises(RuntimeError):
        governance.assert_control_plane()
    monkeypatch.setenv("AOS_ALLOW_MISSING_CONTROL_PLANE", "1")
    governance.assert_control_plane()   # acknowledged -> must NOT raise


def test_claude_gate_caps_concurrency_and_reclaims_leases():
    """The cross-process gate must cap total concurrent claude calls (Nth+1 acquire fails-open to None) and
    reclaim a crashed holder's slot after its lease — so a dead process can't hold a slot forever."""
    import psycopg
    import claude_gate
    t = f"claude_slots_ut_{_rid()}"
    try:
        claude_gate._ensure(t, n=2)                                  # a deliberately-sized 2-slot pool
        s1 = claude_gate.acquire("h1", wait_s=0, table=t)
        s2 = claude_gate.acquire("h2", wait_s=0, table=t)
        assert s1 and s2 and s1 != s2, "both slots should be grantable"
        assert claude_gate.acquire("h3", wait_s=0, table=t) is None, "pool exhausted -> None (fail-open)"
        claude_gate.release(s1, table=t)
        assert claude_gate.acquire("h4", wait_s=0, table=t) is not None, "released slot must be reusable"
        # Lease expiry belongs to the holder's persisted generation; a new caller cannot shorten it to steal.
        with psycopg.connect(claude_gate.DB) as c, c.cursor() as cur:
            cur.execute(f"UPDATE {t} SET lease_until=now()-interval '1 second' WHERE slot_id=%s", (s2,))
            c.commit()
        reclaimed = claude_gate.acquire("h5", wait_s=0, table=t, lease_s=60)
        assert reclaimed is not None, "persisted expired lease must reclaim"
        claude_gate.release(s2, table=t)  # stale generation must not clear its successor
        with psycopg.connect(claude_gate.DB) as c, c.cursor() as cur:
            cur.execute(f"SELECT holder FROM {t} WHERE slot_id=%s", (reclaimed,))
            assert cur.fetchone()[0] == "h5"
    finally:
        with psycopg.connect(claude_gate.DB) as c, c.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {t}"); c.commit()


def test_thread_drive_lock_enforces_single_owner():
    """Step 2 single-owner-per-build: at most one holder of a thread's drive lock at a time, so two
    sweepers (or a sweeper racing jobd) can never advance the same build concurrently. A different thread
    is independent, and the lock is reacquirable once released."""
    import loopcontroller as lc
    tid = 990000 + int(_rid(), 16) % 1000
    with lc.thread_drive_lock(tid) as owned1:
        assert owned1 is True, "first acquire must win"
        with lc.thread_drive_lock(tid) as owned2:            # separate connection -> separate session
            assert owned2 is False, "a second concurrent acquire of the SAME thread must be denied"
        with lc.thread_drive_lock(tid + 1) as owned3:
            assert owned3 is True, "a DIFFERENT thread's lock is independent"
    with lc.thread_drive_lock(tid) as owned4:
        assert owned4 is True, "the lock must be reacquirable after release"


def test_estimate_runtime_is_spawn_free_by_default(monkeypatch):
    """Latency: the default timeout/retry estimate must NOT cold-spawn a second `claude` subprocess (that
    doubled per-stage latency). It returns sane, bounded budgets from a heuristic; the LLM callee-handshake
    is opt-in via AOS_LLM_ESTIMATE. Liveness is guarded by the heartbeat + hard ceiling, not this estimate."""
    import factory

    def _boom(*a, **k):
        raise AssertionError("the default estimate must not spawn a subprocess")

    monkeypatch.delenv("AOS_LLM_ESTIMATE", raising=False)
    monkeypatch.setattr(factory.subprocess, "run", _boom)
    mins, rets = factory._estimate_runtime("builder", "x" * 2000, None)
    assert 2 <= mins <= 60 and 0 <= rets <= 3, f"budget out of bounds: {mins},{rets}"
    short_mins, _ = factory._estimate_runtime("controller", "hi", None)
    assert short_mins <= mins, "a tiny chat task must not budget more time than a big build task"


def test_dispatch_wont_double_run_an_in_flight_thread():
    """Single-writer at dispatch (Step 2): _dispatch must not create a SECOND running job — nor start the
    phase fn — for a thread that already has one in flight. Guards against a duplicate dispatch double-running
    a phase even if a racing driver slipped past the advance lock."""
    import psycopg
    import loopcontroller as lc
    lc._ensure()                                           # create controller_state/controller_jobs (fresh CI DB)
    tid = 970000 + int(_rid(), 16) % 1000
    try:
        with psycopg.connect(lc.DB) as c, c.cursor() as cur:
            cur.execute("""INSERT INTO controller_state
                           (thread_id, tenant_id, org_id, phase, awaiting, updated_at, execution_scope)
                           VALUES (%s, 1, 1, 'IMPLEMENT', NULL, now(), 'test')
                           ON CONFLICT (thread_id) DO UPDATE SET phase='IMPLEMENT', awaiting=NULL,
                             execution_scope='test'""", (tid,))
            cur.execute("""INSERT INTO controller_jobs
                           (thread_id, tenant_id, phase, kind, status, heartbeat_at, execution_scope)
                           VALUES (%s, 1, 'IMPLEMENT', 'build', 'running', now(), 'test')""", (tid,))
            c.commit()
        called = {"n": 0}
        res = lc._dispatch(tid, "build", lambda: called.__setitem__("n", called["n"] + 1) or {"ok": True})
        assert res is None, "guard must skip (return None) when a job is already in flight"
        import time
        time.sleep(0.3)                                    # if it wrongly started _work, catch it
        with psycopg.connect(lc.DB) as c, c.cursor() as cur:
            cur.execute("SELECT count(*) FROM controller_jobs WHERE thread_id=%s", (tid,))
            n = cur.fetchone()[0]
        assert n == 1, f"guard must not create a second job (found {n})"
        assert called["n"] == 0, "the phase fn must not run while a job is already in flight"
    finally:
        with psycopg.connect(lc.DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM controller_jobs WHERE thread_id=%s", (tid,))
            cur.execute("DELETE FROM controller_state WHERE thread_id=%s", (tid,))
            c.commit()


def test_gated_ceo_approval_posts_registered_ack(monkeypatch):
    """A CEO approval at a parked gate must leave a visible receipt before the controller advances.

    The live J5 dogfood path needs proof that "Approve the plan" was accepted, not just that a button was
    clickable. Keep the phase advance stubbed so the test does not spawn model/build work.
    """
    import json
    import psycopg
    import consent
    import loopcontroller as lc

    lc._ensure()
    tid = f"ack-{_rid()}"
    thread = 980000 + int(_rid(), 16) % 1000
    monkeypatch.setattr(consent, "require_consent", lambda _tid: True)
    monkeypatch.setattr(lc, "_resolved_provider", lambda _tid: {"provider": "openai", "auth_mode": "default_cli"})
    monkeypatch.setattr(lc, "_apply_provider_ctx", lambda _prov: None)
    monkeypatch.setattr(lc, "_advance_owned", lambda _thread: None)
    try:
        with psycopg.connect(lc.DB) as c, c.cursor() as cur:
            cur.execute("""INSERT INTO controller_state
                           (thread_id, tenant_id, org_id, phase, awaiting, plan, updated_at, execution_scope)
                           VALUES (%s,%s,1,'DEEP_DESIGN','user_feedback',%s,now(),'test')""",
                        (thread, tid, json.dumps({"name": "test-plan", "kind": "web"})))
            c.commit()

        out = lc.say(tid, thread, "Approve the plan")
        assert out.get("advanced") is True

        with psycopg.connect(lc.DB) as c, c.cursor() as cur:
            cur.execute("""SELECT content, meta FROM chat_messages
                           WHERE thread_id=%s AND role='assistant'
                           ORDER BY id DESC LIMIT 1""", (thread,))
            content, meta = cur.fetchone()
        meta = meta if isinstance(meta, dict) else json.loads(meta or "{}")
        assert "Registered" in content
        assert meta.get("kind") == "decision_registered"
        assert meta.get("verdict") == "approve"
    finally:
        with psycopg.connect(lc.DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM controller_jobs WHERE thread_id=%s", (thread,))
            cur.execute("DELETE FROM chat_messages WHERE thread_id=%s", (thread,))
            cur.execute("DELETE FROM controller_state WHERE thread_id=%s", (thread,))
            c.commit()


def test_plan_approval_accepts_default_cli_provider(monkeypatch):
    """PLAN_APPROVAL must match preflight/factory provider semantics: default CLI auth is usable."""
    import json
    import psycopg
    import loopcontroller as lc

    lc._ensure()
    tid = f"default-cli-{_rid()}"
    thread = 981000 + int(_rid(), 16) % 1000
    dispatched = {}
    monkeypatch.setattr(lc, "_resolved_provider",
                        lambda _tid: {"provider": "openai", "auth_mode": "default_cli", "key": None})
    monkeypatch.setattr(lc, "_dispatch",
                        lambda _thread, kind, **_kw: dispatched.setdefault("kind", kind) or 1)
    try:
        with psycopg.connect(lc.DB) as c, c.cursor() as cur:
            cur.execute("""INSERT INTO controller_state
                           (thread_id, tenant_id, org_id, phase, awaiting, plan, updated_at, execution_scope)
                           VALUES (%s,%s,1,'PLAN_APPROVAL',NULL,%s,now(),'test')""",
                        (thread, tid, json.dumps({"name": "default-cli-plan", "kind": "web"})))
            c.commit()

        lc.advance(thread)

        with psycopg.connect(lc.DB) as c, c.cursor() as cur:
            cur.execute("SELECT phase, awaiting FROM controller_state WHERE thread_id=%s", (thread,))
            phase, awaiting = cur.fetchone()
            cur.execute("""SELECT count(*) FROM audit_log
                           WHERE actor='agent_request' AND action='AgentRequestAsk'
                             AND payload->>'thread_id'=%s""", (str(thread),))
            credential_asks = cur.fetchone()[0]
        assert phase == "PROTOTYPE"
        assert awaiting is None
        assert dispatched["kind"] == "design"
        assert credential_asks == 0
    finally:
        with psycopg.connect(lc.DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM controller_jobs WHERE thread_id=%s", (thread,))
            cur.execute("DELETE FROM chat_messages WHERE thread_id=%s", (thread,))
            cur.execute("DELETE FROM controller_state WHERE thread_id=%s", (thread,))
            c.commit()


def test_options_retry_reruns_research_without_llm_elaboration(monkeypatch):
    """If option cards are bad/stale, 'retry' must rerun research, not become an options Q&A turn."""
    import json
    import psycopg
    import consent
    import loopcontroller as lc

    lc._ensure()
    tid = f"opt-retry-{_rid()}"
    thread = 982000 + int(_rid(), 16) % 1000
    advanced = {"n": 0}
    monkeypatch.setattr(consent, "require_consent", lambda _tid: True)
    monkeypatch.setattr(lc, "_resolved_provider", lambda _tid: {"provider": "openai", "auth_mode": "default_cli"})
    monkeypatch.setattr(lc, "_apply_provider_ctx", lambda _prov: None)
    monkeypatch.setattr(lc, "_advance_owned", lambda _thread: advanced.__setitem__("n", advanced["n"] + 1))
    try:
        with psycopg.connect(lc.DB) as c, c.cursor() as cur:
            cur.execute("""INSERT INTO controller_state
                           (thread_id, tenant_id, org_id, phase, awaiting, options, research_run_id, updated_at,
                            execution_scope)
                           VALUES (%s,%s,1,'OPTIONS','user_approval',%s,777,now(),'test')""",
                        (thread, tid, json.dumps([{"id": 1, "title": "Bad option"}])))
            c.commit()
        out = lc.say(tid, thread, "retry")
        assert out.get("retried") is True
        assert advanced["n"] == 1
        st = lc._st(thread)
        assert st["phase"] == "RESEARCH"
        assert st["awaiting"] is None
        assert st["options"] is None
        assert st["research_run_id"] is None
    finally:
        with psycopg.connect(lc.DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM chat_messages WHERE thread_id=%s", (thread,))
            cur.execute("DELETE FROM controller_jobs WHERE thread_id=%s", (thread,))
            cur.execute("DELETE FROM controller_state WHERE thread_id=%s", (thread,))
            c.commit()


def test_dispatch_and_park_worker_finishes_without_advancing():
    """Step 3 dispatch-and-park: run_job (the parked-worker entrypoint) rebuilds the phase from state, runs
    it, writes the job's terminal result, and LEAVES awaiting='fleet' WITHOUT advancing — the poller advances
    under the drive lock (single-owner), so a worker can never race a driver. Uses the trivial '__selftest__'
    phase (no claude spawn) and a no-op sentinel phase so a concurrent daemon can't turn it into real work."""
    import psycopg
    import loopcontroller as lc
    lc._ensure()
    tid = 960000 + int(_rid(), 16) % 1000
    try:
        with psycopg.connect(lc.DB) as c, c.cursor() as cur:
            cur.execute("""INSERT INTO controller_state
                           (thread_id, tenant_id, org_id, phase, awaiting, updated_at, execution_scope)
                           VALUES (%s,1,1,'__PARKTEST__','fleet', now(),'test')
                           ON CONFLICT (thread_id) DO UPDATE SET phase='__PARKTEST__', awaiting='fleet',
                             execution_scope='test'""", (tid,))
            cur.execute("""INSERT INTO controller_jobs
                           (thread_id, tenant_id, phase, kind, status, heartbeat_at, execution_scope)
                           VALUES (%s,1,'__PARKTEST__','__selftest__','running', now(),'test') RETURNING id""", (tid,))
            jid = cur.fetchone()[0]; c.commit()
        lc.run_job(tid, "__selftest__", jid)
        with psycopg.connect(lc.DB) as c, c.cursor() as cur:
            cur.execute("SELECT status, result FROM controller_jobs WHERE id=%s", (jid,))
            st, res = cur.fetchone()
            cur.execute("SELECT awaiting FROM controller_state WHERE thread_id=%s", (tid,))
            awaiting = cur.fetchone()[0]
        assert st == "done", f"parked worker must finish the job (got {st})"
        assert isinstance(res, dict) and res.get("parked_selftest"), f"terminal result not written: {res}"
        assert awaiting == "fleet", "parked worker must NOT advance — the poller does, under the drive lock"
    finally:
        with psycopg.connect(lc.DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM controller_jobs WHERE thread_id=%s", (tid,))
            cur.execute("DELETE FROM controller_state WHERE thread_id=%s", (tid,))
            c.commit()


def test_demo_e2e_driver_navigates_every_gate_to_deliver():
    """The end-to-end demo harness must play the CEO correctly: choose a research option, clear both approval
    gates, and ride the fleet phases through to DELIVER. Validated against an in-memory controller (no spend)
    so a regression in the auto-CEO drive logic is caught in CI."""
    import demo_e2e
    sim = demo_e2e._SimController()
    events = []
    r = demo_e2e.drive(sim, "sim", 1, "a demo product", poll_s=0, max_min=1, on_event=events.append)
    phases = [e["phase"] for e in events if e.get("event") == "state"]
    assert r.get("ok") and r.get("phase") == "DELIVER", f"driver must reach DELIVER: {r}"
    assert sim.saw_choose, "driver must choose a research option at OPTIONS"
    assert sim.gate_actions >= 2, "driver must clear the approval gates"
    assert "OPTIONS" in phases and "IMPLEMENT" in phases, f"must traverse the pipeline: {phases}"


def test_dispatch_and_park_defaults_on():
    """Detached, identity-recorded workers are an invariant, not an unsafe runtime toggle."""
    import loopcontroller as lc
    assert lc._PARK is True, "dispatch-and-park should default ON"


def test_plan_parser_survives_markdown_wrapped_fields():
    """Observed on a LIVE build: the model bolds the [[PLAN]] field labels/values (**name:** x, kind: **web**),
    and the old parser captured the markdown -> name='**' (a broken build slug) and a mis-read kind (wrong
    builder path). The parser must strip emphasis from the scalar fields while leaving the multi-line plan
    bullets (which legitimately contain '- ' and '**') intact, and never mangle clean input."""
    import loopcontroller as lc
    p = lc._parse_plan("name: **finance-tracker**\nkind: **web**\n"
                       "plan:\n- **Foundation:** src/db.ts\n- import/export\n"
                       "agentic: ** none\ncharter: **A local-first tracker.**")
    assert p["name"] == "finance-tracker", f"markdown must not leak into the slug: {p['name']!r}"
    assert p["kind"] == "web", f"kind must parse past emphasis: {p['kind']!r}"
    assert not p["charter"].startswith("*") and p["charter"].endswith("."), p["charter"]
    assert p["plan"].splitlines()[0] == "- **Foundation:** src/db.ts", "plan bullets must be preserved verbatim"
    # THE actual live failure: EVERY label bolded -> the terminator missed the next label and each field ate the
    # rest of the block (kind captured "web\nplan\n..." and squashed to garbage -> wrong builder path).
    q = lc._parse_plan("**name:** local-first-finance-tracker\n**kind:** web\n**plan:**\n"
                       "- **Foundation:** src/db.ts\n- import/export handles edge cases\n"
                       "**agentic:** none\n**charter:** A local-first tracker.")
    assert q["kind"] == "web", f"bolded label must not collapse field boundaries: {q['kind']!r}"
    assert "import/export" in q["plan"], "every plan bullet must survive, not just the first"
    assert q["charter"] == "A local-first tracker.", q["charter"]
    # an unknown/garbled kind still falls back safely; clean input is untouched
    assert lc._parse_plan("name: x\nkind: nonsense")["kind"] == "service"
    clean = lc._parse_plan("name: myapp\nkind: project\nplan:\n- do x\ncharter: Build it.")
    assert clean["name"] == "myapp" and clean["kind"] == "project"


def test_rebuild_ctx_restores_billing_context_in_a_fresh_process(monkeypatch):
    """The one thing a DETACHED park worker must get right (the risk I flagged): rebuild factory._ctx — tenant,
    org, product, and the resolved provider's engine+key — from the thread's persisted state alone, so model
    spend lands on the TENANT's account exactly as the in-process path. Proven WITHOUT a live build by stubbing
    provider resolution; this is what de-risks turning dispatch-and-park on."""
    import psycopg
    import factory
    import loopcontroller as lc
    lc._ensure()
    tid_thread = 930000 + int(_rid(), 16) % 1000
    try:
        with psycopg.connect(lc.DB) as c, c.cursor() as cur:
            cur.execute("""INSERT INTO controller_state
                           (thread_id, tenant_id, org_id, phase, product, updated_at, execution_scope)
                           VALUES (%s,'acme-tenant',7,'IMPLEMENT','acme-app', now(),'test')
                           ON CONFLICT (thread_id) DO UPDATE
                             SET tenant_id='acme-tenant', org_id=7, product='acme-app',
                                 execution_scope='test'""", (tid_thread,))
            c.commit()
        monkeypatch.setattr(lc, "_resolved_provider",
                            lambda tid: {"engine": "claude", "key": "sk-ant-TENANTKEY", "auth_mode": "api_key"})
        factory._ctx.tenant = factory._ctx.api_key = factory._ctx.engine = None   # dirty it, prove it's rebuilt
        lc._rebuild_ctx(tid_thread)
        assert factory._ctx.tenant == "acme-tenant"
        assert factory._ctx.org == 7
        assert factory._ctx.product == "acme-app"
        assert factory._ctx.api_key == "sk-ant-TENANTKEY", "the tenant's BYO key must be restored (billing lands on THEM)"
        assert factory._ctx.engine == "claude"
    finally:
        factory._ctx.tenant = factory._ctx.api_key = factory._ctx.engine = None
        with psycopg.connect(lc.DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM controller_state WHERE thread_id=%s", (tid_thread,)); c.commit()


def test_qa_verdict_ok_is_strict_and_fail_closed():
    """The single ship predicate shared by the QA stage, TESTQA, and the LAUNCH gate: a build reaches a human
    ONLY if passed is exactly True AND blocking_open==0 AND stories>0. Every missing/garbled/loose fact must
    FAIL closed — this is the contract that keeps a bad or unverified build from shipping. Locking it here
    stops a future refactor from silently loosening the gate."""
    import factory
    # the one shape that ships
    assert factory.qa_verdict_ok({"passed": True, "blocking_open": 0, "stories": 5}) is True
    # every failing shape must NOT ship
    fail_shapes = [
        {"passed": False, "blocking_open": 0, "stories": 5},      # not passed
        {"passed": "true", "blocking_open": 0, "stories": 5},     # truthy string, not bool True
        {"passed": 1, "blocking_open": 0, "stories": 5},          # int 1, not bool True
        {"passed": True, "blocking_open": 2, "stories": 5},       # open blocking bugs
        {"passed": True, "blocking_open": 0, "stories": 0},       # zero stories = QA didn't explore
        {"passed": True, "stories": 5},                           # blocking_open missing
        {"passed": True, "blocking_open": 0},                     # stories missing
        {"blocking_open": 0, "stories": 5},                       # passed missing
        {"passed": True, "blocking_open": None, "stories": 5},    # garbled fact
        {"passed": True, "blocking_open": 0, "stories": None},    # garbled fact
        {},                                                       # empty
        None,                                                     # not even a dict
        "passed",                                                 # not a dict
    ]
    for shape in fail_shapes:
        assert factory.qa_verdict_ok(shape) is False, f"must FAIL closed on: {shape!r}"


def test_worker_crash_is_transparently_resumed_and_persistent_crash_goes_to_management(monkeypatch):
    """Zero bugs reach a human: a worker CRASH (reaped -> job marked crashed=true) is re-run transparently,
    NOT surfaced to the CEO. Persistent crashes become an internal management incident/reassignment decision;
    elapsed retries alone cannot manufacture a human authority boundary. Uses a no-op sentinel phase."""
    import json
    import psycopg
    import loopcontroller as lc
    lc._ensure()
    tid = 940000 + int(_rid(), 16) % 1000
    crashed = {"error": "worker died (heartbeat lapsed or hard ceiling)", "status": "failed", "crashed": True}

    def _mkcrash(n=1):
        with psycopg.connect(lc.DB) as c, c.cursor() as cur:
            for _ in range(n):
                cur.execute("""INSERT INTO controller_jobs
                               (thread_id,tenant_id,phase,kind,status,result,execution_scope)
                               VALUES (%s,1,'__PARKTEST__','build','failed',%s::jsonb,'test')""",
                            (tid, json.dumps(crashed)))
            cur.execute("UPDATE controller_state SET awaiting=NULL WHERE thread_id=%s", (tid,))
            c.commit()

    def _awaiting():
        with psycopg.connect(lc.DB) as c, c.cursor() as cur:
            cur.execute("SELECT awaiting FROM controller_state WHERE thread_id=%s", (tid,))
            return cur.fetchone()[0]
    try:
        with psycopg.connect(lc.DB) as c, c.cursor() as cur:
            cur.execute("""INSERT INTO controller_state
                           (thread_id, tenant_id, org_id, phase, awaiting, updated_at, execution_scope)
                           VALUES (%s,1,1,'__PARKTEST__',NULL, now(),'test')
                           ON CONFLICT (thread_id) DO UPDATE SET phase='__PARKTEST__', awaiting=NULL,
                             execution_scope='test'""", (tid,))
            c.commit()
        _mkcrash(1)                                            # a single crash -> transparent resume
        lc.advance(tid, job_result=crashed)
        assert _awaiting() != "user_feedback", "a transient crash must NOT escalate to the human"

        monkeypatch.setattr(lc, "_manage_operational_failure", lambda *a, **k: {
            "status": "open", "action": "open_incident", "manager_role": "department-head"})
        _mkcrash(lc.CRASH_RETRY_MAX + 1)                       # persistent -> internal management
        lc.advance(tid, job_result=crashed)
        assert _awaiting() != "user_feedback", "operational crashes must stay inside management"
    finally:
        with psycopg.connect(lc.DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM controller_jobs WHERE thread_id=%s", (tid,))
            cur.execute("DELETE FROM controller_state WHERE thread_id=%s", (tid,))
            c.commit()


def test_pulse_reap_orphans_finalizes_dead_only():
    """After a host/WSL restart a pulse whose process is gone must not linger as 'stalled' forever (it
    pollutes the in-flight view and pages the owner on work that no longer exists). reap_orphans() finalizes
    a pulse silent past the reap floor to terminal 'reaped', while leaving a freshly-beating pulse ALONE."""
    import psycopg
    import pulse
    if not pulse.DB:
        pytest.skip("no DB")
    dead, live = f"reap-dead-{_rid()}", f"reap-live-{_rid()}"
    try:
        pulse.start(dead, "test", "orphan"); pulse.start(live, "test", "alive")
        # backdate the orphan's heartbeat past the reap floor; the live one keeps a fresh beat
        with psycopg.connect(pulse.DB) as c, c.cursor() as cur:
            cur.execute("UPDATE agent_pulse SET last_beat = now() - interval '2 hours' WHERE work_id=%s", (dead,))
            c.commit()
        n = pulse.reap_orphans()
        assert n >= 1
        with psycopg.connect(pulse.DB) as c, c.cursor() as cur:
            cur.execute("SELECT status FROM agent_pulse WHERE work_id=%s", (dead,))
            assert cur.fetchone()[0] == "reaped", "a dead-heartbeat pulse must be reconciled to terminal"
            cur.execute("SELECT status FROM agent_pulse WHERE work_id=%s", (live,))
            assert cur.fetchone()[0] != "reaped", "a freshly-beating pulse must NOT be reaped"
    finally:
        with psycopg.connect(pulse.DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM agent_pulse WHERE work_id = ANY(%s)", ([dead, live],))
            c.commit()


def test_proactive_comms_pushes_matters_dedups_and_rerouts_by_severity():
    """Proactive comms: the CEO is briefed on the calls that matter without opening the app — new actionable
    items push once (high→urgent/phone, medium→standard/feed), a standing item is deduped, and it re-reminds
    when overdue. Injected seams keep it offline."""
    import psycopg
    import proactivecomms as pc
    tid = f"pc-{_rid()}"
    sent = []
    notify = lambda t, cat, title, body="", level="standard", url="": sent.append((level, title))
    items = [{"id": "blocked_build:x", "title": "Build blocked", "detail": "needs a key", "severity": "high"},
             {"id": "q:1", "title": "Agent question", "detail": "which region?", "severity": "medium"}]
    sig = lambda t, o: items
    try:
        first = pc.sweep(tid, notify=notify, signals=sig)
        assert len(first) == 2                                    # both new -> pushed
        assert pc.sweep(tid, notify=notify, signals=sig) == []    # same items -> deduped
        assert len(pc.sweep(tid, cooldown_s=0, notify=notify, signals=sig)) == 2   # overdue -> re-remind
        levels = {title: lvl for (lvl, title) in sent}
        assert levels["Build blocked"] == "urgent" and levels["Agent question"] == "standard"
    finally:
        with psycopg.connect(pc.DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM proactive_sent WHERE tenant_id=%s", (tid,)); c.commit()


def test_proactive_comms_pings_ceo_when_build_parks_on_a_decision_gate():
    """THE gap that made the owner poll: a build parked mid-pipeline on a decision gate (OPTIONS / plan approval /
    clarification) was invisible — approvals.inbox never surfaced controller-pipeline gates. _controller_gates now
    reads controller_state so a parked build becomes a high-severity 'ceo_decision' that the sweep PAGES to the
    phone. A CANCELLED build (killswitch-halted) must NOT re-ping. Uses a throwaway tenant + real rows."""
    import psycopg
    import proactivecomms as pc
    import killswitch
    from aoscfg import DB
    rid = _rid()
    tid = f"pc-gate-{rid}"
    base = 990000 + (abs(hash(rid)) % 9000)
    live, cancelled = base, base + 100000
    try:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            for thr, prod in ((live, "finance-tracker"), (cancelled, "dead-app")):
                cur.execute("""INSERT INTO controller_state
                               (thread_id, tenant_id, phase, awaiting, product, updated_at, execution_scope)
                               VALUES (%s,%s,'OPTIONS','user_feedback',%s, now(),'test')
                               ON CONFLICT (thread_id) DO UPDATE SET tenant_id=EXCLUDED.tenant_id,
                                 phase='OPTIONS', awaiting='user_feedback', updated_at=now(),
                                 execution_scope='test'""", (thr, tid, prod))
            c.commit()
        killswitch.halt(f"thread-{cancelled}", "test: a stopped build must not re-ping")

        gates = pc._controller_gates(tid)
        refs = {g["ref"] for g in gates}
        assert str(live) in refs, f"a parked build must surface as a decision gate: {gates}"
        assert str(cancelled) not in refs, "a cancelled/halted build must NOT surface"
        g = next(x for x in gates if x["ref"] == str(live))
        assert g["kind"] == "ceo_decision" and g["severity"] == "high"

        # the sweep must PAGE the phone (founder pager) for a ceo_decision — not just drop it in the in-app feed.
        paged = []
        import notify as pager
        real_send = pager.send
        try:
            pager.send = lambda *a, **k: paged.append((a, k)) or True
            pushed = pc.sweep(tid, notify=lambda *a, **k: None)
        finally:
            pager.send = real_send
        assert f"ceo_gate:{live}" in pushed, f"the live gate must be pushed, got {pushed}"
        assert paged, "a CEO decision gate must page the phone, not just the in-app feed"
    finally:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM controller_state WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM proactive_sent WHERE tenant_id=%s", (tid,)); c.commit()
        for thr in (live, cancelled):
            try:
                killswitch.resume(f"thread-{thr}")
            except Exception:
                pass


def test_approvals_selftest_mode_never_launches_detached_build(monkeypatch):
    """The automatic North-Star proof is process/model-free, even on retry approval."""
    import subprocess
    import approvals

    monkeypatch.setenv("AOS_SELFTEST", "1")
    monkeypatch.setattr(subprocess, "Popen",
                        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not spawn")))
    assert approvals._retry_build("selftest-product") is False
    harness = (ROOT / "scripts" / "selftest.sh").read_text()
    assert "export AOS_SELFTEST=1" in harness
    assert 'os.environ["AOS_SELFTEST"] = "1"' in (ROOT / "scripts" / "approvals.py").read_text()


def test_approvals_surfaces_controller_gate_and_thread_reply_clears_notification(monkeypatch):
    """A proactive CEO-gate notification must open to a real Approvals item, not "All clear".

    Replying from Approvals has to hit the exact parked controller thread and clear the matching unread
    notification, otherwise the CEO gets a stale badge after responding.
    """
    import json
    import psycopg
    import consent
    import approvals
    import console
    import loopcontroller as lc
    from aoscfg import DB

    lc._ensure()
    tid = f"ap-gate-{_rid()}"
    thread = 985000 + int(_rid(), 16) % 1000
    monkeypatch.setattr(consent, "require_consent", lambda _tid: True)
    monkeypatch.setattr(lc, "_resolved_provider", lambda _tid: {"provider": "openai", "auth_mode": "default_cli"})
    monkeypatch.setattr(lc, "_apply_provider_ctx", lambda _prov: None)
    monkeypatch.setattr(lc, "_advance_owned", lambda _thread: None)
    try:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("""INSERT INTO tenants (tenant_id, name, api_token) VALUES (%s,%s,%s)
                           ON CONFLICT (tenant_id) DO NOTHING""", (tid, tid, f"aos_{_rid()}"))
            cur.execute("""INSERT INTO controller_state
                           (thread_id, tenant_id, org_id, phase, awaiting, plan, updated_at, execution_scope)
                           VALUES (%s,%s,1,'DEEP_DESIGN','user_feedback',%s,now(),'test')""",
                        (thread, tid, json.dumps({"name": "gate-plan", "kind": "web"})))
            cur.execute("""INSERT INTO notifications (tenant_id, category, level, title, body, url)
                           VALUES (%s,'approvals','urgent','Your build is waiting on you (DEEP_DESIGN)',
                                   %s,'/#approvals') RETURNING id""",
                        (tid, f"Reply to task 'ceo-{thread}' from your phone, or open the console."))
            nid = cur.fetchone()[0]
            c.commit()

        gate = next((i for i in approvals.inbox(tid)["items"]
                     if i.get("kind") == "ceo_decision" and str(i.get("ref")) == str(thread)), None)
        assert gate and "waiting on you" in gate["title"]

        out = console._ctl_say_thread(tid, thread, "Approve the plan")
        assert out.get("advanced") is True

        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("SELECT read_at IS NOT NULL FROM notifications WHERE id=%s", (nid,))
            assert cur.fetchone()[0] is True
            cur.execute("""SELECT content, meta FROM chat_messages
                           WHERE thread_id=%s AND role='assistant'
                           ORDER BY id DESC LIMIT 1""", (thread,))
            content, meta = cur.fetchone()
        meta = meta if isinstance(meta, dict) else json.loads(meta or "{}")
        assert "Registered" in content
        assert meta.get("kind") == "decision_registered"
    finally:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM notifications WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM chat_messages WHERE thread_id=%s", (thread,))
            cur.execute("DELETE FROM controller_jobs WHERE thread_id=%s", (thread,))
            cur.execute("DELETE FROM controller_state WHERE thread_id=%s", (thread,))
            cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,))
            c.commit()


def test_proactive_comms_sends_bounded_progress_update_from_pulse():
    """A real CEO expects operating updates even when nothing needs a decision. Long-running pulse-tracked
    work should produce one bounded progress briefing, deduped by its own shorter cadence."""
    import psycopg
    import sys
    import types
    import proactivecomms as pc
    tid = f"pc-progress-{_rid()}"
    real_pulse = sys.modules.get("pulse")
    fake_pulse = types.ModuleType("pulse")
    fake_pulse.live = lambda: [
        {"work_id": "qa:one", "kind": "qa-run", "label": "QA explorer", "tenant_id": tid,
         "status": "active", "stage": "wait-watch", "progress": "checking live progress",
         "age_s": pc.PROGRESS_MIN_AGE_S + 20, "stalled": False},
        {"work_id": "actor:two", "kind": "fleet-actor", "label": "researcher · market", "tenant_id": tid,
         "status": "working", "stage": "research", "progress": "payment providers",
         "age_s": pc.PROGRESS_MIN_AGE_S + 80, "stalled": False},
        {"work_id": "other", "kind": "qa-run", "label": "other tenant", "tenant_id": tid + "-other",
         "status": "active", "stage": "noise", "age_s": 99999, "stalled": False},
    ]
    sys.modules["pulse"] = fake_pulse
    sent = []
    try:
        items = pc._progress_update(tid)
        assert len(items) == 1
        item = items[0]
        assert item["kind"] == "progress_update"
        assert item["severity"] == "low"
        assert "2 active work item" in item["detail"] and "other tenant" not in item["detail"]
        first = pc.sweep(tid, notify=lambda *a, **k: sent.append((a, k)), signals=lambda _t, _o: items)
        second = pc.sweep(tid, notify=lambda *a, **k: sent.append((a, k)), signals=lambda _t, _o: items)
        assert first == ["progress:active-work"]
        assert second == []
        assert sent[0][0][2] == item["title"] and sent[0][1]["level"] == "passive"
    finally:
        if real_pulse is not None:
            sys.modules["pulse"] = real_pulse
        else:
            sys.modules.pop("pulse", None)
        with psycopg.connect(pc.DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM proactive_sent WHERE tenant_id=%s", (tid,)); c.commit()


def test_notification_delivery_receipts_are_honest_and_retryable(monkeypatch):
    """Persisting an in-app row is not proof that ntfy/email accepted it. Failed/unconfigured transports
    remain explicit and a later scheduler retry can transition only the observed push attempt to accepted."""
    import psycopg
    import notifications
    import push
    from aoscfg import DB
    tid = f"notif-honest-{_rid()}"
    monkeypatch.setattr(notifications, "_email", lambda *a, **k: False)
    monkeypatch.setattr(notifications, "_push_now", lambda *a, **k: {"sent": False, "reason": "no topic"})
    monkeypatch.setattr(notifications.notify, "send", lambda *a, **k: False)
    monkeypatch.setattr(push.audit, "append", lambda *a, **k: None)
    try:
        out = notifications.send(tid, "approvals", "Decision needed", "Choose A or B", level="urgent")
        assert out["channels"] == ["in_app"]
        assert out["attempted"] == {
            "email": "unavailable", "push": "unavailable", "operator_page": "unavailable"}
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("""SELECT channel,status,attempts FROM notification_deliveries
                           WHERE notification_id=%s ORDER BY channel""", (out["id"],))
            assert cur.fetchall() == [
                ("email", "unavailable", 1), ("operator_page", "unavailable", 1),
                ("push", "unavailable", 1)]
            cur.execute("""SELECT next_attempt_at IS NULL FROM notification_deliveries
                           WHERE notification_id=%s AND channel='push'""", (out["id"],))
            assert cur.fetchone()[0] is True
        assert notifications._claim_pending(notification_id=out["id"]) is None
        push.register(tid, topic=f"test-{tid}")
        monkeypatch.setattr(notifications, "_push_now", lambda *a, **k: {"sent": True})
        retried = notifications.retry_pending(limit=5, notification_id=out["id"])
        assert retried == {"attempted": 1, "accepted": 1}
        # The scheduler calls without a notification id. This path must not bind an untyped SQL NULL.
        assert notifications.retry_pending(limit=0) == {"attempted": 0, "accepted": 0}
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("""SELECT status,attempts,accepted_at IS NOT NULL,next_attempt_at IS NULL
                           FROM notification_deliveries WHERE notification_id=%s AND channel='push'""",
                        (out["id"],))
            # Registration starts a fresh, now-actionable delivery generation.
            assert cur.fetchone() == ("accepted", 1, True, True)
    finally:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM notification_deliveries WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM notifications WHERE tenant_id=%s", (tid,)); c.commit()
            cur.execute("DELETE FROM push_targets WHERE tenant_id=%s", (tid,)); c.commit()


def test_agent_request_notification_resolves_and_askuser_answers_linked_request(monkeypatch):
    """Human questions have one canonical request lifecycle: the notification is correlated and clears on
    answer, and askuser's compatibility row cannot leave an immortal duplicate agent_request behind."""
    import psycopg
    import billing
    import notifications
    import agent_request
    import askuser
    from aoscfg import DB
    monkeypatch.setattr(notifications, "_email", lambda *a, **k: False)
    monkeypatch.setattr(notifications, "_push_now", lambda *a, **k: {"sent": False, "reason": "no topic"})
    monkeypatch.setattr(notifications.notify, "send", lambda *a, **k: False)
    tid = billing.signup(f"linked-ask-{_rid()}")["tenant_id"]
    try:
        made = askuser.ask(tid, 77, "launch", "Which launch region?")
        assert made["agent_request_id"] is not None
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("SELECT agent_request_id FROM ask_user_requests WHERE id=%s", (made["ask_id"],))
            assert cur.fetchone()[0] == made["agent_request_id"]
            cur.execute("""SELECT context_key,read_at,resolved_at FROM notifications
                           WHERE tenant_id=%s AND context_key=%s ORDER BY id DESC LIMIT 1""",
                        (tid, f"agent_request:{made['agent_request_id']}"))
            context_key, read_at, resolved_at = cur.fetchone()
            assert context_key and read_at is None and resolved_at is None
        askuser.answer(made["ask_id"], "us-west", tenant_id=tid)
        assert askuser.is_answered(made["ask_id"], tenant_id=tid)
        assert agent_request.get(made["agent_request_id"], tenant_id=tid)["status"] == "answered"
        assert agent_request.open_requests(tid) == []
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("""SELECT read_at IS NOT NULL,resolved_at IS NOT NULL FROM notifications
                           WHERE tenant_id=%s AND context_key=%s""",
                        (tid, f"agent_request:{made['agent_request_id']}"))
            assert cur.fetchone() == (True, True)
    finally:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM ask_user_requests WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM agent_requests WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM notification_deliveries WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM notifications WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,)); c.commit()


def test_proactive_priority_lane_keeps_old_unresolved_gate_visible():
    """An unresolved CEO gate does not expire after 12h and is serviced by the priority lane even when the
    ordinary active-tenant cursor is many hours away."""
    import psycopg
    import billing
    import proactivecomms as pc
    from aoscfg import DB
    tid = billing.signup(f"priority-gate-{_rid()}")["tenant_id"]
    thread = 970000000 + int(_rid(), 16) % 1000000
    try:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("""INSERT INTO controller_state
                             (thread_id,tenant_id,phase,awaiting,product,updated_at,execution_scope)
                           VALUES (%s,%s,'OPTIONS','user_approval','old-but-open',
                                   now()-interval '3 days','test')""", (thread, tid))
            c.commit()
        assert any(g["ref"] == str(thread) for g in pc._controller_gates(tid))
        assert tid in pc._priority_tenant_batch(limit=5000)
    finally:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM controller_state WHERE thread_id=%s", (thread,))
            cur.execute("DELETE FROM proactive_sent WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,)); c.commit()


def test_accountability_consumes_generic_resolver_once():
    """One generic 'done' cannot close every earlier request in a conversation. If an agent receives two
    handoffs and only acts once, accountability must leave one dropped instead of manufacturing a green handoff
    history."""
    import psycopg
    import accountability
    from aoscfg import DB
    cid = f"acct-multi-{_rid()}"
    try:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("""INSERT INTO conversations
                             (conversation_id, sender, recipient, intent, message_id, ts)
                           VALUES
                             (%s, 'reviewer', 'dev', 'review_request', %s, now() - interval '5 hours'),
                             (%s, 'reviewer', 'dev', 'test_request', %s, now() - interval '5 hours'),
                             (%s, 'dev', 'reviewer', 'done', %s, now() - interval '4 hours')""",
                        (cid, f"{cid}-m1", cid, f"{cid}-m2", cid, f"{cid}-m3"))
        hs = [h for h in accountability.handoffs(24) if h["conversation"] == cid]
        assert len(hs) == 2
        assert sum(1 for h in hs if h["resolved"]) == 1, hs
        assert sum(1 for h in hs if not h["resolved"]) == 1, hs
        assert any(h["conversation"] == cid for h in accountability.dropped(24)), hs
    finally:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM conversations WHERE conversation_id=%s", (cid,)); c.commit()


def test_appregistry_never_publishes_secret_paths():
    """A published product repo must never carry credentials. _is_secret_path flags .env/keys/pem/secrets so
    publish's fail-closed guard unstages them; ordinary source is untouched."""
    import appregistry as ar
    for p in [".env", ".env.local", "server/.env", "keys/id.key", "config/secrets/db", "id_rsa", "cert.pem",
              "my_credentials.json"]:
        assert ar._is_secret_path(p), p
    for p in ["src/app.py", "README.md", "notes.txt", "package.json", "docs/keys-guide.md"]:
        assert not ar._is_secret_path(p), p
    assert ".env" in ar._GITIGNORE and "keys/" in ar._GITIGNORE and "secrets/" in ar._GITIGNORE


def test_auth_verify_code_locks_after_max_attempts():
    """A 6-digit code (1M space) with no cap is brute-forceable in the 15-min window. After MAX_CODE_ATTEMPTS
    wrong guesses the code is BURNED (even a correct guess then fails) and a resend is required — which
    restores a fresh attempt budget. Bounds an attacker to a handful of tries per issued code."""
    import psycopg
    import auth
    email = f"bf-{_rid()}@example.com"
    try:
        assert not auth.signup(email, "password123").get("error")
        code = auth.resend_code(email).get("dev_code")
        assert code
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(auth.MAX_CODE_ATTEMPTS):
            assert "wrong" in (auth.verify_email(email, wrong).get("error") or "")
        locked = auth.verify_email(email, code)                    # right code, but budget spent -> burned
        assert "too many attempts" in (locked.get("error") or "")
        fresh = auth.resend_code(email).get("dev_code")            # resend -> fresh code + fresh budget
        assert auth.verify_email(email, fresh).get("api_token")    # now it works
    finally:
        with psycopg.connect(auth.DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM email_codes WHERE email=%s", (email,))
            cur.execute("DELETE FROM accounts WHERE email=%s", (email,)); c.commit()


def test_codex_spend_is_counted_not_zero():
    """Codex reports tokens, not USD — but it must not record $0 (a platform failover would burn uncounted
    money and never trip the budget cap). _codex_cost converts tokens to USD so the spend is real."""
    import factory
    assert factory._codex_cost(0, 0) == 0.0
    c = factory._codex_cost(1_000_000, 1_000_000)
    assert c == factory.CODEX_PRICE[0] + factory.CODEX_PRICE[1] and c > 0
    assert factory._codex_cost(500_000, 250_000) > 0        # any real usage costs > $0
    assert factory._codex_cost(1_000_000, 0, "gpt-5.6-sol") > \
           factory._codex_cost(1_000_000, 0, "gpt-5.6-luna")


def test_consent_names_the_tenants_actual_provider():
    """Compliance (Apple 5.1.2(i) / EU AI Act Art.50): a tenant whose data goes to OpenAI must consent to
    OpenAI, not Anthropic. consent now auto-resolves the tenant's ACTUAL provider, so the gate + the named
    disclosure name the right one, and an OpenAI consent does NOT satisfy an Anthropic gate."""
    import psycopg
    import consent
    import tenantproviders
    real = tenantproviders.resolve
    tid = f"consent-{_rid()}"
    try:
        tenantproviders.resolve = lambda t: {"engine": "codex", "key": "x"}
        assert consent.for_tenant(tid) == "OpenAI"
        st = consent.state(tid)
        assert st["provider"] == "OpenAI" and "OpenAI" in st["disclosure"] and "Anthropic" not in st["disclosure"]
        assert consent.require_consent(tid) is False              # not yet consented
        consent.record(tid)                                       # records for the RESOLVED provider (OpenAI)
        assert consent.require_consent(tid) is True
        tenantproviders.resolve = lambda t: {"engine": "claude", "key": "y"}
        assert consent.require_consent(tid) is False              # OpenAI consent must NOT satisfy Anthropic
        assert consent.state(tid)["provider"] == "Anthropic Claude"
    finally:
        tenantproviders.resolve = real
        with psycopg.connect(consent.DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM ai_consent WHERE tenant_id=%s", (tid,)); c.commit()


def test_visionkeeper_seeds_refines_and_feeds_controller():
    """The requirements-provider agent: it SEEDS the CEO's standing vision (no restating needed), REFINES it
    into a spec that names UPFRONT human prerequisites, and the controller seam returns a build-ready spec from
    a vague prompt — so the CEO isn't pestered for requirements. factory.agent stubbed (offline)."""
    import json as _j
    import uuid
    import psycopg
    import visionkeeper
    import factory
    if not visionkeeper.DB:
        pytest.skip("no DB")
    tid = f"vk-test-{uuid.uuid4().hex[:8]}"
    real = factory.agent
    factory.agent = lambda role, repo, task, **k: {"rc": 0, "out": _j.dumps({
        "refined_vision": "multi-company AI operator", "goals": ["one-prompt company creation"],
        "non_goals": [], "quality_bar": ["zero bugs reach a human"],
        "prerequisites": [{"item": "connect a model provider", "kind": "credential", "why": "agents need a model",
                           "when": "before_start"}],
        "next_capabilities": ["wire the living org"], "open_questions": []})}
    try:
        seed = visionkeeper.get(tid, "meta")                      # seeded from the standing directive
        assert "CEO" in seed["vision"] and seed["requirements"] is None
        req = visionkeeper.refine(tid, "meta")                    # refine -> names an up-front prerequisite
        assert req["goals"] and any(p["when"] == "before_start" for p in req["prerequisites"])
        seam = visionkeeper.requirements_for_controller(tid, org_id=5, hint="build a TikTok competitor")
        assert seam["standing_vision"] and seam["requirements"] and seam["scope"] == "org:5"
        # the controller-context helper folds it in without spending (allow_refine=False), fail-open
        import loopcontroller as lc
        ctx = lc._ceo_context(tid, 5)
        assert "CEO STANDING VISION" in ctx and "prerequisites" in ctx.lower()
    finally:
        factory.agent = real
        with psycopg.connect(visionkeeper.DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM ceo_vision WHERE tenant_id=%s", (tid,)); c.commit()


def test_reaper_catches_ffmpeg_and_all_chromium_variants():
    """A leaked QA session = a Chromium ROOT + an ffmpeg video recorder. The old reaper matched only
    chrome-headless-shell (missed ffmpeg entirely + full 'chromium'/'chrome' channels), so those leaked
    forever -> slow OOM over a long run. _reap_browser_decision stays conservative (orphan or very-old only),
    and the browser-proc matcher now recognizes the leaking names."""
    import reap
    # decision logic unchanged + conservative: a young, parented proc is NEVER reaped; an orphan/old one is.
    assert reap._reap_browser_decision(ppid=1234, etimes=30) is False, "young + parented -> keep (live QA safe)"
    assert reap._reap_browser_decision(ppid=1, etimes=30) is True, "orphaned (PPID 1) -> reap"
    assert reap._reap_browser_decision(ppid=1234, etimes=reap.BROWSER_STALE_S + 1) is True, "very old -> reap"
    # the matcher source now covers ffmpeg recorders + all chromium variants (not just headless-shell)
    src = __import__("inspect").getsource(reap._browser_procs)
    assert "ffmpeg" in src and "chromium" in src and "chrome-headless-shell" in src, \
        "must match ffmpeg recorders AND all chromium variants, not just headless-shell"


def test_qa_video_encoding_is_single_threaded():
    """Evidence conversion must not saturate WSL with libx264 threads while browser QA and agents run."""
    import inspect
    from qa import artifacts
    src = inspect.getsource(artifacts)
    assert src.count('"-threads", "1"') >= 2, \
        "both per-story transcode and concat re-encode fallback must preserve interactive CPU headroom"


def test_qa_video_encoders_die_with_cancelled_worker():
    """A slice deadline must not leave an ffmpeg transcode reparented to WSL init."""
    import inspect
    from qa import artifacts
    src = inspect.getsource(artifacts)
    assert "PR_SET_PDEATHSIG" in src
    if __import__("sys").platform.startswith("linux"):
        assert artifacts._ENCODER_CHILD_KWARGS.get("preexec_fn") is artifacts._parent_death_signal
        # All four admitted media launches (decode verification, transcode,
        # and two stitch attempts) share the exact owned runner, which
        # applies the parent-death hook and durable resource fence once.
        assert src.count("_run_media_owned(") == 5  # definition + verification + transcode + two stitch attempts


def test_live_qa_retry_defaults_recover_without_runaway_pressure():
    """Several serialized recovery attempts are safe; one-and-stop strands ordinary incomplete work."""
    src = (ROOT / "scripts" / "orchestra" / "runtime.py").read_text()
    assert 'AOS_QA_MAX_GAPFILL", "3"' in src
    assert 'AOS_QA_MAX_RETEST", "3"' in src

    sys.path.insert(0, str(ROOT / "scripts" / "orchestra"))
    import runtime
    candidates = runtime._qa_gapfill_candidates(
        {"US-1": "clean", "US-2": "incomplete"}, ["US-1", "US-2", "US-3"],
        {"US-2": 1, "US-3": 3}, 3)
    assert candidates == ["US-2"], "recover incomplete work but respect the per-slice safety envelope"


def test_qa_aggregate_restores_a_missing_story_instead_of_finishing(monkeypatch):
    """A missing explorer result at the join is durable unfinished work, not a final red verdict."""
    from types import SimpleNamespace
    sys.path.insert(0, str(ROOT / "scripts" / "orchestra"))
    import runtime

    child = {"actor_id": 52, "supervisor_id": 51, "role": "qa-explorer",
             "status": "done", "memory": {}, "result": {}}
    actor = {"actor_id": 51, "supervisor_id": 50, "name": "qa", "role": "qa-coordinator",
             "status": "working", "assignment": "verify", "memory": {
                 "phase": "delegating", "story_status": {"US-1": "clean"},
                 "context": {"stories": [{"id": "US-1"}, {"id": "US-2"}],
                             "target_url": "http://127.0.0.1:1", "product": "fixture"}}}
    hires, persisted = [], []
    monkeypatch.setattr(runtime.store, "actors", lambda *_: [child])
    monkeypatch.setattr(runtime, "_hire_or_request",
                        lambda ctx, a, specs, step, corr=None: hires.extend(specs) or "hired")
    monkeypatch.setattr(runtime, "_persist",
                        lambda ctx, a, step, evs: persisted.append(step))
    monkeypatch.setattr(runtime, "_audit", lambda *_a, **_k: None)

    runtime._supervisor_step(SimpleNamespace(run_id=9, tenant="t", repo="/tmp"), actor, [])
    assert len(hires) == 1 and hires[0]["tool_args"]["story"]["id"] == "US-2"
    assert persisted[0].status != "done" and persisted[0].result is None
    assert persisted[0].memory["aggregated"] is False
    assert persisted[0].memory["gapfills"] == {"US-2": 1}


def test_resumed_fixer_verifies_applied_work_before_mutating_again(monkeypatch, tmp_path):
    """A slice ending during post-fix QA must not launch the same repository mutation a second time."""
    from qa import dev_loop
    monkeypatch.setattr(dev_loop, "_triage_finding", lambda *_a, **_k: {
        "disposition": "confirmed_defect", "may_mutate": True, "reviews": [], "reason": "confirmed"})
    monkeypatch.setattr(dev_loop, "_triage_citations_current", lambda *_a, **_k: True)
    monkeypatch.setattr(dev_loop, "dev_self_qa", lambda *_a, **_k: [])
    judged = {}
    def judge(*args, **kwargs):
        judged.update({"changed_files": args[2], "verification": kwargs.get("verification"),
                       "prior_mutation_receipt": kwargs.get("prior_mutation_receipt")})
        return {"fixed": True, "confidence": 0.99, "reason": "fresh pass"}
    monkeypatch.setattr(dev_loop, "_judge_fixed", judge)
    monkeypatch.setattr(dev_loop, "_plan_fix",
                        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not mutate")))
    result = dev_loop.fix_bug(
        {"story": "US-1", "title": "already corrected"}, {}, "vision",
        target_url="http://127.0.0.1:1", stories=[{"id": "US-1"}], repo=str(tmp_path),
        resume_changed_files=["src/policy.py"],
        resume_existing=True)
    assert result["fixed"] is True and result["recovery_verified"] is True
    assert result["attempts"] == 0 and result["files"] == ["src/policy.py"]
    assert judged == {"changed_files": ["src/policy.py"], "verification": None,
                      "prior_mutation_receipt": True}

    # The resume signal is independent of result-event generation: a process can die after mutation but
    # before it ever emits a tool_result, leaving tool_attempt at zero.
    jobrunner_src = (ROOT / "scripts" / "orchestra" / "jobrunner.py").read_text()
    tools_src = (ROOT / "scripts" / "orchestra" / "tools.py").read_text()
    assert '"resumed": True' in jobrunner_src and 'args.get("_tool_resumed")' in tools_src


def test_resumed_fixer_recovers_lost_mutation_receipt_from_sealed_finding_delta(
        monkeypatch, tmp_path):
    from qa import dev_loop
    source = tmp_path / "src" / "policy.js"
    source.parent.mkdir()
    source.write_text("export const allowed = true;\n")
    evidence = tmp_path.parent / f"{tmp_path.name}-evidence"
    evidence.mkdir()
    finding = {
        "story": "US-010", "title": "token leak",
        "evidence_provenance": dev_loop.capture_finding_provenance(tmp_path, evidence),
    }
    source.write_text("export const allowed = false;\n")
    monkeypatch.setattr(dev_loop, "_triage_finding", lambda *_a, **_k: {
        "disposition": "confirmed_defect", "may_mutate": True, "reviews": [], "reason": "confirmed"})
    monkeypatch.setattr(dev_loop, "_triage_citations_current", lambda *_a, **_k: True)
    monkeypatch.setattr(dev_loop, "dev_self_qa", lambda *_a, **_k: [])
    judged = {}

    def judge(*args, **kwargs):
        judged.update({"changed_files": args[2],
                       "prior_mutation_receipt": kwargs.get("prior_mutation_receipt"),
                       "mutation_receipt_source": kwargs.get("mutation_receipt_source")})
        return {"fixed": True, "confidence": 0.99, "reason": "fresh pass plus sealed delta"}

    monkeypatch.setattr(dev_loop, "_judge_fixed", judge)
    monkeypatch.setattr(dev_loop, "_plan_fix", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("a sealed mutation delta and fresh pass must not launch another writer")))

    result = dev_loop.fix_bug(
        finding, {}, "vision", target_url="http://app", stories=[{"id": "US-010"}],
        repo=str(tmp_path), resume_existing=True)

    assert result["fixed"] is True and result["attempts"] == 0
    assert result["files"] == ["src/policy.js"]
    assert judged == {"changed_files": ["src/policy.js"], "prior_mutation_receipt": True,
                      "mutation_receipt_source": "sealed_finding_delta"}


def test_complete_sealed_recovery_proof_cannot_loop_back_into_another_writer(
        monkeypatch, tmp_path):
    from qa import dev_loop
    source = tmp_path / "src" / "policy.js"
    source.parent.mkdir()
    source.write_text("export const allowed = true;\n")
    evidence = tmp_path.parent / f"{tmp_path.name}-sealed-evidence"
    evidence.mkdir()
    finding = {
        "story": "US-010", "title": "token leak",
        "evidence_provenance": dev_loop.capture_finding_provenance(tmp_path, evidence),
    }
    source.write_text("export const allowed = false;\n")
    monkeypatch.setattr(dev_loop, "_triage_finding", lambda *_a, **_k: {
        "disposition": "confirmed_defect", "may_mutate": True, "reviews": [], "reason": "confirmed"})
    monkeypatch.setattr(dev_loop, "_triage_citations_current", lambda *_a, **_k: True)
    monkeypatch.setattr(dev_loop, "dev_self_qa", lambda *_a, **_k: {
        "bugs": [], "complete": True, "stop_reason": "coverage-complete",
        "coverage": [{"aspect": "exact regression", "covered": True, "explicit": True}],
    })
    monkeypatch.setattr(dev_loop, "_judge_fixed", lambda *_a, **_k: {
        "fixed": False, "confidence": 0.6, "reason": "text diff unavailable after handoff",
    })
    monkeypatch.setattr(dev_loop, "_plan_fix", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("complete sealed recovery evidence must not launch another writer")))

    result = dev_loop.fix_bug(
        finding, {}, "vision", target_url="http://app", stories=[{"id": "US-010"}],
        repo=str(tmp_path), resume_existing=True)

    assert result["fixed"] is True and result["attempts"] == 0
    assert result["verdict"]["acceptance_basis"] == "sealed_complete_recovery"
    assert result["verdict"]["reviewer_advisory"]["fixed"] is False


def test_resumed_fixer_never_treats_empty_incomplete_browser_result_as_clean(monkeypatch, tmp_path):
    from qa import dev_loop
    monkeypatch.setattr(dev_loop, "_triage_finding", lambda *_a, **_k: {
        "disposition": "confirmed_defect", "may_mutate": True, "reviews": [], "reason": "confirmed"})
    monkeypatch.setattr(dev_loop, "_triage_citations_current", lambda *_a, **_k: True)
    monkeypatch.setattr(dev_loop, "dev_self_qa", lambda *_a, **_k: {
        "bugs": [], "complete": False,
        "stories": [{"story": "US-1", "stop_reason": "cancelled-incomplete",
                     "covered": 3, "coverage_total": 4, "complete": False}]})
    monkeypatch.setattr(dev_loop, "_judge_fixed", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("an incomplete browser run cannot reach the clean fix judge")))
    monkeypatch.setattr(dev_loop, "_plan_fix", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("checkpointed verification cannot launch another mutation")))

    result = dev_loop.fix_bug(
        {"story": "US-1", "title": "already corrected"}, {}, "vision",
        target_url="http://app", stories=[{"id": "US-1"}], repo=str(tmp_path),
        resume_changed_files=["src/policy.py"], resume_existing=True)

    assert result["fixed"] is False and result["checkpoint_required"] is True
    assert result["files"] == ["src/policy.py"]
    assert result["attempts_log"][0]["failed_stage"] == "resume-verify-incomplete"


def test_resumed_fixer_dismisses_false_positive_only_with_verified_contract_citations(
        monkeypatch, tmp_path):
    """The US011 shape: UI retry exhaustion is contract behavior, so recovery must not rewrite it."""
    from qa import dev_loop
    source = tmp_path / "src" / "queue.js"
    contract = tmp_path / "tests" / "browser_app_e2e.test.js"
    source.parent.mkdir(); contract.parent.mkdir()
    source.write_text("const drainClick = () => drainAgentQueue(10);\n")
    contract.write_text("assert.equal(job.status, 'dead_letter'); // one UI drain exhausts retries\n")
    evidence = tmp_path.parent / f"{tmp_path.name}-evidence"; evidence.mkdir()
    finding = {"bug": "one Drain queue click dead-lettered timeout attempts",
               "severity": "high", "blocking": True,
               "evidence_provenance": dev_loop.capture_finding_provenance(tmp_path, evidence)}
    answers = iter([
        {"verdict": "false_positive", "confidence": 0.99, "reason": "handler contract",
         "citations": [{"path": "src/queue.js", "start_line": 1, "end_line": 1,
                         "quote": "const drainClick = () => drainAgentQueue(10);"}]},
        {"verdict": "false_positive", "confidence": 0.98, "reason": "browser contract",
         "citations": [{"path": "tests/browser_app_e2e.test.js", "start_line": 1, "end_line": 1,
                         "quote": "assert.equal(job.status, 'dead_letter'); // one UI drain exhausts retries"}]},
    ])
    reviewer_prompts = []
    def review(*args, **_kwargs):
        reviewer_prompts.append(args[2])
        return next(answers)
    monkeypatch.setattr(dev_loop, "_ai_json", review)
    monkeypatch.setattr(dev_loop, "dev_self_qa", lambda *_a, **_k: [finding])
    monkeypatch.setattr(dev_loop, "_plan_fix", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("verified contract behavior must not reach a mutation plan")))
    before = (source.read_text(), contract.read_text())

    out = dev_loop.fix_bug(
        {"bug": "old US011 defect", "evidence_provenance": finding["evidence_provenance"]}, {},
        "queue retries are durable",
        target_url="http://app", stories=[{"id": "US011"}], repo=str(tmp_path),
        resume_existing=True)

    assert out["fixed"] is True and out["resolved_without_mutation"] is True
    assert out["triage"]["disposition"] == "false_positive"
    assert out["attempts"] == 0 and out["files"] == []
    assert (source.read_text(), contract.read_text()) == before
    assert all(r["citations"][0]["sha256"] for r in out["triage"]["reviews"])
    assert len(reviewer_prompts) == 2
    assert all("SEALED EVIDENCE CAPSULE" in p and "Do not run shell/filesystem commands" in p
               and "Do not edit files" in p for p in reviewer_prompts)


def test_finding_triage_runs_independent_read_only_reviewers_concurrently(monkeypatch, tmp_path):
    """Independent sealed-capsule reviews share wall time without weakening the two-verdict gate."""
    import threading
    from qa import dev_loop

    source = tmp_path / "app.py"
    source.write_text("def save(): return False\n")
    evidence = tmp_path.parent / f"{tmp_path.name}-evidence"
    evidence.mkdir()
    finding = {
        "story": "US-1", "title": "save fails", "severity": "high", "blocking": True,
        "evidence_provenance": dev_loop.capture_finding_provenance(tmp_path, evidence),
    }
    both_started = threading.Barrier(2)

    def review(*_args, **_kwargs):
        both_started.wait(timeout=2)
        return {
            "verdict": "defect", "confidence": 0.99, "reason": "source contradicts contract",
            "citations": [{"path": "app.py", "start_line": 1, "end_line": 1,
                           "quote": "def save(): return False"}],
        }

    monkeypatch.setattr(dev_loop, "_ai_json", review)
    out = dev_loop._triage_finding(finding, {}, "saving succeeds", repo=str(tmp_path))

    assert out["disposition"] == "confirmed_defect" and out["may_mutate"] is True
    assert [item["reviewer"] for item in out["reviews"]] == [1, 2]


def test_finding_dispute_or_unverified_citation_fails_safe_to_internal_review(
        monkeypatch, tmp_path):
    from qa import dev_loop
    app = tmp_path / "app.py"; app.write_text("def save(): return True\n")
    evidence = tmp_path.parent / f"{tmp_path.name}-evidence"; evidence.mkdir()
    finding = {"bug": "save is broken", "severity": "high", "blocking": True,
               "evidence_provenance": dev_loop.capture_finding_provenance(tmp_path, evidence)}
    answers = iter([
        {"verdict": "defect", "confidence": 0.9, "reason": "looks broken",
         "citations": [{"path": "app.py", "start_line": 1, "end_line": 1,
                         "quote": "def save(): return True"}]},
        {"verdict": "false_positive", "confidence": 0.9, "reason": "invented contract",
         "citations": [{"path": "app.py", "start_line": 1, "end_line": 1,
                         "quote": "this quote does not exist"}]},
    ])
    monkeypatch.setattr(dev_loop, "_ai_json", lambda *_a, **_k: next(answers))
    monkeypatch.setattr(dev_loop, "_plan_fix", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("disputed finding must not reach a mutation plan")))

    out = dev_loop.fix_bug(finding, {}, "saving works", target_url="http://app",
                           stories=[{"id": "US-1"}], repo=str(tmp_path))

    assert out["fixed"] is False and out["attempts"] == 0 and out["files"] == []
    assert out["internal_review_required"] is True
    assert out["review_route"] == "qa-internal-review"
    assert [r["verdict"] for r in out["triage"]["reviews"]] == ["defect", "uncertain"]


def test_post_finding_test_rewrite_is_non_authoritative_and_cannot_bootstrap_defect(
        monkeypatch, tmp_path):
    """Post-finding text grants no authority; incomplete current-revision QA checkpoints read-only."""
    from qa import dev_loop
    source = tmp_path / "src" / "app.js"; source.parent.mkdir()
    test = tmp_path / "tests" / "browser_app_e2e.test.js"; test.parent.mkdir()
    source.write_text("drainButton.onclick = () => app.drainAgentQueue(10);\n")
    test.write_text("it('one click exhausts retries into dead-letter', contract);\n")
    evidence = tmp_path.parent / f"{tmp_path.name}-evidence"; evidence.mkdir()
    provenance = dev_loop.capture_finding_provenance(tmp_path, evidence)
    # This is the exact dangerous ordering from the live worker: finding first, test mutation second.
    test.write_text("it('one click performs exactly one timeout attempt', changedContract);\n")
    finding = {"kind": "bug", "story": "US011", "bug": "UI drain must attempt only once",
               "severity": "high", "evidence_provenance": provenance}
    monkeypatch.setattr(dev_loop, "_ai_json", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("stale capsule reviewers must not run before current-revision verification")))
    monkeypatch.setattr(dev_loop, "dev_self_qa", lambda *_a, **_k: {
        "bugs": [], "complete": False, "stop_reason": "checkpointed",
        "coverage": [{"aspect": "exact regression", "covered": False}],
    })
    monkeypatch.setattr(dev_loop, "_plan_fix", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("post-finding test text must never authorize mutation")))

    out = dev_loop.fix_bug(finding, {}, "one UI click exhausts scheduled retries",
                           target_url="http://app", stories=[{"id": "US011"}], repo=str(tmp_path))

    assert out["checkpoint_required"] is True and out["attempts"] == 0
    assert out["triage"]["disposition"] == "revision_reverify_required"
    assert out["triage"]["reviews"] == []
    assert "tests/browser_app_e2e.test.js" in out["triage"]["changed_since_finding"]
    assert test.read_text() == "it('one click performs exactly one timeout attempt', changedContract);\n"


def test_exact_prior_worker_mutation_receipt_allows_recovery_triage_but_not_new_authority(
        monkeypatch, tmp_path):
    """A durable writer receipt explains revision drift so its successor can run fresh recovery QA."""
    from qa import dev_loop
    source = tmp_path / "src" / "app.js"; source.parent.mkdir()
    contract = tmp_path / "tests" / "browser_app_e2e.test.js"; contract.parent.mkdir()
    source.write_text("export const privacyContract = 'aggregate-only';\n")
    contract.write_text("it('does not expose raw metadata', originalContract);\n")
    evidence = tmp_path.parent / f"{tmp_path.name}-authorized-evidence"; evidence.mkdir()
    finding = {
        "story": "US-011", "bug": "raw metadata is exposed", "severity": "high",
        "evidence_provenance": dev_loop.capture_finding_provenance(tmp_path, evidence),
    }
    contract.write_text("it('projects aggregate counters only', repairedContract);\n")
    answer = {
        "verdict": "defect", "confidence": 0.99, "reason": "sealed contract requires aggregates",
        "citations": [{"path": "src/app.js", "start_line": 1, "end_line": 1,
                       "quote": "export const privacyContract = 'aggregate-only';"}],
    }
    monkeypatch.setattr(dev_loop, "_ai_json", lambda *_a, **_k: dict(answer))

    out = dev_loop._triage_finding(
        finding, {}, "raw metadata remains private", repo=str(tmp_path),
        authorized_changed_files=["tests/browser_app_e2e.test.js"])

    assert out["disposition"] == "confirmed_defect" and out["may_mutate"] is True
    assert out["changed_since_finding"] == ["tests/browser_app_e2e.test.js"]
    assert out["unattributed_changed_since_finding"] == []
    assert out["authorized_changed_files"] == ["tests/browser_app_e2e.test.js"]


def test_resume_empty_residual_cannot_bypass_post_finding_revision_gate(monkeypatch, tmp_path):
    """Only complete fresh browser evidence may retire a stale finding after the revision gate."""
    from qa import dev_loop
    source = tmp_path / "src" / "app.js"; source.parent.mkdir()
    contract = tmp_path / "tests" / "browser_app_e2e.test.js"; contract.parent.mkdir()
    source.write_text("drainButton.onclick = () => app.drainAgentQueue(10);\n")
    contract.write_text("it('one click exhausts retries', originalContract);\n")
    evidence = tmp_path.parent / f"{tmp_path.name}-evidence"; evidence.mkdir()
    finding = {"bug": "one click should attempt only once", "severity": "high",
               "evidence_provenance": dev_loop.capture_finding_provenance(tmp_path, evidence)}
    contract.write_text("it('one click performs one attempt', fixerWrittenContract);\n")
    answer = {"verdict": "defect", "confidence": 0.99, "reason": "handler differs from claim",
              "citations": [{"path": "src/app.js", "start_line": 1, "end_line": 1,
                             "quote": "drainButton.onclick = () => app.drainAgentQueue(10);"}]}
    monkeypatch.setattr(dev_loop, "_ai_json", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("old finding-time reviewers must not decide the changed revision")))
    monkeypatch.setattr(dev_loop, "dev_self_qa", lambda *_a, **_k: {
        "bugs": [], "complete": True, "stop_reason": "coverage-complete",
        "coverage": [{"aspect": "exact regression", "covered": True, "explicit": True}],
    })
    monkeypatch.setattr(dev_loop, "_judge_fixed", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("empty-diff recovery judge must not bypass revision triage")))
    monkeypatch.setattr(dev_loop, "_plan_fix", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("post-finding mutation must not reach planning")))

    out = dev_loop.fix_bug(finding, {}, "one UI click exhausts scheduled retries",
                           target_url="http://app", stories=[{"id": "US011"}], repo=str(tmp_path),
                           resume_existing=True)

    assert out["fixed"] is True and out["resolved_without_mutation"] is True
    assert out["attempts"] == 0 and out["files"] == []
    assert "tests/browser_app_e2e.test.js" in out["triage"]["changed_since_finding"]
    assert out["verdict"]["acceptance_basis"] == "current_revision_complete_reverification"


def test_qa_tool_seals_finding_time_repository_manifest(monkeypatch, tmp_path):
    import types
    sys.path.insert(0, str(ROOT / "scripts" / "orchestra"))
    import tools
    from qa import dev_loop
    repo = tmp_path / "product"; repo.mkdir(); (repo / "app.js").write_text("export const ok = true;\n")
    evidence = tmp_path / "evidence"; evidence.mkdir()

    class Explorer:
        def __init__(self, *_a, **_k):
            self.coverage, self.stop_reason = [], "blocking-wall"
            self.resume_state_path = self.video_mp4 = self.pulse_work_id = None
        def explore(self, _story, **kwargs):
            kwargs["on_bug"]({"bug": "disputed", "severity": "high", "blocking": True,
                              "expected": "contract", "action": {"cmd": "click"}})
            return []
        def close(self): pass

    fake = types.ModuleType("qa_explorer"); fake.Explorer = Explorer
    monkeypatch.setitem(sys.modules, "qa_explorer", fake)
    out = tools.qa_explore({"target_url": "http://app", "vision": "v", "product": "p",
                            "repo": str(repo), "artifact_dir": str(evidence),
                            "story": {"id": "US011", "title": "Drain"}})
    provenance = out["findings"][0]["evidence_provenance"]
    ref, manifest = dev_loop._load_finding_provenance(repo, provenance)
    assert ref == provenance and manifest["files"]["app.js"]
    assert out["findings"][0]["expected"] == "contract"


def test_finding_provenance_ignores_only_runtime_qa_gate_artifacts(tmp_path):
    from qa import dev_loop

    (tmp_path / "src").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "src" / "app.js").write_text("export const ready = true;\n")
    (tmp_path / "docs" / "PLAN.json").write_text('{"contract":"stable"}\n')
    (tmp_path / "docs" / "QA-CHECKPOINT.json").write_text('{"slice":1}\n')
    (tmp_path / "docs" / "QA-VERDICT.json").write_text('{"passed":false}\n')

    before = dev_loop._repo_file_hashes(tmp_path)
    (tmp_path / "docs" / "QA-CHECKPOINT.json").write_text('{"slice":2}\n')
    (tmp_path / "docs" / "QA-VERDICT.json").write_text('{"passed":true}\n')
    after_runtime = dev_loop._repo_file_hashes(tmp_path)
    (tmp_path / "docs" / "PLAN.json").write_text('{"contract":"changed"}\n')
    after_contract = dev_loop._repo_file_hashes(tmp_path)

    assert before == after_runtime
    assert "docs/QA-CHECKPOINT.json" not in before and "docs/QA-VERDICT.json" not in before
    assert before["docs/PLAN.json"] != after_contract["docs/PLAN.json"]


def test_confirmed_real_bug_still_reaches_fix_agent(monkeypatch, tmp_path):
    from qa import dev_loop
    app = tmp_path / "app.py"
    app.write_text("def save():\n    raise RuntimeError('broken')\n")
    evidence = tmp_path.parent / f"{tmp_path.name}-evidence"; evidence.mkdir()
    finding = {"bug": "save returns 500", "severity": "critical",
               "evidence_provenance": dev_loop.capture_finding_provenance(tmp_path, evidence)}
    spawned = []
    reviewer_count = 0
    def decisions(_role, _repo, prompt, **_kwargs):
        nonlocal reviewer_count
        if "INDEPENDENT, READ-ONLY" in prompt:
            reviewer_count += 1
            return {"verdict": "defect", "confidence": 0.99, "reason": "implementation raises",
                    "citations": [{"path": "app.py", "start_line": 1, "end_line": 2,
                                   "quote": "def save():\n    raise RuntimeError('broken')"}]}
        raise AssertionError("unexpected model decision")
    monkeypatch.setattr(dev_loop, "_ai_json", decisions)
    monkeypatch.setattr(dev_loop, "_plan_fix", lambda *_a, **_k: {
        "agents": [{"role": "builder", "files": ["app.py"], "task": "repair save"}]})
    monkeypatch.setattr(dev_loop, "_spawn_fix_agents", lambda *_a, **_k: (
        spawned.append(True) or {"ok": True, "failures": [], "results": [{"failed": False}]}))
    monkeypatch.setattr(dev_loop, "_directory_snapshot", lambda *_a: {})
    monkeypatch.setattr(dev_loop, "_directory_changed_since", lambda *_a: ([], {}))
    monkeypatch.setattr(dev_loop, "_directory_diff", lambda *_a: "")
    monkeypatch.setattr(dev_loop, "dev_self_qa", lambda *_a, **_k: [])
    monkeypatch.setattr(dev_loop, "_judge_fixed", lambda *_a, **_k: {
        "fixed": True, "confidence": 1.0, "reason": "fresh story passes"})

    out = dev_loop.fix_bug(
        finding, {}, "saving works",
        target_url="http://app", stories=[{"id": "US-1"}], repo=str(tmp_path), max_attempts=1)

    assert reviewer_count == 2 and spawned == [True]
    assert out["fixed"] is True and out["attempts"] == 1
    assert out["internal_review_required"] is False


def test_zero_agent_repair_plan_is_not_coerced_into_product_mutation(monkeypatch, tmp_path):
    from qa import dev_loop
    monkeypatch.setattr(dev_loop, "_ai_json", lambda *_a, **_k: {
        "agents": [], "rationale": "the alleged defect contradicts the contract"})
    plan = dev_loop._plan_fix({"bug": "disputed"}, {}, "vision", repo=str(tmp_path))
    assert plan["agents"] == []


def test_low_runway_checkpoints_before_any_reviewer_or_writer(monkeypatch, tmp_path):
    import time
    from qa import dev_loop
    app = tmp_path / "app.py"; app.write_text("def broken(): return True\n")
    evidence = tmp_path.parent / f"{tmp_path.name}-evidence"; evidence.mkdir()
    bug = {"bug": "broken", "evidence_provenance":
           dev_loop.capture_finding_provenance(tmp_path, evidence)}
    monkeypatch.setattr(dev_loop, "_ai_json", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("reviewer/plan model call launched without runway")))
    monkeypatch.setattr(dev_loop, "_spawn_fix_agents", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("writer launched without runway")))
    before = app.read_bytes()

    out = dev_loop.fix_bug(bug, {}, "vision", target_url="http://app", stories=[{"id": "US-1"}],
                           repo=str(tmp_path), deadline=time.time() + dev_loop.READ_ONLY_STAGE_TIMEOUT + 29)

    assert out["checkpoint_required"] is True and out["attempts"] == 0 and out["files"] == []
    assert out["internal_review_required"] is False and app.read_bytes() == before


def test_runway_lost_during_read_only_plan_never_launches_writer(monkeypatch, tmp_path):
    from qa import dev_loop
    monkeypatch.setattr(dev_loop, "_triage_finding", lambda *_a, **_k: {
        "disposition": "confirmed_defect", "may_mutate": True, "reviews": [], "reason": "confirmed"})
    monkeypatch.setattr(dev_loop, "_triage_citations_current", lambda *_a, **_k: True)
    monkeypatch.setattr(dev_loop, "_plan_fix", lambda *_a, **_k: {
        "agents": [{"role": "builder", "task": "fix", "files": ["app.py"]}]})
    monkeypatch.setattr(dev_loop, "_spawn_fix_agents", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("writer launched after plan consumed its safety runway")))
    runway = iter([True, False])
    monkeypatch.setattr(dev_loop, "_external_stage_has_runway", lambda *_a, **_k: next(runway))

    out = dev_loop.fix_bug({"bug": "real"}, {}, "vision", target_url="http://app",
                           stories=[{"id": "US-1"}], repo=str(tmp_path), deadline=10**12)

    assert out["checkpoint_required"] is True and out["attempts"] == 0 and out["files"] == []


def test_runway_lost_after_writer_preserves_mutation_evidence_without_launching_judge(
        monkeypatch, tmp_path):
    from qa import dev_loop
    app = tmp_path / "app.py"; app.write_text("broken\n")
    monkeypatch.setattr(dev_loop, "_triage_finding", lambda *_a, **_k: {
        "disposition": "confirmed_defect", "may_mutate": True, "reviews": [], "reason": "confirmed"})
    monkeypatch.setattr(dev_loop, "_triage_citations_current", lambda *_a, **_k: True)
    monkeypatch.setattr(dev_loop, "_plan_fix", lambda *_a, **_k: {
        "agents": [{"role": "builder", "task": "fix", "files": ["app.py"]}]})
    def spawn(*_a, **_k):
        app.write_text("fixed\n")
        return {"ok": True, "failures": [], "results": [{"failed": False}]}
    monkeypatch.setattr(dev_loop, "_spawn_fix_agents", spawn)
    monkeypatch.setattr(dev_loop, "dev_self_qa", lambda *_a, **_k: [])
    monkeypatch.setattr(dev_loop, "_judge_fixed", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("post-fix reviewer launched without runway")))
    runway = iter([True, True, False])
    monkeypatch.setattr(dev_loop, "_external_stage_has_runway", lambda *_a, **_k: next(runway))

    out = dev_loop.fix_bug({"bug": "real"}, {}, "vision", target_url="http://app",
                           stories=[{"id": "US-1"}], repo=str(tmp_path), deadline=10**12,
                           max_attempts=1)

    assert out["checkpoint_required"] is True and out["attempts"] == 1
    assert out["files"] == ["app.py"] and app.read_text() == "fixed\n"
    assert "-broken" in out["change_diff"] and "+fixed" in out["change_diff"]
    assert out["attempts_log"][-1]["failed_stage"] == "verification-checkpoint"


def test_dev_checkpoint_is_durable_and_replay_safe(monkeypatch):
    """A no-runway dev result parks for reconciliation; replay cannot become a false done/fix event."""
    from types import SimpleNamespace
    sys.path.insert(0, str(ROOT / "scripts" / "orchestra"))
    import runtime
    actor = {"actor_id": 7, "supervisor_id": 6, "name": "fixer", "role": "dev-fixer",
             "status": "blocked", "assignment": "fix", "memory": {"context": {
                 "tool": "dev_fix", "tool_dispatched": True, "tool_attempt": 0,
                 "tool_args": {"bug": {"story": "US011"}}}, "progress": [], "steps": 0}}
    event = {"id": 1, "kind": "tool_result", "frm": 0, "corr_id": None, "payload": {
        "tool": "dev_fix", "status": "checkpoint", "findings": [],
        "result": {"fixed": False, "checkpoint_required": True,
                   "files": ["src/policy.js", "tests/policy.test.js"],
                   "change_diff": "@@ policy.js\n-deny = false\n+deny = true\n",
                   "triage": {"disposition": "confirmed_defect", "may_mutate": True,
                              "finding_fingerprint": "exact-receipt"},
                   "resume_triage_finding": {
                       "finding_id": "fresh-current", "story": "US011"},
                   "verdict": {"reason": "insufficient runway"}}}}
    persisted = []
    monkeypatch.setattr(runtime, "_persist", lambda _c, _a, step, _e: persisted.append(step))
    ctx = SimpleNamespace(run_id=1, tenant="t", repo="/tmp")

    runtime._worker_step(ctx, actor, [event])
    runtime._worker_step(ctx, actor, [event])  # crash-before-commit replay starts from the same durable row

    assert len(persisted) == 2
    assert all(step.status == "blocked" and step.memory["context"]["tool_attempt"] == 1
               for step in persisted)
    assert all(step.memory["context"]["tool_args"]["resume_changed_files"] ==
               ["src/policy.js", "tests/policy.test.js"] for step in persisted)
    assert all("+deny = true" in step.memory["context"]["tool_args"]["resume_change_diff"]
               for step in persisted)
    assert all(step.memory["context"]["tool_args"]["resume_triage_finding"] ==
               {"finding_id": "fresh-current", "story": "US011"} for step in persisted)
    assert all(step.memory["context"]["tool_args"]["resume_triage_receipt"][
                   "finding_fingerprint"] == "exact-receipt" for step in persisted)
    assert all(not any(emit[2] == "done" for emit in step.emits) for step in persisted)


def test_internal_review_routes_durably_through_dev_and_qa_management_without_retest_or_ceo(
        monkeypatch):
    from types import SimpleNamespace
    sys.path.insert(0, str(ROOT / "scripts" / "orchestra"))
    import runtime
    ctx = SimpleNamespace(run_id=9, tenant="t", repo="/tmp", human_hook=None)
    persisted = []
    monkeypatch.setattr(runtime, "_persist", lambda _c, _a, step, _e: persisted.append(step))
    monkeypatch.setattr(runtime, "_audit", lambda *_a, **_k: None)
    monkeypatch.setattr(runtime, "_ai_json", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("internal evidence dispute must not enter generic/CEO decision machinery")))
    monkeypatch.setattr(runtime, "_hire_or_request", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("dev-level routing must not schedule another fixer or blind retest")))

    finding = {"story": "US011", "title": "disputed drain behavior"}
    fixer_actor = {"actor_id": 22, "supervisor_id": 21, "name": "fixer", "role": "dev-fixer",
                   "status": "blocked", "assignment": "fix", "memory": {"context": {
                       "tool": "dev_fix", "tool_dispatched": True, "tool_attempt": 0,
                       "tool_args": {"bug": finding}}, "progress": [], "steps": 0}}
    runtime._worker_step(ctx, fixer_actor, [{"id": 0, "kind": "tool_result", "frm": 0,
        "corr_id": None, "payload": {"tool": "dev_fix", "status": "internal_review", "findings": [],
        "result": {"fixed": False, "internal_review_required": True,
                   "residual": [{"story": "US011", "finding_id": "qaf-residual",
                                 "title": "different post-fix residual"}],
                   "triage": {"disposition": "internal_review_required"},
                   "verdict": {"reason": "reviewers disagreed"}}}}])
    fixer_step = persisted.pop()
    assert fixer_step.status == "done"
    internal_emit = next(e for e in fixer_step.emits if e[2] == "internal_review_required")
    done_emit = next(e for e in fixer_step.emits if e[2] == "done")
    review_payload = internal_emit[3]
    assert review_payload["finding"]["finding_id"] == "qaf-residual"
    assert review_payload["finding"]["story"] == "US011"
    assert review_payload["finding"]["_qa_parent_finding_id"] is None

    fixer = {"actor_id": 22, "supervisor_id": 21, "name": "fixer", "role": "dev-fixer",
             "status": "done", "memory": {}, "result": {}}
    dev = {"actor_id": 21, "supervisor_id": 20, "name": "dev", "role": "dev-coordinator",
           "kind": "supervisor", "status": "working", "assignment": "fix", "memory": {
               "phase": "delegating", "context": {"bug": review_payload["finding"]}}}
    monkeypatch.setattr(runtime.store, "actors", lambda *_a, **_k: [fixer])
    monkeypatch.setattr(runtime.store, "actor", lambda *_a, **_k: fixer)
    replayed = {"id": 1, "kind": "internal_review_required", "frm": 22,
                "corr_id": "c", "payload": review_payload}
    runtime._supervisor_step(ctx, dev, [replayed, {**replayed, "id": 2},
        {"id": 3, "kind": "done", "frm": 22, "corr_id": done_emit[4], "payload": done_emit[3]}])
    dev_step = persisted.pop()
    assert dev_step.status == "done" and dev_step.result["internal_review_required"] is True
    assert len(dev_step.memory["internal_reviews"]) == 1, "at-least-once replay must deduplicate"
    assert [e[2] for e in dev_step.emits].count("internal_review_required") == 1

    dev_child = {"actor_id": 21, "supervisor_id": 20, "name": "dev", "role": "dev-coordinator",
                 "status": "done", "memory": {"context": {"bug": review_payload["finding"]}},
                 "result": dev_step.result}
    qa = {"actor_id": 20, "supervisor_id": 19, "name": "qa", "role": "qa-coordinator",
          "kind": "supervisor", "status": "working", "assignment": "verify", "memory": {
              "phase": "delegating", "context": {"stories": [{"id": "US011"}]},
              "story_status": {"US011": "blocking"}}}
    monkeypatch.setattr(runtime.store, "actors", lambda *_a, **_k: [dev_child])
    monkeypatch.setattr(runtime.store, "actor", lambda *_a, **_k: dev_child)
    hires = []
    def hire_review(_ctx, _actor, specs, _step, _corr=None):
        hires.extend(specs)
        return "hired"
    monkeypatch.setattr(runtime, "_hire_or_request", hire_review)
    manager_event = next(e for e in dev_step.emits if e[2] == "internal_review_required")
    qa_events = [
        {"id": 4, "kind": "internal_review_required", "frm": 21, "corr_id": manager_event[4],
         "payload": manager_event[3]},
        {"id": 5, "kind": "done", "frm": 21, "corr_id": None,
         "payload": {"task": "fix", "result": dev_step.result}},
    ]
    runtime._supervisor_step(ctx, qa, qa_events)
    qa_step = persisted.pop()
    assert qa_step.status is None and qa_step.result is None
    assert len(hires) == 1 and hires[0]["tool"] == "qa_review"
    assert hires[0]["role"] == "qa-evidence-reviewer"
    assert qa_step.memory["story_status"]["US011"] == "internal_review"
    assert qa_step.memory["internal_reviews"][0]["review_id"]
    assert qa_step.memory["internal_review_states"][
        qa_step.memory["internal_reviews"][0]["review_id"]]["status"] == "review_scheduled"
    assert qa_step.memory["retests"] == {}


def test_internal_qa_management_events_are_part_of_the_persisted_bus_contract():
    sys.path.insert(0, str(ROOT / "scripts" / "orchestra"))
    import bus
    import store

    assert {"internal_review_required", "finding_resolution"}.issubset(bus.KINDS)
    assert {"internal_review_required", "finding_resolution"}.issubset(store.KINDS)


def test_qa_evidence_recovery_is_single_flight_per_review(monkeypatch):
    from types import SimpleNamespace
    sys.path.insert(0, str(ROOT / "scripts" / "orchestra"))
    import runtime

    review_id = "qa-review-one"
    record = {"review_id": review_id, "story": "US-012",
              "finding": {"story": "US-012", "title": "disputed labels"}}
    coordinator = {
        "actor_id": 700, "supervisor_id": 699, "status": "working", "assignment": "qa",
        "role": "qa-coordinator", "name": "qa-coordinator", "memory": {
            "phase": "delegating", "internal_reviews": [record],
            "internal_review_states": {review_id: {"verification_attempts": 1}},
            "context": {"stories": [{"id": "US-012"}]},
        },
    }
    active = {
        "actor_id": 701, "supervisor_id": 700, "status": "blocked", "role": "qa-explorer",
        "memory": {"context": {"tool_args": {"_qa_review_id": review_id}}},
    }
    saved = []
    monkeypatch.setattr(runtime.store, "actors", lambda *_a, **_k: [active])
    monkeypatch.setattr(runtime, "_persist", lambda _c, _a, step, _e: saved.append(step))
    monkeypatch.setattr(runtime, "_audit", lambda *_a, **_k: None)
    monkeypatch.setattr(runtime, "_hire_or_request", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("an active verifier for this review must be reused")))
    event = {"id": 1, "kind": "context_update", "frm": None, "corr_id": "manager-retry",
             "payload": {"qa_evidence_recovery": {
                 "review_id": review_id, "case_id": "case-one", "action": "reverify"}}}

    runtime._supervisor_step(
        SimpleNamespace(run_id=1, tenant="t", repo="/tmp", human_hook=None),
        coordinator, [event])

    state = saved[-1].memory["internal_review_states"][review_id]
    assert state["status"] == "verification_inflight"
    assert state["verification_actor_id"] == 701
    assert state["verification_attempts"] == 1


def test_qa_review_state_updates_queue_behind_active_lease_holder(monkeypatch):
    from types import SimpleNamespace
    sys.path.insert(0, str(ROOT / "scripts" / "orchestra"))
    import runtime

    review_id = "qa-review-one"
    record = {"review_id": review_id, "story": "US-012",
              "finding": {"story": "US-012", "title": "disputed labels"}}
    coordinator = {
        "actor_id": 710, "supervisor_id": 709, "status": "working", "assignment": "qa",
        "role": "qa-coordinator", "name": "qa-coordinator", "memory": {
            "phase": "delegating", "internal_reviews": [record],
            "internal_review_states": {review_id: {"review_attempts": 1}},
            "context": {"stories": [{"id": "US-012"}]},
        },
    }
    active = {
        "actor_id": 711, "supervisor_id": 710, "status": "blocked",
        "role": "qa-evidence-reviewer", "memory": {"context": {"tool_args": {
            "internal_review": record, "state": {"generation": 1}}}},
    }
    saved = []
    monkeypatch.setattr(runtime.store, "actors", lambda *_a, **_k: [active])
    monkeypatch.setattr(runtime, "_persist", lambda _c, _a, step, _e: saved.append(step))
    monkeypatch.setattr(runtime, "_audit", lambda *_a, **_k: None)
    monkeypatch.setattr(runtime, "_hire_or_request", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("a second reviewer must not steal the active case lease")))
    event = {"id": 1, "kind": "context_update", "frm": None, "corr_id": "state-change",
             "payload": {"qa_review_state_changed": {
                 "review_id": review_id, "state": {"generation": 2}}}}

    runtime._supervisor_step(
        SimpleNamespace(run_id=1, tenant="t", repo="/tmp", human_hook=None),
        coordinator, [event])

    state = saved[-1].memory["internal_review_states"][review_id]
    assert state["status"] == "review_inflight"
    assert state["review_actor_id"] == 711
    assert state["review_attempts"] == 1
    assert state["pending_review_state"] == {"generation": 2}


def test_same_story_pending_fixer_waits_for_authoritative_evidence_review():
    sys.path.insert(0, str(ROOT / "scripts" / "orchestra"))
    import runtime

    reviewer = {
        "status": "blocked", "role": "qa-evidence-reviewer", "memory": {"context": {
            "tool_args": {"internal_review": {
                "review_id": "review-one", "story": "US-012",
                "finding": {"story": "US-012"},
            }}}},
    }
    other = {
        "status": "blocked", "role": "qa-evidence-reviewer", "memory": {"context": {
            "tool_args": {"internal_review": {"story": "US-010"}}}},
    }

    assert runtime._qa_story_review_inflight({1: reviewer}, "US-012") is True
    assert runtime._qa_story_review_inflight({1: reviewer}, "US-010") is False
    assert runtime._qa_story_review_inflight({2: other}, "US-012") is False
    assert runtime._qa_story_review_inflight(
        {1: {**reviewer, "status": "done"}}, "US-012") is False


def test_recovery_verification_hands_actionable_bug_to_fixer_immediately(monkeypatch):
    """Fresh high-severity evidence is a state-change trigger: recovery QA hands off instead of wandering."""
    from qa import qa_explorer

    class Bridge:
        def state(self):
            return {"url": "http://app", "title": "app", "elements": [], "bodyText": "broken"}
        def act(self, _action):
            return {"ok": True, "effect": True}

    ex = qa_explorer.Explorer("http://127.0.0.1:1", "vision", autostart=False)
    ex.bridge = Bridge()
    ex._ai_coverage_plan = lambda *_a, **_k: [
        {"aspect": "first", "covered": False}, {"aspect": "second", "covered": False}]
    calls = {"decide": 0}
    def decide(*_a, **_k):
        calls["decide"] += 1
        return {"next_action": {"cmd": "wait", "value": "0"}, "expected": "safe",
                "reasoning": "probe", "covers": [], "done": False}
    ex._ai_decide = decide
    ex._ai_evaluate = lambda *_a, **_k: {
        "matches_expected": False, "verdict": "bug", "target_confirmed": True,
        "bug": "security audit is missing", "severity": "critical", "blocking": False,
        "demonstrated": []}
    ex._checkpoint = lambda *_a, **_k: None
    bugs = []
    ex.explore({"title": "story"}, on_bug=bugs.append, stop_on_actionable_bug=True)
    assert calls["decide"] == 1 and len(bugs) == 1
    assert ex.stop_reason == "actionable-finding"


def test_nonblocking_high_finding_gets_independent_manager_decision(monkeypatch):
    """A potentially overreaching explorer cannot send a repository mutation straight to the fixer."""
    from qa import qa_explorer
    class Bridge:
        def state(self): return {"url": "http://app", "title": "app", "elements": [], "bodyText": "claims"}
        def act(self, _action): return {"ok": True, "effect": True}
    ex = qa_explorer.Explorer("http://127.0.0.1:1", "vision", autostart=False)
    ex.bridge = Bridge()
    ex._ai_coverage_plan = lambda *_a, **_k: [{"aspect": "publish", "covered": False}]
    ex._ai_decide = lambda *_a, **_k: {"next_action": {"cmd": "wait", "value": "0"},
        "expected": "publish is blocked", "reasoning": "probe", "covers": [], "done": False}
    ex._ai_evaluate = lambda *_a, **_k: {"matches_expected": False, "verdict": "bug",
        "target_confirmed": True, "bug": "maybe missing blocker", "severity": "high",
        "blocking": False, "demonstrated": []}
    ex._ai_incomplete_diagnosis = lambda *_a, **_k: {"disposition": "app_defect",
        "bug": "No publication workflow exists.", "severity": "high", "blocking": True,
        "reason": "The story requires a reachable staff control."}
    ex._checkpoint = lambda *_a, **_k: None
    bugs = []
    ex.explore({"title": "story"}, on_bug=bugs.append, stop_on_actionable_bug=True)
    assert len(bugs) == 2 and bugs[-1]["bug"] == "No publication workflow exists."
    assert ex.stop_reason == "managed-actionable-finding"


def test_cumulative_diagnosis_closes_a_multistep_aspect_without_another_explorer():
    """A final action can complete a composite denial→approval→send contract from sealed prior receipts."""
    from qa import qa_explorer

    aspect = "deny before approval, record approver and reason, then send once after approval"

    class Bridge:
        def state(self):
            return {"url": "http://app", "title": "app", "elements": [],
                    "bodyText": "Denied, approved by staff with reason, then sent once",
                    "console_errors": [], "recent_requests": []}
        def act(self, _action):
            return {"ok": True, "effect": True}

    ex = qa_explorer.Explorer("http://app", "vision", autostart=False)
    ex.bridge = Bridge()
    ex._ai_coverage_plan = lambda *_a, **_k: [{"aspect": aspect, "covered": False}]
    ex._ai_decide = lambda *_a, **_k: {
        "next_action": {"cmd": "wait", "value": "0"}, "expected": "final state remains settled",
        "reasoning": "final cumulative check", "covers": [], "done": True,
    }
    ex._ai_evaluate = lambda *_a, **_k: {
        "matches_expected": True, "verdict": "pass", "target_confirmed": True,
        "bug": None, "severity": "none", "blocking": False, "demonstrated": [],
    }
    ex._ai_incomplete_diagnosis = lambda *_a, **_k: {
        "disposition": "continue_possible", "bug": None, "severity": "none", "blocking": False,
        "reason": "the bounded receipts prove the full sequence", "demonstrated": [aspect],
    }
    ex._checkpoint = lambda *_a, **_k: None

    records = ex.explore({"title": "Gate send"})

    assert ex.coverage[0]["covered"] is True
    assert ex.stop_reason == "coverage-complete"
    assert any((item.get("action") or {}).get("cmd") == "diagnose_cumulative_progress"
               and item.get("demonstrated") == [aspect] for item in records)
    assert qa_explorer.campaign_checkpoint.proven_aspects(records) == {aspect.casefold()}


def test_partial_cumulative_diagnosis_continues_same_explorer_from_advanced_ledger():
    """Diagnosis progress must not return to the coordinator and replay the same restored snapshot."""
    from qa import qa_explorer

    first = "Confirm the existing panel state."
    second = "Verify the final outcome."

    class Bridge:
        def state(self):
            return {"url": "http://app", "title": "app", "elements": [],
                    "bodyText": "Panel and final outcome", "console_errors": [],
                    "recent_requests": []}

        def act(self, action):
            if action.get("cmd") == "inspect_surfaces":
                return {"ok": True, "landmarkDwell": True, "targets": ["Panel"],
                        "duration_ms_each": 25, "observations": [{
                            "target": "Panel", "scroll": {"scrolled": True, "matched": "Panel"},
                            "stable": True}]}
            return {"ok": True, "waited": True}

    ex = qa_explorer.Explorer("http://app", "vision", autostart=False)
    ex.bridge = Bridge()
    ex._ai_coverage_plan = lambda *_a, **_k: [
        {"aspect": first, "covered": False}, {"aspect": second, "covered": False}]
    decisions = []

    def decide(*_a, checklist=None, **_k):
        first_open = not next(item for item in checklist if item["aspect"] == first)["covered"]
        action = ({"cmd": "inspect_surfaces", "targets": ["Panel"]}
                  if first_open else {"cmd": "wait", "value": "0"})
        decisions.append(action["cmd"])
        return {"next_action": action, "expected": "settled evidence", "reasoning": "advance",
                "covers": [], "done": False}

    ex._ai_decide = decide
    ex._ai_evaluate = lambda *_a, **_k: {
        "matches_expected": True, "verdict": "pass", "target_confirmed": True,
        "bug": None, "severity": "none", "blocking": False,
        "demonstrated": ([second] if ex.coverage[0].get("covered") else []),
    }
    diagnoses = []

    def diagnose(*_a, **_k):
        diagnoses.append(True)
        return {"disposition": "continue_possible", "bug": None, "severity": "none",
                "blocking": False, "reason": "the repeated receipts prove only the first clause",
                "demonstrated": [first]}

    ex._ai_incomplete_diagnosis = diagnose
    ex._checkpoint = lambda *_a, **_k: None

    records = ex.explore({"title": "Partial cumulative proof"})

    assert decisions == ["inspect_surfaces", "inspect_surfaces", "wait"]
    assert len(diagnoses) == 1 and len(records) == 4
    assert any((item.get("action") or {}).get("cmd") == "diagnose_cumulative_progress"
               for item in records)
    assert all(item["covered"] for item in ex.coverage)
    assert ex.stop_reason == "coverage-complete"


def test_resumed_fixer_plans_against_fresh_residual_not_stale_trigger(monkeypatch, tmp_path):
    """A resumed worker owns what fresh QA proves; it must not repeat the already-applied old mutation."""
    from qa import dev_loop
    original = {"bug": "old seeded-state defect", "severity": "medium"}
    fresh = {"bug": "sensitive text is public", "severity": "critical", "blocking": True}
    qa_calls = {"n": 0}
    def self_qa(*_a, **_k):
        qa_calls["n"] += 1
        return [fresh]
    planned = []
    monkeypatch.setattr(dev_loop, "dev_self_qa", self_qa)
    monkeypatch.setattr(dev_loop, "_triage_finding", lambda *_a, **_k: {
        "disposition": "confirmed_defect", "may_mutate": True, "reviews": [], "reason": "confirmed"})
    monkeypatch.setattr(dev_loop, "_triage_citations_current", lambda *_a, **_k: True)
    monkeypatch.setattr(dev_loop, "_plan_fix",
                        lambda bug, *_a, **_k: planned.append(bug) or {"agents": []})
    monkeypatch.setattr(dev_loop, "_spawn_fix_agents",
                        lambda *_a, **_k: (_ for _ in ()).throw(
                            AssertionError("zero-agent plan must not be coerced into a writer")))
    monkeypatch.setattr(dev_loop, "_directory_snapshot", lambda *_a: {})
    monkeypatch.setattr(dev_loop, "_directory_changed_since", lambda *_a: ([], {}))
    monkeypatch.setattr(dev_loop, "_directory_diff", lambda *_a: "")
    monkeypatch.setattr(dev_loop, "_judge_fixed",
                        lambda *_a, **_k: {"fixed": False, "confidence": 1.0, "reason": "still broken"})
    out = dev_loop.fix_bug(original, {}, "vision", target_url="http://app", stories=[{"id": "US-1"}],
                           repo=str(tmp_path), resume_existing=True, max_attempts=1)
    assert planned and planned[0]["bug"] == fresh["bug"]
    assert planned[0]["recovery_trigger"] == original
    assert out["internal_review_required"] is True
    assert out["attempts_log"][-1]["failed_stage"] == "planning"


def test_dev_recovery_resumes_only_checkpoint_newer_than_product_revision(tmp_path):
    """Slice rotation keeps proven browser coverage; a code change invalidates the old observation."""
    from qa import dev_loop
    repo = tmp_path / "product"; repo.mkdir()
    source = repo / "app.js"; source.write_text("v1")
    evidence = tmp_path / "evidence"; run = evidence / "20260815-000000"; run.mkdir(parents=True)
    state = run / "storage-state.json"; state.write_text('{"cookies":[],"origins":[]}')
    checkpoint = run / "checkpoint.json"
    checkpoint.write_text(__import__("json").dumps({
        "ts": __import__("time").time() + 2, "story": "S", "tested": ["a", "b"],
        "yet_to_test": ["c"], "resume_state_path": str(state)}))
    assert dev_loop._latest_resume_coverage({"title": "S"}, repo, evidence) == ["a", "b"]
    generated = repo / "docs" / "QA-VERDICT.json"; generated.parent.mkdir()
    generated.write_text("generated later")
    future_generated = __import__("time").time() + 4
    __import__("os").utime(generated, (future_generated, future_generated))
    assert dev_loop._latest_resume_coverage({"title": "S"}, repo, evidence) == ["a", "b"]
    future = __import__("time").time() + 5
    __import__("os").utime(source, (future, future))
    assert dev_loop._latest_resume_coverage({"title": "S"}, repo, evidence) == []


def test_qa_resume_matches_conservative_coverage_paraphrase():
    from qa import qa_explorer
    old = ["Attempt publication against each seeded claim type and a missing claim ID; confirm only eligible "
           "references publish, while missing or ineligible references fail closed with staff-owned blockers."]
    same = ("Attempt publication against each seeded claim type and a missing claim ID; confirm only eligible "
            "references publish while every missing or ineligible reference fails closed with a staff-owned blocker.")
    different = "Attach evidence, verify a claim, and record the approval decision."
    assert qa_explorer._covered_in_prior_ledger(same, old) is True
    assert qa_explorer._covered_in_prior_ledger(different, old) is False

    reordered = ("Attempt publication against each seeded claim type and a missing claim; confirm ineligible "
                 "references fail closed with staff-owned blockers while eligible references proceed only after "
                 "governance checks.")
    expanded_old = ["Attempt publication against each seeded claim type and a missing claim; confirm only eligible "
                    "evidenced references publish, while missing, draft, unverified, private, and evidence-free "
                    "references fail closed with staff-owned blockers and remain absent publicly after governance checks."]
    broader_new = reordered + " Also prove every email, phone, token, password, and secret is blocked."
    assert qa_explorer._covered_in_prior_ledger(reordered, expanded_old) is True
    assert qa_explorer._covered_in_prior_ledger(broader_new, expanded_old) is False

    combined = "Create one enquiry with a follow-up draft, edit its subject and body, and verify both changes persist."
    split_prior = [
        "Create or seed one enquiry with a follow-up draft and verify the draft is associated with that enquiry.",
        "Edit the draft subject and body and verify both changes persist before submission.",
    ]
    assert qa_explorer._covered_in_prior_ledger(combined, split_prior) is True


def test_qa_wait_cannot_turn_disabled_control_miss_into_product_bug():
    from qa import qa_explorer
    records = [{"targeting": {"control_action": True, "effect_registered": False,
                               "before_control_disabled": True, "after_control_disabled": True}}]
    assert qa_explorer._passive_probe_after_disabled_miss({"cmd": "wait"}, records) is True
    assert qa_explorer._passive_probe_after_disabled_miss({"cmd": "click"}, records) is False
    successful_prior = [{"targeting": {"control_action": True, "effect_registered": True,
                                        "before_control_disabled": False, "after_control_disabled": False}}]
    assert qa_explorer._passive_probe_after_disabled_miss({"cmd": "wait"}, successful_prior) is False


def test_qa_gapfill_inherits_only_coverage_with_latest_portable_state(tmp_path):
    sys.path.insert(0, str(ROOT / "scripts" / "orchestra"))
    import runtime
    state = tmp_path / "storage-state.json"
    state.write_text('{"cookies":[],"origins":[]}')
    results = {
        "1": {"story": "US-010", "result": {"coverage": [
            {"aspect": "seed five claim states", "covered": True},
            {"aspect": "publish governed content", "covered": False}]}},
        "2": {"story": "US-009", "result": {"coverage": [
            {"aspect": "unrelated approval", "covered": True}]}},
        "3": {"story": "US-010", "result": {"coverage": [
            {"aspect": "block sensitive content", "covered": True},
            {"aspect": "seed five claim states", "covered": True}],
            "steps_detail": [{"verdict": "match", "covers": [
                "block sensitive content", "seed five claim states"]}],
            "resume_state_path": str(state)}}
    }
    assert runtime._qa_prior_coverage(results, "US-010") == [
        "block sensitive content", "seed five claim states"]


def test_qa_manager_turns_missing_required_workflow_into_a_real_bug(monkeypatch):
    """A nonexistent story-required control is diagnosed above targeting, not retried forever as a missed click."""
    from qa import qa_explorer
    monkeypatch.setattr(qa_explorer, "_call_agent", lambda *_a, **_k: {"out_full": __import__("json").dumps({
        "disposition": "app_defect", "bug": "No staff control exists to add claim evidence.",
        "severity": "high", "blocking": True, "reason": "The story requires it; settled controls omit it."
    })})
    ex = qa_explorer.Explorer("http://127.0.0.1:1", "vision", autostart=False)
    result = ex._ai_incomplete_diagnosis(
        {"steps": ["Add evidence to a claim"], "expected_outcome": "claim becomes eligible"},
        {"url": "http://127.0.0.1:1", "bodyText": "Trust claims", "elements": []},
        ["Add evidence to a valid claim"], [])
    assert result["disposition"] == "app_defect" and result["blocking"] is True
    assert "No staff control" in result["bug"]


def test_cancelled_qa_tool_result_stays_resumable(monkeypatch, tmp_path):
    """A safety cancellation parks the story and atomically acknowledges its event exactly once."""
    from types import SimpleNamespace
    sys.path.insert(0, str(ROOT / "scripts" / "orchestra"))
    import runtime
    actor = {"actor_id": 41, "supervisor_id": 40, "status": "blocked",
             "assignment": "QA-explore story", "role": "qa-explorer",
             "memory": {"steps": 0, "progress": [], "context": {
                 "tool": "qa_explore", "tool_dispatched": True}}}
    state = tmp_path / "storage-state.json"
    state.write_text('{"cookies":[],"origins":[]}')
    event = {"id": 99, "kind": "tool_result", "payload": {"tool": "qa_explore", "status": "failed",
             "findings": [{"title": "partial bug", "story": "US-2"}], "result": {
                 "stop_reason": "cancelled-incomplete", "coverage": [
                     {"aspect": "loaded", "covered": True}, {"aspect": "submit", "covered": False}],
                 "resume_state_path": str(state),
                 "steps_detail": [{"action": "open", "verdict": "match"}]}},
             "corr_id": None, "frm": 41}
    persisted = []
    monkeypatch.setattr(runtime, "_persist", lambda ctx, a, step, evs:
                        persisted.append((step.status, [e["id"] for e in evs], step.memory, step.emits)))
    step = runtime._worker_step(SimpleNamespace(), actor, [event])
    assert step.status == "blocked" and step.emits[0][2] == "finding"
    assert step.result["checkpointed"] is True
    assert persisted[0][0:2] == ("blocked", [99])
    assert persisted[0][2]["context"]["tool_args"]["resume_covered"] == ["loaded"]
    assert persisted[0][2]["context"]["tool_args"]["resume_state_path"] == str(state)
    assert persisted[0][2]["context"]["tool_args"]["resume_steps_detail"][0]["action"] == "open"
    assert persisted[0][2]["context"]["tool_attempt"] == 1


def test_resume_repairs_legacy_terminal_cancellations_only():
    from types import SimpleNamespace
    from qa import qa_agentic
    actors = [
        {"actor_id": 1, "role": "qa-coordinator", "status": "working", "memory": {
            "results": {"2": {"cancelled": True}, "3": {"ok": True}},
            "handled": [{"frm": 2, "kind": "done"}, {"frm": 3, "kind": "done"}],
            "story_status": {"US-2": "incomplete", "US-3": "clean"}}},
        {"actor_id": 2, "role": "qa-explorer", "status": "done",
         "memory": {"context": {"tool_args": {"story": {"id": "US-2"}}}},
         "result": {"result": {"stop_reason": "cancelled-before-browser-start"}}},
        {"actor_id": 3, "role": "qa-explorer", "status": "done", "memory": {},
         "result": {"result": {"stop_reason": "coverage-complete", "story": "US-3"}}},
    ]
    updates = []
    fake = SimpleNamespace(actors=lambda *_: actors,
                           update_actor=lambda *a, **kw: updates.append((a, kw)))
    assert qa_agentic._reopen_legacy_cancelled_explorers(fake, 9, "t") == 1
    assert updates[0][0][0] == 2 and updates[0][1]["status"] == "blocked"
    repaired = updates[1][1]["memory"]
    assert "2" not in repaired["results"] and "3" in repaired["results"]
    assert "US-2" not in repaired["story_status"] and repaired["story_status"]["US-3"] == "clean"


def test_failed_qa_job_preserves_story_identity_and_is_incomplete():
    """A browser/provider crash is assigned work, not a clean story that may disappear from the verdict."""
    sys.path.insert(0, str(ROOT / "scripts" / "orchestra"))
    import jobrunner

    class FakeStore:
        def __init__(self):
            self.emits = []
        def emit(self, *args):
            self.emits.append(args)

    def crash(*_):
        raise RuntimeError("browser died")

    store = FakeStore()
    job = {"run_id": 991, "tenant": "qa-failure-test", "actor_id": 992,
           "tool": "qa_explore", "args": {"story": {"id": "US-CRASH", "title": "Crash"}}}
    jobrunner.dispatch(job, store, run_tool=crash, sync=True)
    payload = store.emits[0][5]
    assert payload["status"] == "failed"
    assert payload["result"]["story"] == "US-CRASH"
    assert "incomplete" in payload["result"]["stop_reason"]


def test_tool_job_lock_is_cross_process_single_flight():
    """Two controller processes cannot launch the same durable actor's browser/fixer concurrently."""
    sys.path.insert(0, str(ROOT / "scripts" / "orchestra"))
    import store
    with store.tool_job_lock(9911, "tool-lock-test", 9912, "qa_explore") as first:
        with store.tool_job_lock(9911, "tool-lock-test", 9912, "qa_explore") as second:
            assert first and first["fence_token"] >= 1 and not second
    with store.tool_job_lock(1, "tenant-a", 10, "dev_fix", resource_key="/products/p") as first:
        with store.tool_job_lock(2, "tenant-b", 20, "dev_fix", resource_key="/products/p") as second:
            assert first and first["fence_token"] >= 1 and not second


def test_tool_result_emit_once_survives_ambiguous_retry():
    """A lost DB acknowledgement cannot duplicate a tool_result and repeat fixes/exploration."""
    import psycopg
    sys.path.insert(0, str(ROOT / "scripts" / "orchestra"))
    import store
    tenant = f"emit-once-{_rid()}"
    run = store.start_run(tenant, "idempotent tool result")
    try:
        actor = store.spawn_actor(run["run_id"], tenant, "qa", "qa-explorer")
        args = (run["run_id"], tenant, actor["actor_id"], actor["actor_id"], "tool_result",
                {"status": "done"}, "stable-job-result")
        one = store.emit_once(*args)
        two = store.emit_once(*args)
        assert one["id"] == two["id"]
        assert len([e for e in store.events(run["run_id"], tenant)
                    if e["corr_id"] == "stable-job-result"]) == 1
    finally:
        with psycopg.connect(store.DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM orchestra_events WHERE run_id=%s", (run["run_id"],))
            cur.execute("DELETE FROM orchestra_actors WHERE run_id=%s", (run["run_id"],))
            cur.execute("DELETE FROM orchestra_runs WHERE run_id=%s", (run["run_id"],))


def test_qa_coordinator_cannot_pass_with_a_missing_planned_story(monkeypatch):
    from types import SimpleNamespace
    sys.path.insert(0, str(ROOT / "scripts" / "orchestra"))
    import runtime

    coordinator = {"actor_id": 501, "supervisor_id": None, "status": "working",
                   "assignment": "qa", "role": "qa-coordinator", "name": "qa-coordinator",
                   "memory": {"phase": "delegating", "story_status": {"US-1": "clean"},
                              "gapfills": {"US-2": 3},
                              "context": {"stories": [{"id": "US-1"}, {"id": "US-2"}]}}}
    child = {"actor_id": 502, "supervisor_id": 501, "status": "done", "role": "qa-explorer"}
    saved = []
    monkeypatch.setattr(runtime.store, "actors", lambda *_: [coordinator, child])
    monkeypatch.setattr(runtime.store, "finish_run", lambda *a, **k: None)
    monkeypatch.setattr(runtime, "_persist", lambda ctx, actor, step, evs: saved.append(step.result))
    runtime._supervisor_step(SimpleNamespace(run_id=1, tenant="t", repo="."), coordinator, [])
    assert saved[0]["passed"] is False
    assert saved[0]["missing_stories"] == ["US-2"]
    assert saved[0]["recovery_exhausted"] is True


def test_qa_cleanup_distinguishes_process_contained_threads_from_orphans():
    from qa import qa_agentic
    contained = qa_agentic._cleanup_facts(8, 0)
    assert contained["cleanup_threads_incomplete"] == 8
    assert contained["cleanup_processes_incomplete"] == 0
    assert contained["cleanup_process_contained"] is True
    orphaned = qa_agentic._cleanup_facts(0, 2)
    assert orphaned["cleanup_processes_incomplete"] == 2
    assert orphaned["cleanup_process_contained"] is False


def test_consumed_checkpoint_result_redispatches_same_actor(monkeypatch):
    """A cancellation consumed in this process must resume without requiring a fresh Python process."""
    import time
    sys.path.insert(0, str(ROOT / "scripts" / "orchestra"))
    import jobrunner

    actor = {"actor_id": 602, "supervisor_id": 601, "status": "blocked", "name": "qa",
             "assignment": "story", "memory": {"context": {"tool": "qa_explore",
             "tool_dispatched": True, "tool_attempt": 1, "tool_args": {"story": {"id": "US-R"}}}}}
    emitted = []
    processed = {"kind": "tool_result", "to_actor": 602, "processed_at": "now",
                 "corr_id": "job-600:602:qa_explore:1-result"}
    fake = type("Store", (), {
        "actors": staticmethod(lambda *_: [actor]),
        "events": staticmethod(lambda *_: [processed]),
        "emit": staticmethod(lambda *args: emitted.append(args)),
        "update_actor": staticmethod(lambda *_a, **_kw: None),
    })
    job = {"run_id": 600, "tenant": "t", "actor_id": 602, "tool": "qa_explore", "attempt": 1}
    jid = jobrunner.job_id_for(job)
    with jobrunner._LOCK:
        jobrunner._JOBS[jid] = {"state": "done", "job": job}
    monkeypatch.setattr(jobrunner, "_default_run_tool", lambda: (
        lambda *_: {"status": "done", "findings": [], "result": {"story": "US-R"}}))
    assert jobrunner.reconcile_parked(fake, 600, "t") == 1
    for _ in range(100):
        if emitted:
            break
        time.sleep(0.01)
    assert emitted


def test_unconsumed_terminal_tool_result_cannot_relaunch_same_attempt(monkeypatch):
    """The actor/result handoff window must not start a second browser or fixer."""
    sys.path.insert(0, str(ROOT / "scripts" / "orchestra"))
    import jobrunner

    actor = {"actor_id": 622, "supervisor_id": 621, "status": "blocked", "name": "fixer",
             "assignment": "repair", "memory": {"context": {"tool": "dev_fix",
             "tool_dispatched": True, "tool_attempt": 0, "tool_args": {}}}}
    pending = {"kind": "tool_result", "to_actor": 622, "processed_at": None,
               "corr_id": "job-620:622:dev_fix:0-result"}
    fake = type("Store", (), {
        "actors": staticmethod(lambda *_: [actor]),
        "events": staticmethod(lambda *_: [pending]),
    })
    job = {"run_id": 620, "tenant": "t", "actor_id": 622, "tool": "dev_fix", "attempt": 0}
    jid = jobrunner.job_id_for(job)
    called = []
    with jobrunner._LOCK:
        jobrunner._JOBS[jid] = {"state": "done", "job": job}
    try:
        assert jobrunner.reconcile_parked(fake, 620, "t") == 0
        assert jobrunner.dispatch(job, fake, run_tool=lambda *_: called.append(True), sync=True) == jid
        assert called == []
        assert jobrunner._JOBS[jid]["state"] == "done"
    finally:
        with jobrunner._LOCK:
            jobrunner._JOBS.pop(jid, None)


def test_reconcile_parked_fails_closed_when_result_ledger_is_unavailable(monkeypatch):
    """A transient event-ledger read error pauses recovery instead of duplicating side effects."""
    sys.path.insert(0, str(ROOT / "scripts" / "orchestra"))
    import jobrunner

    actor = {"actor_id": 632, "status": "blocked", "memory": {"context": {
        "tool": "qa_explore", "tool_dispatched": True, "tool_args": {}}}}
    fake = type("Store", (), {
        "actors": staticmethod(lambda *_: [actor]),
        "events": staticmethod(lambda *_: (_ for _ in ()).throw(ConnectionError("db churn"))),
    })
    called = []
    monkeypatch.setattr(jobrunner, "reconcile", lambda *_a, **_k: called.append(True) or 1)
    assert jobrunner.reconcile_parked(fake, 630, "t") == 0
    assert called == []


def test_reconcile_parked_supersedes_orphaned_verifier_for_terminal_review(monkeypatch):
    """A restart must not relaunch a browser after the exact evidence case is already decided."""
    sys.path.insert(0, str(ROOT / "scripts" / "orchestra"))
    import jobrunner
    import qareview

    actor = {"actor_id": 642, "supervisor_id": 641, "status": "blocked", "name": "verify",
             "assignment": "focused review", "memory": {"context": {
                 "tool": "qa_explore", "tool_dispatched": True, "tool_attempt": 0,
                 "tool_args": {"story": {"id": "US-9"},
                               "_qa_review_id": "qa-review-terminal"}}}}
    emitted, dispatched = [], []
    fake = type("Store", (), {
        "actors": staticmethod(lambda *_: [actor]),
        "events": staticmethod(lambda *_: []),
        "emit_once": staticmethod(lambda *args: emitted.append(args) or {"id": 1}),
    })
    monkeypatch.setattr(qareview, "stable_case_id", lambda *_: "case-terminal")
    monkeypatch.setattr(qareview, "get", lambda *_: {
        "case_id": "case-terminal", "status": "resolved",
        "disposition": "confirmed_defect"})
    monkeypatch.setattr(jobrunner, "reconcile",
                        lambda pending, *_a, **_k: dispatched.extend(pending) or len(pending))

    assert jobrunner.reconcile_parked(fake, 640, "t") == 1
    assert dispatched == []
    payload = emitted[0][5]
    assert payload["status"] == "done"
    assert payload["result"]["stop_reason"] == "superseded-terminal-decision"
    assert payload["result"]["terminal_review"]["disposition"] == "confirmed_defect"


def test_reconcile_parked_recovers_dev_mutation_receipt_from_legacy_actor_result(monkeypatch):
    """A rolling upgrade must retain exact changed files even if the old runtime consumed the checkpoint."""
    sys.path.insert(0, str(ROOT / "scripts" / "orchestra"))
    import jobrunner

    actor = {"actor_id": 612, "supervisor_id": 611, "status": "blocked", "name": "fixer",
             "assignment": "repair", "result": {"checkpointed": True, "partial_result": {
                 "files": ["src/policy.js", "tests/policy.test.js"],
                 "triage": {"disposition": "confirmed_defect", "may_mutate": True,
                            "finding_fingerprint": "legacy-receipt"},
                 "resume_triage_finding": {
                     "finding_id": "fresh-current", "story": "US-10"}}},
             "memory": {"context": {"tool": "dev_fix", "tool_dispatched": True,
                                        "tool_attempt": 2, "tool_args": {"bug": {"story": "US-10"}}}}}
    captured = []
    fake = type("Store", (), {
        "actors": staticmethod(lambda *_: [actor]),
        "events": staticmethod(lambda *_: []),
    })
    monkeypatch.setattr(jobrunner, "reconcile",
                        lambda pending, *_a, **_k: captured.extend(pending) or len(pending))

    assert jobrunner.reconcile_parked(fake, 610, "t") == 1
    assert captured[0]["args"]["resume_changed_files"] == [
        "src/policy.js", "tests/policy.test.js"]
    assert captured[0]["args"]["resume_triage_finding"] == {
        "finding_id": "fresh-current", "story": "US-10"}
    assert captured[0]["args"]["resume_triage_receipt"]["finding_fingerprint"] == "legacy-receipt"
    assert captured[0]["resumed"] is True


def test_queued_tool_admission_observes_cancellation_without_waiting_for_slot():
    import threading
    import time
    sys.path.insert(0, str(ROOT / "scripts" / "orchestra"))
    import jobrunner
    sem = threading.BoundedSemaphore(1)
    sem.acquire()
    job = {"run_id": 710, "tenant": "t", "actor_id": 711, "tool": "qa_explore"}
    jid = jobrunner.job_id_for(job)
    cancelled = threading.Event(); cancelled.set()
    with jobrunner._LOCK:
        jobrunner._JOBS[jid] = {"state": "running", "job": job, "cancel_event": cancelled}
    started = time.time()
    with jobrunner._Admission(sem, jid, job) as admitted:
        assert admitted is False
    assert time.time() - started < 0.1
    sem.release()


def test_dev_fix_jobs_are_serial_by_default():
    """Fixers serialize mutation and reserve capacity for their mandatory browser verification."""
    src = (ROOT / "scripts" / "orchestra" / "jobrunner.py").read_text()
    assert 'AOS_DEV_FIX_CONCURRENCY", "1"' in src
    assert '[_DEV_FIX_SEM] if job.get("tool") == "dev_fix"' in src
    assert 'capacity acquired; claiming durable fence' in src
    assert 'stage="coordination-retry"' in src
    assert 'admission = _Admissions(sems, jid, job)' in src
    assert 'cancelled while waiting for repository mutation slot' in src


def test_grouped_tool_admission_releases_partial_capacity_on_cancellation():
    import threading
    sys.path.insert(0, str(ROOT / "scripts" / "orchestra"))
    import jobrunner
    first = threading.BoundedSemaphore(1)
    second = threading.BoundedSemaphore(1); second.acquire()
    job = {"run_id": 720, "tenant": "t", "actor_id": 721, "tool": "dev_fix"}
    jid = jobrunner.job_id_for(job)
    cancelled = threading.Event()
    with jobrunner._LOCK:
        jobrunner._JOBS[jid] = {"state": "running", "job": job, "cancel_event": cancelled}
    timer = threading.Timer(0.05, cancelled.set); timer.start()
    try:
        with jobrunner._Admissions([first, second], jid, job) as admitted:
            assert admitted is False
        assert first.acquire(blocking=False) is True
        first.release()
    finally:
        timer.cancel(); second.release()


def test_qa_tool_admission_matches_safe_browser_ceiling():
    """Tool threads must queue before browser acquisition instead of timing out against the two-slot gate."""
    src = (ROOT / "scripts" / "orchestra" / "jobrunner.py").read_text()
    assert 'os.environ.get("AOS_QA_AGENTIC_WORKER_CAP", "2")' in src


def test_browser_gate_bounds_global_concurrency():
    """Production reliability: at scale (1000s of agents) concurrent browser QA must not thrash the box.
    browser_gate caps TOTAL concurrent browser sessions across all processes; a slot is released on close and
    reclaimable. Without this cap, competing QA runs stalled each other (observed live)."""
    import browser_gate
    import claude_gate
    if not claude_gate.DB:
        pytest.skip("no DB")
    # 1) the cap is auto-sized to this box (RAM+CPU), sane range — not a hardcoded guess.
    assert 1 <= browser_gate.GLOBAL_MAX <= browser_gate._SAFE_AUTO_CEILING, \
        f"automatic laptop cap must stay below its safety ceiling, got {browser_gate.GLOBAL_MAX}"
    assert browser_gate._SAFE_AUTO_CEILING == 2, "default WSL/laptop operation must retain interactive CPU headroom"
    # 2) prove the pool MECHANISM (cap + reclaim) on an ISOLATED throwaway table, so this never races the live
    #    browser_slots pool that a running QA fleet is churning (that raced when run in the full suite).
    import psycopg
    tbl = f"browser_slots_test_{_rid()}"
    cap = 3
    def _acq(holder):
        # acquire fail-OPENS to None on a transient DB hiccup (by design, claude_gate.py). When the box is under
        # duress (a live QA fleet churning during the full suite) that's expected behavior, not a mechanism defect —
        # retry the transient case a few times so this test measures the CAP, not the box's momentary load.
        for _ in range(4):
            s = claude_gate.acquire(holder, wait_s=2, table=tbl)
            if s is not None:
                return s
        return None
    try:
        claude_gate._ensure(tbl, cap)
        got = [_acq(f"t-{i}") for i in range(cap)]
        if not all(s is not None for s in got):
            pytest.skip(f"DB under duress, gate fail-opened (by design) before pool filled: {got}")
        assert claude_gate.acquire("t-over", wait_s=1, table=tbl) is None, "pool full -> refuse, never over-grant"
        claude_gate.release(got[0], table=tbl)
        assert _acq("t-reuse") is not None, "a released slot must be reclaimable"
    finally:
        with psycopg.connect(claude_gate.DB) as c, c.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {tbl}"); c.commit()
    # 3) DB coordination is the cross-process safety boundary: if it cannot grant a slot, fail CLOSED and
    # release the local reservation. Otherwise each process could launch its own local maximum during an outage.
    import threading
    monkeypatch = pytest.MonkeyPatch()
    held = []
    try:
        class FakeResourceLease:
            def start_heartbeat(self, **_kwargs): return self
            def stop_heartbeat(self): return None

        monkeypatch.setattr(browser_gate, "GLOBAL_MAX", 2)
        monkeypatch.setattr(browser_gate, "_LOCAL_SEM", threading.BoundedSemaphore(2))
        monkeypatch.setattr(browser_gate.claude_gate, "_ensure", lambda *a, **k: None)
        monkeypatch.setattr(browser_gate.claude_gate, "acquire_host_resource",
                            lambda *a, **k: FakeResourceLease())
        monkeypatch.setattr(browser_gate.claude_gate, "release_host_resource", lambda *_a, **_k: None)
        monkeypatch.setattr(browser_gate.claude_gate, "release", lambda *_a, **_k: None)
        monkeypatch.setattr(browser_gate.claude_gate, "acquire", lambda *a, **k: None)
        assert browser_gate.acquire("db-unavailable", wait_s=1) is None
        # The failed acquire released its local slot; two later real DB grants can still be represented.
        grants = iter([1, 2])
        monkeypatch.setattr(browser_gate.claude_gate, "acquire", lambda *a, **k: next(grants))
        held = [browser_gate.acquire("local-1", wait_s=1), browser_gate.acquire("local-2", wait_s=1)]
        assert all(isinstance(s, dict) and s.get("kind") == "local" for s in held)
        assert browser_gate.acquire("local-overflow", wait_s=1) is None
    finally:
        for sid in held:
            browser_gate.release(sid)
        monkeypatch.undo()


def test_browser_gate_reclaims_dead_qa_holders():
    """Hard-cancelled QA browser sessions must not block the next run until the long browser lease expires."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import browser_gate
    import psycopg
    import uuid

    tbl = f"browser_slots_reclaim_{uuid.uuid4().hex[:8]}"
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(browser_gate, "TABLE", tbl)
        browser_gate.claude_gate._ensure(tbl, 2)
        with psycopg.connect(browser_gate.claude_gate.DB) as c, c.cursor() as cur:
            cur.execute(f"""UPDATE {tbl}
                            SET holder = CASE slot_id
                                WHEN 1 THEN 'qa:v2:dead-boot:999999999:123:/tmp/dead'
                                ELSE 'qa:/tmp/legacy-dead'
                            END,
                            acquired_at = now() - interval '10 minutes',
                            lease_until = now() + interval '30 minutes'""")
            c.commit()
        reclaimed = browser_gate._reclaim_dead_qa_slots()
        assert reclaimed == 1
        state = browser_gate.status()
        assert state["held"] == 1 and state["live_held"] == 1
    finally:
        monkeypatch.undo()
        with psycopg.connect(browser_gate.claude_gate.DB) as c, c.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {tbl}"); c.commit()


def test_dogfood_yields_to_active_qa():
    """Scheduled acceptance-QA must yield the box to a real build's QA (the collision that stalled a live run).
    dogfood.cron() skips when _qa_active() is True instead of thrashing browsers against the active run."""
    sys.path.insert(0, str(ROOT / "scripts" / "qa"))
    import dogfood
    real = dogfood._qa_active
    try:
        dogfood._qa_active = lambda: True
        r = dogfood.cron()
        assert r.get("skipped") == "qa-active" and not r.get("detached"), r
    finally:
        dogfood._qa_active = real


def test_dogfood_capacity_ignores_historical_stalled_pulses(monkeypatch):
    """Persisted stalled QA rows are incident history, not live browser pressure."""
    sys.path.insert(0, str(ROOT / "scripts" / "qa"))
    import dogfood
    import types

    fake = types.SimpleNamespace(live=lambda: [
        {"kind": "tool-job", "status": "stalled", "stalled": False},
        {"kind": "qa-run", "status": "reaped", "stalled": False},
    ])
    monkeypatch.setitem(sys.modules, "pulse", fake)
    assert dogfood._qa_active() is False
    fake.live = lambda: [{"kind": "qa-run", "status": "active", "stalled": False}]
    assert dogfood._qa_active() is True


def test_dogfood_preflight_blocks_known_open_acceptance_blockers(monkeypatch):
    """A fresh live dogfood run spends browser/model time. If known critical/high dogfood findings are still
    open, preflight must surface and block them instead of burning a run to rediscover the same defect."""
    sys.path.insert(0, str(ROOT / "scripts" / "qa"))
    import dogfood
    monkeypatch.setattr(dogfood, "_console_up", lambda base=dogfood.BASE: True)
    monkeypatch.setattr(dogfood, "_qa_active", lambda: False)
    monkeypatch.setattr(dogfood, "_last_run", lambda persona=None: {
        "persona": "first-run", "age_hours": 315, "passed": 4, "total": 5,
        "story_status": {"J4-wait-watch": "failed"}})
    monkeypatch.setattr(dogfood, "_open_dogfood_findings", lambda: {
        "total": 1, "blocking": 1, "allow_override": False,
        "items": [{"id": 515, "severity": "critical", "title": "known wait/watch failure"}]})
    monkeypatch.setattr(dogfood, "ALLOW_OPEN_FINDINGS", False)
    blocked = dogfood.preflight("first-run")
    assert blocked["ok"] is False
    assert blocked["checks"]["known_dogfood_blockers_clear"] is False
    assert blocked["last_run"]["passed"] == 4
    assert blocked["open_dogfood_findings"]["items"][0]["id"] == 515

    monkeypatch.setattr(dogfood, "ALLOW_OPEN_FINDINGS", True)
    monkeypatch.setattr(dogfood, "_open_dogfood_findings", lambda: {
        "total": 1, "blocking": 1, "allow_override": True,
        "items": [{"id": 515, "severity": "critical", "title": "known wait/watch failure"}]})
    allowed = dogfood.preflight("first-run")
    assert allowed["ok"] is True
    assert allowed["checks"]["known_dogfood_blockers_clear"] is True
    assert "AOS_DOGFOOD_ALLOW_OPEN_FINDINGS=1" in allowed["override"]["allow_open_findings_env"]


def test_controller_workstream_prevents_false_idle_cockpit_and_projects():
    """Dogfood #515: after the CEO directs work, controller_state can be live before tenant_products exists.
    Cockpit/projects/live-status must surface that workstream so the UI cannot report a false idle company."""
    import psycopg
    import billing
    import cockpit
    import chiefofstaff
    import livestatus
    import loopcontroller as lc
    import orgs
    import projectsview
    import traceview
    import workstreamview
    from aoscfg import DB

    lc._ensure()
    tid = billing.signup(f"false-idle-{_rid()}", "free")["tenant_id"]
    org = orgs.create(tid, "False idle regression", "Build the first product")
    oid = org["org_id"]
    thread = 940000000 + int(_rid(), 16) % 100000
    try:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("""INSERT INTO controller_state
                              (thread_id, tenant_id, org_id, phase, product, awaiting,
                               job_kind, job_status, updated_at, execution_scope)
                           VALUES (%s,%s,%s,'IMPLEMENT',NULL,'fleet','build',
                                   'building the first product', now(),'test')""",
                        (thread, tid, oid))
            c.commit()

        cp = cockpit.cockpit(tid, oid)
        assert cp["summary"]["products"] == 0
        assert cp["summary"]["building"] == 1
        assert cp["summary"]["in_flight_workstreams"] == 1
        assert cp["queue"]["active"] == 1
        assert cp["workstreams"][0]["thread_id"] == thread
        assert cp["workstreams"][0]["running"] is True
        assert cp["workstreams"][0]["can_cancel"] is True

        projects = projectsview.list_projects(tid, oid)
        assert len(projects) == 1
        assert projects[0]["provisional"] is True
        assert projects[0]["result"] == "building"
        assert projects[0]["thread_id"] == thread
        assert projects[0]["can_cancel"] is True

        live = livestatus.live_status(tid)
        mine = next((r for r in live if r.get("thread_id") == thread), None)
        assert mine and mine["provisional"] is True and mine["running"] is True

        obs = traceview.overview(tid, oid)
        assert obs["in_flight_workstreams"] == 1
        assert obs["recent_activity"][0]["workstream"] is True
        assert obs["recent_activity"][0]["thread_id"] == thread
        assert obs["recent_activity"][0]["can_cancel"] is True
        assert obs["workstreams"][0]["can_cancel"] is True

        chiefofstaff._cache_put(tid, oid, {"headline": "Team is ready and idle.",
                                           "needs_you": [], "team_did": [], "watch": [],
                                           "suggestion": "Start something."})
        brief = chiefofstaff.brief(tid, oid, use_cache=True)
        assert "active workstream" in brief["headline"]
        assert any("IMPLEMENT" in item for item in brief["team_did"])

        stopped = workstreamview.cancel_workstream(
            tid, oid, thread, reason="test stop from cockpit", who="pytest")
        assert stopped.get("cancelled") is True
        after = next(w for w in workstreamview.active_workstreams(tid, oid) if w["thread_id"] == thread)
        assert after["running"] is False
        assert after["can_cancel"] is False
    finally:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM kill_switch WHERE scope=%s", (f"thread-{thread}",))
            cur.execute("DELETE FROM brief_cache WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM controller_state WHERE thread_id=%s", (thread,))
            cur.execute("DELETE FROM orgs WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,))
            c.commit()


def test_dogfood_515_focused_verifier_proves_surfaces_before_resolve():
    """The #515 closeout needs a focused proof, not a broad persona run that may spend its slice before J4.
    The verifier recreates zero-product active-controller state and proves every status surface is non-idle."""
    import dogfood
    out = dogfood.verify_finding_515(use_http=False, record=False)
    assert out["ok"] is True
    checks = out["evidence"]["checks"]
    assert all(checks.values()), checks
    surfaces = out["evidence"]["surfaces"]
    assert surfaces["cockpit_summary"]["products"] == 0
    assert surfaces["cockpit_summary"]["in_flight_workstreams"] >= 1
    assert checks["cockpit_can_cancel"] is True
    assert surfaces["project_row"]["provisional"] is True
    assert surfaces["livestatus_row"]["running"] is True
    assert checks["projects_can_cancel"] is True
    assert surfaces["observability_activity"]["workstream"] is True
    assert checks["observability_can_cancel"] is True
    assert "active workstream" in surfaces["brief_headline"]


def test_dogfood_j5_verifier_proves_question_response_loop():
    """J5 requires a real CEO-awaiting item to open/read/respond, then clear durably."""
    import dogfood
    out = dogfood.verify_j5_review_respond(use_http=False)
    assert out["ok"] is True
    checks = out["evidence"]["checks"]
    assert all(checks.values()), checks
    surfaces = out["evidence"]["surfaces"]
    assert surfaces["question_item"]["kind"] == "question"
    assert surfaces["answer_result"]["status"] == "answered"
    assert surfaces["answered_row"]["status"] == "answered"


def test_research_non_answer_report_retries_before_options(monkeypatch, tmp_path):
    """A report that says it cannot answer the CEO's question must not become selectable strategy cards."""
    import time
    import research
    import billing
    import consent

    bad = tmp_path / "bad.md"
    bad.write_text("""# Report

The current findings are insufficient for dog-walking landing-page strategy.

## Positioning
Unknown.

## Trust Signals
Unknown.

No recommendation can be made from the current findings without inventing unsupported claims.
The findings are not materially relevant to the dog-walking question and require new public research.
""")
    good = tmp_path / "good.md"
    good.write_text("""# Dog-walking landing page research

Local dog-walking visitors need quick trust proof, service-area clarity, safety policies, reviews, and a
short booking enquiry form that asks for owner contact, dog details, walk frequency, schedule, address area,
temperament notes, and meet-and-greet availability.
""")
    calls = {"org": 0, "fleet": 0}
    monkeypatch.setattr(research.research_org, "run_research",
                        lambda *a, **k: calls.__setitem__("org", calls["org"] + 1) or {"report": str(bad)})
    monkeypatch.setattr(research.research_fleet, "research",
                        lambda *a, **k: calls.__setitem__("fleet", calls["fleet"] + 1) or {"report": str(good)})
    monkeypatch.setattr(research.factory, "agent", lambda *a, **k: {"rc": 0, "out": (
        "* OPT: Trust-first page :: Lead with safety, reviews, service area, and a short enquiry form.\n"
        "OPT: Pricing-first page :: Lead with transparent packages and qualify budget early.\n"
        "OPT: Minimal booking page :: Ship a compact form-led page first.")})
    reg = billing.signup(f"research-nonanswer-{_rid()}", "free")
    tid = reg["tenant_id"]
    consent.record(tid)
    rid = None
    try:
        rid = research.start(tid, "org-test", 1, "dog-walking landing page strategy")["run_id"]
        deadline = time.time() + 10
        st = research.run_state(tid, rid)
        while st["status"] not in ("done", "failed") and time.time() < deadline:
            time.sleep(0.2)
            st = research.run_state(tid, rid)
        assert st["status"] == "done", st
        assert calls == {"org": 1, "fleet": 1}
        assert len(st["options"]) == 3
        assert "Trust-first" in st["options"][0]["title"]
    finally:
        from dbpool import connection
        with connection() as c, c.cursor() as cur:
            if rid is not None:
                cur.execute("DELETE FROM research_options WHERE run_id=%s", (rid,))
                cur.execute("DELETE FROM research_runs WHERE id=%s", (rid,))
            cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,))


def test_qa_resolver_accepts_native_implicit_roles():
    """Dogfood actions often target native links/textboxes by role; implicit roles must resolve."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("qa_explorer_mod", ROOT / "scripts/qa/qa_explorer.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    elements = [
        {"idx": 3, "tag": "a", "text": "▤ Projects", "role": "", "href": "#projects"},
        {"idx": 25, "tag": "textarea", "text": "", "placeholder": "e.g. build a competitor to YouTube"},
    ]
    idx, label, score = mod._resolve_target({"target_text": "▤ Projects", "role": "link"}, elements)
    assert (idx, label, score) == (3, "▤ Projects", 3)
    idx, label, score = mod._resolve_target(
        {"target_text": "e.g. build a competitor to YouTube", "role": "textbox"}, elements)
    assert (idx, label, score) == (25, "e.g. build a competitor to YouTube", 3)


def test_dogfood_critical_readiness_bundles_j4_j5_no_model_verifiers():
    """Before spending on a full browser/model pass, the known J4/J5 contracts should be cheaply provable."""
    import dogfood
    out = dogfood.verify_critical_readiness(use_http=False)
    assert out["ok"] is True
    assert out["checks"]["j4_wait_watch_status_and_cancel"] is True
    assert out["checks"]["j5_review_respond_question_answer"] is True
    assert out["verifiers"]["j4"]["ok"] is True
    assert out["verifiers"]["j5"]["ok"] is True


def test_default_codex_cli_satisfies_first_run_provider_gate():
    """A self-hosted operator already signed into Codex is a usable default model path.

    The UI and controller must not force a first-time CEO through a manual Providers detour when the host's
    default Codex CLI is available; tenant-specific keys/subscriptions can still override it later.
    """
    import billing
    import tenantproviders
    import auth
    import loopcontroller
    from dbpool import connection

    real_cli = tenantproviders._cli_logged_in
    tenantproviders._cli_logged_in = lambda provider: {"ok": True} if provider == "openai" else {"error": "no"}
    tid = billing.signup(f"default-codex-{_rid()}", "free")["tenant_id"]
    try:
        providers = tenantproviders.list_providers(tid)
        openai = next(p for p in providers if p["slug"] == "openai")
        resolved = tenantproviders.resolve(tid)

        assert openai["connected"] is True
        assert openai["default_connected"] is True
        assert openai["auth_mode"] == "default_cli"
        assert resolved["engine"] == "codex"
        assert resolved["provider"] == "openai"
        assert resolved["auth_mode"] == "default_cli"
        assert auth.provider_resolved(tid) is True
        assert loopcontroller._resolved_provider(tid)["auth_mode"] == "default_cli"
    finally:
        tenantproviders._cli_logged_in = real_cli
        with connection() as c, c.cursor() as cur:
            cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,))


def test_findings_external_verification_still_uses_gated_resolve():
    """Deterministic browser/CI evidence may be stronger than another AI review, but it must still become an
    immutable verification artifact and pass through resolve(..., verification_id=...)."""
    import psycopg
    import findings
    from aoscfg import DB

    findings._ensure()
    source = f"external-verification-{_rid()}"
    try:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("""INSERT INTO findings (source, title, detail, severity, need_role, status)
                           VALUES (%s,'external evidence gate','detail','high','builder','open')
                           RETURNING id""", (source,))
            fid = cur.fetchone()[0]
            c.commit()

        failed = findings.record_verification(fid, "browser-smoke", False, {"url": "http://app"})
        refused = findings.resolve(fid, by="pytest", verification_id=failed["verification_id"])
        assert refused["resolved"] is False

        passed = findings.record_verification(fid, "browser-smoke", True, {"url": "http://app", "passed": True},
                                              by="pytest-browser")
        resolved = findings.resolve(fid, by="pytest", verification_id=passed["verification_id"])
        assert resolved["resolved"] is True
        assert resolved["verification_id"] == passed["verification_id"]
    finally:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("""DELETE FROM finding_verifications
                           WHERE finding_id IN (SELECT id FROM findings WHERE source=%s)""", (source,))
            cur.execute("DELETE FROM findings WHERE source=%s", (source,))
            c.commit()


def test_findings_file_reuses_upstream_idempotency_key(monkeypatch):
    """A resumed durable workflow must not route a second company task for one immutable finding."""
    import psycopg
    import findings
    from aoscfg import DB

    source = f"idempotent-finding-{_rid()}"
    monkeypatch.setattr(findings.orchestrate, "request_collaborator",
                        lambda *args, **kwargs: {"action": "no_role"})
    monkeypatch.setattr(findings.taskboard, "add", lambda *args, **kwargs: None)
    monkeypatch.setattr(findings.audit, "append", lambda *args, **kwargs: None)
    try:
        first = findings.file(source, "builder", "same defect", tenant_id="_platform",
                              dedupe_key="qaf-immutable")
        replay = findings.file(source, "builder", "same defect replay", tenant_id="_platform",
                               dedupe_key="qaf-immutable")
        assert replay["finding_id"] == first["finding_id"]
        assert replay["deduped"] is True and replay["routing"] == "deduped"
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("SELECT count(*) FROM findings WHERE source=%s", (source,))
            assert cur.fetchone()[0] == 1
        retired = findings.supersede_source(
            source, "durable QA campaign retains ownership", by="pytest", tenant_id="_platform")
        assert retired["count"] == 1
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("SELECT status, drop_reason FROM findings WHERE source=%s", (source,))
            assert cur.fetchone() == ("dropped", "durable QA campaign retains ownership")
    finally:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM findings WHERE source=%s", (source,))
            c.commit()


def test_byo_key_never_fails_over_to_platform_codex():
    """The load-bearing billing rule: a tenant paying with their OWN Anthropic key (api_key on _ctx, no full
    tenant record, no codex key) must NEVER fall over to platform-funded Codex — that would silently bill the
    platform for their outage. Only a platform run (no api_key) uses the host Codex. Previously unasserted."""
    import factory
    real_which, real_fb = factory.shutil.which, factory.FALLBACK_ENGINE
    save = (getattr(factory._ctx, "api_key", None), getattr(factory._ctx, "tenant", None),
            getattr(factory._ctx, "codex_key", None))
    try:
        factory.shutil.which = lambda n: "/usr/bin/codex"          # pretend the Codex CLI is present
        factory.FALLBACK_ENGINE = "codex"
        factory._ctx.codex_key = None
        factory._ctx.tenant = None
        factory._ctx.api_key = "sk-tenant-own"                     # BYO key, no tenant, no codex
        env, src = factory._codex_fallback_env()
        assert env is None and src == "byo-key-no-platform-failover", (env is None, src)
        factory._ctx.api_key = None                                # platform run -> host Codex is allowed
        env2, src2 = factory._codex_fallback_env()
        assert env2 is not None and src2 == "platform-codex", src2
    finally:
        factory.shutil.which, factory.FALLBACK_ENGINE = real_which, real_fb
        factory._ctx.api_key, factory._ctx.tenant, factory._ctx.codex_key = save


def test_company_tools_bill_the_tenant_via_agent_tool():
    """Every knowledge-work tool the living company org runs (research/finance/legal/data/artifact/design)
    goes through _agent_tool in a jobrunner thread with no inherited factory._ctx — so it must rebuild the
    tenant's provider from the tenant id jobrunner injects, or the whole org would bill the platform."""
    sys.path.insert(0, str(ROOT / "scripts" / "orchestra"))
    import tools, factory, tenantproviders
    real = tenantproviders.resolve, factory.agent
    seen = {}
    try:
        tenantproviders.resolve = lambda t: {"engine": "claude", "key": "sk-tenant-9"}
        factory.agent = lambda role, repo, prompt, **k: (
            seen.update(tenant=factory._ctx.tenant, key=factory._ctx.api_key) or
            {"rc": 0, "out": "r", "out_full": "r"})
        out = tools._agent_tool("researcher", "do research", {"tenant": "acme", "org": "2", "repo": "/tmp"})
        assert out["status"] == "done"
        assert seen["tenant"] == "acme" and seen["key"] == "sk-tenant-9"   # billed to the tenant, not platform
    finally:
        tenantproviders.resolve, factory.agent = real
        factory._ctx.tenant = factory._ctx.api_key = None


def test_research_subq_ctx_rebuild_bills_the_tenant():
    """BILLING correctness for the run_org research path: research_subq runs in a jobrunner thread that does
    NOT inherit factory._ctx, so it must rebuild the provider from the tenant id (never a persisted key) so
    spend lands on THEIR account. Platform -> host subscription (no key); a claude tenant -> their key; a codex
    tenant -> engine codex. This is the single most important invariant of the rewire."""
    sys.path.insert(0, str(ROOT / "scripts" / "orchestra"))
    import tools, factory, tenantproviders
    real = tenantproviders.resolve
    try:
        assert tools._apply_tenant_ctx("platform", None) is None          # platform -> configured host engine
        assert factory._ctx.tenant is None and factory._ctx.api_key is None
        assert factory._ctx.engine == factory.DEFAULT_ENGINE
        tenantproviders.resolve = lambda t: {"engine": "claude", "key": "sk-tenant-abc"}
        k = tools._apply_tenant_ctx("acme", "3", product="dog-app")       # provider + product authority
        assert k == "sk-tenant-abc" and factory._ctx.api_key == "sk-tenant-abc"
        assert factory._ctx.engine == "claude" and factory._ctx.tenant == "acme" and factory._ctx.org == "3"
        assert factory._ctx.product == "dog-app" and factory._ctx.stage == "QA"
        tenantproviders.resolve = lambda t: {"engine": "codex", "key": "cdx-xyz"}
        k2 = tools._apply_tenant_ctx("beta", None)                        # codex -> engine codex, no claude key
        assert k2 is None and factory._ctx.engine == "codex" and factory._ctx.codex_key == "cdx-xyz"
        assert factory._ctx.api_key is None
    finally:
        tenantproviders.resolve = real
        factory._ctx.tenant = factory._ctx.api_key = factory._ctx.codex_key = factory._ctx.product = None
        factory._ctx.engine = "claude"


def test_qa_model_failure_never_becomes_repeated_noop(monkeypatch):
    """An unavailable provider is resumable infra work, never a fabricated browser action/stall."""
    from qa import qa_explorer

    class Bridge:
        def __init__(self):
            self.actions = []
        def state(self):
            return {"url": "http://app", "title": "app", "bodyText": "count 0 Increment",
                    "elements": [{"idx": 0, "label": "Increment", "role": "button"}]}
        def act(self, action):
            self.actions.append(action)
            return {"ok": True}

    monkeypatch.setattr(qa_explorer, "_call_agent", lambda *_a, **_k: {
        "rc": 1, "failed": True, "out": "provider authentication failed"})
    explorer = qa_explorer.Explorer("http://app", "vision", autostart=False)
    explorer.bridge = Bridge()
    explorer._ai_coverage_plan = lambda *_a, **_k: [{"aspect": "one click increments", "covered": False}]
    explorer._checkpoint = lambda *_a, **_k: None
    records = explorer.explore({"title": "increment", "expected": "count is one"}, max_steps=8)
    assert records == [] and explorer.bridge.actions == []
    assert explorer.stop_reason == "model-infrastructure-incomplete"
    assert "authentication failed" in explorer.infrastructure_error


def test_research_via_org_durable_and_crash_resumable():
    """Band-2 rewire: research runs as a crash-resumable run_org org (research-coordinator -> dispatch-and-
    parked research_subq workers), preserves the REPORT.md output contract, and reconcile re-dispatches a
    worker whose job died — losslessly. Delegates to the module's offline proof (stubbed agent, real DB)."""
    sys.path.insert(0, str(ROOT / "scripts" / "orchestra"))
    import research_org
    assert research_org._selftest_via_org() == 0


def test_watchdog_pages_out_of_band_when_db_down():
    """The DB-outage blind spot: if Postgres itself is down, tick() must NOT throw (which would mute the
    pager) — it pages out-of-band via notify and returns db_down. The one failure that blinds the whole plane
    can't also silence the alert. Restores globals so it can't disturb other tests."""
    import watchdog
    real_db, real_send, real_mark = watchdog.DB, watchdog.notify.send, watchdog._DB_DOWN_MARK
    import pathlib, tempfile
    sent = []
    watchdog._DB_DOWN_MARK = pathlib.Path(tempfile.gettempdir()) / f"wd-dbdown-{_rid()}.mark"
    watchdog.DB = "postgresql://nouser@127.0.0.1:5/ nodb"      # unreachable
    watchdog.notify.send = lambda *a, **k: sent.append(k.get("priority"))
    try:
        assert watchdog._db_reachable() is False
        r = watchdog.tick()                                    # must return, not raise
        assert r.get("db_down") is True and "urgent" in sent
    finally:
        watchdog.DB, watchdog.notify.send = real_db, real_send
        try:
            watchdog._DB_DOWN_MARK.unlink()
        except Exception:
            pass
        watchdog._DB_DOWN_MARK = real_mark


def test_watchdog_does_not_claim_failed_page_was_sent(monkeypatch):
    """A failed ntfy call must stay retryable; last_sent is reserved for transport acceptance."""
    import psycopg
    import watchdog
    from aoscfg import DB
    sig = f"test-delivery-{_rid()}"
    attempts = []
    monkeypatch.setattr(watchdog, "_db_reachable", lambda: True)
    monkeypatch.setattr(watchdog, "beat", lambda *a, **k: None)
    monkeypatch.setattr(watchdog, "check", lambda: [{"sig": sig, "level": "warn", "msg": "test page"}])
    monkeypatch.setattr(watchdog.responder, "remediate", lambda *_: None)
    monkeypatch.setattr(watchdog.notify, "send", lambda *a, **k: attempts.append((a, k)) or False)
    try:
        one = watchdog.tick(auto_heal=False)
        two = watchdog.tick(auto_heal=False)
        test_pages = [a for a in attempts if a[0] and "test page" in str(a[0][0])]
        assert len(test_pages) == 2
        assert one["paged"] == [] and one["delivery_failed"] == ["test page"]
        assert two["delivery_failed"] == ["test page"]
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("""SELECT last_sent IS NULL,last_attempt IS NOT NULL,delivery_status
                           FROM watchdog_alerts WHERE signature=%s""", (sig,))
            assert cur.fetchone() == (True, True, "failed")
    finally:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM watchdog_alerts WHERE signature=%s", (sig,)); c.commit()


def test_dashboard_surfaces_scheduler_failures_and_overdue_jobs():
    """Scheduler telemetry is only useful if the monitor reads it. Repeated failed jobs and enabled jobs left
    overdue must become dashboard/watchdog alerts instead of silently degrading recovery loops."""
    import psycopg
    import dashboard
    import scheduler
    from aoscfg import DB
    suf = _rid()
    fail_name = f"sched-fail-{suf}"
    recovered_name = f"sched-recovered-{suf}"
    late_name = f"sched-late-{suf}"
    scheduler._ensure()
    try:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("""INSERT INTO scheduler_runs (name, decision, rc, duration_ms, detail, at)
	                           VALUES (%s,'timeout',NULL,120000,'pytest timeout',now()-interval '5 minutes'),
	                                  (%s,'timeout',NULL,120000,'pytest timeout',now()-interval '4 minutes')""",
	                        (fail_name, fail_name))
            cur.execute("""INSERT INTO scheduler_runs (name, decision, rc, duration_ms, detail, at)
	                           VALUES (%s,'timeout',NULL,120000,'pytest timeout',now()-interval '5 minutes'),
	                                  (%s,'timeout',NULL,120000,'pytest timeout',now()-interval '4 minutes'),
	                                  (%s,'executed',0,1000,'pytest recovered',now()-interval '3 minutes')""",
	                        (recovered_name, recovered_name, recovered_name))
            cur.execute("""INSERT INTO schedules (name, command, interval_s, enabled, next_run)
                           VALUES (%s, %s, 300, true, now()-interval '30 minutes')""",
                        (late_name, f"{scheduler.VENV_PY} -c pass"))
            c.commit()
            # Keep the real ticker from claiming this production-shaped fixture between
            # commit and observation.  Its SKIP LOCKED claim path leaves this row alone,
            # while the dashboard's ordinary read can still see the committed overdue row.
            cur.execute("SELECT name FROM schedules WHERE name=%s FOR UPDATE", (late_name,))
            issues = dashboard._scheduler_issues(fail_window_min=60)
            msgs = [i["msg"] for i in issues]
            assert any(f"scheduler job {fail_name} timeout x2" in m for m in msgs), issues
            assert any(f"scheduler job {late_name} overdue" in m for m in msgs), issues
            assert any(i["level"] == "crit" and fail_name in i["msg"] for i in issues), issues
            assert not any(recovered_name in m for m in msgs), issues
    finally:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM scheduler_runs WHERE name = ANY(%s)", ([fail_name, recovered_name, late_name],))
            cur.execute("DELETE FROM schedules WHERE name = ANY(%s)", ([fail_name, recovered_name, late_name],))
            c.commit()


def test_scheduler_runs_due_jobs_concurrently_and_retries_failures(monkeypatch):
    """One poison schedule cannot serialize every recovery loop; failures retry sooner than normal cadence."""
    import threading
    import time
    import psycopg
    import scheduler
    from aoscfg import DB

    suffix = _rid()
    names = [f"sched-par-a-{suffix}", f"sched-par-b-{suffix}"]
    active = peak = 0
    lock = threading.Lock()

    def fake_execute(name, command, claim_token=None):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.15)
        with lock:
            active -= 1
        decision = "timeout" if name == names[0] else "executed"
        return {"name": name, "decision": decision, "rc": None if decision == "timeout" else 0,
                "detail": "pytest", "duration_ms": 150, "claim_token": claim_token}

    scheduler._ensure()
    monkeypatch.setattr(scheduler, "MAX_PARALLEL", 2)
    monkeypatch.setattr(scheduler, "_execute_due_job", fake_execute)
    try:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            for name in names:
                cur.execute("""INSERT INTO schedules
                                  (name,command,interval_s,enabled,manual_only,next_run)
                               VALUES (%s,%s,3600,true,true,now())""",
                            (name, f"{scheduler.VENV_PY} -c pass"))
        started = time.monotonic()
        assert scheduler.tick(names) == 1
        assert time.monotonic() - started < 1.0             # bounded DB bookkeeping; overlap is proven by peak
        assert peak == 2
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("SELECT failure_count, EXTRACT(EPOCH FROM next_run-now()) FROM schedules WHERE name=%s",
                        (names[0],))
            failures, retry_s = cur.fetchone()
            assert failures == 1 and 0 < retry_s <= scheduler.RETRY_BASE_S + 2
            cur.execute("SELECT failure_count, last_error FROM schedules WHERE name=%s", (names[1],))
            assert cur.fetchone() == (0, None)
    finally:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM scheduler_runs WHERE name = ANY(%s)", (names,))
            cur.execute("DELETE FROM schedules WHERE name = ANY(%s)", (names,))


def test_scheduler_driver_cadence_and_stale_ticker_recovery_are_wired():
    ticker = (ROOT / "scripts" / "ticker.sh").read_text()
    recover = (ROOT / "scripts" / "recover.sh").read_text()
    import responder
    assert 'INTERVAL="${TICKER_INTERVAL:-60}"' in ticker
    assert "REMAINING=$(( INTERVAL - ELAPSED ))" in ticker
    assert "service_recovery.py\" repair watchdog" in ticker
    assert "WSL boot auto-recovery" in recover
    assert responder.classify({"sig": "heartbeat:ticker", "msg": "ticker heartbeat stale"}) == "auto"


def test_ceo_run_preflight_blocks_before_live_submit_when_provider_missing():
    """The one-shot live proof is expensive; it must have a no-spend gate before it starts a controller thread
    or tells the CEO work is building. Missing provider should block in preflight."""
    import ceo_run
    helpers = {name: getattr(ceo_run, name) for name in (
        "_db_ok", "_tenant_exists", "_consent_ok", "_provider_ok", "_billing_snapshot",
        "_build_budget_ok", "_critical_ops_alerts", "_watchdog_critical_issues",
        "_process_count", "_halted")}
    try:
        ceo_run._db_ok = lambda: True
        ceo_run._tenant_exists = lambda tenant: tenant == "acme"
        ceo_run._consent_ok = lambda tenant: True
        ceo_run._provider_ok = lambda tenant: False
        ceo_run._billing_snapshot = lambda tenant: {"ok": True, "plan": "free"}
        ceo_run._build_budget_ok = lambda: True
        ceo_run._critical_ops_alerts = lambda: []
        ceo_run._watchdog_critical_issues = lambda: []
        ceo_run._process_count = lambda pattern: 1
        ceo_run._halted = lambda scope: False
        blocked = ceo_run.preflight("ship a booking product", tenant="acme", org=1)
        assert blocked["ok"] is False
        assert blocked["checks"]["model_provider_resolved"] is False

        ceo_run._provider_ok = lambda tenant: True
        ceo_run._billing_snapshot = lambda tenant: {"ok": False, "reason": "over_quota_no_overage_plan"}
        quota_blocked = ceo_run.preflight("ship a booking product", tenant="acme", org=1)
        assert quota_blocked["ok"] is False
        assert quota_blocked["checks"]["billing_quota_ready"] is False
        assert quota_blocked["billing"]["reason"] == "over_quota_no_overage_plan"

        ceo_run._billing_snapshot = lambda tenant: {"ok": True, "plan": "free"}
        ceo_run._critical_ops_alerts = lambda: [{"level": "crit", "msg": "scheduler job proactive-comms timeout x2"}]
        ops_blocked = ceo_run.preflight("ship a booking product", tenant="acme", org=1)
        assert ops_blocked["ok"] is False
        assert ops_blocked["checks"]["no_critical_ops_alerts"] is False
        assert ops_blocked["critical_ops_alerts"][0]["level"] == "crit"

        ceo_run._critical_ops_alerts = lambda: []
        ceo_run._watchdog_critical_issues = lambda: [{"level": "crit", "msg": "cockpit-web process is DOWN"}]
        watchdog_blocked = ceo_run.preflight("ship a booking product", tenant="acme", org=1)
        assert watchdog_blocked["ok"] is False
        assert watchdog_blocked["checks"]["no_watchdog_critical_issues"] is False
        assert watchdog_blocked["watchdog_critical_issues"][0]["level"] == "crit"

        ceo_run._watchdog_critical_issues = lambda: []
        ok = ceo_run.preflight("ship a booking product", tenant="acme", org=1)
        assert ok["ok"] is True
        assert "controller_state" in " ".join(ok["artifacts"])
        assert "stop_conditions" in ok and ok["command"]
    finally:
        for name, fn in helpers.items():
            setattr(ceo_run, name, fn)


def test_qa_auditor_gate_fails_closed_when_audit_unavailable():
    """The skeptical auditor is the anti-rubber-stamp control. If it was supposed to run but CRASHED, a run
    that the coverage tally thought passed must be downgraded to not-passed (an unverifiable run does not
    ship) — but a run that already failed stays failed, and the passing case is only ever downgraded, never
    upgraded. Locks the fail-CLOSED contract (previously the exception path left 'passed' untouched)."""
    sys.path.insert(0, str(ROOT / "scripts" / "qa"))
    import qa_run
    would_pass = {"passed": True, "verdict": "ALL STORIES PASSED"}
    assert qa_run._audit_unavailable(would_pass, RuntimeError("auditor boom")) is True
    assert would_pass["passed"] is False and "AUDIT UNAVAILABLE" in would_pass["verdict"]
    assert would_pass["audit_error"] == "auditor boom"
    already_failed = {"passed": False, "verdict": "2 blocking bugs open"}
    assert qa_run._audit_unavailable(already_failed, RuntimeError("boom")) is False
    assert already_failed["passed"] is False and already_failed["verdict"] == "2 blocking bugs open"


def test_reap_dead_parked_worker_by_pid_before_floor():
    """A parked worker whose process is provably gone must be reaped IMMEDIATELY via its pid — not left
    pinning the build on 'fleet' for up to the 20-min floor. A young, freshly-BEATING job with a LIVE pid
    must NOT be reaped (that's a healthy build). Closes the early-worker-death invisible-strand gap."""
    import os, subprocess, psycopg
    import loopcontroller as lc
    lc._ensure()
    dead = subprocess.Popen(["true"]); dead.wait()            # a pid that is now definitively gone
    assert lc._pid_alive(dead.pid) is False
    ins = []
    try:
        with psycopg.connect(lc.DB) as c, c.cursor() as cur:
            for pid, tag in ((dead.pid, "dead"), (os.getpid(), "live")):
                cur.execute("""INSERT INTO controller_jobs (thread_id, tenant_id, phase, kind, status,
                                 started_at, heartbeat_at, worker_pid, execution_scope)
                               VALUES (0,'pidreap-selftest','IMPLEMENT','build','running',
                                       now(), now(), %s, 'test') RETURNING id""", (pid,))   # young + beating NOW
                ins.append((cur.fetchone()[0], tag))
            c.commit()
        lc._reap_dead_jobs(execution_scope="test")
        with psycopg.connect(lc.DB) as c, c.cursor() as cur:
            got = {}
            for jid, tag in ins:
                cur.execute("SELECT status, result FROM controller_jobs WHERE id=%s", (jid,))
                got[tag] = cur.fetchone()
        assert got["dead"][0] == "failed" and got["dead"][1].get("crashed") is True, "dead-pid worker must reap now"
        assert got["live"][0] == "running", "a live, freshly-beating worker must NOT be reaped"
    finally:
        with psycopg.connect(lc.DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM controller_jobs WHERE tenant_id='pidreap-selftest'"); c.commit()


def test_release_stale_claims_frees_abandoned_events():
    """A live company-org run exposed this: run_org's pool claims an event then abandons it when the pool
    stalls out, and the 900s claim lease meant the org hung ~15 min. run_org fully joins its pool before
    returning, so at the next re-entry any claimed-but-unprocessed event is orphaned — release_stale_claims
    frees it (guarded by an age threshold), so the org self-heals in seconds instead of waiting out the lease."""
    import psycopg
    sys.path.insert(0, str(ROOT / "scripts" / "orchestra"))
    import store
    if not store.DB:
        pytest.skip("no DB")
    r = store.start_run("t-stale", f"stale-{_rid()}")
    run_id = r["run_id"]
    try:
        a = store.spawn_actor(run_id, "t-stale", "w", "engineer")
        store.emit(run_id, "t-stale", None, a["actor_id"], "task", {"task": "x"})
        claimed = store.claim_events(a["actor_id"], "t-stale")     # simulate a pool claiming it
        assert claimed
        assert store.release_stale_claims(run_id, "t-stale", older_than_s=100) == 0   # too fresh -> kept
        freed = store.release_stale_claims(run_id, "t-stale", older_than_s=0)          # orphaned -> freed
        assert freed == 1
        again = store.claim_events(a["actor_id"], "t-stale")       # now re-claimable immediately
        assert again and again[0]["kind"] == "task"
    finally:
        with psycopg.connect(store.DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM orchestra_events WHERE run_id=%s", (run_id,))
            cur.execute("DELETE FROM orchestra_actors WHERE run_id=%s", (run_id,))
            cur.execute("DELETE FROM orchestra_runs WHERE run_id=%s", (run_id,))
            c.commit()


def test_release_stale_claims_clears_only_dead_runtime_actor_owner():
    """A controller replacement must not wait 15 minutes on the dead pool's actor-step fence."""
    import psycopg
    sys.path.insert(0, str(ROOT / "scripts" / "orchestra"))
    import store
    if not store.DB:
        pytest.skip("no DB")
    r = store.start_run("t-stale-actor", f"stale-actor-{_rid()}")
    run_id = r["run_id"]
    dead_owner = "987654321-dead-runtime:orgw-0"
    live_owner = f"{os.getpid()}-live-runtime:orgw-0"
    try:
        dead = store.spawn_actor(run_id, "t-stale-actor", "dead-owner", "engineer")
        live = store.spawn_actor(run_id, "t-stale-actor", "live-owner", "engineer")
        assert store.claim_actor_step(dead["actor_id"], "t-stale-actor", claimed_by=dead_owner)
        assert store.claim_actor_step(live["actor_id"], "t-stale-actor", claimed_by=live_owner)

        store.release_stale_claims(run_id, "t-stale-actor", older_than_s=0)

        with psycopg.connect(store.DB) as c, c.cursor() as cur:
            cur.execute("""SELECT actor_id,step_claimed_by FROM orchestra_actors
                           WHERE actor_id IN (%s,%s) ORDER BY actor_id""",
                        (dead["actor_id"], live["actor_id"]))
            claims = dict(cur.fetchall())
        assert claims[dead["actor_id"]] is None
        assert claims[live["actor_id"]] == live_owner
    finally:
        with psycopg.connect(store.DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM orchestra_events WHERE run_id=%s", (run_id,))
            cur.execute("DELETE FROM orchestra_actors WHERE run_id=%s", (run_id,))
            cur.execute("DELETE FROM orchestra_runs WHERE run_id=%s", (run_id,))
            c.commit()


def test_orchestra_persist_step_is_atomic_and_validating():
    """One decide-step's actor-update + emits + event-completion must land together (store.persist_step),
    so a crash can't strand a parent with a committed 'done' status whose 'done' emit never fired. Proves:
    the happy path applies all three; a bad event kind is rejected as a unit (nothing partially written);
    and a halted org still drains claimed events but emits no new work."""
    import psycopg
    sys.path.insert(0, str(ROOT / "scripts" / "orchestra"))
    import store, killswitch
    if not store.DB:
        pytest.skip("no DB")
    r = store.start_run("t-atomic", f"atomic-{_rid()}")
    run_id = r["run_id"]
    try:
        a = store.spawn_actor(run_id, "t-atomic", "worker-a", "engineer")
        sup = store.spawn_actor(run_id, "t-atomic", "sup", "supervisor")
        aid, sid = a["actor_id"], sup["actor_id"]
        # give the worker an event to complete in the same step
        ev = store.emit(run_id, "t-atomic", sid, aid, "task", {"task": "x"})
        claimed = store.claim_events(aid, "t-atomic")
        # happy path: mark done + emit 'done' up + complete the task event, all at once
        res = store.persist_step(run_id, "t-atomic", aid, status="done",
                                 emits=[(aid, sid, "done", {"result": "ok"}, None)],
                                 complete_ids=[e["id"] for e in claimed])
        assert res["ok"] and res["emitted"] == 1 and res["completed"] == 1
        assert store.actor(aid, "t-atomic")["status"] == "done"
        assert store.pending_count(aid, "t-atomic") == 0                 # task event consumed
        assert any(e["kind"] == "done" for e in store.events(run_id, "t-atomic"))  # 'done' reached the bus
        # validation: an unknown kind rejects the WHOLE step (no partial write)
        bad = store.persist_step(run_id, "t-atomic", sid, status="working",
                                 emits=[(sid, aid, "not_a_real_kind", {}, None)])
        assert "error" in bad
        assert store.actor(sid, "t-atomic")["status"] != "working"       # status change did NOT apply
        # halt: the whole durable unit is deferred; suppressing only the emit would lose the parent hand-off
        ev2 = store.emit(run_id, "t-atomic", sid, aid, "task", {"task": "y"})
        claimed2 = store.claim_events(aid, "t-atomic")
        killswitch.halt(reason="test-atomic")             # scope defaults to 'global' (store checks 'orchestra')
        try:
            res2 = store.persist_step(run_id, "t-atomic", aid,
                                      emits=[(aid, sid, "finding", {"bug": "z"}, None)],
                                      complete_ids=[e["id"] for e in claimed2])
        finally:
            killswitch.resume()
        assert res2.get("error") and res2["halted"]
        assert store.pending_count(aid, "t-atomic") == 1
        assert not any(e["kind"] == "finding" and (e["payload"] or {}).get("bug") == "z"
                       for e in store.events(run_id, "t-atomic"))
    finally:
        with psycopg.connect(store.DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM orchestra_events WHERE run_id=%s", (run_id,))
            cur.execute("DELETE FROM orchestra_actors WHERE run_id=%s", (run_id,))
            cur.execute("DELETE FROM orchestra_runs WHERE run_id=%s", (run_id,))
            c.commit()


def test_g1_retired_parked_worker_survives_driver_crash():
    """The core dispatch-and-park guarantee: a driver process that hard-CRASHES (SIGKILL) right after
    dispatching a parked phase does NOT take the worker down — the detached worker finishes on its own.
    This is what retires G1. Reuses the proven crash harness (spawns a real driver + detached worker, no
    claude); generous deadlines keep it non-flaky in CI."""
    import loopcontroller as lc
    assert lc._park_crash_selftest() == 0, "parked worker must survive a driver crash (see printed reason)"


def test_ceo_run_and_replybridge_close_the_e2e_loop():
    """The real e2e loop: ceo_run.submit starts a durable thread + says the prompt (jobd drives, not this
    process); replybridge routes a phone reply for ceo-<id> into loopcontroller.say. Delegates to the modules'
    own offline selftests (stubbed say/state/notify)."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import ceo_run, notify, replybridge
    real_notify_send = notify.send
    assert ceo_run._selftest() == 0
    assert notify.send is real_notify_send, "ceo_run selftest must restore the process-wide pager"
    assert replybridge._selftest() == 0


def test_qa_never_passes_a_ui_target_without_a_real_interface():
    """THE false-pass this whole change closes: a browser/UI product graded by a code-only path (no browser,
    no screenshots) must NOT be reported passed — even if every AI-declared story says 'passed'. The invariant
    lives in qa_report._tally so EVERY QA path (browser or independent) is bound by it. Non-UI targets (lib)
    are unaffected: code-graded verification is legitimate for them."""
    sys.path.insert(0, str(ROOT / "scripts" / "qa"))
    import qa_report as qr
    ui_no_browser = {"product": "x", "requires_interface": True, "bugs": [], "stories": [
        {"id": "US-1", "status": "passed", "steps": [{"verdict": "match"}]},
        {"id": "US-2", "status": "passed", "steps": [{"verdict": "match"}]}]}
    t = qr._tally(ui_no_browser)
    assert t["passed"] is False, "a UI graded without a browser must never pass"
    assert "NOT VERIFIED" in qr._verdict_line(t)
    # same stories, but WITH real browser evidence (a screenshot on an executed step) -> may pass
    ui_browser = {"product": "x", "requires_interface": True, "bugs": [], "stories": [
        {"id": "US-1", "status": "passed", "steps": [{"verdict": "match", "screenshot": "/e/US-1.png"}]}]}
    assert qr._tally(ui_browser)["passed"] is True
    # a plain library (no interface required) is unaffected — code QA is a real pass for it
    lib = {"product": "x", "bugs": [], "stories": [{"id": "US-1", "status": "passed",
                                                    "steps": [{"verdict": "match"}]}]}
    assert qr._tally(lib)["passed"] is True


def test_independent_qa_flags_a_misrouted_ui_product():
    """factory.run_independent_qa is the code-only grader. If a UI product is MISROUTED to it (kind mis-detected
    / no servable URL), it must declare requires_interface so qa_report refuses the pass. _is_ui_target is the
    seam; assert it classifies the browser kinds and not the code kinds."""
    import factory
    for k in ("web", "spa", "game-web", "pwa", "browser-game"):
        assert factory._is_ui_target(k), k
    for k in ("lib", "service", "api", "cli", "python", ""):
        assert not factory._is_ui_target(k), k


def test_detect_kind_recognizes_a_web_app_without_root_index_html():
    """_detect_kind mislabelling a bundler/SPA as 'lib' was what routed a web app to the browserless grader.
    A package.json with a frontend framework (or a build/dev script) is a web app, even with no root
    index.html; a plain python project stays 'lib'."""
    import json as _json
    import factory
    web = ROOT / "scripts"  # dummy; use a temp dir instead
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        r = Path(d)
        (r / "package.json").write_text(_json.dumps({"dependencies": {"react": "^18"}}))
        assert factory._detect_kind(r) == "web"
    with tempfile.TemporaryDirectory() as d:
        r = Path(d)
        (r / "src").mkdir()
        (r / "src" / "app.py").write_text("x = 1\n")
        assert factory._detect_kind(r) == "lib"


def test_project_builder_targets_the_right_stack():
    """The hierarchical builder must build in the RIGHT language for the target, not always Python. stack_for
    maps a web platform to the web (ES-module + node-test) descriptor and everything else to Python; the
    descriptors' prompt fragments format cleanly (no leaked Python for web)."""
    import project as pj
    assert pj.stack_for("web")["id"] == "web"
    assert pj.stack_for("game-web")["id"] == "web"
    assert pj.stack_for("cli")["id"] == "python"
    assert pj.stack_for(None)["id"] == "python"          # unknown/absent -> safe Python default
    web = pj.stack_for("web")
    comp = web["component"].format(pkg="ledger")
    assert "index.js" in comp and "python" not in comp.lower()
    assert "node --test" in web["fix_cmd"].format(pkg="ledger")
    # python stack is byte-for-byte the old behaviour
    py = pj.stack_for("python")
    assert "python -m pytest" in py["fix_cmd"].format(pkg="ledger")
    assert "__init__.py" in py["component"].format(pkg="ledger")


def test_run_tests_routes_web_stack_to_node_runner():
    """factory.run_tests(stack='web') must grade with the node runner even for a per-component target, so a
    web component's *.test.js aren't force-run through pytest (which would error / falsely fail)."""
    import factory
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        r = Path(d)
        (r / "tests").mkdir()
        # a web target with NO test files -> the node runner reports the honest 'NO functional tests' fail,
        # proving it routed to node (pytest would instead say 'no tests ran'/collected 0).
        ok, out = factory.run_tests(str(r), target="tests/ledger", stack="web")
        assert ok is False
        assert "functional tests" in out.lower() or "test.js" in out.lower(), out[:200]


def test_agentic_web_qa_unwraps_gate_report(monkeypatch):
    """qa_agentic returns an envelope; factory must gate on envelope.report, not top-level missing fields."""
    import factory
    sys.path.insert(0, str(ROOT / "scripts" / "qa"))
    import qa_run

    def fake_qa_run(*args, **kwargs):
        return {"run_id": 123, "status": "done", "report": {
            "passed": True, "total_stories": 10, "blocking_open": 0,
            "verdict": "AGENTIC QA - ALL CLEAR", "verdict_json": "/tmp/verdict.json",
            "md": "/tmp/report.md"}}

    monkeypatch.setattr(qa_run, "qa_run", fake_qa_run)
    out = factory.run_agentic_web_qa(str(ROOT), "p", "vision", "summary", target_url="http://127.0.0.1:1")
    assert out["passed"] is True
    assert out["stories"] == 10
    assert out["blocking_open"] == 0
    assert factory.qa_verdict_ok(out)


def test_blocked_at_verify_does_not_advance_build_boundary():
    """A build result of BLOCKED_AT_VERIFY is a hold/failure, even when it produced files on disk."""
    import loopcontroller as lc
    assert lc._build_result_ok({"result": "BLOCKED_AT_VERIFY"}) is False
    assert lc._build_result_ok({"result": "error"}) is False
    assert lc._build_result_ok({"passed": False, "result": "BLOCKED_AT_VERIFY"}) is False
    assert lc._build_result_ok({"passed": True}) is True
    assert lc._build_result_ok({"result": "SUCCESS"}) is True
    assert lc._build_result_ok({"result": "INTEGRATED"}) is True


def test_verify_uses_the_declared_product_stack(monkeypatch, tmp_path):
    """The scalable verifier must run the web baseline through the web/node test path, not default pytest."""
    import factory
    import verify

    prod = "verify-stack-web"
    root = tmp_path / "products"
    repo = root / prod
    repo.mkdir(parents=True)
    monkeypatch.setattr(factory, "PRODUCTS", root)

    seen = {}

    def fake_run_tests(repo_arg, *args, **kwargs):
        seen["stack"] = kwargs.get("stack")
        return True, "ok"

    monkeypatch.setattr(factory, "run_tests", fake_run_tests)
    out = verify.verify(prod, rigor=1, stack="web")
    assert out["passed"] is True
    assert seen["stack"] == "web"


def test_verify_adversarial_uses_node_tests_for_web_stack(monkeypatch, tmp_path):
    """High-rigor web verification should create/run JS adversarial tests, not Python tests."""
    import factory
    import verify

    repo = tmp_path / "web-prod"
    (repo / "tests" / "adversarial").mkdir(parents=True)
    prompts = []
    runs = []

    def fake_agent(role, repo_arg, prompt, **kwargs):
        prompts.append(prompt)
        idx = len(prompts) - 1
        (Path(repo_arg) / "tests" / "adversarial" / f"adv_{idx}.test.js").write_text(
            "import test from 'node:test';\nimport assert from 'node:assert/strict';\n"
            "test('edge', () => assert.equal(1, 1));\n"
        )
        return {"rc": 0, "out": "ok", "out_full": "ok"}

    def fake_run_tests(repo_arg, *args, **kwargs):
        runs.append(kwargs)
        return True, "ok"

    monkeypatch.setattr(factory, "agent", fake_agent)
    monkeypatch.setattr(factory, "run_tests", fake_run_tests)
    ok, detail = verify.adversarial(repo, 2, stack="web")
    assert ok is True, detail
    assert all("adv_" in prompt and ".test.js" in prompt for prompt in prompts)
    assert runs and runs[-1]["target"] == "tests/adversarial"
    assert runs[-1]["stack"] == "web"


def test_agentic_qa_workers_scale_with_story_count(monkeypatch):
    sys.path.insert(0, str(ROOT / "scripts" / "qa"))
    import qa_agentic

    monkeypatch.delenv("AOS_QA_AGENTIC_WORKERS", raising=False)
    monkeypatch.setenv("AOS_QA_AGENTIC_WORKER_CAP", "8")
    assert qa_agentic._planned_workers(0) == 1
    assert qa_agentic._planned_workers(5) == 2
    assert qa_agentic._planned_workers(90) == 8
    assert qa_agentic._planned_workers(90, requested=3) == 3


def test_run_org_deadline_does_not_wait_for_blocked_pool_steps(monkeypatch):
    """A model call inside a pool thread must not turn QA's wall-clock budget into an advisory limit."""
    import time
    sys.path.insert(0, str(ROOT / "scripts" / "orchestra"))
    import runtime
    import jobrunner

    monkeypatch.setattr(runtime.store, "ensure", lambda: None)
    monkeypatch.setattr(runtime.store, "release_stale_claims", lambda *a, **k: 0)
    monkeypatch.setattr(runtime.store, "run", lambda *a, **k: {"status": "running"})
    monkeypatch.setattr(jobrunner, "reconcile_parked", lambda *a, **k: 0)
    monkeypatch.setattr(runtime, "_pool_loop", lambda *a, **k: time.sleep(0.35))

    started = time.time()
    out = runtime.run_org(1, "deadline-test", workers=2, deadline=started + 0.05)
    assert time.time() - started < 0.2
    assert out["deadline_exceeded"] is True and out["threads_alive"] == 2


def test_agentic_qa_honors_explicit_story_step_cap_without_a_default_quality_cap(monkeypatch):
    """Explicit envelopes propagate, while the live default relies on deadline/pressure/checkpoint guards."""
    sys.path.insert(0, str(ROOT / "scripts" / "qa"))
    import qa_run
    import qa_agentic

    seen = {}

    def fake_run_agentic_qa(*args, **kwargs):
        seen["max_steps"] = kwargs.get("max_steps")
        seen["story_limit"] = kwargs.get("story_limit")
        return {"report": {"passed": False}}

    monkeypatch.setattr(qa_agentic, "run_agentic_qa", fake_run_agentic_qa)
    qa_run.qa_run("http://app", "vision", None, "0", "summary", agentic=True, max_steps=7)
    assert seen["max_steps"] == 7
    assert seen["story_limit"] == qa_run.MAX_STORIES == 12
    assert qa_run.MAX_ROUNDS == 3 and qa_run.MAX_STEPS is None


def test_qa_coverage_ledger_is_bounded_without_dropping_requirements():
    sys.path.insert(0, str(ROOT / "scripts" / "qa"))
    import qa_explorer
    source = [f"check requirement {i}" for i in range(10)]
    bounded = qa_explorer._bounded_aspects(source, cap=6)
    assert len(bounded) == 6
    assert all(item in " ".join(bounded) for item in source)
    assert source[0] in bounded[0] and source[-1] in bounded[-1]


def test_qa_does_not_blame_disabled_submit_when_required_field_is_blank():
    sys.path.insert(0, str(ROOT / "scripts" / "qa"))
    import qa_explorer
    after = {"elements": [
        {"tag": "input", "type": "text", "name": "dogName", "required": "true", "value": "Biscuit"},
        {"tag": "input", "type": "number", "name": "dogAgeYears", "required": "true", "value": ""},
        {"tag": "input", "type": "checkbox", "required": "true", "checked": "true"},
        {"tag": "button", "type": "submit", "disabled": "true"},
    ]}
    assert qa_explorer._submit_disabled_incomplete_false_positive(
        "Send enquiry should become enabled", {"action_kind": "click"},
        "Send enquiry remained disabled", after)
    assert qa_explorer._submit_disabled_incomplete_false_positive(
        "Send enquiry should become enabled after this field", {"action_kind": "type"},
        "The value landed but Send enquiry remained disabled", after)
    assert qa_explorer._submit_disabled_incomplete_false_positive(
        "Send enquiry should become enabled after settling", {"action_kind": "wait"},
        "Send enquiry remained disabled after waiting", after)
    assert qa_explorer._submit_disabled_incomplete_false_positive(
        "Tab should leave Send enquiry enabled", {"action_kind": "press"},
        "Send enquiry remained disabled after Tab", after)
    assert "(required)" in qa_explorer._fmt_elements(after["elements"])
    after["elements"][1]["value"] = "4"
    assert not qa_explorer._submit_disabled_incomplete_false_positive(
        "Send enquiry should become enabled", {"action_kind": "click"},
        "Send enquiry remained disabled", after)


def test_qa_does_not_blame_drain_for_missing_submission_prerequisite():
    sys.path.insert(0, str(ROOT / "scripts" / "qa"))
    import qa_explorer
    targeting = {"action_kind": "click", "targeted_label": "Drain queue"}
    before = {"visible_text": 'enquiriesTotal 0 No records. {"appJobs": [], "deadLetters": []}'}
    assert qa_explorer._empty_queue_drain_false_positive(
        targeting, "Panels did not transition from empty to populated and remain 0", before)


def test_clean_complete_retest_closes_stale_deferred_findings_for_that_story():
    """Proven clean evidence supersedes old observations instead of spawning another fixer."""
    sys.path.insert(0, str(ROOT / "scripts" / "orchestra"))
    import runtime
    pending = [
        {"story": "US-9", "title": "old blocker"},
        {"story": "US-8", "title": "different story"},
    ]
    clean = {"bugs": 0, "stop_reason": "coverage-complete",
             "coverage": [{"aspect": "fixed journey", "covered": True}],
             "steps_detail": [{"verdict": "match", "covers": ["fixed journey"]}]}
    assert runtime._qa_clear_proven_fixed(pending, "US-9", clean) == [pending[1]]
    assert runtime._qa_clear_proven_fixed(
        pending, "US-9", {"bugs": 1, "stop_reason": "coverage-complete"}) == pending
    assert runtime._qa_clear_proven_fixed(
        pending, "US-9", {"bugs": 0, "stop_reason": "deadline"}) == pending


def test_qa_combobox_click_with_value_selects_option():
    sys.path.insert(0, str(ROOT / "scripts" / "qa"))
    import qa_explorer
    bridge = object.__new__(qa_explorer.BrowserBridge)
    sent = []
    bridge._send = lambda payload: sent.append(payload) or {"ok": True}
    bridge.act({"cmd": "click", "idx": 1, "role": "combobox", "value": "Timeout"})
    assert sent == [{"cmd": "fill", "idx": 1, "selector": None, "value": "Timeout"}]


def test_story_generation_caps_before_saturation_fanout(monkeypatch):
    """A verbose model reply is bounded before another saturation call can amplify it."""
    sys.path.insert(0, str(ROOT / "scripts" / "qa"))
    import story_gen
    raw = [{"id": f"US-{i}", "title": f"story {i}", "persona": "user", "category": "edge",
            "steps": ["act"], "expected_outcome": "observable"} for i in range(30)]
    seen = {}
    def fake_text(role, repo, prompt):
        seen["prompt"] = prompt
        return __import__("json").dumps(raw)
    monkeypatch.setattr(story_gen, "_ai_text", fake_text)
    out = story_gen.generate_stories("vision", "summary")
    assert story_gen.MAX_ROUNDS == 3 and story_gen.SATURATE_MAX == 12
    assert len(out) == 12
    assert "AT MOST 12 stories" in seen["prompt"]


def test_qa_safety_limit_propagates_to_controller_gate(monkeypatch):
    """A resource backstop carries cleanup + progress facts needed for an autonomous safe hand-off."""
    import loopcontroller as lc
    monkeypatch.setattr(lc.factory, "run_grounded_qa", lambda *a, **k: {
        "passed": False, "stories": 12, "blocking_open": 0, "open_bugs": 0,
        "verdict": "SAFETY LIMIT", "safety_limited": True, "deferred_stories": 28,
        "timed_out": True, "cleanup_incomplete": 0, "explorers_done": 7, "explorers_total": 14,
        "stories_done": 3, "stories_total": 12,
        "qa_campaign_run_id": 912,
        "qa_campaign_key": "orchestra:912:policy:2:revision:a:generation:0",
        "evidence_policy_revision": 2,
    })
    import sys as _sys
    fake = type("FakeDevserve", (), {"up": staticmethod(lambda product: {"url": "http://app"})})
    monkeypatch.setitem(_sys.modules, "devserve", fake)
    out = lc.qa_gate("safe-limit-product", platform="web")
    assert out["qa_ok"] is False and out["safety_limited"] is True
    assert out["deferred_stories"] == 28
    assert out["cleanup_incomplete"] == 0
    assert (out["explorers_done"], out["explorers_total"]) == (7, 14)
    assert (out["stories_done"], out["stories_total"]) == (3, 12)
    assert out["qa_campaign_run_id"] == 912
    assert out["qa_campaign_key"] == "orchestra:912:policy:2:revision:a:generation:0"
    assert out["evidence_policy_revision"] == 2


def test_qa_checkpoint_accounting_distinguishes_progress_from_stall():
    """Safe worker rotation continues productive campaigns and counts only proven no-progress slices."""
    import loopcontroller as lc
    assert lc._qa_checkpoint_counts(None, None, 0, 0) == (1, 0)  # legacy worker: unknown, not guilty
    assert lc._qa_checkpoint_counts(7, 8, 1, 2) == (2, 0)        # progress resets stall streak
    assert lc._qa_checkpoint_counts(8, 8, 2, 0) == (3, 1)
    assert lc._qa_checkpoint_counts(8, 7, 3, 1) == (4, 2)        # regressions are not progress
    # A stricter evidence policy/new orchestra run restarts at a lower raw count by design. It is a fresh
    # campaign checkpoint, not a regression or a third inherited stall.
    assert lc._qa_campaign_checkpoint_counts(
        "orchestra:1:policy:1", "orchestra:2:policy:2", 8, 2, 3, 2
    ) == (1, 0, True)
    assert lc._qa_campaign_checkpoint_counts(
        "orchestra:2:policy:2", "orchestra:2:policy:2", 2, 2, 1, 0
    ) == (2, 1, False)


def test_qa_shift_count_never_manufactures_a_ceo_authority_gate():
    """Internal QA rotations continue indefinitely unless a concrete safety/authority fact says otherwise."""
    import loopcontroller as lc
    assert lc._qa_checkpoint_can_continue(0, {"action": "continue_fresh_worker"}) is True
    assert lc._qa_checkpoint_can_continue(0, {"action": "open_internal_incident_and_cleanup"}) is True
    assert lc._qa_checkpoint_can_continue(1, {"action": "continue_fresh_worker"}) is False
    assert lc._qa_checkpoint_can_continue(0, {"action": "request_new_authority"}) is False


def test_qa_worker_process_loss_has_bounded_checkpoint_retry():
    """A single dead QA process is recoverable infrastructure, not a CEO/management decision."""
    import loopcontroller as lc
    assert lc.QA_CRASH_RETRY_MAX >= 1
    assert lc.QA_CRASH_RETRY_MAX <= lc.CRASH_RETRY_MAX


def test_jobd_capacity_queue_never_becomes_a_human_gate(monkeypatch):
    """Age is not corruption: a durable transition waiting behind capacity survives restarts and stays runnable."""
    import psycopg
    import jobd
    from aoscfg import DB

    thread_id = 980000 + int(_rid(), 16) % 10000
    tenant = f"jobd-queued-{_rid()}"
    monkeypatch.setattr(jobd, "_dispatch_limit", lambda _active: 50)
    try:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("""INSERT INTO controller_state
                              (thread_id, tenant_id, org_id, phase, awaiting, updated_at, execution_scope)
                           VALUES (%s,%s,1,'TESTQA',NULL,now()-interval '1 day','test')""",
                        (thread_id, tenant))
        assert thread_id in jobd.queued_runnables(execution_scope="test")
        assert thread_id in jobd.runnable_threads(execution_scope="test")
        assert jobd.park_stale_runnables() == []
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("SELECT awaiting FROM controller_state WHERE thread_id=%s", (thread_id,))
            assert cur.fetchone()[0] is None, "capacity wait must never manufacture a CEO decision"
    finally:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM controller_state WHERE thread_id=%s", (thread_id,))


def test_jobd_unknown_command_cannot_accidentally_start_daemon(monkeypatch):
    import jobd
    monkeypatch.setattr(jobd, "serve", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("unknown command must not start the daemon")))
    assert jobd._main(["status"]) == 2


def test_management_can_list_all_cases_without_an_untyped_null_filter():
    import management
    assert isinstance(management.list_cases(), list)


def test_management_resolves_scheduler_incident_after_durable_recovery():
    import management
    import scheduler
    name = f"test-recovered-{_rid()}"
    scheduler.register(name, "true", 3600)
    case = management.signal(f"duty:schedule:{name}", f"Scheduler job {name} repeatedly failing",
                             "control_mechanism_failure", {"schedule": name, "failure_count": 3},
                             worker=f"schedule:{name}", manager_role="duty-manager")
    try:
        management.duty_audit()
        resolved = {x["case_id"]: x for x in management.list_cases("resolved")}
        assert resolved[case["case_id"]]["state"]["recovered"] is True
    finally:
        with management._conn() as c, c.cursor() as cur:
            cur.execute("DELETE FROM management_events WHERE case_id=%s", (case["case_id"],))
            cur.execute("DELETE FROM management_decisions WHERE case_id=%s", (case["case_id"],))
            cur.execute("DELETE FROM management_cases WHERE case_id=%s", (case["case_id"],))
        scheduler.deregister(name)


def test_management_reconciles_orphaned_controller_recovery_case():
    import management
    thread_id = 980_000_000 + int(_rid(), 16)
    case = management.signal(
        f"controller:{thread_id}:TESTQA", "retired worker repeatedly failed",
        "worker_state_changed", {"thread_id": thread_id, "failure_count": 4},
        work_id=f"controller:{thread_id}:TESTQA", worker="test-worker",
        manager_role="duty-manager")
    try:
        evidence = management.duty_audit()
        resolved = {x["case_id"]: x for x in management.list_cases("resolved")}
        assert evidence["orphaned_control_cases"] >= 1
        assert resolved[case["case_id"]]["state"]["orphan_reconciled"] is True
    finally:
        with management._conn() as c, c.cursor() as cur:
            cur.execute("DELETE FROM management_events WHERE case_id=%s", (case["case_id"],))
            cur.execute("DELETE FROM management_decisions WHERE case_id=%s", (case["case_id"],))
            cur.execute("DELETE FROM management_cases WHERE case_id=%s", (case["case_id"],))


def test_resumed_qa_campaign_refreshes_obsolete_execution_envelope():
    from qa import qa_agentic

    class FakeStore:
        def __init__(self):
            self.saved = None

        def actors(self, run_id, tenant):
            return [{"actor_id": 7, "role": "qa-coordinator",
                     "memory": {"context": {"max_steps": 12, "target_url": "old"},
                                "story_status": {"US-1": "clean"}}},
                    {"actor_id": 8, "role": "qa-explorer", "memory": {}}]

        def update_actor(self, actor_id, tenant, **kwargs):
            self.saved = (actor_id, tenant, kwargs)

    fake = FakeStore()
    assert qa_agentic._refresh_resumed_coordinator_context(
        fake, 99, "tenant", {"max_steps": None, "target_url": "new"}) == 1
    assert fake.saved == (7, "tenant", {"memory": {
        "context": {"max_steps": None, "target_url": "new"}}})


def test_expensive_tool_launch_waits_for_a_full_shift(monkeypatch):
    import time
    sys.path.insert(0, str(ROOT / "scripts" / "orchestra"))
    import jobrunner
    job = {"run_id": 42, "tenant": "t", "tool": "qa_explore"}
    monkeypatch.setenv("AOS_TOOL_MIN_LAUNCH_RUNWAY_S", "600")
    jobrunner.set_run_deadline(42, "t", time.time() + 599)
    try:
        assert jobrunner._has_launch_runway(job) is False
        assert jobrunner._has_launch_runway({**job, "tool": "research_subq"}) is True
        jobrunner.set_run_deadline(42, "t", time.time() + 601)
        assert jobrunner._has_launch_runway(job) is True
    finally:
        jobrunner.clear_run_deadline(42, "t")


def test_page_level_keyboard_navigation_is_not_a_missing_control():
    from qa import qa_explorer
    explorer = qa_explorer.Explorer("http://x", "vision", autostart=False)
    action, aim = explorer._prepare_action({"cmd": "press", "value": "End"}, [])
    assert action["cmd"] == "press"
    assert aim["control_action"] is False


def test_every_confirmed_qa_defect_blocks_release_until_resolved():
    sys.path.insert(0, str(ROOT / "scripts" / "orchestra"))
    import runtime
    assert runtime._qa_finding_needs_fix(
        {"kind": "bug", "severity": "high", "blocking": False}) is True
    assert runtime._qa_finding_needs_fix(
        {"kind": "bug", "severity": "medium", "blocking": False}) is True
    assert runtime._qa_finding_needs_fix(
        {"kind": "bug", "severity": "low", "blocking": True}) is True
    assert runtime._qa_finding_needs_fix(
        {"kind": "bug", "severity": "medium", "blocking": False, "resolved": True}) is False
    assert runtime._qa_finding_needs_fix(
        {"kind": "observation", "severity": "medium", "blocking": False}) is False


def test_qa_fix_waits_for_a_quiescent_browser_slice():
    sys.path.insert(0, str(ROOT / "scripts" / "orchestra"))
    import runtime
    assert runtime._qa_mutation_may_start({
        1: {"role": "qa-explorer", "status": "blocked"},
        2: {"role": "qa-explorer", "status": "done"},
    }) is False
    assert runtime._qa_mutation_may_start({
        1: {"role": "qa-explorer", "status": "done"},
        2: {"role": "qa-explorer", "status": "dead"},
        3: {"role": "dev-coordinator", "status": "working"},
    }) is True
    assert runtime._qa_story_continuation_may_start({
        1: {"role": "qa-explorer", "status": "done"},
        2: {"role": "dev-coordinator", "status": "working"},
    }) is False
    assert runtime._qa_story_continuation_may_start({
        1: {"role": "dev-coordinator", "status": "done"},
        2: {"role": "qa-explorer", "status": "done"},
    }) is True
    active_us12 = {"role": "qa-explorer", "status": "blocked", "memory": {"context": {
        "tool_args": {"story": {"id": "US-012"}},
    }}}
    assert runtime._qa_story_continuation_may_start({1: active_us12}, "US-012") is False
    assert runtime._qa_story_continuation_may_start({1: active_us12}, "US-011") is True


def test_story_status_treats_every_unresolved_bug_as_release_blocking():
    sys.path.insert(0, str(ROOT / "scripts" / "orchestra"))
    import runtime
    assert runtime._qa_story_status({"blocking_found": False, "result": {
        "bugs": 1, "stop_reason": "coverage-complete"}}) == "blocking"
    assert runtime._qa_story_status({"blocking_found": False, "findings_count": 1, "result": {
        "bugs": 0, "stop_reason": "coverage-complete"}}) == "blocking"
    assert runtime._qa_story_status({"blocking_found": False, "result": {
        "bugs": [], "stop_reason": "coverage-complete",
        "coverage": [{"aspect": "journey", "covered": True}],
        "steps_detail": [{"verdict": "match", "covers": ["journey"]}]}}) == "clean"
    assert runtime._qa_story_status({"blocking_found": False, "result": {
        "bugs": [], "stop_reason": "coverage-complete",
        "coverage": [{"aspect": "label only", "covered": True}]}}) == "incomplete"
    assert runtime._qa_story_status({"blocking_found": False, "result": {
        "bugs": 0, "stop_reason": "shift-deadline"}}) == "incomplete"


def test_dev_resume_reuses_completed_revision_fenced_browser_evidence(tmp_path):
    import json
    import os
    import time
    from qa import dev_loop
    repo = tmp_path / "repo"
    evidence = tmp_path / "evidence"
    checkpoint_dir = evidence / "finished"
    repo.mkdir()
    checkpoint_dir.mkdir(parents=True)
    source = repo / "app.js"
    source.write_text("fixed")
    checkpoint = checkpoint_dir / "checkpoint.json"
    state = checkpoint_dir / "storage-state.json"
    state.write_text('{"cookies":[],"origins":[]}')
    checkpoint.write_text(json.dumps({
        "story": "Approval story", "tested": ["approve", "reject"],
        "yet_to_test": [], "ts": time.time() + 2, "resume_state_path": str(state)
    }))
    future = time.time() + 1
    os.utime(source, (future, future))
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    test_file = tests_dir / "approval.test.js"
    test_file.write_text("new regression only")
    os.utime(test_file, (future + 10, future + 10))
    assert dev_loop._latest_resume_coverage(
        {"title": "Approval story"}, repo, evidence) == ["approve", "reject"]


def test_qa_coverage_without_portable_browser_state_is_never_resumed(tmp_path):
    """A fresh localStorage context cannot inherit claims about fixtures created in an old browser."""
    import json
    import time
    from qa import dev_loop
    repo = tmp_path / "repo"; repo.mkdir(); (repo / "app.js").write_text("v1")
    run = tmp_path / "evidence" / "run"; run.mkdir(parents=True)
    (run / "checkpoint.json").write_text(json.dumps({
        "story": "Local workflow", "tested": ["draft exists"], "yet_to_test": ["approve it"],
        "ts": time.time() + 5
    }))
    assert dev_loop._latest_resume_checkpoint(
        {"title": "Local workflow"}, repo, tmp_path / "evidence") is None


def test_qa_portable_state_resumes_setup_before_first_aspect_closes(tmp_path):
    import json
    import time
    from qa import dev_loop
    repo = tmp_path / "repo"; repo.mkdir(); (repo / "app.js").write_text("v1")
    run = tmp_path / "evidence" / "run"; run.mkdir(parents=True)
    state = run / "storage-state.json"; state.write_text('{"cookies":[],"origins":[]}')
    (run / "checkpoint.json").write_text(json.dumps({
        "story": "Long setup", "tested": [], "yet_to_test": ["submit"],
        "resume_state_path": str(state), "ts": time.time() + 5
    }))
    assert dev_loop._latest_resume_checkpoint(
        {"title": "Long setup"}, repo, tmp_path / "evidence") == {
            "covered": [], "coverage": [], "resume_state_path": str(state)}


def test_process_rotation_reuses_exact_coverage_ledger_without_replanning():
    import threading
    from qa import qa_explorer
    explorer = qa_explorer.Explorer("http://x", "vision", autostart=False)
    explorer.bridge = object()
    cancelled = threading.Event(); cancelled.set()
    ledger = [
        {"aspect": "already proven", "covered": True},
        {"aspect": "one remaining decision", "covered": False},
    ]
    assert explorer.explore({"title": "story"}, resume_coverage=ledger,
                            resume_steps_detail=[{"verdict": "match",
                                                  "covers": ["already proven"]}],
                            cancel_event=cancelled) == []
    assert [{key: item[key] for key in ("aspect", "covered")}
            for item in explorer.coverage] == ledger
    assert explorer.coverage[0]["proof"] == {
        "engine": "durable-grounded-evidence",
        "action_kind": "checkpoint",
        "recorded_at": 1.0,
    }
    assert explorer.stop_reason == "cancelled-incomplete"


def test_qa_empty_state_requires_explicit_reset_not_same_context_goto():
    from qa import qa_explorer
    targeting = {"action_kind": "goto", "control_action": False}
    before = {"bodyText": 'ENQUIRY RECEIVED enquiriesTotal 1 agentJobsQueued 1 '
                          '"appJobs": [{"id":"job-1"}]'}
    assert qa_explorer._empty_state_without_reset_false_positive(
        "Empty-storage initial load shows zero enquiry/job metrics", targeting,
        "Reload retained an enquiry instead of showing no persisted records", before)
    assert not qa_explorer._empty_state_without_reset_false_positive(
        "Persist the enquiry across reload", targeting,
        "Reload lost the enquiry", before)


def test_qa_initial_state_classifier_protects_perishable_preconditions():
    from qa import qa_explorer
    assert qa_explorer._initial_state_aspect(
        "Verify empty-storage initial load autofocuses Name and shows zero enquiry metrics")
    assert qa_explorer._empty_storage_aspect(
        "Verify empty-storage initial load autofocuses Name and shows zero enquiry metrics")
    assert not qa_explorer._initial_state_aspect(
        "Submit the completed form and observe the confirmation")


def test_qa_bridge_cancellation_is_scoped_to_run_and_tenant():
    from qa import qa_explorer
    class Bridge:
        def __init__(self, run_id, tenant):
            self.scope_run_id, self.scope_tenant, self.closed = run_id, tenant, False
        def close(self):
            self.closed = True
    a, b, c = Bridge(1, "t1"), Bridge(2, "t1"), Bridge(1, "t2")
    with qa_explorer._LIVE_BRIDGES_LOCK:
        qa_explorer._LIVE_BRIDGES.update((a, b, c))
    try:
        assert qa_explorer.close_live_bridges(run_id=1, tenant="t1") == 1
        assert a.closed and not b.closed and not c.closed
    finally:
        with qa_explorer._LIVE_BRIDGES_LOCK:
            qa_explorer._LIVE_BRIDGES.difference_update((a, b, c))


def test_qa_targeting_exposes_driver_failure_as_ground_truth():
    from qa import qa_explorer
    explorer = qa_explorer.Explorer("http://app", "vision", autostart=False)
    facts = explorer._targeting_facts(
        {"action_kind": "press", "control_action": False, "intended": "", "role": ""},
        {"ok": False, "error": "press requires selector or idx"},
        {"url": "http://app", "bodyText": "before", "elements": []},
        {"url": "http://app", "bodyText": "before", "elements": []},
        settled=True)
    assert facts["driver_ok"] is False
    assert facts["driver_error"] == "press requires selector or idx"
    rendered = qa_explorer._fmt_targeting(facts)
    assert "browser driver command succeed? false" in rendered.lower()
    assert "press requires selector or idx" in rendered


def test_qa_driver_failure_can_never_become_product_bug(monkeypatch):
    from qa import qa_explorer
    monkeypatch.setattr(qa_explorer, "_call_agent", lambda *a, **k: {
        "out_full": '{"target_confirmed":true,"matches_expected":false,"verdict":"bug",'
                    '"bug":"the app ignored Enter","severity":"high","blocking":true}'})
    explorer = qa_explorer.Explorer("http://app", "vision", autostart=False)
    verdict = explorer._ai_evaluate(
        {"title": "submit"}, "form submits", {"driver_ok": False, "driver_error": "bad command"},
        {"url": "http://app"}, {"url": "http://app"}, ["submit"])
    assert verdict["verdict"] == "inconclusive"
    assert verdict["bug"] is None
    assert verdict["blocking"] is False


def test_qa_incomplete_submit_is_setup_retry_not_product_bug(monkeypatch):
    from qa import qa_explorer
    monkeypatch.setattr(qa_explorer, "_call_agent", lambda *a, **k: {
        "out_full": '{"target_confirmed":true,"matches_expected":false,"verdict":"bug",'
                    '"bug":"Publish failed with form_incomplete instead of creating the record",'
                    '"severity":"high","blocking":true}'})
    explorer = qa_explorer.Explorer("http://app", "vision", autostart=False)
    verdict = explorer._ai_evaluate(
        {"title": "publish"}, "publication is created",
        {"action_kind": "click", "driver_ok": True,
         "empty_required_fields_before": [{"label": "Publication title"}]},
        {"url": "http://app"}, {"url": "http://app", "statusText": "form_incomplete"},
        ["publish"])
    assert verdict["verdict"] == "retry"
    assert verdict["bug"] is None
    assert verdict["blocking"] is False


def test_dev_self_qa_does_not_reopen_a_resolved_explorer_finding(monkeypatch):
    """A later successful repro closes a transient finding; the fixer must not mutate for it."""
    from qa import dev_loop
    sys.path.insert(0, str(ROOT / "scripts" / "qa"))
    import qa_explorer

    class FakeExplorer:
        def __init__(self, *args, **kwargs):
            self.bugs = []

        def explore(self, story, **kwargs):
            bug = {"bug": "stale observation", "severity": "high", "resolved": True}
            kwargs["on_bug"](bug)
            return [{"bug": bug}]

        def close(self):
            pass

    monkeypatch.setattr(qa_explorer, "Explorer", FakeExplorer)
    assert dev_loop.dev_self_qa(
        "http://app", "vision", [{"title": "story"}]) == []


def test_dev_fix_refuses_to_mutate_for_resolved_finding(monkeypatch, tmp_path):
    from qa import dev_loop
    source = tmp_path / "app.py"; source.write_text("def save(): return True\n")
    contract = tmp_path / "contract.md"; contract.write_text("Saving succeeds.\n")
    evidence = tmp_path.parent / f"{tmp_path.name}-evidence"; evidence.mkdir()
    finding = {"bug": "old", "resolved": True,
               "evidence_provenance": dev_loop.capture_finding_provenance(tmp_path, evidence)}
    answers = iter([
        {"verdict": "false_positive", "confidence": 0.99, "reason": "implementation",
         "citations": [{"path": "app.py", "start_line": 1, "end_line": 1,
                        "quote": "def save(): return True"}]},
        {"verdict": "false_positive", "confidence": 0.99, "reason": "contract",
         "citations": [{"path": "contract.md", "start_line": 1, "end_line": 1,
                        "quote": "Saving succeeds."}]},
    ])
    monkeypatch.setattr(dev_loop, "_ai_json", lambda *_a, **_k: next(answers))
    monkeypatch.setattr(dev_loop, "_plan_fix", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("resolved finding must not reach planning")))
    out = dev_loop.fix_bug(
        finding, {}, "vision", repo=str(tmp_path),
        target_url="http://app", stories=[{"title": "story"}])
    assert out["fixed"] is True and out["resolved_without_mutation"] is True
    assert out["attempts"] == 0 and out["files"] == []


def test_resumed_tool_finds_product_scoped_nested_checkpoint(monkeypatch, tmp_path):
    """Crash before actor-memory persistence still resumes the durable browser state."""
    import json
    import time
    sys.path.insert(0, str(ROOT / "scripts" / "qa"))
    import artifacts
    from qa import dev_loop
    repo = tmp_path / "products" / "product-a"; repo.mkdir(parents=True)
    (repo / "app.js").write_text("v1")
    evidence = tmp_path / "evidence"
    run = evidence / "qa-explorer-product-a-US-9-123-456" / "20260815-000000"
    run.mkdir(parents=True)
    state = run / "storage-state.json"
    state.write_text('{"cookies":[],"origins":[]}')
    (run / "checkpoint.json").write_text(json.dumps({
        "story": "Approval story", "tested": ["draft saved"],
        "yet_to_test": ["approve"], "resume_state_path": str(state),
        "ts": time.time() + 5}))
    monkeypatch.setattr(artifacts, "root", lambda: evidence)
    assert dev_loop._latest_resume_checkpoint(
        {"title": "Approval story"}, repo, product="product-a") == {
            "covered": ["draft saved"], "coverage": [], "resume_state_path": str(state)}


def test_live_checkpoint_resume_does_not_scan_legacy_windows_evidence_by_default(
        monkeypatch, tmp_path):
    import json
    import time
    sys.path.insert(0, str(ROOT / "scripts" / "qa"))
    import artifacts
    from qa import dev_loop
    repo = tmp_path / "products" / "product-a"; repo.mkdir(parents=True)
    (repo / "app.js").write_text("v1")
    native = tmp_path / "native"
    run = native / "qa-explorer-product-a-US-9-123-456" / "20260815-000000"
    run.mkdir(parents=True)
    state = run / "storage-state.json"; state.write_text('{"cookies":[],"origins":[]}')
    (run / "checkpoint.json").write_text(json.dumps({
        "story": "Approval story", "tested": ["draft saved"],
        "resume_state_path": str(state), "ts": time.time() + 5}))
    monkeypatch.setattr(artifacts, "root", lambda: native)
    monkeypatch.setattr(artifacts, "search_roots", lambda: (_ for _ in ()).throw(
        AssertionError("legacy evidence enumeration must be opt-in")))
    monkeypatch.delenv("AOS_QA_SCAN_LEGACY_RESUME", raising=False)

    assert dev_loop._latest_resume_checkpoint(
        {"title": "Approval story"}, repo, product="product-a") == {
            "covered": ["draft saved"], "coverage": [], "resume_state_path": str(state)}


def test_cancelled_checkpoint_recovers_sibling_portable_state(tmp_path):
    """Cancellation must not erase recovery merely because its final checkpoint omitted the pointer."""
    import json
    import time
    from qa import dev_loop
    repo = tmp_path / "repo"; repo.mkdir(); (repo / "app.js").write_text("v1")
    run = tmp_path / "evidence" / "run"; run.mkdir(parents=True)
    state = run / "storage-state.json"; state.write_text('{"cookies":[],"origins":[]}')
    (run / "checkpoint.json").write_text(json.dumps({
        "story": "Long approval", "tested": ["draft", "denial"],
        "yet_to_test": ["reject"], "stop_reason": "cancelled-incomplete",
        "resume_state_path": None, "ts": time.time() + 5
    }))
    assert dev_loop._latest_resume_checkpoint(
        {"title": "Long approval"}, repo, tmp_path / "evidence") == {
            "covered": ["draft", "denial"], "coverage": [],
            "resume_state_path": str(state)}


def test_devserve_bulk_restore_is_off_and_bounded_by_default(monkeypatch):
    """WSL recovery must never boot every historical generated app server."""
    import devserve
    rows = [{"name": f"app-{i}", "kind": "service"} for i in range(10)]
    started = []
    monkeypatch.setattr(devserve.ar, "as_rows", lambda: rows)
    monkeypatch.setattr(devserve, "up", lambda name: started.append(name) or {"name": name})
    monkeypatch.delenv("AOS_DEVSERVE_MAX_ACTIVE", raising=False)
    assert devserve.up_all() == [] and started == []
    monkeypatch.setenv("AOS_DEVSERVE_MAX_ACTIVE", "3")
    assert len(devserve.up_all()) == 3 and started == ["app-0", "app-1", "app-2"]


def test_watchdog_detects_host_pressure_before_wsl_hangs():
    import watchdog
    issues = watchdog._pressure_issues(700_000, 16_000_000, dev_servers=75, browser_roots=12)
    by_sig = {i["sig"]: i for i in issues}
    assert by_sig["host:memory"]["level"] == "crit"
    assert by_sig["host:devservers"]["level"] == "warn"
    assert by_sig["host:browsers"]["level"] == "crit"
    assert watchdog._pressure_issues(12_000_000, 16_000_000, 0, 0) == []


def test_agentic_qa_uses_saturated_story_corpus(monkeypatch):
    """The default durable QA org must not regress to a thin one-shot story list."""
    sys.path.insert(0, str(ROOT / "scripts" / "qa"))
    sys.path.insert(0, str(ROOT / "scripts" / "orchestra"))
    import qa_agentic
    import qa_run
    import story_gen
    import store

    seen = {}

    def fake_saturate(vision, summary, product=None, repo=None):
        seen.update({"vision": vision, "summary": summary, "product": product, "repo": repo})
        return [{"id": "US-1", "title": "Open app", "steps": ["Open app"], "expected_outcome": "App opens"}]

    monkeypatch.setattr(story_gen, "saturate_stories", fake_saturate)
    monkeypatch.setattr(qa_agentic, "_planned_workers", lambda n, requested=None: 1)
    monkeypatch.setattr(store, "start_run", lambda tenant, vision: {"run_id": 123})
    monkeypatch.setattr(store, "spawn_actor", lambda *a, **k: {"actor_id": 456})
    monkeypatch.setattr(store, "emit", lambda *a, **k: None)
    monkeypatch.setattr(store, "run", lambda *a, **k: {"status": "done"})
    monkeypatch.setattr(store, "actors", lambda *a, **k: [{"role": "qa-coordinator", "result": {"passed": True}}])
    monkeypatch.setattr(store, "events", lambda *a, **k: [])
    monkeypatch.setattr(qa_run, "_persist_run", lambda *a, **k: 999)
    monkeypatch.setattr(qa_run, "write_verdict", lambda *a, **k: "/tmp/verdict.json")

    out = qa_agentic.run_agentic_qa("http://app", "vision", product="p", summary="summary",
                                    repo="/tmp/repo", file_findings=False, drive_budget_s=0)
    assert seen == {"vision": "vision", "summary": "summary", "product": "p", "repo": "/tmp/repo"}
    assert out["report"]["total_stories"] == 1


def test_qa_tool_emits_per_story_pulse(monkeypatch):
    """Tool-level QA should expose per-story heartbeat progress, not only a run-level 'blocked actors' count."""
    sys.path.insert(0, str(ROOT / "scripts" / "orchestra"))
    import types
    import tools

    events = []
    fake_pulse = types.ModuleType("pulse")
    fake_pulse.start = lambda *a, **k: events.append(("start", a, k))
    fake_pulse.beat = lambda *a, **k: events.append(("beat", a, k))
    fake_pulse.finish = lambda *a, **k: events.append(("finish", a, k))
    monkeypatch.setitem(sys.modules, "pulse", fake_pulse)
    artifact_dirs = []

    class FakeExplorer:
        def __init__(self, *a, **k):
            artifact_dirs.append(k.get("artifact_dir"))
            self.coverage = [{"aspect": "open", "covered": True}]
            self.stop_reason = "coverage-complete"
            self.video_mp4 = Path("/tmp/video.webm")
            self.pulse_work_id = None

        def explore(self, story, max_steps=None, on_bug=None, stop_on_actionable_bug=None):
            assert self.pulse_work_id
            assert stop_on_actionable_bug is True
            fake_pulse.beat(self.pulse_work_id, stage="explore", progress="1/1 aspects covered")
            return [{"action": {"cmd": "noop"}, "actual": {}, "verdict": {"matches_expected": True}}]

        def close(self):
            pass

    fake_qx = types.ModuleType("qa_explorer")
    fake_qx.Explorer = FakeExplorer
    monkeypatch.setitem(sys.modules, "qa_explorer", fake_qx)
    import artifacts as qa_artifacts
    monkeypatch.setattr(qa_artifacts, "validate_media",
                        lambda path: {"path": str(path), "duration_s": 1.0})

    out = tools.qa_explore({"target_url": "http://app", "vision": "v", "product": "p",
                            "tenant": "t", "story": {"id": "US-1", "title": "Open"}})
    assert out["status"] == "done"
    assert [e[0] for e in events] == ["start", "beat", "finish"]
    assert events[0][1][1] == "qa-story"
    assert "US-1" in events[0][1][0]
    assert artifact_dirs and "US-1" in str(artifact_dirs[0])
    assert "artifact_dir" in out["result"]
    assert out["result"]["video"] == "/tmp/video.webm"


def test_qa_tool_normalizes_structured_bug_payload_instead_of_slicing_mapping(monkeypatch):
    import types as pytypes
    sys.path.insert(0, str(ROOT / "scripts" / "orchestra"))
    import tools

    class FakeExplorer:
        def __init__(self, *args, **kwargs):
            self.coverage = [{"aspect": "blank form", "covered": True}]
            self.stop_reason = "coverage-complete"
            self.video_mp4 = None
            self.artifact_evidence = []

        def explore(self, _story, **kwargs):
            kwargs["on_bug"]({
                "bug": {"summary": "blank form has no error", "field": "Name"},
                "severity": "medium", "blocking": False,
            })
            return [{"action": {"cmd": "press", "value": "Enter"}}]

        def close(self):
            pass

    fake_qx = pytypes.ModuleType("qa_explorer")
    fake_qx.Explorer = FakeExplorer
    fake_qx._recorder_requirement_stage = lambda _aspect: "start"
    monkeypatch.setitem(sys.modules, "qa_explorer", fake_qx)

    out = tools.qa_explore({
        "target_url": "http://app", "vision": "v", "product": "p", "tenant": "t",
        "story": {"id": "US-004", "title": "Blank form"},
    })

    assert out["status"] == "done"
    assert out["findings"][0]["title"].startswith('{"field": "Name"')
    assert '"summary": "blank form has no error"' in out["findings"][0]["detail"]


def test_untestable_native_target_is_honestly_blocked_not_faked():
    """A native target with no harness on this box (ios/android/desktop/pc-game) must yield an HONEST
    non-pass verdict (escalate to a device farm) — never a code-graded fake pass. run_grounded_qa routes
    _UNTESTABLE_HERE platforms to _honest_untestable_verdict."""
    import factory
    for p in ("mobile-ios", "mobile-android", "mobile-cross", "desktop", "pc-game"):
        assert p in factory._UNTESTABLE_HERE, p
    # the honest verdict is a real non-pass with a blocking bug + written artifact
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        (repo / "docs").mkdir()
        v = factory._honest_untestable_verdict(repo, "some-app", "mobile-ios")
        assert v["passed"] is False and int(v["blocking_open"]) >= 1
        assert v.get("untestable_here") == "mobile-ios"
        assert not factory.qa_verdict_ok(v), "an untestable target must fail the ship gate"


def test_qa_infra_failover_is_inconclusive_not_a_blocking_app_bug():
    """A provider rate-limit/failover or dead browser bridge must NOT masquerade as a product defect in the
    verdict (observed: 31 stories flip blocked<->passed purely from failover noise, poisoning a real 40/40
    pass into a 'FAILED'). An infra crash → story 'unknown' (inconclusive, retried); a genuine app crash still
    blocks. _is_infra_error classifies; _story_report routes."""
    sys.path.insert(0, str(ROOT / "scripts" / "qa"))
    import qa_run as q
    assert q._is_infra_error("Navigation timeout of 30000ms exceeded")
    assert q._is_infra_error("Claude exhausted on transient errors — failing over to Codex")
    assert q._is_infra_error("target closed / browser has been closed")
    assert not q._is_infra_error("expected the submit button to be enabled, but it was missing")
    infra = [{"bug": "explorer crashed mid-story: timeout", "infra": True, "blocking": False}]
    appbug = [{"bug": "clicking Save does nothing", "infra": False, "blocking": True, "severity": "high"}]
    assert q._story_report({"id": "US-1"}, [], infra)["status"] == "unknown", "infra hiccup must be inconclusive"
    assert q._story_report({"id": "US-1"}, [], infra)["inconclusive"] is True
    assert q._story_report({"id": "US-2"}, [], appbug)["status"] == "blocked", "a real app bug still blocks"
    assert q._story_report({"id": "US-3"}, [], [])["status"] == "passed"
