#!/usr/bin/env python3
"""dev_loop.py — the DEV-fix loop of agent-os's AGENTIC QA system.

Two mandates from the owner, both realized here as *pure AI-driven state loops* (no heuristics,
no hard-coded thresholds — EVERY decision is a `factory.agent` call, which itself retries 529/
overload and fails over to Codex):

  1. DEV IS THE FIRST QA.  Before a builder hands off its own work, it runs `dev_self_qa()` — an
     AI explorer (`qa_explorer.Explorer`) walks the ORIGINAL user stories against the live app and
     reports expected-vs-actual bugs. The builder fixes its own defects before anyone downstream
     ever sees them. Callable in-process (dev_self_qa) or as a CLI gate the factory's
     dev-is-first-QA hook shells out to:  `dev_loop.py self-qa <url> --vision ... --stories ...`
     (exit 0 clean / 1 non-blocking bugs / 2 blocking bugs).

  2. FIX-ON-BLOCKING-BUG.  When a blocking bug is found, `fix_bug()` runs a STATE-BASED loop —
     and the judge is NEVER allowed to grade prose or absence-of-evidence:
         observe(bug)                                   ── the failure + code context + vision
      -> AI DECIDES how to fix (staff-engineer plan)    ── how many dev agents, which files, roles
      -> ACT (spawn exactly that many dev agents)       ── role-specialized, in parallel;
                                                           ANY agent returning rc!=0 FAILS the attempt
      -> take the REAL git diff of what changed         ── evidence, not the plan's file list
      -> restart the app (tracked-PID kill + relaunch)  ── observe a FRESH process, never a stale one
      -> re-explore the failing story (MANDATORY)       ── what the app ACTUALLY does now; no
                                                           judge-without-repro path exists
      -> AI EVALUATES expected-vs-actual (is-it-fixed)  ── judged on the diff + fresh observation
      -> repeat until fixed or attempts exhausted.

    `target_url` and `stories` are therefore MANDATORY on fix_bug: a fix that was never re-observed
    against the live app cannot be judged fixed, period. If re-exploration itself fails, the
    attempt fails — the loop never substitutes "no observation" for "story passes".

The ORIGINAL VISION + EXPECTED behavior are threaded through every prompt so each AI call judges
against what the product was SUPPOSED to do, not just "does it crash". Cost is intentionally not a
concern: this file is deliberately maximally AI-driven.

  python dev_loop.py selftest   # offline wiring check — stubs factory.agent + Explorer, no API calls
  python dev_loop.py self-qa <target_url> --vision <text|@file> --stories <json|@file> [--token T] [--org O]

Run with the agent-os venv python.
"""
import hashlib
import json
import os
import signal
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
DIFF_LIMIT = int(os.environ.get("AOS_QA_DIFF_LIMIT", "14000"))           # chars of real git diff fed to the judge


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


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# ground truth #1 — the REAL git diff (the judge never grades agent prose)
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def _is_git_repo(repo) -> bool:
    try:
        p = subprocess.run(["git", "-C", str(repo), "rev-parse", "--is-inside-work-tree"],
                           capture_output=True, text=True, timeout=15)
        return p.returncode == 0 and p.stdout.strip() == "true"
    except Exception:
        return False


def _dirty_paths(repo) -> list:
    """Repo-relative paths git currently sees as modified/untracked (mirrors factory._changed_paths)."""
    try:
        p = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"],
                           capture_output=True, text=True, timeout=30)
        if p.returncode != 0:
            return []
        out = []
        for line in (p.stdout or "").splitlines():
            if not line.strip():
                continue
            path = line[3:]
            if " -> " in path:                             # rename/copy: the destination is the written path
                path = path.split(" -> ", 1)[1]
            out.append(path.strip().strip('"'))
        return out
    except Exception:
        return []


def _worktree_snapshot(repo) -> dict:
    """Content hash of every currently-dirty/untracked file — taken BEFORE spawning fix agents so the
    attempt's changed-file set is *this attempt's* real footprint, not pre-existing worktree dirt."""
    snap = {}
    for rel in _dirty_paths(repo):
        f = Path(repo) / rel
        try:
            snap[rel] = hashlib.sha1(f.read_bytes()).hexdigest()
        except Exception:
            snap[rel] = "<unreadable-or-deleted>"
    return snap


def _changed_since(repo, before: dict) -> list:
    """Repo-relative files whose content actually changed since the `before` snapshot (new, modified,
    or reverted/deleted). This — not the plan's file list — is what the judge sees."""
    after = _worktree_snapshot(repo)
    changed = [rel for rel, h in after.items() if before.get(rel) != h]
    changed += [rel for rel in before if rel not in after]  # dirty-before, gone-now (revert/delete)
    return sorted(set(changed))


def _git_diff(repo, paths: list, limit: int = DIFF_LIMIT) -> str:
    """The REAL unified diff of `paths` against HEAD (tracked), plus full-content pseudo-diffs for new
    untracked files. This is the evidence the fix judge grades — never the dev agents' own claims."""
    if not paths:
        return ""
    chunks = []
    try:
        p = subprocess.run(["git", "-C", str(repo), "diff", "HEAD", "--"] + list(paths),
                           capture_output=True, text=True, timeout=60)
        if p.returncode == 0 and p.stdout:
            chunks.append(p.stdout)
        covered = {ln[6:].strip() for ln in (p.stdout or "").splitlines() if ln.startswith("+++ b/")}
        for rel in paths:                                  # untracked new files don't appear in diff HEAD
            if rel in covered:
                continue
            f = Path(repo) / rel
            if f.exists():
                q = subprocess.run(["git", "-C", str(repo), "diff", "--no-index", "--", "/dev/null", rel],
                                   capture_output=True, text=True, timeout=30)
                chunks.append(q.stdout or f"NEW FILE {rel} (content unavailable)")
            else:
                chunks.append(f"DELETED/REVERTED: {rel}")
    except Exception as e:
        chunks.append(f"(diff error: {e})")
    return "\n".join(chunks)[:limit]


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# ground truth #2 — restart by TRACKED PID (never `pkill -f <pattern>`, which can murder bystanders)
# ─────────────────────────────────────────────────────────────────────────────────────────────────
_TRACKED = {}                                              # cmd-key -> Popen of the instance WE launched


def _cmd_key(cmd, cwd) -> str:
    return json.dumps([list(cmd) if isinstance(cmd, (list, tuple)) else str(cmd), str(cwd or "")])


def _kill_pid(pid, proc=None, grace: float = 5.0) -> bool:
    """Terminate the process GROUP we created (start_new_session=True => pgid == pid): SIGTERM, wait up
    to `grace`, then SIGKILL. Reaps via the Popen handle when we hold it. Returns True when it is gone."""
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return True
        except Exception:
            return False
    deadline = time.time() + grace
    while time.time() < deadline:
        if proc is not None:
            if proc.poll() is not None:
                return True
        else:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
        time.sleep(0.1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except Exception:
        pass
    if proc is not None:
        try:
            proc.wait(timeout=grace)
            return True
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return False
    except ProcessLookupError:
        return True


def restart_target(cmd, *, health_url=None, cwd=None, env=None, timeout=HEALTH_TIMEOUT,
                   pid=None) -> dict:
    """Restart/reset the app under test: kill the OLD instance by its TRACKED PID, relaunch, then wait
    until healthy. This is what makes the fix loop observe a *fresh* process rather than a stale one
    still serving the old (buggy) code.

      cmd        — argv list (or shell string) that launches the app.
      health_url — URL polled until it returns 2xx (readiness gate). If None, we just wait a beat and
                   trust the launch (best we can do without a health endpoint).
      pid        — an EXTERNALLY-tracked pid of the old instance (e.g. the builder launched the app
                   itself and knows the pid). When omitted, we kill the pid WE launched last time for
                   this exact cmd+cwd — and if we never launched it, we kill NOTHING: a first launch
                   must never take out an unrelated process the way `pkill -f <pattern>` could.

    Returns {restarted, healthy, pid, killed_old, detail}.
    """
    argv = cmd if isinstance(cmd, (list, tuple)) else None
    shell = None if argv else str(cmd)
    key = _cmd_key(cmd, cwd)

    # 1) KILL the old instance — by tracked PID only, never by pattern.
    killed = False
    if pid is not None:
        killed = _kill_pid(int(pid))
    else:
        old = _TRACKED.pop(key, None)
        if old is not None:
            killed = _kill_pid(old.pid, proc=old)
    if killed:
        time.sleep(float(os.environ.get("AOS_QA_KILL_SETTLE", "1.0")))   # let sockets/pidfiles clear

    # 2) RELAUNCH (its own session/process group so the NEXT restart can killpg exactly this tree).
    try:
        proc = subprocess.Popen(
            list(argv) if argv else shell, shell=bool(shell), cwd=cwd,
            env=({**os.environ, **env} if env else None),
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True)
    except Exception as e:
        return {"restarted": False, "healthy": False, "pid": None, "killed_old": killed,
                "detail": f"relaunch failed: {e}"}
    _TRACKED[key] = proc                                    # the pid the NEXT restart will kill

    # 3) WAIT for health.
    healthy, detail = _wait_healthy(health_url, timeout)
    _audit("RestartTarget", {"killed_old": killed, "pid": proc.pid, "healthy": healthy})
    return {"restarted": True, "healthy": healthy, "pid": proc.pid, "killed_old": killed,
            "detail": detail or f"launched pid={proc.pid}, old_killed={killed}"}


def _wait_healthy(health_url, timeout) -> tuple:
    if not health_url:
        time.sleep(float(os.environ.get("AOS_QA_SETTLE", "2.0")))
        return True, "no health_url — waited and assumed up"
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
def dev_self_qa(target_url, vision, stories, *, token=None, org="0", api_key=None) -> list:
    """A builder QAs its OWN work before handoff (dev = first QA). An AI EXPLORER (qa_explorer.Explorer)
    walks each ORIGINAL user story against the LIVE app at `target_url`, holding the `vision` in context so
    it judges expected-vs-actual — and returns every bug it finds. Returning a non-empty list means the
    builder is NOT ready to hand off and should fix its own defects first.

    This is the entry point the factory's dev-is-first-QA hook calls: hand it the served URL + the
    ORIGINAL stories, get the bugs back. Also exposed as the `self-qa` CLI subcommand (see _main) so a
    hook can shell out and gate on the exit code.

    Contract with qa_explorer:
        Explorer(target_url, vision, token=None, org="0") with .explore(story) -> list[bug-dict].
    Each bug-dict is expected to carry at least {story, expected, actual, severity, blocking}.
    (Explorer routes its own factory.agent calls; token/org seed the browser's tenant auth.)
    """
    import qa_explorer                                     # sibling QA module (imported lazily on purpose)
    explorer = qa_explorer.Explorer(target_url, vision, token=token, org=org)
    bugs = []
    try:
        for story in _as_list(stories):
            try:
                found = explorer.explore(story)           # AI-driven observe->judge per story
            except TypeError:                             # tolerate Explorer.explore(stories=[...]) shape
                found = explorer.explore(stories=[story])
            bugs.extend(_as_list(found))
    finally:
        try:
            explorer.close()
        except Exception:
            pass
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


def _spawn_fix_agents(plan, bug, vision, *, repo, api_key=None) -> dict:
    """ACT — spawn EXACTLY the AI-decided number of dev agents, each role-specialized to its area, IN
    PARALLEL. Every agent's factory.agent RESULT is checked: rc!=0 / failed / crashed means that agent
    did NOT fix anything, and the whole attempt FAILS (a crashed or refused dev agent must never be
    indistinguishable from a successful fix). Returns {ok, results, failures}."""
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
        res = factory.agent(role, str(repo), task, api_key=api_key) if api_key is not None \
            else factory.agent(role, str(repo), task)
        rc = (res or {}).get("rc")
        failed = res is None or bool((res or {}).get("failed")) or (rc is not None and rc != 0)
        return {"role": role, "rc": rc, "failed": failed,
                "blocker": (res or {}).get("blocker"), "planned_files": files}

    results = []
    workers = min(len(agents), int(os.environ.get("AOS_FLEET_WORKERS", "5")))
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for fut in as_completed([ex.submit(_one, s) for s in agents]):
            try:
                results.append(fut.result())
            except Exception as e:                         # a crashed spawn IS a failed agent
                results.append({"role": "?", "rc": None, "failed": True, "blocker": str(e)[:300],
                                "planned_files": []})
    failures = [r for r in results if r["failed"]]
    return {"ok": not failures, "results": results, "failures": failures}


def _judge_fixed(bug, vision, changed_files, diff_text, residual_bugs, restart, *,
                 repo, api_key=None) -> dict:
    """AI DECISION #3 — evaluate expected-vs-actual against the ORIGINAL VISION: is the bug ACTUALLY fixed?
    Fed ONLY ground truth: the REAL git diff of what changed (never the dev agents' prose) and the FRESH
    post-restart observation from re-exploring the failing story. Fail-closed: if the model is unsure it
    should say not-fixed so the loop tries again."""
    prompt = (
        "You are the staff engineer VERIFYING a fix. Judge strictly against the ORIGINAL VISION and the "
        "bug's EXPECTED behavior — expected-vs-actual, not merely 'it no longer crashes'. Be adversarial; "
        "if you are not confident it is truly fixed, say so. Ground rules: (a) the GIT DIFF below is the "
        "ONLY change evidence — if it is empty or does not plausibly address the bug, the fix is NOT real, "
        "whatever anyone claimed; (b) the FRESH OBSERVATION below is the app's ACTUAL behavior after a "
        "restart — any residual bug matching the original story means NOT fixed.\n\n"
        f"ORIGINAL VISION:\n{vision}\n\n"
        f"THE BUG that was supposed to be fixed:\n{json.dumps(bug, default=str)[:4000]}\n\n"
        f"FILES ACTUALLY CHANGED (from git, not from agent claims):\n"
        f"{json.dumps(changed_files, default=str)[:2000]}\n\n"
        f"REAL GIT DIFF of those changes:\n{diff_text or '(EMPTY — no verifiable change was made)'}\n\n"
        f"APP RESTART: {json.dumps(restart, default=str)[:500]}\n\n"
        f"FRESH OBSERVATION — bugs the explorer STILL sees after restart, re-exploring the failing story "
        f"(empty means the story now passes):\n{json.dumps(residual_bugs, default=str)[:6000]}\n\n"
        "Reply with ONLY a JSON object: "
        '{"fixed": <true|false>, "confidence": <0..1>, "reason": "<evidence-based justification>"}'
    )
    verdict = _ai_json("principal-engineer", repo, prompt, api_key=api_key,
                       default={"fixed": False, "confidence": 0.0, "reason": "no verdict"})
    verdict["fixed"] = bool(verdict.get("fixed"))
    return verdict


def fix_bug(bug, code_context, vision, *, target_url, stories, repo=None, restart_cmd=None,
            health_url=None, token=None, org="0", api_key=None,
            max_attempts=MAX_FIX_ATTEMPTS) -> dict:
    """Fix ONE blocking bug via the state-based loop (observe -> AI plans -> spawn dev agents -> real git
    diff -> restart -> RE-EXPLORE the failing story -> AI judges), repeating up to `max_attempts` until
    the AI judge confirms the fix against the ORIGINAL VISION.

    `target_url` and `stories` are MANDATORY: every attempt re-explores the failing story against the
    live app, and the judge only ever sees that fresh observation plus the real diff. There is no
    judge-without-repro path — an attempt whose agents fail (rc!=0), whose app won't come back healthy,
    or whose re-exploration errors is a FAILED attempt, never a judged pass.

      bug          — the failure dict (expected vs actual), typically from dev_self_qa / the explorer.
      code_context — relevant code/state; if a dict it may carry {"repo": <path>} used to locate the app.
      vision       — the ORIGINAL product vision + expected behavior (held in context for every AI call).
      target_url   — REQUIRED: the live app to re-observe after each attempt.
      stories      — REQUIRED: the failing story/stories to re-explore (usually just the bug's story).
      restart_cmd  — argv/str to relaunch the app after the fix (restart_target, tracked-PID kill). When
                     None the target is managed externally (e.g. a dev server the builder owns) — the
                     mandatory re-exploration still observes the live app either way.
      token/org    — tenant auth for the explorer's browser session.

    Returns {fixed: bool, files: [...repo-relative, from git...], attempts, verdict, plan, restart,
             residual, attempts_log}.
    """
    if not target_url:
        raise ValueError("fix_bug: target_url is mandatory — a fix that is never re-observed against "
                         "the live app cannot be judged fixed")
    stories = _as_list(stories)
    if not stories:
        raise ValueError("fix_bug: stories is mandatory — the failing story must be re-explored after "
                         "every fix attempt (no judge-without-repro path)")
    if repo is None and isinstance(code_context, dict):
        repo = code_context.get("repo")
    repo = repo or str(factory.PRODUCTS)
    in_git = _is_git_repo(repo)

    all_files, attempts, attempts_log = [], 0, []
    plan, verdict, restart, residual = None, {"fixed": False}, None, []
    while attempts < max_attempts:
        attempts += 1
        # observe -> AI DECIDES the fix
        plan = _plan_fix(bug, code_context, vision, repo=repo, api_key=api_key)
        before = _worktree_snapshot(repo) if in_git else {}
        # ACT — spawn exactly the AI-decided number of role-specialized dev agents
        spawn = _spawn_fix_agents(plan, bug, vision, repo=repo, api_key=api_key)
        if not spawn["ok"]:                                # rc!=0 / crashed agent -> the attempt FAILS
            verdict = {"fixed": False, "confidence": 0.0,
                       "reason": f"{len(spawn['failures'])}/{len(spawn['results'])} spawned fix agent(s) "
                                 f"failed (rc!=0) — a crashed/refused agent is not a fix"}
            attempts_log.append({"attempt": attempts, "failed_stage": "spawn",
                                 "failures": spawn["failures"]})
            _audit("FixBugAttempt", {"attempt": attempts, "agents": len(plan["agents"]),
                                     "fixed": False, "stage": "spawn-failed",
                                     "failures": len(spawn["failures"])})
            continue
        # GROUND TRUTH — what actually changed, straight from git (never the plan's file list)
        changed = _changed_since(repo, before) if in_git else []
        diff_text = _git_diff(repo, changed) if in_git \
            else "(target repo is not a git checkout — no verifiable diff; treat as no change evidence)"
        all_files.extend(f for f in changed if f not in all_files)
        # restart/reset the app (tracked-PID kill + relaunch) so we observe a FRESH process
        if restart_cmd:
            restart = restart_target(restart_cmd, health_url=health_url,
                                     cwd=repo if isinstance(repo, str) else None)
            if not restart.get("healthy"):
                verdict = {"fixed": False, "confidence": 0.0,
                           "reason": f"app failed to come back healthy after restart: "
                                     f"{restart.get('detail')}"}
                attempts_log.append({"attempt": attempts, "failed_stage": "restart", "restart": restart})
                _audit("FixBugAttempt", {"attempt": attempts, "agents": len(plan["agents"]),
                                         "fixed": False, "stage": "restart-failed"})
                continue
        else:
            restart = {"restarted": False, "healthy": None,
                       "detail": "no restart_cmd — target managed externally; re-exploring live URL"}
        # MANDATORY re-observation — re-run the failing story against the live app
        try:
            residual = dev_self_qa(target_url, vision, stories, token=token, org=org, api_key=api_key)
        except Exception as e:
            verdict = {"fixed": False, "confidence": 0.0,
                       "reason": f"re-exploration failed ({e}) — no observation, no pass"}
            attempts_log.append({"attempt": attempts, "failed_stage": "re-explore", "error": str(e)[:300]})
            _audit("FixBugAttempt", {"attempt": attempts, "agents": len(plan["agents"]),
                                     "fixed": False, "stage": "reexplore-failed"})
            continue
        # AI EVALUATES expected-vs-actual against the vision, on diff + fresh observation only
        verdict = _judge_fixed(bug, vision, changed, diff_text, residual, restart,
                               repo=repo, api_key=api_key)
        attempts_log.append({"attempt": attempts, "changed": changed, "residual": len(residual),
                             "fixed": verdict["fixed"]})
        _audit("FixBugAttempt", {"attempt": attempts, "agents": len(plan["agents"]),
                                 "fixed": verdict["fixed"], "files": all_files})
        if verdict["fixed"]:
            break

    return {"fixed": bool(verdict.get("fixed")), "files": all_files, "attempts": attempts,
            "verdict": verdict, "plan": plan, "restart": restart, "residual": residual,
            "attempts_log": attempts_log}


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# offline selftest — stubs factory.agent + qa_explorer.Explorer; NO real API calls, deterministic
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def _selftest():
    import shutil
    import tempfile
    import types
    checks = {}
    real_agent = factory.agent
    os.environ["AOS_QA_SETTLE"] = "0.1"                    # fast no-health-url waits
    os.environ["AOS_QA_KILL_SETTLE"] = "0.1"

    # a stub Explorer module: records every story re-explored; returns whatever `residual_script` says.
    explored = []
    residual_script = {"bugs": []}

    class _FakeExplorer:
        def __init__(self, url, vision, token=None, org="0", **kw):
            self.url, self.token, self.org = url, token, org
        def explore(self, story):
            explored.append(story)
            return list(residual_script["bugs"])
        def close(self):
            pass

    fake_mod = types.ModuleType("qa_explorer")
    fake_mod.Explorer = _FakeExplorer
    sys.modules["qa_explorer"] = fake_mod

    # a REAL throwaway git repo — the diff the judge sees must be genuine, not prose.
    tmp = Path(tempfile.mkdtemp(prefix="devloop-selftest-"))
    subprocess.run(["git", "init", "-q", str(tmp)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp), "config", "user.email", "qa@test"], check=True)
    subprocess.run(["git", "-C", str(tmp), "config", "user.name", "qa"], check=True)
    app = tmp / "app.py"
    app.write_text("def login():\n    return 500\n")
    subprocess.run(["git", "-C", str(tmp), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp), "commit", "-qm", "init"], check=True)

    bug = {"story": "log in", "expected": "dashboard", "actual": "500 error", "blocking": True}
    vision = "A todo app that lets users log in."
    sleeper = [sys.executable, "-c", "import time; time.sleep(120)"]

    # ── (1) MANDATORY ARGS: no judge-without-repro path exists.
    try:
        fix_bug(bug, {"repo": str(tmp)}, vision)           # no target_url/stories at all
        checks["mandatory_omitted"] = False
    except TypeError:
        checks["mandatory_omitted"] = True
    try:
        fix_bug(bug, {"repo": str(tmp)}, vision, target_url="", stories=["log in"])
        checks["mandatory_empty_url"] = False
    except ValueError:
        checks["mandatory_empty_url"] = True
    try:
        fix_bug(bug, {"repo": str(tmp)}, vision, target_url="http://x", stories=[])
        checks["mandatory_empty_stories"] = False
    except ValueError:
        checks["mandatory_empty_stories"] = True

    # ── (2) HAPPY PATH: AI plans N agents -> exactly N spawned -> REAL diff judged -> restart+re-explore
    #        happen EVERY attempt -> fixed. The judge prompt must carry the actual git diff content.
    PLANNED = 3
    calls, judge_prompts = [], []

    def fake_agent(role, repo, task, **kw):
        calls.append(role)
        if role == "staff-engineer":                       # AI DECISION #1: the plan (3 builder agents)
            plan = {"agents": [{"role": "builder", "files": ["src/imaginary%d.py" % i],
                                "task": f"fix part {i}"} for i in range(PLANNED)],
                    "rationale": "three agents"}
            return {"out_full": "here is the plan " + json.dumps(plan), "rc": 0}
        if role == "principal-engineer":                   # AI DECISION #3: is-it-fixed
            judge_prompts.append(task)
            return {"out_full": json.dumps({"fixed": True, "confidence": 0.95,
                                            "reason": "diff fixes login; story passes"}), "rc": 0}
        # a spawned dev agent: ONE of them actually edits the code (the real footprint)
        if calls.count("builder") == 1:
            app.write_text("def login():\n    return 'dashboard'  # FIXED-SENTINEL\n")
        return {"out_full": "done", "rc": 0}

    factory.agent = fake_agent
    explored.clear()
    residual_script["bugs"] = []
    try:
        res = fix_bug(bug, {"repo": str(tmp)}, vision, target_url="http://127.0.0.1:1",
                      stories=["log in"], restart_cmd=sleeper)
    finally:
        factory.agent = real_agent
    checks["spawn_exact"] = calls.count("builder") == PLANNED
    checks["fixed"] = res["fixed"] is True and res["attempts"] == 1
    # diff-based judging: files come from GIT (the actually-edited file), never the plan's prose list
    checks["files_from_git"] = res["files"] == ["app.py"]
    checks["plan_prose_ignored"] = not any("imaginary" in f for f in res["files"])
    checks["judge_saw_real_diff"] = bool(judge_prompts) and "FIXED-SENTINEL" in judge_prompts[0] \
        and "app.py" in judge_prompts[0]
    # always-restart + always-re-explore: the attempt restarted a fresh process AND re-ran the story
    checks["restarted"] = bool(res["restart"] and res["restart"]["restarted"]
                               and res["restart"]["pid"])
    checks["reexplored"] = explored == ["log in"]
    happy_pid = (res.get("restart") or {}).get("pid")

    # ── (3) rc!=0 FAILS THE ATTEMPT: a crashed/refused dev agent is never judged as a fix.
    calls2 = []

    def failing_agent(role, repo, task, **kw):
        calls2.append(role)
        if role == "staff-engineer":
            return {"out_full": json.dumps({"agents": [{"role": "builder", "files": [], "task": "fix"}],
                                            "rationale": "one"}), "rc": 0}
        if role == "principal-engineer":                   # must NEVER be reached
            return {"out_full": json.dumps({"fixed": True, "confidence": 1.0, "reason": "??"}), "rc": 0}
        return {"out_full": "I refuse / crashed", "rc": 1, "failed": True}

    factory.agent = failing_agent
    explored.clear()
    try:
        res2 = fix_bug(bug, {"repo": str(tmp)}, vision, target_url="http://127.0.0.1:1",
                       stories=["log in"], max_attempts=2)
    finally:
        factory.agent = real_agent
    checks["rc_nonzero_fails"] = res2["fixed"] is False and res2["attempts"] == 2 \
        and "principal-engineer" not in calls2 \
        and all(a.get("failed_stage") == "spawn" for a in res2["attempts_log"])
    checks["rc_nonzero_no_explore"] = explored == []       # a failed attempt is never "observed fixed"

    # ── (4) RE-EXPLORATION FAILURE fails the attempt (no observation => no pass, judge never asked).
    calls3 = []

    def ok_agent(role, repo, task, **kw):
        calls3.append(role)
        if role == "staff-engineer":
            return {"out_full": json.dumps({"agents": [{"role": "builder", "files": [], "task": "fix"}],
                                            "rationale": "one"}), "rc": 0}
        if role == "principal-engineer":
            return {"out_full": json.dumps({"fixed": True, "confidence": 1.0, "reason": "??"}), "rc": 0}
        return {"out_full": "done", "rc": 0}

    class _BoomExplorer(_FakeExplorer):
        def explore(self, story):
            raise RuntimeError("browser died")

    fake_mod.Explorer = _BoomExplorer
    factory.agent = ok_agent
    try:
        res3 = fix_bug(bug, {"repo": str(tmp)}, vision, target_url="http://127.0.0.1:1",
                       stories=["log in"], max_attempts=1)
    finally:
        factory.agent = real_agent
        fake_mod.Explorer = _FakeExplorer
    checks["reexplore_error_fails"] = res3["fixed"] is False and "principal-engineer" not in calls3 \
        and res3["attempts_log"][0]["failed_stage"] == "re-explore"

    # ── (5) TRACKED-PID RESTART: kills exactly the pid it launched; never a pattern-kill of bystanders.
    bystander = subprocess.Popen(sleeper, start_new_session=True)   # same cmdline — pkill -f would kill it
    try:
        r1 = restart_target(sleeper)                       # first launch of this key: kills NOTHING
        checks["first_launch_kills_nothing"] = r1["restarted"] and r1["killed_old"] is False
        r2 = restart_target(sleeper)                       # second: kills EXACTLY r1's pid
        old_gone = False
        try:
            os.kill(r1["pid"], 0)
        except ProcessLookupError:
            old_gone = True
        bystander_alive = bystander.poll() is None
        checks["tracked_pid_killed"] = r2["killed_old"] is True and old_gone and r2["pid"] != r1["pid"]
        checks["bystander_survives"] = bystander_alive
        _kill_pid(r2["pid"], proc=_TRACKED.pop(_cmd_key(sleeper, None), None))   # cleanup
    finally:
        try:
            os.killpg(bystander.pid, signal.SIGKILL)
            bystander.wait(timeout=5)
        except Exception:
            pass
    if happy_pid:                                          # cleanup the happy-path restart's process
        _kill_pid(happy_pid, proc=_TRACKED.pop(_cmd_key(sleeper, str(tmp)), None))

    # ── (6) dev_self_qa: runs the Explorer over every story, returns the bugs (the self-QA gate).
    explored.clear()
    residual_script["bugs"] = [{"story": "s", "expected": "works", "actual": "broken",
                                "severity": "high", "blocking": True}]
    bugs = dev_self_qa("http://127.0.0.1:8080", "A todo app.", ["story-1", "story-2"], token="T", org="7")
    checks["selfqa"] = len(bugs) == 2 and explored == ["story-1", "story-2"] \
        and all(b["blocking"] for b in bugs)

    del sys.modules["qa_explorer"]
    shutil.rmtree(tmp, ignore_errors=True)

    ok = all(checks.values())
    for name, passed in checks.items():
        print(f"  {'PASS' if passed else 'FAIL'}  {name}")
    print("PASS: dev-fix loop wired — mandatory repro, rc!=0 fails, real-diff judging, tracked-PID "
          "restart, always re-explore ✅" if ok else "FAIL")
    sys.exit(0 if ok else 1)


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# CLI — `self-qa` is the shell entry point for the factory's dev-is-first-QA hook.
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def _read_arg(v):
    """CLI values may be inline text/JSON or @/path/to/file."""
    if isinstance(v, str) and v.startswith("@"):
        return Path(v[1:]).read_text()
    return v


def _cli_self_qa(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="dev_loop.py self-qa",
                                 description="Dev-is-first-QA gate: explore the ORIGINAL stories against "
                                             "the served build; exit 0 clean, 1 bugs, 2 blocking bugs.")
    ap.add_argument("target_url")
    ap.add_argument("--vision", required=True, help="product vision text, or @file")
    ap.add_argument("--stories", required=True, help="JSON list of stories, or @file")
    ap.add_argument("--token", default=None)
    ap.add_argument("--org", default="0")
    a = ap.parse_args(argv)
    vision = _read_arg(a.vision)
    stories = json.loads(_read_arg(a.stories))
    bugs = dev_self_qa(a.target_url, vision, stories, token=a.token, org=a.org)
    blocking = [b for b in bugs if isinstance(b, dict) and b.get("blocking")]
    print(json.dumps({"target": a.target_url, "stories": len(_as_list(stories)),
                      "bugs": bugs, "blocking": len(blocking),
                      "ready_to_handoff": not bugs}, default=str, indent=2))
    sys.exit(2 if blocking else (1 if bugs else 0))


# public API
__all__ = ["fix_bug", "dev_self_qa", "restart_target"]


def _main(argv):
    if argv and argv[0] == "selftest":
        _selftest()
    if argv and argv[0] == "self-qa":
        _cli_self_qa(argv[1:])
    print(__doc__)
    print("commands: selftest | self-qa <target_url> --vision <text|@file> --stories <json|@file>")
    sys.exit(0)


if __name__ == "__main__":
    _main(sys.argv[1:])
