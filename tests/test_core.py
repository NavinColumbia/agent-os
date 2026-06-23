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

    def fake_build(product, comp, dep_ifaces, api_key=None):
        seen[comp["id"]] = set(dep_ifaces)
        return {"id": comp["id"], "passed": True, "fix_attempts": 0}
    monkeypatch.setattr(project, "build_component", fake_build)
    integrated = {"called": False}
    monkeypatch.setattr(project, "integrate",
                        lambda p, pl: (integrated.__setitem__("called", True), {"passed": True})[1])
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
    """If any component fails to build, the line must NOT integrate — it reports BLOCKED_AT_COMPONENTS."""
    import shutil
    import project
    plan_obj = {"components": [{"id": "a", "name": "a", "description": "d", "deps": [], "interface": "a()"},
                               {"id": "b", "name": "b", "description": "d", "deps": ["a"], "interface": "b()"}],
                "integration_tests": "x"}
    monkeypatch.setattr(project, "plan", lambda *a, **k: plan_obj)
    monkeypatch.setattr(project, "build_component",
                        lambda product, comp, dep_ifaces, api_key=None: {"id": comp["id"],
                        "passed": comp["id"] != "b"})   # 'b' fails
    integrated = {"called": False}
    monkeypatch.setattr(project, "integrate",
                        lambda p, pl: (integrated.__setitem__("called", True), {"passed": True})[1])
    prod = f"ut-complex-{_rid()}"
    try:
        log = project.build_complex(prod, "goal")
        assert log["result"] == "BLOCKED_AT_COMPONENTS" and "b" in log["failed_components"]
        assert not integrated["called"], "must not integrate when a component failed"
    finally:
        shutil.rmtree(factory_products_dir() / prod, ignore_errors=True)


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
    # ambiguous / silent -> APPROVE (QA is the hard gate; never block a green build on a parse miss)
    assert verdict("Looks fine to me, nice work.") == "APPROVE"
    # missing file -> APPROVE
    rv.unlink()
    assert factory._review_verdict(str(tmp_path)) == "APPROVE"


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
