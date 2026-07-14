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


def _rid():
    return os.urandom(4).hex()


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
    monkeypatch.setattr(project, "plan", lambda *a, **k: plan_obj)
    seen = {}

    def fake_build(product, comp, dep_ifaces, api_key=None, ns=""):
        seen[comp["id"]] = set(dep_ifaces)
        return {"id": comp["id"], "passed": True, "fix_attempts": 0}
    monkeypatch.setattr(project, "build_component", fake_build)
    integrated = {"called": False}
    monkeypatch.setattr(project, "integrate",
                        lambda product, p, ns="", facade=None: (integrated.__setitem__("called", True), {"passed": True})[1])
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
    monkeypatch.setattr(project, "plan", lambda *a, **k: plan_obj)
    monkeypatch.setattr(project, "build_component",
                        lambda product, comp, dep_ifaces, api_key=None, ns="": {"id": comp["id"],
                        "passed": comp["id"] != "b", "blocker": None if comp["id"] != "b" else "b failed"})
    integrated = {"called": False}
    monkeypatch.setattr(project, "integrate",
                        lambda product, p, ns="", facade=None: (integrated.__setitem__("called", True), {"passed": True})[1])
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

    def fake_arch(product, goal, model, ns, depth, variant, out_name):
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
                        lambda product, goal, model=None, ns="", depth=0: (plans_for.append(ns), root if ns == "" else sub)[1])
    leaves = []
    monkeypatch.setattr(project, "build_component",
                        lambda product, comp, dep_ifaces, api_key=None, ns="": (leaves.append(ns + comp["id"]), {"id": comp["id"], "passed": True})[1])
    integ_ns = []
    monkeypatch.setattr(project, "integrate",
                        lambda product, p, ns="", facade=None: (integ_ns.append(ns), {"passed": True})[1])
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


def test_project_resume_sweep_detects_interrupted_only(tmp_path, monkeypatch):
    """A complex build with no terminal ProjectComplete (an outage casualty) + a PLAN.json must be
    detected for auto-resume; one with a ProjectComplete must be excluded (no infinite re-resume)."""
    import psycopg
    import factory
    import project
    import audit
    monkeypatch.setattr(factory, "PRODUCTS", tmp_path)
    sfx = _rid()
    p_int, p_done = f"sweepproj-int-{sfx}", f"sweepproj-done-{sfx}"
    for prod in (p_int, p_done):                              # both look resumable (have a PLAN.json)
        (tmp_path / prod / "docs").mkdir(parents=True)
        (tmp_path / prod / "docs" / "PLAN.json").write_text('{"components":[],"integration_tests":""}')
    with psycopg.connect(factory._DB) as c, c.cursor() as cur:
        for prod in (p_int, p_done):                          # aged 60m so the idle guard passes
            cur.execute("""INSERT INTO traces (run_id,product,stage,role,kind,rc,ts)
                           VALUES (%s,%s,'BUILD:x','builder','agent',0, now()-interval '60 minutes')""",
                        (f"proj-{prod}", prod))
        c.commit()
    audit.append(actor="pytest", action="ProjectComplete", resource=p_done, decision="INTEGRATED")
    try:
        found = factory_find_projects(project, 20)
        assert p_int in found and p_done not in found
    finally:
        with psycopg.connect(factory._DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM traces WHERE run_id IN (%s,%s)", (f"proj-{p_int}", f"proj-{p_done}"))
            c.commit()


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
        for a in (f"a@{p}", f"b@{p}", f"c@{p}"):
            directory.release(a)


def test_directory_contact_is_brokered_not_socket():
    import directory
    m = directory.contact("x@p", "y@p", "ask", "hello")
    assert m["message_id"].startswith("dm-") and m["to"] == "y@p"   # durable message id, no connection


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
        # lease reclaim: with lease_s=0 every held slot is past-lease, so a crashed holder's slot is reclaimable
        assert claude_gate.acquire("h5", wait_s=0, table=t, lease_s=0) is not None, "expired lease must reclaim"
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
