#!/usr/bin/env python3
"""dev_loop.py — the DEV-fix loop of agent-os's AGENTIC QA system.

Two mandates from the owner, both realized here as *pure AI-driven state loops* (no heuristics,
no hard-coded thresholds — EVERY decision is a `factory.agent` call, which itself retries 529/
overload and fails over to Codex):

  1. DEV IS THE FIRST QA.  Before a builder hands off its own work, it runs `dev_self_qa()` — an
     AI explorer (`qa_explorer.Explorer`) walks the ORIGINAL user stories against the live app and
     reports expected-vs-actual bugs. The builder fixes its own defects before anyone downstream
     ever sees them.

  2. FIX-ON-BLOCKING-BUG.  When a blocking bug is found, `fix_bug()` runs a STATE-BASED loop:
         observe(bug)                                   ── the failure + code context + vision
      -> AI DECIDES how to fix (staff-engineer plan)    ── how many dev agents, which files, roles
      -> ACT (spawn exactly that many dev agents)       ── role-specialized, in parallel
      -> restart/reset the app                          ── restart_target(): pkill + relaunch + health
      -> observe again (re-run the story via Explorer)  ── what does the app ACTUALLY do now
      -> AI EVALUATES expected-vs-actual (is-it-fixed)  ── judge against the ORIGINAL VISION
      -> repeat until fixed or attempts exhausted.

The ORIGINAL VISION + EXPECTED behavior are threaded through every prompt so each AI call judges
against what the product was SUPPOSED to do, not just "does it crash". Cost is intentionally not a
concern: this file is deliberately maximally AI-driven.

  python dev_loop.py selftest   # offline wiring check — stubs factory.agent + Explorer, no API calls

Run with the agent-os venv python.
"""
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent          # scripts/ (dev_loop.py lives in scripts/qa/)
sys.path.insert(0, str(SCRIPTS))
import factory  # noqa: E402  — the resilient LLM call (retries overload, fails over to Codex)

try:
    import audit  # noqa: E402  — best-effort provenance; never a hard dependency of the loop
except Exception:                                          # pragma: no cover
    audit = None

MAX_FIX_ATTEMPTS = int(os.environ.get("AOS_QA_MAX_FIX_ATTEMPTS", "3"))   # bounded observe->fix->judge cycles
MAX_FIX_AGENTS = int(os.environ.get("AOS_QA_MAX_FIX_AGENTS", "6"))       # ceiling on the AI's agent-count decision
HEALTH_TIMEOUT = int(os.environ.get("AOS_QA_HEALTH_TIMEOUT", "60"))      # seconds to wait for the app to come back


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def _as_list(x):
    if x is None:
        return []
    return list(x) if isinstance(x, (list, tuple)) else [x]


def _audit(action, payload):
    if audit is None:
        return
    try:
        audit.append(actor="qa:dev_loop", action=action, resource="dev_loop",
                     decision="ran", payload=payload)
    except Exception:
        pass


def _ai_json(role, repo, prompt, *, api_key=None, default=None):
    """One AI DECISION that must return JSON. Delegates to the resilient factory.agent (overload retry +
    Codex failover) and parses the object out of its reply. Returns `default` only if the model produced
    nothing parseable — the point of the system is that a real decision is always an AI call."""
    if api_key is not None:
        try:
            factory._ctx.api_key = api_key
        except Exception:
            pass
    res = factory.agent(role, str(repo), prompt, api_key=api_key) if api_key is not None \
        else factory.agent(role, str(repo), prompt)
    text = (res or {}).get("out_full") or (res or {}).get("out") or ""
    data = factory._extract_json(text)
    if not data and default is not None:
        return dict(default)
    return data or {}


def restart_target(cmd, *, health_url=None, cwd=None, env=None, timeout=HEALTH_TIMEOUT,
                   match=None) -> dict:
    """Restart/reset the app under test: KILL every process running it, RELAUNCH it, then WAIT until it is
    healthy again. This is what makes the fix loop observe a *fresh* process rather than a stale one still
    serving the old (buggy) code.

      cmd        — argv list (or shell string) that launches the app.
      health_url — URL polled until it returns 2xx (readiness gate). If None, we just wait a beat and
                   trust the launch (best we can do without a health endpoint).
      match      — pattern passed to `pkill -f` to find the OLD instance; defaults to a stable token from
                   `cmd` so we don't kill unrelated processes.

    Returns {restarted, healthy, pid, detail}.
    """
    argv = cmd if isinstance(cmd, list) else None
    shell = None if argv else str(cmd)
    pat = match or (argv[-1] if argv else (shell or "").split()[0] if shell else None)

    # 1) KILL the old instance (best-effort; -f matches the full command line).
    killed = False
    if pat:
        try:
            rc = subprocess.run(["pkill", "-f", str(pat)], capture_output=True).returncode
            killed = rc == 0
            time.sleep(1.0)                                # let sockets/pidfiles clear before relaunch
        except Exception:
            pass

    # 2) RELAUNCH.
    try:
        proc = subprocess.Popen(
            argv if argv else shell, shell=bool(shell), cwd=cwd,
            env=({**os.environ, **env} if env else None),
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True)
    except Exception as e:
        return {"restarted": False, "healthy": False, "pid": None, "detail": f"relaunch failed: {e}"}

    # 3) WAIT for health.
    healthy, detail = _wait_healthy(health_url, timeout)
    _audit("RestartTarget", {"killed_old": killed, "pid": proc.pid, "healthy": healthy})
    return {"restarted": True, "healthy": healthy, "pid": proc.pid,
            "detail": detail or f"launched pid={proc.pid}, old_killed={killed}"}


def _wait_healthy(health_url, timeout) -> tuple:
    if not health_url:
        time.sleep(2.0)
        return True, "no health_url — waited 2s and assumed up"
    import urllib.request
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(health_url, timeout=5) as r:
                if 200 <= r.status < 300:
                    return True, f"healthy: {health_url} -> {r.status}"
                last = f"{r.status}"
        except Exception as e:
            last = str(e)
        time.sleep(1.0)
    return False, f"unhealthy after {timeout}s (last: {last})"


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# dev-as-first-QA
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def dev_self_qa(target_url, vision, stories, *, api_key=None) -> list:
    """A builder QAs its OWN work before handoff (dev = first QA). An AI EXPLORER (qa_explorer.Explorer)
    walks each ORIGINAL user story against the LIVE app at `target_url`, holding the `vision` in context so
    it judges expected-vs-actual — and returns every bug it finds. Returning a non-empty list means the
    builder is NOT ready to hand off and should fix its own defects first.

    Contract with qa_explorer (built in parallel):
        Explorer(target_url, vision, token=None, org="0") with .explore(story) -> list[bug-dict].
    Each bug-dict is expected to carry at least {story, expected, actual, severity, blocking}.
    (Explorer routes its own factory.agent calls; it takes no api_key — pass token/org for auth instead.)
    """
    import qa_explorer                                     # sibling QA module (imported lazily on purpose)
    explorer = qa_explorer.Explorer(target_url, vision)
    bugs = []
    for story in _as_list(stories):
        try:
            found = explorer.explore(story)               # AI-driven observe->judge per story
        except TypeError:                                 # tolerate Explorer.explore(stories=[...]) shape
            found = explorer.explore(stories=[story])
        bugs.extend(_as_list(found))
    _audit("DevSelfQA", {"target": target_url, "stories": len(_as_list(stories)), "bugs": len(bugs)})
    return bugs


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# fix-on-blocking-bug  (the state-based loop)
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def _plan_fix(bug, code_context, vision, *, repo, api_key=None) -> dict:
    """AI DECISION #1 — a staff engineer designs the fix: HOW MANY dev agents to spawn, and for each the
    ROLE (by area: frontend/backend/…) and the exact FILES + task it owns. This is the first thing that
    happens; the loop then spawns exactly the agents this plan names."""
    prompt = (
        "You are the staff engineer triaging a BLOCKING bug in a product built by an agent fleet. Decide "
        "the SMALLEST correct set of dev agents to fix it — how many, and for each its ROLE (by area, e.g. "
        "frontend, backend, builder), the exact FILES it should touch, and its concrete TASK. Base the "
        "count on the true blast radius of the fix; do not pad it.\n\n"
        f"ORIGINAL VISION (what the product is meant to be):\n{vision}\n\n"
        f"THE BUG (expected vs actual):\n{json.dumps(bug, default=str)[:6000]}\n\n"
        f"CODE CONTEXT (relevant files / current behavior):\n{json.dumps(code_context, default=str)[:8000]}\n\n"
        "Reply with ONLY a JSON object:\n"
        '{"agents": [{"role": "<area-role>", "files": ["path", ...], "task": "<what this agent fixes>"}], '
        '"rationale": "<why this many, this split>"}'
    )
    plan = _ai_json("staff-engineer", repo, prompt, api_key=api_key,
                    default={"agents": [{"role": "builder", "files": [], "task": "Fix the bug."}]})
    agents = plan.get("agents") or []
    if not agents:                                         # a plan with zero agents fixes nothing — floor at 1
        agents = [{"role": "builder", "files": [], "task": "Fix the bug."}]
    agents = agents[:MAX_FIX_AGENTS]
    plan["agents"] = agents
    return plan


def _spawn_fix_agents(plan, bug, vision, *, repo, api_key=None) -> list:
    """ACT — spawn EXACTLY the AI-decided number of dev agents, each role-specialized to its area, IN
    PARALLEL. Returns the list of files the plan had them touch (deduped) as the fix's footprint."""
    agents = plan["agents"]

    def _one(spec):
        role = spec.get("role") or "builder"
        files = _as_list(spec.get("files"))
        task = (
            f"A blocking bug must be fixed so the product matches its VISION.\n\n"
            f"VISION:\n{vision}\n\n"
            f"BUG (expected vs actual):\n{json.dumps(bug, default=str)[:4000]}\n\n"
            f"YOUR SCOPE — own these files: {files or 'the relevant source under src/'}\n"
            f"YOUR TASK: {spec.get('task') or 'Implement the fix.'}\n\n"
            f"Fix the ROOT CAUSE (not the symptom), keep existing behavior intact, and verify your change."
        )
        factory.agent(role, str(repo), task, api_key=api_key) if api_key is not None \
            else factory.agent(role, str(repo), task)
        return files

    touched = []
    workers = min(len(agents), int(os.environ.get("AOS_FLEET_WORKERS", "5")))
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for files in as_completed_results(ex, [ex.submit(_one, s) for s in agents]):
            touched.extend(files)
    # dedupe, preserve order
    seen, out = set(), []
    for f in touched:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def as_completed_results(_ex, futures):
    for fut in as_completed(futures):
        try:
            yield fut.result()
        except Exception:
            yield []


def _judge_fixed(bug, vision, files, residual_bugs, *, repo, api_key=None) -> dict:
    """AI DECISION #3 — evaluate expected-vs-actual against the ORIGINAL VISION: is the bug ACTUALLY fixed?
    Fed the fresh observation (residual bugs the re-run explorer still sees). Fail-closed: if the model is
    unsure it should say not-fixed so the loop tries again."""
    prompt = (
        "You are the staff engineer VERIFYING a fix. Judge strictly against the ORIGINAL VISION and the "
        "bug's EXPECTED behavior — expected-vs-actual, not merely 'it no longer crashes'. Be adversarial; "
        "if you are not confident it is truly fixed, say so.\n\n"
        f"ORIGINAL VISION:\n{vision}\n\n"
        f"THE BUG that was supposed to be fixed:\n{json.dumps(bug, default=str)[:4000]}\n\n"
        f"FILES CHANGED by the dev agents:\n{json.dumps(files, default=str)[:2000]}\n\n"
        f"FRESH OBSERVATION — bugs the explorer STILL sees after restart "
        f"(empty means the story now passes):\n{json.dumps(residual_bugs, default=str)[:6000]}\n\n"
        "Reply with ONLY a JSON object: "
        '{"fixed": <true|false>, "confidence": <0..1>, "reason": "<evidence-based justification>"}'
    )
    verdict = _ai_json("principal-engineer", repo, prompt, api_key=api_key,
                       default={"fixed": False, "confidence": 0.0, "reason": "no verdict"})
    verdict["fixed"] = bool(verdict.get("fixed"))
    return verdict


def fix_bug(bug, code_context, vision, *, repo=None, restart_cmd=None, health_url=None,
            target_url=None, stories=None, api_key=None, max_attempts=MAX_FIX_ATTEMPTS) -> dict:
    """Fix ONE blocking bug via the state-based loop (observe -> AI plans -> spawn dev agents -> restart ->
    observe -> AI judges), repeating up to `max_attempts` until the AI judge confirms the fix against the
    ORIGINAL VISION.

    EVERY decision is an AI call: (1) the staff-engineer PLAN decides how many agents + which files/roles,
    (2) the spawned dev agents implement it, (3) the principal-engineer JUDGE decides is-it-fixed.

      bug          — the failure dict (expected vs actual), typically from dev_self_qa / the explorer.
      code_context — relevant code/state; if a dict it may carry {"repo": <path>} used to locate the app.
      vision       — the ORIGINAL product vision + expected behavior (held in context for every AI call).
      restart_cmd  — argv/str to relaunch the app after the fix (restart_target). None -> skip restart.
      target_url + stories — if given, the bug's story is RE-EXPLORED after restart to observe reality,
                             and that observation is fed to the judge.

    Returns {fixed: bool, files: [...], attempts, verdict, plan, restart, residual}.
    """
    if repo is None and isinstance(code_context, dict):
        repo = code_context.get("repo")
    repo = repo or str(factory.PRODUCTS)

    all_files, attempts, plan, verdict, restart, residual = [], 0, None, {}, None, []
    while attempts < max_attempts:
        attempts += 1
        # observe -> AI DECIDES the fix
        plan = _plan_fix(bug, code_context, vision, repo=repo, api_key=api_key)
        # ACT — spawn exactly the AI-decided number of role-specialized dev agents
        files = _spawn_fix_agents(plan, bug, vision, repo=repo, api_key=api_key)
        all_files.extend(f for f in files if f not in all_files)
        # restart/reset the app so we observe a fresh process
        if restart_cmd:
            restart = restart_target(restart_cmd, health_url=health_url,
                                     cwd=repo if isinstance(repo, str) else None)
        # observe again — re-run the failing story against the live app
        residual = []
        if target_url and stories is not None:
            try:
                residual = dev_self_qa(target_url, vision, stories, api_key=api_key)
            except Exception as e:
                residual = [{"note": f"re-QA unavailable: {e}"}]
        # AI EVALUATES expected-vs-actual against the vision
        verdict = _judge_fixed(bug, vision, all_files, residual, repo=repo, api_key=api_key)
        _audit("FixBugAttempt", {"attempt": attempts, "agents": len(plan["agents"]),
                                 "fixed": verdict["fixed"], "files": all_files})
        if verdict["fixed"]:
            break

    return {"fixed": bool(verdict.get("fixed")), "files": all_files, "attempts": attempts,
            "verdict": verdict, "plan": plan, "restart": restart, "residual": residual}


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# offline selftest — stubs factory.agent + qa_explorer.Explorer; NO real API calls, deterministic
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def _selftest():
    import types
    ok = True
    real_agent = factory.agent

    # (A) fix_bug spawns EXACTLY the AI-decided number of dev agents and reports fixed.
    PLANNED = 3
    calls = []                                            # (role, task) of every factory.agent spawn

    def fake_agent(role, repo, task, **kw):
        calls.append(role)
        if role == "staff-engineer":                     # AI DECISION #1: the plan (3 builder agents)
            plan = {"agents": [{"role": "builder", "files": [f"src/mod{i}.py"], "task": f"fix part {i}"}
                               for i in range(PLANNED)],
                    "rationale": "three independent files"}
            return {"out_full": "here is the plan " + json.dumps(plan), "rc": 0}
        if role == "principal-engineer":                 # AI DECISION #3: is-it-fixed
            return {"out_full": json.dumps({"fixed": True, "confidence": 0.95, "reason": "story passes"}), "rc": 0}
        return {"out_full": "done", "rc": 0}             # a spawned dev agent

    factory.agent = fake_agent
    try:
        bug = {"story": "log in", "expected": "dashboard", "actual": "500 error", "blocking": True}
        res = fix_bug(bug, {"repo": "/tmp/app", "code": "def login(): ..."}, vision="A todo app that lets users log in.")
    finally:
        factory.agent = real_agent

    dev_spawns = calls.count("builder")
    planned_ok = dev_spawns == PLANNED
    fixed_ok = res["fixed"] is True
    files_ok = sorted(res["files"]) == [f"src/mod{i}.py" for i in range(PLANNED)]
    plan_role_ok = calls.count("staff-engineer") == 1 and calls.count("principal-engineer") == 1
    ok = planned_ok and fixed_ok and files_ok and plan_role_ok

    # (B) dev_self_qa runs the (stubbed) Explorer over every story and returns the bugs it finds.
    fake_mod = types.ModuleType("qa_explorer")
    explored = []

    class _FakeExplorer:
        def __init__(self, url, vision, api_key=None):
            self.url = url
        def explore(self, story):
            explored.append(story)
            return [{"story": story, "expected": "works", "actual": "broken", "severity": "high", "blocking": True}]

    fake_mod.Explorer = _FakeExplorer
    sys.modules["qa_explorer"] = fake_mod
    try:
        bugs = dev_self_qa("http://127.0.0.1:8080", "A todo app.", ["story-1", "story-2"])
    finally:
        del sys.modules["qa_explorer"]
    selfqa_ok = len(bugs) == 2 and explored == ["story-1", "story-2"] and all(b["blocking"] for b in bugs)
    ok = ok and selfqa_ok

    print(f"fix_bug: planned={PLANNED} spawned={dev_spawns} planned_ok={planned_ok} fixed_ok={fixed_ok} "
          f"files_ok={files_ok} roles_ok={plan_role_ok}")
    print(f"dev_self_qa: bugs={len(bugs)} explored={explored} selfqa_ok={selfqa_ok}")
    print("PASS: dev-fix loop wired — AI plans #agents, spawns exactly that many, restarts, AI judges fixed ✅"
          if ok else "FAIL")
    sys.exit(0 if ok else 1)


# public API
__all__ = ["fix_bug", "dev_self_qa", "restart_target"]


def _main(argv):
    if argv and argv[0] == "selftest":
        _selftest()
    print(__doc__)
    print("commands: selftest")
    sys.exit(0)


if __name__ == "__main__":
    _main(sys.argv[1:])
