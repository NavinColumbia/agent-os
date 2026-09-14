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
import threading
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
# How many levels the architect tree may recurse: a component the architect marks "decompose" is itself
# planned into a sub-DAG (sub-architect → sub-builders → sub-integrator) up to this depth. depth 0 is the
# top, so AOS_MAX_DEPTH=1 allows the root plus one decomposed subsystem. Total LIVE agents across the whole
# tree stay bounded by factory._AGENT_SEM, but concurrency alone is insufficient: recursive plans multiply
# total work and wall time even when only a few agents run at once. The component budget bounds that pressure.
# (>~30 concurrent agents is RAM-bound on one box; 100s = the cloud worker-pool path, same Postgres queue.)
MAX_DEPTH = int(os.environ.get("AOS_MAX_DEPTH", "1"))
MAX_TOTAL_COMPONENTS = int(os.environ.get("AOS_MAX_TOTAL_COMPONENTS", "16"))


class ScopeBudgetExceeded(RuntimeError):
    pass


def _reserve_component_budget(budget, count, ns, depth):
    """Atomically reserve planned components across the whole recursive project tree.

    Recursive children in one DAG layer plan concurrently, so a plain counter can race past the limit. The
    shared lock makes the budget a real admission boundary. Planning may discover that a subtree is too large,
    but no builders for that subtree start after the reservation is refused.
    """
    with budget["lock"]:
        projected = budget["planned"] + int(count)
        if projected > budget["limit"]:
            raise ScopeBudgetExceeded(
                f"recursive project scope requires at least {projected} components; safe limit is "
                f"{budget['limit']} (namespace {ns or 'root'}, depth {depth}). Simplify the plan or explicitly "
                "raise AOS_MAX_TOTAL_COMPONENTS on a measured worker node."
            )
        budget["planned"] = projected
        return projected


# ── STACK DESCRIPTORS ─────────────────────────────────────────────────────────────────────────────────
# The hierarchical builder used to hardcode Python everywhere (architect: "ONE Python codebase"; builders:
# `src/<pkg>/__init__.py` + `python -m pytest`), so a browser web app got built as a Python library with a
# simulated DOM and QA had no URL to drive. A stack descriptor makes every builder/integrator/test-runner
# prompt speak the RIGHT language for the requested target. `python` reproduces the old behaviour exactly
# (default, zero regression); `web` builds a real, servable, no-build-step browser app that browser-QA can
# actually exercise. New stacks slot in here.
_STACK_PY = {
    "id": "python", "lang": "Python",
    "codebase": "ONE Python codebase",
    "component": "a Python package under src/{pkg}/ (create src/{pkg}/__init__.py; use `from src.{pkg}...` imports)",
    "deps_line": "import and call these — do NOT reimplement them",
    "tests": "Also write tests under tests/{pkg}/ covering this component. Touch ONLY src/{pkg}/** and "
             "tests/{pkg}/**. Make `python -m pytest -q tests/{pkg}` pass.",
    "fix_cmd": "`python -m pytest -q tests/{pkg}`",
    "integ_sub": "Build the FACADE package src/{base}/ (src/{base}/__init__.py) that composes those parts THROUGH "
                 "their interfaces (do NOT rewrite them) and exposes EXACTLY this public interface:\n{iface}\n"
                 "Write tests under {target}/ proving the facade. Use `from src...` imports. "
                 "Make `python -m pytest -q {target}` pass.",
    "integ_root": "Wire them into one coherent product: add the top-level entrypoint/orchestration under src/ that "
                  "composes the components THROUGH their public interfaces (do not rewrite internals), and write "
                  "END-TO-END INTEGRATION TESTS under tests/integration/ that exercise: {itests}. "
                  "Use `from src...` imports. Make `python -m pytest -q` (the WHOLE suite) pass.",
    "roles_pref": "",
}
_STACK_WEB = {
    "id": "web", "lang": "web (HTML/CSS/vanilla JS, runs in a browser)",
    "codebase": "ONE browser web app (HTML/CSS/vanilla JavaScript) — NO build step, NO bundler, NO external CDNs; "
                "it must run by opening index.html",
    "component": "an ES module folder src/{pkg}/ with an index.js that EXPORTS its public interface (plus any "
                 ".js/.css it owns); import a dependency via a RELATIVE path like `import {{...}} from "
                 "'../{pkg}/index.js'` — never a bundler alias",
    "deps_line": "import and call these via relative ES-module paths — do NOT reimplement them",
    "tests": "Also write tests under tests/{pkg}/ as framework-free `*.test.js` files (use node's built-in "
             "`node:assert` + `node:test`), importing the component's ES modules. Touch ONLY src/{pkg}/** and "
             "tests/{pkg}/**. Every test file must pass under `node --test tests/{pkg}`.",
    "fix_cmd": "`node --test tests/{pkg}`",
    "integ_sub": "Build the FACADE module src/{base}/index.js that composes those parts THROUGH their interfaces "
                 "(do NOT rewrite them) and exports EXACTLY this public interface:\n{iface}\n"
                 "Write `*.test.js` under {target}/ proving the facade. Every file must pass under "
                 "`node --test {target}`.",
    "integ_root": "Wire them into one coherent browser app: create index.html at the repo ROOT that loads the app "
                  "as an ES module (`<script type=\"module\">`) composing the components through their public "
                  "interfaces (do not rewrite internals) — NO build step, NO external CDNs, so it runs by opening "
                  "the file. Write END-TO-END `*.test.js` under tests/integration/ that exercise: {itests}. "
                  "Every test file must pass under `node --test`.\n"
                  "DEPLOYABLE, CONNECTED, END-TO-END (non-negotiable — this is the DEFINITION OF DONE): the "
                  "product must RUN as one unit a real person can visit. Create an executable run.sh at the repo "
                  "root that starts a server binding $PORT (default 8000) on 127.0.0.1 which serves BOTH the static "
                  "frontend AND the backend/API this app calls — at the SAME ORIGIN, so visiting the URL shows the "
                  "working app with live data (a same-origin SPA whose /api/* 404s is a FAILED deliverable). If the "
                  "app talks to an external/pre-existing backend, run.sh must start a server that PROXIES /api/* to "
                  "it. QA will run `bash run.sh` and drive the URL exactly as a user would — build for that.",
    "roles_pref": " PREFER frontend-engineer / fullstack-engineer for UI components.",
}
# Target platforms → the stack that actually builds+tests them on this box. Native mobile/desktop/game-engine
# targets have no toolchain/simulator here; they map to the closest buildable stack and are honestly flagged by
# QA rather than faked (see factory qa routing).
_PLATFORM_STACK = {
    "web": _STACK_WEB, "spa": _STACK_WEB, "webapp": _STACK_WEB, "pwa": _STACK_WEB,
    "game-web": _STACK_WEB, "browser-game": _STACK_WEB, "static-web": _STACK_WEB, "desktop-web": _STACK_WEB,
    "python": _STACK_PY, "service": _STACK_PY, "api": _STACK_PY, "cli": _STACK_PY, "lib": _STACK_PY,
}


def stack_for(platform):
    """Resolve a target platform (or stack id) to its build descriptor. Unknown/None → Python (safe default)."""
    if isinstance(platform, dict):
        return platform
    return _PLATFORM_STACK.get((platform or "").strip().lower(), _STACK_PY)


def _pkg(ns, cid):
    return ns + cid.replace("-", "_")


def _ensure_product_deps(repo, deps):
    """OSS-ASSEMBLY: when a component declares `reuse` packages AND AOS_ALLOW_DEPS is set, maintain a
    per-product venv with those deps installed (isolated from the platform venv) and record them in
    requirements.txt. Returns that venv's python, or '' to use the platform venv (stdlib-only default).
    Best-effort: any failure -> '' (the build proceeds stdlib-only rather than breaking)."""
    if not (deps and os.environ.get("AOS_ALLOW_DEPS")):
        return ""
    repo = Path(repo)
    req = repo / "requirements.txt"
    have = {l.strip() for l in (req.read_text().splitlines() if req.exists() else []) if l.strip() and not l.startswith("#")}
    have |= set(deps)
    req.write_text("\n".join(sorted(have)) + "\n")
    venv = repo / ".venv"; py = venv / "bin" / "python"
    try:
        if not py.exists():
            subprocess.run(["python3", "-m", "venv", str(venv)], capture_output=True, timeout=120, check=True)
            subprocess.run([str(py), "-m", "pip", "install", "-q", "pytest"], capture_output=True, timeout=300)
        subprocess.run([str(py), "-m", "pip", "install", "-q", *deps], capture_output=True, timeout=600)
        return str(py)
    except Exception:
        return ""


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
# Real specialized roles from the governed role library (~90 exist) — the architect assigns the best fit
# per component, so infra/SRE/ML/data/security work is built by the right specialist, not a generic builder.
_ROLES = ("builder | backend-engineer | frontend-engineer | fullstack-engineer | data-engineer | "
          "ml-engineer | mlops-engineer | devops-sre | platform-infra | database-admin | security-appsec | "
          "mobile-engineer | staff-engineer")


def _architect(product, goal, model, ns, depth, variant, out_name, stack=None):
    """One architect pass -> a validated plan written to docs/<out_name>. Supports recursion (decompose),
    role assignment per component, OSS reuse (when AOS_ALLOW_DEPS), an optional design `variant`, and the
    target STACK (Python vs web vs …) so the decomposition targets the right kind of codebase."""
    stack = stack or _STACK_PY
    repo = factory.PRODUCTS / product
    (repo / "docs").mkdir(parents=True, exist_ok=True)
    factory._ctx.product = product; factory._ctx.run = f"proj-{product}"; factory._ctx.stage = f"PLAN:{ns or 'root'}"
    can_recurse = depth < MAX_DEPTH
    decomp = (' A component that is ITSELF a large subsystem may be marked "decompose": true with a "subgoal" '
              "— it is planned recursively into its own sub-components. Mark decompose ONLY for genuinely "
              "large parts.") if can_recurse else ' Every component must be a LEAF (do NOT set decompose).'
    reuse = (' A component MAY set "reuse":["pypi-package",...] to BUILD ON proven open-source packages '
             'instead of reimplementing non-trivial wheels (they will be installed for that component).'
             if os.environ.get("AOS_ALLOW_DEPS") else ' Do NOT set "reuse" — standard library only.')
    var = f' DESIGN PHILOSOPHY for this plan: prioritise {variant}.' if variant else ""
    task = (
        f"You are the system ARCHITECT. Decompose this goal into 4-8 INTERDEPENDENT components of {stack['codebase']}. "
        f"GOAL:\n{goal}\n\n"
        f"Write {out_name} (under docs/) containing ONLY this JSON (no prose, no fences):\n"
        f'{{"components":[{{"id":"kebab-id","name":"short name","description":"what it does",'
        f'"deps":["other-id"],"interface":"the EXACT public functions/classes other components call — a '
        f'contract","role":"builder","decompose":false,"subgoal":"","reuse":[],"integrates":[]}}],'
        f'"integration_tests":"end-to-end behaviours that prove the components work TOGETHER"}}\n'
        f"Rules: 'deps' acyclic, referencing other component ids; ids kebab-case + UNIQUE; 'interface' "
        f"precise. Assign each component the best-fit 'role' from: {_ROLES}. A component that must talk to a "
        f"THIRD-PARTY system (e.g. Stripe, Twilio, Samsara) sets \"integrates\":[\"System name\"] — its docs "
        f"get researched before building.{stack.get('roles_pref','')}{decomp}{reuse}{var}")
    r = factory.agent("staff-engineer", str(repo), task, model=model)
    if r.get("failed"):
        raise RuntimeError(f"architect failed to plan: {r.get('out','')[:200]}")
    p = _load_json(repo / "docs" / out_name)
    if not can_recurse:
        for c in p["components"]:
            c["decompose"] = False
    validate_dag(p["components"])
    return p


def plan(product, goal, model=None, ns="", depth=0, stack=None):
    """Single-architect plan with RESUME (reuse an existing per-namespace plan file)."""
    repo = factory.PRODUCTS / product
    pj = repo / "docs" / (f"PLAN_{ns.rstrip('_')}.json" if ns else "PLAN.json")
    if pj.exists():
        try:
            p = _load_json(pj); validate_dag(p["components"])
            print(f"[project] resuming — reusing plan {pj.name} ({len(p['components'])} components)", flush=True)
            return p
        except Exception:
            pass
    p = _architect(product, goal, model, ns, depth, None, pj.name, stack=stack)
    audit.append(actor="project:architect", action="Plan", resource=product, decision="executed",
                 payload={"ns": ns or "root", "depth": depth, "components": [c["id"] for c in p["components"]]})
    return p


def _judge_plans(product, goal, cands):
    """A judge agent picks the best candidate architecture. Returns the chosen plan dict."""
    repo = factory.PRODUCTS / product
    summary = "\n".join(
        f"CANDIDATE {i}: components=" + ", ".join(f"{c['id']}({c.get('role','builder')})" for c in p["components"])
        for i, p in cands.items())
    factory._ctx.stage = "JUDGE"
    factory.agent("reviewer", str(repo),
                  f"Choose the BEST architecture for this goal:\n{goal}\n\nCandidates (component breakdowns):\n"
                  f"{summary}\n\nWeigh clarity, cohesion, testability and right-sized decomposition. Write "
                  f'docs/JUDGE.json containing ONLY {{"best": <candidate index>, "why": "one line"}}.')
    try:
        best = int(_load_json(repo / "docs" / "JUDGE.json").get("best"))
    except Exception:
        best = min(cands)
    return cands.get(best, cands[min(cands)])


def explore_plan(product, goal, model=None, ns="", depth=0, n=1, stack=None):
    """PARALLEL DESIGN EXPLORATION: generate n candidate architectures (different design philosophies) at
    once, judge, and build the best. 'More budget -> better, not just more.' n<=1 (or resume) = single plan."""
    repo = factory.PRODUCTS / product
    pj = repo / "docs" / (f"PLAN_{ns.rstrip('_')}.json" if ns else "PLAN.json")
    if pj.exists() or n <= 1:
        return plan(product, goal, model, ns, depth, stack=stack)
    variants = ["simplicity and the fewest moving parts", "clean modular boundaries and testability",
                "robustness, validation and explicit error handling", "performance and scalability"]
    base = ns.rstrip("_") or "root"
    cands, workers = {}, int(os.environ.get("AOS_FLEET_WORKERS", "5"))
    with ThreadPoolExecutor(max_workers=min(n, workers)) as ex:
        futs = {ex.submit(_architect, product, goal, model, ns, depth, variants[i % len(variants)],
                          f"cand_{base}_{i}.json", stack): i for i in range(n)}
        for f in as_completed(futs):
            i = futs[f]
            try:
                cands[i] = f.result()
            except Exception:
                pass
    if not cands:
        return plan(product, goal, model, ns, depth, stack=stack)
    chosen = list(cands.values())[0] if len(cands) == 1 else _judge_plans(product, goal, cands)
    (repo / "docs" / pj.name).write_text(json.dumps(chosen, indent=2))
    audit.append(actor="project:architect", action="Plan", resource=product, decision="explored",
                 payload={"ns": ns or "root", "candidates": len(cands), "components": [c["id"] for c in chosen["components"]]})
    return chosen


def build_component(product, comp, dep_interfaces, api_key=None, ns="", stack=None):
    """Build ONE LEAF component as src/<ns><pkg>/ with its own tests, coding against its interface contract
    and its dependencies' interfaces. Runs in a worker thread, so it sets its OWN thread-local _ctx. Bounded
    per-component test loop. Disjoint namespaced paths make same-layer builds (and whole sub-trees)
    collision-free. On failure returns a `blocker` reason that propagates up the tree for aggregation.
    `stack` selects the language/layout/test-runner (Python default; web = ES modules + node tests)."""
    stack = stack or _STACK_PY
    repo = factory.PRODUCTS / product
    cid = comp["id"]; pkg = _pkg(ns, cid)
    role = comp.get("role") or "builder"              # ROLE SPECIALIZATION (infra/data/ml/staff/builder)
    pybin = _ensure_product_deps(repo, comp.get("reuse"))  # OSS-ASSEMBLY: per-product venv if deps declared
    factory._ctx.api_key = api_key                    # thread-local: must be set inside this worker thread
    factory._ctx.product = product; factory._ctx.run = f"proj-{product}"; factory._ctx.stage = f"BUILD:{pkg}"
    # RESUME: if this component was already built green in a prior (interrupted) run, skip it.
    if (repo / "src" / pkg).exists():
        pre_ok, _ = factory.run_tests(str(repo), target=f"tests/{pkg}", python=pybin, stack=stack["id"])
        if pre_ok:
            print(f"[project] component {cid}: already green — skipping (resume)", flush=True)
            return {"id": cid, "passed": True, "resumed": True, "fix_attempts": 0}
    aid = f"{role}@{product}:{cid}"
    try:
        import directory                              # live coordination: claim disjoint paths, detect overlap
        directory.register(aid, role, product, f"BUILD:{cid}", [f"src/{pkg}/**", f"tests/{pkg}/**"])
    except Exception:
        pass
    deps_block = "\n".join(f"- {d}: {i}" for d, i in dep_interfaces.items()) or "(no dependencies)"
    reuse_block = (f"\nYou MAY (and should, where it saves real work) use these INSTALLED open-source "
                   f"packages — import them directly, don't reimplement: {', '.join(comp['reuse'])}."
                   if comp.get("reuse") and pybin else "")
    integ_block = ""                                  # EXTERNAL-INTEGRATION RESEARCH: read the API docs first
    if comp.get("integrates"):
        nf = repo / "docs" / f"integration-{pkg}.md"
        factory.agent("research-growth", str(repo),
                      f"Research the external system(s) {comp['integrates']} that component '{cid}' must "
                      f"integrate. Use WebSearch/WebFetch to read their official API docs. Write "
                      f"docs/integration-{pkg}.md with: auth model, key endpoints, request/response shapes, "
                      f"rate limits, the official SDK/package name, and gotchas — cite source URLs. Do NOT "
                      f"use real credentials; this is reference for the builder.")
        notes = nf.read_text()[:1400] if nf.exists() else ""
        integ_block = (f"\nThis component INTEGRATES external system(s): {comp['integrates']}. Code against the "
                       f"researched API notes in docs/integration-{pkg}.md (summary below). Use a config/env for "
                       f"any credentials (never hardcode); real credential use is approval-gated.\n{notes}")
    task = (
        f"Implement component '{cid}' of a LARGER system as {stack['component'].format(pkg=pkg)}.\n"
        f"COMPONENT: {comp['name']} — {comp['description']}\n"
        f"THE PUBLIC INTERFACE YOU MUST EXPOSE (your dependents rely on this EXACT contract):\n{comp['interface']}\n"
        f"INTERFACES OF YOUR DEPENDENCIES ({stack['deps_line']}):\n{deps_block}{reuse_block}{integ_block}\n"
        + stack["tests"].format(pkg=pkg))
    factory.agent(role, str(repo), task)
    ok, out = factory.run_tests(str(repo), target=f"tests/{pkg}", python=pybin, stack=stack["id"])
    attempts = 0
    while not ok and attempts < MAX_COMPONENT_FIX:
        attempts += 1
        print(f"[project] component {cid}: test red — fix {attempts}/{MAX_COMPONENT_FIX}", flush=True)
        factory.agent(role, str(repo),
                      f"Component '{cid}' tests FAILING:\n\n{out[-1500:]}\n\nFix src/{pkg}/** (or a genuinely "
                      f"wrong test) so {stack['fix_cmd'].format(pkg=pkg)} passes. Keep the public interface intact.")
        ok, out = factory.run_tests(str(repo), target=f"tests/{pkg}", python=pybin, stack=stack["id"])
    try:
        import directory
        directory.release(aid)
    except Exception:
        pass
    return {"id": cid, "pkg": pkg, "passed": ok, "fix_attempts": attempts,
            "blocker": None if ok else f"leaf '{pkg}' tests still red after {attempts} fixes: {out[-240:]}"}


def integrate(product, p, ns="", facade=None, stack=None):
    """INTEGRATOR wires this level's built components into a coherent whole. At the ROOT (ns="") it builds
    the product entrypoint + end-to-end tests and gates on the WHOLE suite. At a SUB level (ns set) it builds
    a FACADE package src/<base> that composes the sub-components (src/<ns>*) and exposes the parent
    component's exact interface — so a decomposed component looks identical to a leaf to its dependents.
    Bounded integration-fix loop. `stack` selects the language/entrypoint/test-runner."""
    stack = stack or _STACK_PY
    repo = factory.PRODUCTS / product
    factory._ctx.product = product; factory._ctx.run = f"proj-{product}"; factory._ctx.stage = f"INTEGRATE:{ns or 'root'}"
    comp_list = ", ".join(_pkg(ns, c["id"]) for c in p["components"])
    if ns:
        base = ns.rstrip("_")
        target = f"tests/{base}"
        task = (
            f"You are the INTEGRATOR for subsystem '{facade.get('name', base) if facade else base}'. Its parts "
            f"are built as packages ({comp_list}) under src/. "
            + stack["integ_sub"].format(base=base, target=target,
                                         iface=facade.get('interface', '') if facade else ''))
    else:
        target = ""
        task = (
            f"You are the INTEGRATOR. The components ({comp_list}) are built as packages under src/. "
            + stack["integ_root"].format(
                itests=p.get('integration_tests', 'the components working together')))
    factory.agent("staff-engineer", str(repo), task)
    ok, out = factory.run_tests(str(repo), target=target, stack=stack["id"])
    attempts = 0
    while not ok and attempts < MAX_INTEGRATION_FIX:
        attempts += 1
        print(f"[project] integrate({ns or 'root'}) red — fix {attempts}/{MAX_INTEGRATION_FIX}", flush=True)
        factory.agent("staff-engineer", str(repo),
                      f"Integration/tests FAILING:\n\n{out[-1800:]}\n\nFix the wiring or a genuinely wrong "
                      f"integration test (do NOT weaken component tests). Make the integration tests pass.")
        ok, out = factory.run_tests(str(repo), target=target, stack=stack["id"])
    return {"passed": ok, "fix_attempts": attempts, "tail": out[-400:],
            "blocker": None if ok else f"integration({ns or 'root'}) red after {attempts} fixes: {out[-240:]}"}


def _git_init(repo):
    """git-init a freshly built product so the DEV-FIX loop can DIFF to verify its own fixes. Without this the
    fix-verifier reported 'no change evidence — not a git checkout' and flip-flopped fixed:False/True, so the
    loop couldn't converge. Best-effort, idempotent."""
    repo = Path(repo)
    try:
        if (repo / ".git").exists():
            return
        subprocess.run(["git", "init", "-q"], cwd=str(repo), timeout=30, check=False)
        (repo / ".gitignore").write_text("node_modules/\n.venv/\n__pycache__/\n*.db\n*.log\n")
        subprocess.run(["git", "add", "-A"], cwd=str(repo), timeout=60, check=False)
        subprocess.run(["git", "-c", "user.email=fleet@agent-os", "-c", "user.name=agent-os",
                        "commit", "-q", "-m", "initial build"], cwd=str(repo), timeout=60, check=False)
    except Exception:
        pass


def _finalize_deployable(repo, stack, log):
    """Ensure a top-level build is a RUNNABLE, CONNECTED unit + git-tracked. Sets log['deployable'].
    For a UI stack a run.sh (serves frontend + backend at one origin) is the required handoff QA runs; its
    ABSENCE is a real deliverable gap we record honestly rather than paper over."""
    repo = Path(repo)
    _git_init(repo)
    runsh = repo / "run.sh"
    is_ui = (stack or _STACK_PY).get("id") == "web"
    log["deployable"] = bool(runsh.exists()) if is_ui else True
    if is_ui and not runsh.exists():
        log["deploy_gap"] = ("no run.sh — the integrator did not produce a connected run-target; QA cannot "
                             "bring up a same-origin app (static-serving would 404 every /api/*)")
        print(f"[project] DEPLOY GAP: {log['deploy_gap']}", flush=True)


def build_complex(product, goal, api_key=None, depth=0, ns="", facade=None, stack=None, _budget=None):
    """Drive ONE complex product end-to-end as a RECURSIVE tree: PLAN -> dependency-ordered parallel builds
    (each component either a LEAF builder OR, if the architect marked it 'decompose', a recursive sub-build
    with its own architect/builders/integrator) -> INTEGRATE this level. Blockers from any leaf or sub-tree
    propagate UP and aggregate; results aggregate UP through each integrator. Total live agents across the
    whole tree stay bounded by factory._AGENT_SEM regardless of depth/width. depth 0 = the root product.
    `stack` is the target platform/descriptor (e.g. 'web', 'python') — it decides the language, file layout
    and test runner every architect/builder/integrator prompt uses; None → Python (backward compatible)."""
    # A detached recovery used to enter here with stack=None and silently fall back to Python, even when the
    # controller originally started a web build. Recover the top-level choice from its immutable ProjectStart
    # record before applying the backwards-compatible default. Recursive calls always receive the descriptor.
    if depth == 0 and stack is None:
        stack = _persisted_stack(product)
    stack = stack_for(stack)
    if _budget is None:
        _budget = {"planned": 0, "limit": max(1, MAX_TOTAL_COMPONENTS), "lock": threading.Lock()}
    # ThreadPoolExecutor workers do not inherit threading.local state. Capture the controller's checkpoint
    # callback once and explicitly propagate it into component workers so parallel progress renews the same
    # durable IMPLEMENT lease instead of looking silent until the whole layer finishes.
    progress_callback = getattr(factory._ctx, "progress_callback", None)
    repo = factory.PRODUCTS / product
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "tests").mkdir(parents=True, exist_ok=True)
    top = depth == 0
    indent = "  " * depth
    log = {"product": product, "ns": ns or "root", "depth": depth, "phases": [], "stack": stack["id"]}
    if top:
        audit.append(actor="project:controller", action="ProjectStart", resource=product, decision="executed",
                     payload={"stack": stack["id"]})
    factory._emit_progress(f"PLAN:{ns or 'root'}", "started", {"depth": depth})

    p = explore_plan(product, goal, model=os.environ.get("AOS_ARCHITECT_MODEL"), ns=ns, depth=depth,
                     n=int(os.environ.get("AOS_EXPLORATION", "1")), stack=stack)
    factory._emit_progress(f"PLAN:{ns or 'root'}", "completed",
                           {"depth": depth, "components": len(p.get("components") or [])})
    _reserve_component_budget(_budget, len(p["components"]), ns, depth)
    by_id = {c["id"]: c for c in p["components"]}
    layers = topo_layers(p["components"])
    log["plan"] = {"components": list(by_id), "layers": layers}
    n_decomp = sum(1 for c in p["components"] if c.get("decompose") and depth < MAX_DEPTH)
    print(f"{indent}[project] L{depth} {ns or 'root'}: {len(by_id)} components, {len(layers)} layers, "
          f"{n_decomp} to decompose further", flush=True)

    def build_one(comp):
        if progress_callback:
            factory._ctx.progress_callback = progress_callback
        cid = comp["id"]
        factory._emit_progress(f"BUILD:{_pkg(ns, cid)}", "started", {"depth": depth})
        if comp.get("decompose") and depth < MAX_DEPTH:       # RECURSE: this component is its own subsystem
            sub = build_complex(product, comp.get("subgoal") or comp["description"], api_key,
                                depth + 1, ns=f"{_pkg(ns, cid)}_", facade=comp, stack=stack,
                                _budget=_budget)
            result = {"id": cid, "pkg": _pkg(ns, cid), "passed": sub["passed"],
                      "blocker": sub.get("blocker"),
                      "sub": {"result": sub["result"], "layers": sub.get("plan", {}).get("layers")}}
            factory._emit_progress(f"BUILD:{_pkg(ns, cid)}", "completed",
                                   {"depth": depth, "passed": bool(result["passed"])})
            return result
        dep_ifaces = {d: by_id[d]["interface"] for d in comp.get("deps", [])}
        result = build_component(product, comp, dep_ifaces, api_key, ns, stack=stack)
        factory._emit_progress(f"BUILD:{_pkg(ns, cid)}", "completed",
                               {"depth": depth, "passed": bool(result.get("passed"))})
        return result

    built, workers = {}, int(os.environ.get("AOS_FLEET_WORKERS", "5"))
    for li, layer in enumerate(layers):               # BARRIER between layers (layer N needs N-1's interfaces)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(build_one, by_id[cid]): cid for cid in layer}
            for f in as_completed(futs):
                cid = futs[f]
                try:
                    built[cid] = f.result()
                except Exception as e:
                    built[cid] = {"id": cid, "passed": False, "blocker": f"crashed: {e}"}
        log["phases"].append({f"layer{li}": {cid: built[cid].get("passed") for cid in layer}})
        factory._emit_progress(f"LAYER:{ns or 'root'}:{li}", "completed",
                               {"depth": depth, "components": len(layer),
                                "passed": sum(bool(built[cid].get("passed")) for cid in layer)})

    blockers = [b["blocker"] for b in built.values() if not b.get("passed") and b.get("blocker")]
    if all(b.get("passed") for b in built.values()):
        factory._emit_progress(f"INTEGRATE:{ns or 'root'}", "started", {"depth": depth})
        integ = integrate(product, p, ns=ns, facade=facade, stack=stack)
        factory._emit_progress(f"INTEGRATE:{ns or 'root'}", "completed",
                               {"depth": depth, "passed": bool(integ.get("passed"))})
        log["integration"] = integ
        if integ["passed"]:
            log["result"], log["passed"], log["blocker"] = "INTEGRATED", True, None
        else:
            log["result"], log["passed"], log["blocker"] = "BLOCKED_AT_INTEGRATION", False, integ.get("blocker")
    else:
        log["result"], log["passed"] = "BLOCKED_AT_COMPONENTS", False
        log["failed_components"] = [cid for cid, b in built.items() if not b.get("passed")]
        log["blocker"] = " | ".join(blockers)[:600]   # aggregated, bubbles up to the parent level

    if top and log["result"] == "INTEGRATED":         # SCALABLE VERIFICATION: assurance scales with budget
        rigor = int(os.environ.get("AOS_RIGOR", "1"))
        if rigor > 1:
            try:
                import verify
                v = verify.verify(product, rigor=rigor, api_key=api_key, stack=(stack or {}).get("id"))
                log["verification"] = {"rigor": rigor, "passed": v.get("passed"),
                                       "checks": [(c["check"], c["ok"]) for c in v.get("passes", [])]}
                if not v.get("passed"):               # verification is a real gate at rigor>1
                    log["result"], log["passed"] = "BLOCKED_AT_VERIFY", False
            except Exception as e:                    # fail CLOSED: an unverifiable build is NOT INTEGRATED
                log["verification"] = {"error": str(e)[:160]}
                log["result"], log["passed"] = "BLOCKED_AT_VERIFY", False
    if top and log["result"] == "INTEGRATED":
        # DEPLOYABILITY GATE (grand-scheme / end-to-end): a build is only DONE if it can RUN as one connected
        # unit a person can visit. For a UI stack, a run.sh serving the app+backend at one origin is required;
        # if the integrator didn't emit one, that's a real gap — flag it (QA will then correctly fail to bring
        # up a connected app rather than silently static-serving a backend-less shell).
        _finalize_deployable(repo, stack, log)
    if top:
        audit.append(actor="project:controller", action="ProjectComplete", resource=product,
                     decision=log["result"], payload={"components": len(by_id), "layers": len(layers),
                                                       "max_depth_reached": depth, "deployable": log.get("deployable")})
        try:
            import appregistry
            appregistry.register(product, repo)
        except Exception:
            pass
        try:
            import notify
            notify.send(f"🧩 complex build '{product}': {log['result']} ({len(by_id)} top components)",
                        title="project", tags="jigsaw")
        except Exception:
            pass
    print(f"{indent}[project] L{depth} {ns or 'root'}: {log['result']}", flush=True)
    return log


def _persisted_stack(product: str):
    """Return the authoritative original stack, preferring the controller's durable product plan.

    A faulty detached resume may itself append a newer ProjectStart with the wrong default stack. Therefore
    latest-wins is unsafe: controller plan wins, otherwise the first start fixes the project's stack identity.
    """
    try:
        with psycopg.connect(factory._DB) as c, c.cursor() as cur:
            cur.execute("""SELECT plan->>'platform' FROM controller_state
                            WHERE product=%s AND NULLIF(plan->>'platform','') IS NOT NULL
                            ORDER BY updated_at DESC LIMIT 1""", (product,))
            row = cur.fetchone()
            if row and row[0]:
                return row[0]
            cur.execute("""SELECT payload->>'stack' FROM audit_log
                            WHERE action='ProjectStart' AND resource=%s AND payload ? 'stack'
                            ORDER BY id ASC LIMIT 1""", (product,))
            row = cur.fetchone()
        return row[0] if row and row[0] else None
    except Exception:
        return None


def _project_process_alive(product: str) -> bool:
    """Exact argv liveness guard for detached complex builds; probe failure is fail-closed."""
    try:
        proc = Path("/proc")
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                argv = (entry / "cmdline").read_bytes().split(b"\0")
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
            args = [a.decode(errors="replace") for a in argv if a]
            for i, arg in enumerate(args[:-2]):
                if arg.endswith("project.py") and args[i + 1:i + 3] == ["build", product]:
                    return True
        return False
    except Exception:
        return True


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
             WHERE t.run_id LIKE 'proj-%%' AND NOT t.test_run
               AND NOT EXISTS (SELECT 1 FROM kill_switch k
                               WHERE k.scope IN ('global', substring(t.run_id from 6)))
               -- A controller parked on fleet still OWNS this product even when its agent trace is quiet.
               -- The former trace-only heuristic double-launched the same repo from the scheduler.
               AND NOT EXISTS (
                    SELECT 1 FROM controller_state cs
                     WHERE cs.product = substring(t.run_id from 6)
                       AND (cs.awaiting='fleet' OR EXISTS (
                            SELECT 1 FROM controller_jobs cj
                             WHERE cj.thread_id=cs.thread_id AND cj.status IN ('running','pending'))))
             GROUP BY t.run_id
            HAVING count(a.id) = 0                                        -- no terminal verdict
               AND max(t.ts) < now() - (%s || ' minutes')::interval       -- and gone idle
             ORDER BY max(t.ts)
        """, (max_age_min,))
        for (run_id,) in cur.fetchall():
            product = run_id[len("proj-"):]
            if _project_process_alive(product):              # direct recovery/build still owns it
                continue
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
        # Multiple scheduler processes can overlap. Serialize the final liveness check + spawn per product;
        # the new child is visible in /proc before this transaction unlocks, so the next contender skips it.
        with psycopg.connect(factory._DB) as claim_conn, claim_conn.cursor() as claim_cur:
            claim_cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                              (f"project-resume:{product}",))
            claim_cur.execute("""SELECT EXISTS (
                SELECT 1 FROM kill_switch WHERE scope IN ('global', %s)) OR EXISTS (
                SELECT 1 FROM controller_state cs WHERE cs.product=%s AND
                  (cs.awaiting='fleet' OR EXISTS (SELECT 1 FROM controller_jobs cj
                    WHERE cj.thread_id=cs.thread_id AND cj.status IN ('running','pending'))))""",
                              (product, product))
            if claim_cur.fetchone()[0] or _project_process_alive(product):
                continue
            stack = _persisted_stack(product)
            argv = [sys.executable, str(SCRIPTS / "project.py"), "build", product, "resume"]
            if stack:
                argv += ["--stack", stack]
            f = open(f"/tmp/resume-proj-{product}.log", "a")
            subprocess.Popen(argv, stdout=f, stderr=f, stdin=subprocess.DEVNULL,
                             start_new_session=True, cwd=str(SCRIPTS.parent))
            claim_conn.commit()
        audit.append(actor="project:resume-sweep", action="ResumeProject", resource=product,
                     decision="relaunched", payload={"stack": stack or "legacy-default"})
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
        stack = a[a.index("--stack") + 1] if "--stack" in a and a.index("--stack") + 1 < len(a) else None
        print(build_complex(a[1], a[2] if len(a) > 2 else "Build a small multi-module Python product.",
                            stack=stack))
    elif a[0] == "resume-sweep":                       # self-heal interrupted complex builds (scheduler)
        print(resume_incomplete_projects(max_age_min=int(a[1]) if len(a) > 1 else 20))
    elif a[0] == "selftest":
        _selftest()
    else:
        sys.exit(f"unknown command: {a[0]}")


if __name__ == "__main__":
    _main(sys.argv[1:])
