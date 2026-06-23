#!/usr/bin/env python3
"""project.py — autonomous build of ONE COMPLEX, INTERDEPENDENT software product.

Where factory.build_product builds a single bounded artifact, and factory.dispatch_fleet builds many
INDEPENDENT products in parallel, THIS handles one product made of many INTERDEPENDENT components — the
thing a straight SPEC->BUILD->QA line cannot do. The flow:

  1. PLAN     — an architect decomposes the goal into a dependency DAG of components, each with an
                explicit INTERFACE CONTRACT its dependents code against (docs/PLAN.json).
  2. BUILD    — components build DEPENDENCY-ORDERED: Kahn layers, parallel WITHIN a layer (disjoint
                src/<id> paths so they never collide), each given its dependencies' interfaces. A
                bounded per-component test loop gates each one.
  3. INTEGRATE— an integrator wires the components together via their interfaces and writes end-to-end
                integration tests; a bounded integration-fix loop drives it green.

So the loops are no longer one straight line: per-component test loops, an integration loop, and a
component-vs-integration barrier between layers. It composes factory.py's governed agent primitives
(sandboxed tests, audit, directory, comms, crash-resume, Codex failover) — nothing here bypasses them.

    project.py build <product> '<goal>'     # decompose + build + integrate one complex product
    project.py selftest                      # offline checks of the graph logic (no agents)
Run with the agent-os venv python.
"""
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit     # noqa: E402
import factory   # noqa: E402

MAX_COMPONENT_FIX = int(os.environ.get("AOS_MAX_COMPONENT_FIX", "3"))
MAX_INTEGRATION_FIX = int(os.environ.get("AOS_MAX_INTEGRATION_FIX", "3"))


# ───────────────────────── pure graph logic (unit-tested, no agents) ─────────────────────────
def validate_dag(components):
    """Raise ValueError on duplicate ids, deps that reference unknown components, self-deps, or a cycle."""
    ids = [c["id"] for c in components]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate component ids")
    idset = set(ids)
    for c in components:
        for d in c.get("deps", []):
            if d == c["id"]:
                raise ValueError(f"component '{c['id']}' depends on itself")
            if d not in idset:
                raise ValueError(f"component '{c['id']}' depends on unknown '{d}'")
    topo_layers(components)   # raises on cycle
    return True


def topo_layers(components):
    """Kahn layering: [[ids with no unmet deps], [next layer], ...]. Same-layer components are independent
    and build in parallel; later layers depend on earlier ones. Raises ValueError on a dependency cycle."""
    deps = {c["id"]: set(c.get("deps", [])) for c in components}
    done, layers, remaining = set(), [], set(deps)
    while remaining:
        ready = sorted(i for i in remaining if deps[i] <= done)
        if not ready:
            raise ValueError(f"dependency cycle among: {sorted(remaining)}")
        layers.append(ready)
        done |= set(ready)
        remaining -= set(ready)
    return layers


def _load_json(path: Path):
    """Parse JSON the architect wrote, tolerating markdown fences / surrounding prose."""
    txt = path.read_text().strip()
    if "```" in txt:                                  # strip a ```json ... ``` fence if present
        txt = txt.split("```")[1]
        txt = txt[4:] if txt.lower().startswith("json") else txt
    if not txt.lstrip().startswith("{"):              # else grab the outermost {...}
        txt = txt[txt.find("{"): txt.rfind("}") + 1]
    return json.loads(txt)


# ───────────────────────── agent-driven phases (compose factory primitives) ─────────────────
def plan(product, goal, model=None):
    """ARCHITECT decomposes the goal into a dependency DAG with interface contracts -> docs/PLAN.json."""
    repo = factory.PRODUCTS / product
    (repo / "docs").mkdir(parents=True, exist_ok=True)
    pj = repo / "docs" / "PLAN.json"
    if pj.exists():                                   # RESUME: reuse the prior decomposition, don't re-plan
        try:
            p = _load_json(pj)
            validate_dag(p["components"])
            print(f"[project] resuming — reusing existing plan ({len(p['components'])} components)", flush=True)
            return p
        except Exception:
            pass                                      # corrupt/partial -> fall through and re-plan
    factory._ctx.product = product; factory._ctx.run = f"proj-{product}"; factory._ctx.stage = "PLAN"
    task = (
        f"You are the system ARCHITECT. Decompose this product goal into 4-8 INTERDEPENDENT components of "
        f"ONE Python codebase. GOAL:\n{goal}\n\n"
        f"Write docs/PLAN.json containing ONLY this JSON (no prose, no fences):\n"
        f'{{"components":[{{"id":"kebab-id","name":"short name","description":"what it does",'
        f'"deps":["other-id"],"interface":"the EXACT public functions/classes other components import '
        f'and call — this is a contract"}}],'
        f'"integration_tests":"end-to-end behaviours that prove the components work TOGETHER"}}\n'
        f"Rules: each component becomes a package src/<id_with_underscores>/. 'deps' MUST be acyclic and "
        f"reference other component ids. Make the 'interface' precise (names + signatures) because your "
        f"dependents will code against it without seeing your implementation. Keep ids kebab-case.")
    r = factory.agent("staff-engineer", str(repo), task, model=model)
    if r.get("failed"):
        raise RuntimeError(f"architect failed to plan: {r.get('out','')[:200]}")
    p = _load_json(repo / "docs" / "PLAN.json")
    validate_dag(p["components"])
    audit.append(actor="project:architect", action="Plan", resource=product, decision="executed",
                 payload={"components": [c["id"] for c in p["components"]]})
    return p


def build_component(product, comp, dep_interfaces, api_key=None):
    """Build ONE component as src/<pkg>/ with its own tests, coding against its interface contract and its
    dependencies' interfaces. Runs in a worker thread, so it sets its OWN thread-local _ctx. Bounded
    per-component test loop. Disjoint paths (src/<pkg>, tests/<pkg>) make same-layer builds collision-free."""
    repo = factory.PRODUCTS / product
    cid = comp["id"]; pkg = cid.replace("-", "_")
    factory._ctx.api_key = api_key                    # thread-local: must be set inside this worker thread
    factory._ctx.product = product; factory._ctx.run = f"proj-{product}"; factory._ctx.stage = f"BUILD:{cid}"
    # RESUME: if this component was already built green in a prior (interrupted) run, skip it — so a
    # killed complex build re-runs only the missing/failing components, not the whole thing.
    if (repo / "src" / pkg).exists():
        pre_ok, _ = factory.run_tests(str(repo), target=f"tests/{pkg}")
        if pre_ok:
            print(f"[project] component {cid}: already green — skipping (resume)", flush=True)
            return {"id": cid, "passed": True, "resumed": True, "fix_attempts": 0}
    aid = f"builder@{product}:{cid}"
    try:
        import directory                              # live coordination: claim disjoint paths, detect overlap
        directory.register(aid, "builder", product, f"BUILD:{cid}", [f"src/{pkg}/**", f"tests/{pkg}/**"])
    except Exception:
        pass
    deps_block = "\n".join(f"- {d}: {i}" for d, i in dep_interfaces.items()) or "(no dependencies)"
    task = (
        f"Implement component '{cid}' of a LARGER system as a Python package under src/{pkg}/ "
        f"(create src/{pkg}/__init__.py; use `from src.{pkg}...` imports).\n"
        f"COMPONENT: {comp['name']} — {comp['description']}\n"
        f"THE PUBLIC INTERFACE YOU MUST EXPOSE (your dependents rely on this EXACT contract):\n{comp['interface']}\n"
        f"INTERFACES OF YOUR DEPENDENCIES (import and call these — do NOT reimplement them):\n{deps_block}\n"
        f"Also write tests under tests/{pkg}/ covering this component. Touch ONLY src/{pkg}/** and "
        f"tests/{pkg}/**. Make `python -m pytest -q tests/{pkg}` pass.")
    factory.agent("builder", str(repo), task)
    ok, out = factory.run_tests(str(repo), target=f"tests/{pkg}")
    attempts = 0
    while not ok and attempts < MAX_COMPONENT_FIX:
        attempts += 1
        print(f"[project] component {cid}: test red — fix {attempts}/{MAX_COMPONENT_FIX}", flush=True)
        factory.agent("builder", str(repo),
                      f"Component '{cid}' tests FAILING:\n\n{out[-1500:]}\n\nFix src/{pkg}/** (or a genuinely "
                      f"wrong test) so `python -m pytest -q tests/{pkg}` passes. Keep the public interface intact.")
        ok, out = factory.run_tests(str(repo), target=f"tests/{pkg}")
    try:
        import directory
        directory.release(aid)
    except Exception:
        pass
    return {"id": cid, "passed": ok, "fix_attempts": attempts}


def integrate(product, p):
    """INTEGRATOR wires the built components into a coherent product via their interfaces and writes
    end-to-end integration tests; bounded integration-fix loop drives the WHOLE suite green."""
    repo = factory.PRODUCTS / product
    factory._ctx.product = product; factory._ctx.run = f"proj-{product}"; factory._ctx.stage = "INTEGRATE"
    comp_list = ", ".join(c["id"] for c in p["components"])
    task = (
        f"You are the INTEGRATOR. The components ({comp_list}) are built as packages under src/. Wire them "
        f"into one coherent product: add the top-level entrypoint/orchestration under src/ that composes the "
        f"components THROUGH their public interfaces (do not rewrite their internals), and write END-TO-END "
        f"INTEGRATION TESTS under tests/integration/ that exercise: "
        f"{p.get('integration_tests', 'the components working together')}. Use `from src...` imports. "
        f"Make `python -m pytest -q` (the WHOLE suite) pass.")
    factory.agent("staff-engineer", str(repo), task)
    ok, out = factory.run_tests(str(repo))            # full suite = components still green + integration green
    attempts = 0
    while not ok and attempts < MAX_INTEGRATION_FIX:
        attempts += 1
        print(f"[project] integration red — fix {attempts}/{MAX_INTEGRATION_FIX}", flush=True)
        factory.agent("staff-engineer", str(repo),
                      f"Integration/tests FAILING:\n\n{out[-1800:]}\n\nFix the integration wiring or a genuinely "
                      f"wrong integration test (do NOT weaken component tests). Make `python -m pytest -q` pass.")
        ok, out = factory.run_tests(str(repo))
    return {"passed": ok, "fix_attempts": attempts, "tail": out[-400:]}


def build_complex(product, goal, api_key=None):
    """Drive ONE complex, interdependent product end-to-end: PLAN -> dependency-ordered parallel component
    builds -> INTEGRATE. Returns a structured log; every phase is audited."""
    repo = factory.PRODUCTS / product
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "tests").mkdir(parents=True, exist_ok=True)
    log = {"product": product, "phases": []}
    audit.append(actor="project:controller", action="ProjectStart", resource=product, decision="executed")

    p = plan(product, goal, model=os.environ.get("AOS_ARCHITECT_MODEL"))
    by_id = {c["id"]: c for c in p["components"]}
    layers = topo_layers(p["components"])
    log["plan"] = {"components": list(by_id), "layers": layers}
    print(f"[project] plan: {len(by_id)} components in {len(layers)} dependency layers: {layers}", flush=True)

    built, workers = {}, int(os.environ.get("AOS_FLEET_WORKERS", "5"))
    for li, layer in enumerate(layers):               # BARRIER between layers (layer N needs N-1's interfaces)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(build_component, product, by_id[cid],
                              {d: by_id[d]["interface"] for d in by_id[cid].get("deps", [])}, api_key): cid
                    for cid in layer}
            for f in as_completed(futs):
                cid = futs[f]
                try:
                    built[cid] = f.result()
                except Exception as e:
                    built[cid] = {"id": cid, "passed": False, "error": str(e)}
        log["phases"].append({f"layer{li}": {cid: built[cid].get("passed") for cid in layer}})

    if all(b.get("passed") for b in built.values()):
        integ = integrate(product, p)
        log["integration"] = integ
        log["result"] = "INTEGRATED" if integ["passed"] else "BLOCKED_AT_INTEGRATION"
    else:
        failed = [cid for cid, b in built.items() if not b.get("passed")]
        log["result"] = "BLOCKED_AT_COMPONENTS"
        log["failed_components"] = failed

    audit.append(actor="project:controller", action="ProjectComplete", resource=product,
                 decision=log["result"], payload={"components": len(by_id), "layers": len(layers)})
    try:
        import appregistry
        appregistry.register(product, repo)
    except Exception:
        pass
    try:
        import notify
        notify.send(f"🧩 complex build '{product}': {log['result']} "
                    f"({len(by_id)} components, {len(layers)} layers)", title="project", tags="jigsaw")
    except Exception:
        pass
    print(f"\n[project] {product}: {log['result']}", flush=True)
    return log


def find_incomplete_projects(max_age_min: int = 20):
    """Complex builds INTERRUPTED, not finished: a proj-<product> with agent traces but NO terminal
    'ProjectComplete' audit (written for INTEGRATED and every BLOCKED_* outcome, so a genuinely finished
    or blocked project is never re-resumed), idle >= max_age_min, and with a docs/PLAN.json to resume
    from. Mirrors factory.find_incomplete_builds, but for the proj- run-id space + ProjectComplete."""
    out = []
    with psycopg.connect(factory._DB) as c, c.cursor() as cur:
        cur.execute("""
            SELECT t.run_id
              FROM traces t
              LEFT JOIN audit_log a
                     ON a.action='ProjectComplete' AND a.resource = substring(t.run_id from 6)
             WHERE t.run_id LIKE 'proj-%%'
             GROUP BY t.run_id
            HAVING count(a.id) = 0                                        -- no terminal verdict
               AND max(t.ts) < now() - (%s || ' minutes')::interval       -- and gone idle
             ORDER BY max(t.ts)
        """, (max_age_min,))
        for (run_id,) in cur.fetchall():
            product = run_id[len("proj-"):]
            if (factory.PRODUCTS / product / "docs" / "PLAN.json").exists():   # resumable: has a plan
                out.append(product)
    return out


def resume_incomplete_projects(max_age_min: int = 20, limit: int = 2) -> dict:
    """Self-healing for interrupted COMPLEX builds (provider outage / kill mid-run). Re-launches each as
    a DETACHED `project.py build <product> resume` — which reuses the existing plan and skips already-green
    components (component-level resume), so it finishes only what's missing. Returns fast (detached
    children), safe to call from the 120s-bounded scheduler."""
    cands = find_incomplete_projects(max_age_min)[:limit]
    resumed = []
    for product in cands:
        f = open(f"/tmp/resume-proj-{product}.log", "a")
        # 'resume' goal arg is ignored: plan() reuses docs/PLAN.json when present.
        subprocess.Popen([sys.executable, str(SCRIPTS / "project.py"), "build", product, "resume"],
                         stdout=f, stderr=f, stdin=subprocess.DEVNULL,
                         start_new_session=True, cwd=str(SCRIPTS.parent))
        audit.append(actor="project:resume-sweep", action="ResumeProject", resource=product,
                     decision="relaunched")
        resumed.append(product)
    if resumed:
        try:
            import notify
            notify.send(f"♻ auto-resumed {len(resumed)} interrupted complex build(s): " + ", ".join(resumed),
                        title="self-heal", tags="recycle")
        except Exception:
            pass
    return {"found": len(cands), "resumed": resumed}


def _selftest():
    """Offline proof of the graph engine (no agents): correct layering, cycle + bad-dep rejection."""
    comps = [{"id": "store", "deps": []},
             {"id": "core", "deps": ["store"]},
             {"id": "query", "deps": ["core"]},
             {"id": "cli", "deps": ["core", "query"]}]
    layers = topo_layers(comps)
    assert layers == [["store"], ["core"], ["query"], ["cli"]], layers
    assert validate_dag(comps) is True
    # independent components share a layer
    layers2 = topo_layers([{"id": "a", "deps": []}, {"id": "b", "deps": []}, {"id": "c", "deps": ["a", "b"]}])
    assert layers2 == [["a", "b"], ["c"]], layers2
    for bad in ([{"id": "x", "deps": ["y"]}],                                  # unknown dep
                [{"id": "x", "deps": ["y"]}, {"id": "y", "deps": ["x"]}],      # cycle
                [{"id": "x", "deps": []}, {"id": "x", "deps": []}]):           # duplicate id
        try:
            validate_dag(bad); raise AssertionError(f"should have rejected {bad}")
        except ValueError:
            pass
    print("PASS: project graph engine (layering, parallelism, cycle/bad-dep rejection) ✅")


def _main(a):
    if not a:
        sys.exit("usage: project.py build <product> '<goal>' | selftest")
    if a[0] == "build":
        print(build_complex(a[1], a[2] if len(a) > 2 else "Build a small multi-module Python product."))
    elif a[0] == "resume-sweep":                       # self-heal interrupted complex builds (scheduler)
        print(resume_incomplete_projects(max_age_min=int(a[1]) if len(a) > 1 else 20))
    elif a[0] == "selftest":
        _selftest()
    else:
        sys.exit(f"unknown command: {a[0]}")


if __name__ == "__main__":
    _main(sys.argv[1:])
