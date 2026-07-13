#!/usr/bin/env python3
"""runtime.py — THE durable execution engine of the agent-org (REBUILD-PLAN A1).

The arch-review verdict this fixes: "the actual AI org engine sits in a demo folder with zero
production callers" / "agents are stateless subprocess invocations with no identity, memory, or
tenure". The PROVEN in-memory semantics of the old bus.py/actor.py/supervisor.py reactor now run
on Postgres via store.py — actors are rows (identity, tenure, assignment, memory), events are
SKIP-LOCKED-claimable rows, and every decide-loop step is a DURABLE unit:

    claim pending events (FOR UPDATE SKIP LOCKED, lease-reclaimable)
      -> AI decision (factory.agent — Opus default, retries, Codex failover;
         the actor's role + memory + assignment are in the prompt)
      -> persist new state/events/result (store.update_actor / store.emit)
      -> heartbeat, then mark the claimed events processed

Process death between (or during) steps loses NOTHING: state is only advanced by persisted
writes, and an event claimed by a dead worker is lease-reclaimed, so a restarted
runtime.run_org() resumes every non-terminal actor exactly where its rows say it stopped.
Delivery is at-least-once (events are completed LAST), so a crash mid-step can re-run one step —
never skip one.

  run_org(run_id, tenant_id, workers=N)  — a THREAD POOL (like the factory fleet): each worker
      repeatedly picks ANY actor with pending events (SKIP LOCKED — N workers never collide on
      one event) and executes ONE step. Real parallelism across actors; interrupt semantics are
      preserved: a supervisor step triggered by a child's `blocked` runs while siblings work.

  Actor semantics (ported 1:1 from the in-memory reactor):
      worker      — decide-loop via one AI call per step: continue (self-emits `next` to keep its
                    own loop alive), emit blocked/finding/question/need_agent/need_context
                    mid-work, or finish. `blocked` PARKS the actor (status row); a `resolve` /
                    `context_update` event resumes it from its persisted memory.
      supervisor  — decomposes via AI -> hires children (recursive orgs: a child spec can itself
                    be a supervisor); INTERRUPT-DRIVEN on child events: resolve locally (unblock /
                    rebrief / broadcast a correction to all siblings / hand next / spawn a helper,
                    org_decider.should_expand gating expansion) or ESCALATE up; aggregates via AI
                    once every child is terminal, emitting `done` up the tree.
      controller  — the root, top escalation tier: plans the org (org_decider.plan_org), resolves
                    escalations (or consults the human hook), finishes the run on final aggregate.

  FACTORY GATES per actor step:
      killswitch.is_halted — checked before EVERY step's AI work (plus store refuses new
          events/hires while halted); a halted run stops cleanly and is resumable.
      governance spawn gate — factory's exact semantics on HIRING: a manifest role without
          can_spawn may NOT hire; its decomposition becomes a hire REQUEST (`need_agent`) that
          routes up to the controller (can_spawn: true), which performs the hire on its behalf —
          "only the controller spawns", per the hire_requests doctrine in 17-orchestrate.sql.
          A missing manifest fails OPEN with an audit note, exactly like factory.agent.
      budget — factory.agent enforces the USD + token caps itself; a budget `blocker` from it
          surfaces as a `blocked`/`escalate` event, never a crash.

    scripts/orchestra/runtime.py selftest    # OFFLINE (factory.agent stubbed), REAL local
                                             # Postgres, 2-worker pool, crash-resume proven
Run with the agent-os venv python. Library + selftest only — binds no server.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent          # scripts/
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import factory      # noqa: E402  — factory.agent is THE llm call (retries/failover/gates inside)
import killswitch   # noqa: E402  — runtime-level per-step halt gate
import governance   # noqa: E402  — spawn gate on hiring
import store        # noqa: E402  — the durable substrate (rows for actors/events/runs)
import org_decider  # noqa: E402  — plan_org / should_expand (the org-shape AI)

try:                # audit is best-effort; never let logging brick the org
    import audit    # noqa: E402
except Exception:   # pragma: no cover
    audit = None

TERMINAL = {"done", "dead"}          # actor statuses that end its decide-loop
MAX_ACTOR_STEPS = 64                 # runaway guard per actor (matches the old Actor default)
_MAX_RETEST = int(os.environ.get("AOS_QA_MAX_RETEST", "2"))   # qa-coordinator: re-tests per story after a fix
                                     # (bounds the find->fix->re-test loop so a bad fix can't cycle forever)
_MAX_GAPFILL = int(os.environ.get("AOS_QA_MAX_GAPFILL", "3"))  # qa-coordinator: gap-fill re-runs per story when
                                     # an explorer stops with INCOMPLETE coverage (bounded so it terminates)


# ------------------------------------------------------------------------------ small helpers
def _audit(actor_name, action, decision="executed", payload=None):
    if audit is None:
        return
    try:
        audit.append(actor=f"orchestra:{actor_name}", action=action, resource="runtime",
                     decision=decision, payload=payload or {})
    except Exception:
        pass


def _halted():
    """The per-step kill-switch gate (same scope the store gates creation on)."""
    try:
        h = killswitch.is_halted("orchestra")
        return h if h.get("halted") else None
    except Exception:
        return None


def _extract_json(text):
    """First JSON object out of a model reply; tolerant of ```json fences and prose."""
    if not text:
        return None
    t = text.strip()
    if "```" in t:
        seg = t.split("```")[1]
        t = seg[4:] if seg.lower().startswith("json") else seg
    s, e = t.find("{"), t.rfind("}")
    if s < 0 or e <= s:
        return None
    try:
        obj = json.loads(t[s:e + 1])
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _ai_json(role, repo, prompt, spawner=None):
    """ONE AI decision via factory.agent, parsed to a dict. A hard factory blocker
    (budget exhausted / halted / spawn denied / consent) carries through as {"_blocker": ...}
    so the caller surfaces it as a blocked/escalate event instead of crashing the step."""
    try:
        r = factory.agent(role, repo, prompt, spawner=spawner)
    except Exception as e:
        return {"_blocker": f"AI call failed: {e}"}
    if isinstance(r, dict) and r.get("blocker"):
        return {"_blocker": r["blocker"]}
    txt = (r.get("out_full") or r.get("out") or "") if isinstance(r, dict) else str(r)
    return _extract_json(txt) or {}


def _spawn_gate(role):
    """factory.agent's exact spawn-gate semantics, applied to HIRING rows: manifest present ->
    can_spawn must be true (deny reason string returned otherwise); manifest missing/unreadable ->
    fail OPEN with an audit note (infra hiccup must not brick the org, same trade factory makes)."""
    try:
        m = governance.load_manifest(role)
        if not m:
            _audit(role, "SpawnGate", "failopen", {"reason": "manifest missing/unreadable"})
            return None
        if governance.may(role, "spawn"):
            return None
        return (f"role '{role}' is not permitted to spawn sub-agents "
                f"(can_spawn is false in its manifest)")
    except Exception as e:
        _audit(role, "SpawnGate", "failopen", {"error": str(e)[:200]})
        return None


def _merge_context(context, payload):
    """Fold an event payload's context into an actor's memory-context (dict merge; strings noted)."""
    g = payload.get("context", payload)
    if isinstance(g, dict):
        context.update({k: v for k, v in g.items() if not k.startswith("_ev")})
    elif g is not None:
        context.setdefault("notes", []).append(str(g)[:400])


# ================================================================================ org bootstrap
def create_org(tenant_id, vision, repo=".", root_name="Controller", root_role="controller"):
    """Open a run and HIRE its root controller actor (a durable row), handing it the vision as
    its first `task` event. Returns {"run_id", "root_id"} or {"error"}. Kill-switch-gated by the
    store on both the run and the hire."""
    r = store.start_run(tenant_id, vision)
    if r.get("error"):
        return r
    root = store.spawn_actor(r["run_id"], tenant_id, root_name, root_role, kind="controller",
                             assignment=vision, memory={"repo": repo})
    if root.get("error"):
        store.finish_run(r["run_id"], "failed", {"error": root["error"]}, tenant_id=tenant_id)
        return root
    store.emit(r["run_id"], tenant_id, None, root["actor_id"], "task", {"task": vision},
               corr_id=f"run-{r['run_id']}")
    _audit(root_name, "CreateOrg", "executed", {"run_id": r["run_id"], "vision": vision[:160]})
    return {"run_id": r["run_id"], "root_id": root["actor_id"]}


# ================================================================================ one durable step
class _Step:
    """Accumulates one step's outputs so they are persisted in a strict, crash-safe order:
    update actor -> hires -> emits -> only THEN complete the claimed events (at-least-once)."""

    def __init__(self):
        self.emits = []          # (frm, to, kind, payload, corr_id)
        self.status = None       # new actor status (None = unchanged)
        self.result = None       # terminal result dict (set only with a terminal status)
        self.assignment = None   # new assignment text (None = unchanged)
        self.memory = {}         # memory keys to merge


def _persist(ctx, a, step, evs):
    kw = {"memory": step.memory}
    if step.status:
        kw["status"] = step.status
    if step.result is not None:
        kw["result"] = step.result
    if step.assignment is not None:
        kw["assignment"] = step.assignment
    store.update_actor(a["actor_id"], ctx.tenant, **kw)
    for frm, to, kind, payload, corr in step.emits:
        store.emit(ctx.run_id, ctx.tenant, frm, to, kind, payload, corr_id=corr)
    for ev in evs:
        store.complete_event(ev["id"], ctx.tenant)
    store.heartbeat(a["actor_id"], ctx.tenant)
    # NOTE: the fleet is surfaced in the unified pulse view by READING orchestra_actors.last_active
    # (pulse.live() aggregates it) — NOT by a write here. A synchronous pulse write on this hot per-step
    # path added latency that perturbed the timing-sensitive supervisor/sibling race. Heartbeat already exists.


def _hire(ctx, supervisor_id, spec):
    """One governance-passed hire: a durable child row + its kickoff `task` event.
    Returns the new actor_id (or None on a store refusal, which is journaled)."""
    kind = "supervisor" if spec.get("kind") == "supervisor" else "worker"
    mem = {"repo": ctx.repo}
    cblob = dict(spec.get("context") or {})   # a context blob (e.g. a bug handed to a dev-coordinator)
    if spec.get("tool"):                      # a TOOL-worker: tool + args ride in memory.context so its
        cblob["tool"] = spec["tool"]          # step dispatch-and-parks
        cblob["tool_args"] = spec.get("tool_args") or {}
    if cblob:
        mem["context"] = cblob
    child = store.spawn_actor(ctx.run_id, ctx.tenant, spec.get("name") or spec.get("role") or "agent",
                              spec.get("role") or "engineer", kind=kind,
                              supervisor_id=supervisor_id, assignment=spec.get("task"),
                              memory=mem)
    if child.get("error"):
        _audit(f"actor:{supervisor_id}", "HireFailed", "error", {"spec": spec, "err": child["error"]})
        return None
    store.emit(ctx.run_id, ctx.tenant, supervisor_id, child["actor_id"], "task",
               {"task": spec.get("task")}, corr_id=f"spawn-{child['actor_id']}")
    _audit(child["name"], "Hired", "executed",
           {"actor_id": child["actor_id"], "role": child["role"], "kind": kind,
            "supervisor": supervisor_id})
    return child["actor_id"]


# -------------------------------------------------------------------------------- worker step
_WORKER_PROMPT = """You are {name} — an autonomous AI employee in a durable agent org.
IDENTITY: role={role}, actor_id={aid}. TENURE: hired {hired}, decide-step {step} of {maxs}.
ASSIGNMENT: {assignment}
MEMORY/CONTEXT: {context}
RECENT PROGRESS: {progress}

Decide your NEXT action and reply with ONE JSON object:
  {{"action":"continue","note":"what you did this step"}}
  {{"action":"emit","kind":"blocked|finding|question|need_agent|need_context|disagree","payload":{{...}},"note":"..."}}
  {{"action":"finish","result":"the final result","note":"..."}}
Emit 'blocked' the MOMENT you hit a wall you cannot pass alone (e.g. missing API creds); your
supervisor will resolve it and you will resume. Emit 'finding' to propagate a correction. Emit
'need_agent' (payload {{"role","task"}}) if the work needs another hire. Emit 'disagree' (payload
{{"reason":"..."}}) if you professionally believe the ASSIGNMENT ITSELF is wrong, unwise, or harmful —
you don't just execute a bad directive; you object and it goes UP to the CEO to rule on. Reply with JSON only."""


def _worker_step(ctx, a, evs):
    """One durable decide-loop step of an IC. Inbox first (a resolution/correction may unpark or
    re-task it), then at most ONE work AI call, then persist. Ported from actor.py's decide-loop."""
    mem = dict(a["memory"] or {})
    context = dict(mem.get("context") or {})
    progress = list(mem.get("progress") or [])
    steps = int(mem.get("steps") or 0)
    assignment = a["assignment"] or ""
    me, sup = a["actor_id"], a["supervisor_id"]
    step = _Step()
    work = False
    tool_result = None                          # set when our dispatched tool finished (jobrunner emitted it)

    for ev in evs:
        k, p, corr = ev["kind"], (ev["payload"] or {}), ev["corr_id"]
        if k == "task":
            if p.get("task"):
                assignment = p["task"]
                step.assignment = assignment
            work = True
        elif k == "tool_result":                # our dispatched tool finished -> report it up + finish
            tool_result = p
            work = True
        elif k == "next":                       # own continuation token — keep the loop alive
            work = True
        elif k == "resolve":                    # supervisor cleared our blocker -> RESUME
            _merge_context(context, p)
            context["_resolved"] = True
            work = True
        elif k == "context_update":             # propagated correction; resumes a parked actor
            _merge_context(context, p)
            if a["status"] in ("blocked", "parked"):
                work = True
        elif k in ("question", "need_context"):  # a peer asks US -> AI answer back on the corr
            d = _ai_json(a["role"], ctx.repo,
                         f"You are {a['name']} ({a['role']}). A peer asks: {json.dumps(p)[:600]}\n"
                         f"Your context: {json.dumps(context)[:1200]}\n"
                         'Reply ONLY JSON: {"answer":"..."}', spawner=a["role"])
            step.emits.append((me, ev["frm"], "context_update",
                               {"context": {"answer": d.get("answer") or ""}}, corr))
        # other kinds addressed to a worker are informational — completing them records receipt.

    # TOOL-WORKER (agentic-org phase 3b): a worker whose memory carries a `tool` runs REAL long work (a
    # browser QA explore, a dev fix) that must NOT block a lease-bound decide-step. DISPATCH-AND-PARK: hand
    # the job to jobrunner (runs off-loop, browser lives there) and park (blocked). The job emits done+finding
    # to our supervisor and flips us terminal on completion; a crashed job is re-dispatched by reconcile.
    tool = (context or {}).get("tool")
    if work and a["status"] not in TERMINAL and tool_result is not None:
        # the dispatched tool finished: report its findings + a done up to the supervisor, then FINISH. Only
        # the pool (here) writes this actor's row — the job thread merely emitted the tool_result event.
        _findings = tool_result.get("findings") or []
        for f in _findings:
            if sup:
                step.emits.append((me, sup, "finding", f, None))
        if sup:
            # the done carries the story + whether a BLOCKING bug was found, so the qa-coordinator can track
            # per-story status (and thus give an honest verdict + drive the re-test loop) from the done alone.
            step.emits.append((me, sup, "done", {"task": assignment, "tool": tool_result.get("tool"),
                               "status": tool_result.get("status"), "result": tool_result.get("result"),
                               "story": (tool_result.get("result") or {}).get("story"),
                               "blocking_found": any(f.get("blocking") for f in _findings)}, None))
        step.status = "done"
        step.result = {"tool": tool_result.get("tool"), "status": tool_result.get("status"),
                       "result": tool_result.get("result")}
    elif work and a["status"] not in TERMINAL and tool and not context.get("tool_dispatched"):
        try:
            import jobrunner
            jobrunner.dispatch({"run_id": ctx.run_id, "tenant": ctx.tenant, "actor_id": me,
                                "supervisor_id": sup, "actor_name": a["name"], "tool": tool,
                                "args": context.get("tool_args") or {}, "assignment": assignment}, store)
            context["tool_dispatched"] = True
            step.status = "blocked"                 # PARK; memory (tool + tool_dispatched) is the resume handle
            _audit(a["name"], "ToolDispatched", "executed", {"tool": tool})
        except Exception as e:                      # dispatch failed -> escalate, don't silently hang
            step.status = "blocked"
            if sup:
                step.emits.append((me, sup, "blocked", {"reason": f"tool dispatch failed: {e}"}, None))
    elif work and a["status"] not in TERMINAL and tool and context.get("tool_dispatched"):
        pass                                        # dispatched; waiting on the job to emit done (no work call)
    elif work and a["status"] not in TERMINAL:
        steps += 1
        if steps > MAX_ACTOR_STEPS:
            step.status, step.result = "dead", {"failed": True, "reason": "max steps exhausted"}
            if sup:
                step.emits.append((me, sup, "done", {"failed": True, "reason": "max steps"}, None))
        else:
            d = _ai_json(a["role"], ctx.repo, _WORKER_PROMPT.format(
                name=a["name"], role=a["role"], aid=me, hired=a["hired_at"], step=steps,
                maxs=MAX_ACTOR_STEPS, assignment=assignment,
                context=json.dumps(context)[:1800], progress="; ".join(progress[-6:]) or "(none)"),
                spawner=ctx.spawner_role(a))
            if d.get("_blocker"):               # a factory gate refused (budget/halt/…) -> park
                d = {"action": "emit", "kind": "blocked", "payload": {"reason": d["_blocker"]}}
            action = (d.get("action") or "continue").lower()
            if action == "finish":
                res = d.get("result") or d.get("note") or "done"
                step.status, step.result = "done", {"result": res}
                if sup:
                    step.emits.append((me, sup, "done", {"task": assignment, "result": res}, None))
            elif action == "emit":
                kind = d.get("kind") if d.get("kind") in store.KINDS else "finding"
                payload = dict(d.get("payload") or {})
                if d.get("note") and "note" not in payload:
                    payload["note"] = d["note"]
                if sup:
                    step.emits.append((me, sup, kind, payload, f"ev-{me}-{steps}"))
                if kind in ("blocked", "disagree"):
                    step.status = "blocked"     # PARK; memory is the resume handle. A disagreement parks the
                    _audit(a["name"], "WorkerBlocked" if kind == "blocked" else "WorkerDisagree",
                           "blocked", payload)   # agent until the CEO rules (proceed / revise the directive).
                else:
                    step.emits.append((me, me, "next", {}, None))   # keep working after a finding
                    step.status = "working"
            else:                               # continue
                progress.append(d.get("note") or f"step {steps}")
                step.emits.append((me, me, "next", {}, None))
                step.status = "working"

    step.memory = {"context": context, "progress": progress[-30:], "steps": steps}
    _persist(ctx, a, step, evs)


# ----------------------------------------------------------------------------- supervisor step
_DECOMPOSE_PROMPT = (
    "You are a LEAD decomposing a task for your team in a recursive, elastic agent-org. Decide "
    "(a) the INDEPENDENT subtasks, (b) HOW MANY children to staff, and (c) for each child whether "
    "it is a single IC ('worker') or, if its subtask is itself broad enough to need its OWN team, "
    "a sub-lead ('supervisor'). Bias toward EXPANDING structure when the scope is large (cost is "
    "not a constraint). Reply ONLY JSON:\n"
    '{{"org_note":"...", "children":[{{"role":"<role>","kind":"worker|supervisor","task":"..."}}]}}\n'
    "TASK:\n{task}")

_DECIDE_PROMPT = (
    "You are an interrupt-driven LEAD. A child just emitted an event. Decide ONE action and reply "
    'ONLY JSON. RESOLVE locally when you can: "unblock"/"rebrief" (send guidance to the child; '
    'include "message"), "spawn_helper" (staff a new agent; include "spec":{{"role","task"}}), '
    '"hand_next" (give the child its next task; include "task"), "broadcast" (a correction/context '
    'EVERY sibling must get; include "message"), "ack" (note it, no action). ESCALATE '
    '("action":"escalate", include "reason") ONLY when the blocker is beyond your capability '
    "(needs a capability enabled, a sub-fleet restart, or a human). "
    "EVENT: kind={kind} from={frm} payload={payload}")

_AGGREGATE_PROMPT = (
    "You are the LEAD. Synthesize your children's results into ONE coherent result. "
    'Reply ONLY JSON: {{"result":"...", "ok":true}}. '
    "RESULTS: {results} ESCALATIONS(unresolved): {escalations}")

_CONTROLLER_PROMPT = (
    "You are the CONTROLLER — the TOP escalation tier of a recursive agent-org. A supervisor "
    "escalated a blocker beyond its capability. Decide how to CLEAR it: enable a capability / "
    "grant creds / restart a sub-fleet, or CONSULT THE HUMAN if it truly needs a person. Reply "
    'ONLY JSON: {{"action":"resolve"|"consult_human","grant":"<capability or creds to hand back>",'
    '"message":"<what to tell the team / ask the human>"}}. ESCALATION: {payload}')


def _coordinator_specs(ctx, a, task, role):
    """QA/dev COORDINATORS spawn TOOL-workers DETERMINISTICALLY (not an AI decompose): one qa-explorer per
    story, or one dev-fixer per bug. The run params (vision/target_url/token/org/stories/bug) ride in the
    coordinator's memory.context, set by the agentic entrypoint (qa_run agentic=True). Returns [] if there is
    nothing to spawn (e.g. dev-coordinator with no bug yet) so the caller falls back to the generic path."""
    c = dict((a.get("memory") or {}).get("context") or {})
    if role == "qa-coordinator":
        base = {"target_url": c.get("target_url"), "vision": c.get("vision") or task,
                "token": c.get("token"), "org": c.get("org", "0"), "artifact_dir": c.get("artifact_dir")}
        return [{"name": f"{a['name']}.explorer{i}", "role": "qa-explorer", "kind": "worker",
                 "task": f"QA-explore story: {(s.get('title') or s.get('id') or i)}",
                 "tool": "qa_explore", "tool_args": {**base, "story": s}}
                for i, s in enumerate(c.get("stories") or [])]
    if role == "dev-coordinator":
        bug = c.get("bug")
        if not bug:
            return []
        return [{"name": f"{a['name']}.fixer", "role": "dev-fixer", "kind": "worker",
                 "task": f"Fix: {bug.get('title') or bug.get('bug') or 'defect'}",
                 "tool": "dev_fix", "tool_args": {"bug": bug, "vision": c.get("vision"), "repo": c.get("repo"),
                    "target_url": c.get("target_url"), "stories": c.get("stories"),
                    "restart_cmd": c.get("restart_cmd"), "health_url": c.get("health_url"),
                    "token": c.get("token"), "org": c.get("org")}}]
    # COMPANY / CEO coordinator: context.functions = [{role, tool, items, worker_role, task}] -> spawn one
    # SUPERVISOR (a FUNCTION coordinator) per function, each carrying its own tool-team context. This is the
    # top of a full CEO-directed org: CEO-coordinator -> function coordinators -> tool-workers -> reports up.
    funcs = c.get("functions")
    if isinstance(funcs, list) and funcs:
        return [{"name": f.get("role") or f"function-{i}", "role": f.get("role") or f"function-{i}",
                 "kind": "supervisor",
                 "task": f.get("task") or f"Deliver the '{f.get('role', 'function')}' function toward: {task}",
                 "context": {"tool": f.get("tool"), "items": f.get("items") or [],
                             "worker_role": f.get("worker_role")}}
                for i, f in enumerate(funcs)]

    # GENERIC TOOL-TEAM: any coordinator whose context declares a `tool` + a list of `items` spawns one
    # tool-worker per item — this is how the org staffs ANY function (research, finance, legal, data, …) with
    # REAL work, reusing the proven tool-worker/dispatch-and-park pattern instead of a per-role branch.
    tool, items = c.get("tool"), c.get("items")
    if tool and isinstance(items, list) and items:
        worker_role = c.get("worker_role") or (role.replace("-coordinator", "").replace("-lead", "") or "worker")
        return [{"name": f"{a['name']}.w{i}", "role": worker_role, "kind": "worker",
                 "task": (it.get("task") or it.get("topic") or str(it)) if isinstance(it, dict) else str(it),
                 "tool": tool, "tool_args": (dict(it) if isinstance(it, dict) else {"task": str(it)})}
                for i, it in enumerate(items)]
    return []


def _decompose_specs(ctx, a, task):
    """The org-shape AI call. The root controller plans DOMAINS via org_decider.plan_org (one
    supervisor per domain — recursive from there); QA/dev coordinators spawn tool-workers deterministically;
    any other lead splits its task via AI into worker / sub-supervisor child specs. Never returns [] — a
    parse miss degrades to a single IC."""
    if a["kind"] == "controller":
        plan = org_decider.plan_org(task)
        return [{"name": f"{d['name']}-lead", "role": d.get("supervisor") or "supervisor",
                 "kind": "supervisor",
                 "task": f"Deliver the '{d['name']}' domain toward the vision: {task}"}
                for d in plan["root"]["children"]]
    specs = _coordinator_specs(ctx, a, task, (a.get("role") or "").lower())
    if specs:
        return specs
    d = _ai_json(a["role"], ctx.repo, _DECOMPOSE_PROMPT.format(task=task), spawner=a["role"])
    specs = []
    for i, c in enumerate(d.get("children") or []):
        if isinstance(c, dict) and c.get("task"):
            specs.append({"name": f"{a['name']}.c{i}", "role": c.get("role") or a["role"],
                          "kind": "supervisor" if c.get("kind") == "supervisor" else "worker",
                          "task": c["task"]})
    return specs or [{"name": f"{a['name']}.c0", "role": a["role"], "kind": "worker", "task": task}]


def _hire_or_request(ctx, a, specs, step, corr=None):
    """Governance-gated hiring. Allowed -> hire directly. Denied (manifest role without
    can_spawn) -> file a hire REQUEST up the tree as `need_agent` (only the controller spawns).
    Returns 'hired' | 'requested' | 'denied' (top-of-tree denial = hard failure)."""
    deny = _spawn_gate(a["role"])
    if deny is None:
        for spec in specs:
            _hire(ctx, a["actor_id"], spec)
        return "hired"
    _audit(a["name"], "SpawnDenied", "denied", {"reason": deny, "specs": len(specs)})
    if a["supervisor_id"]:
        step.emits.append((a["actor_id"], a["supervisor_id"], "need_agent",
                           {"specs": specs, "for": a["actor_id"], "reason": deny}, corr))
        return "requested"
    return "denied"


def _supervisor_step(ctx, a, evs):
    """One durable step of a lead/controller: interrupt-driven handling of whatever landed in
    its inbox, then the aggregate check. Ported from supervisor.Supervisor + orchestra.Controller."""
    mem = dict(a["memory"] or {})
    phase = mem.get("phase") or "new"
    results = dict(mem.get("results") or {})
    handled = list(mem.get("handled") or [])
    escalations = list(mem.get("escalations") or [])
    blocked_child = mem.get("blocked_child")
    me, tid = a["actor_id"], ctx.tenant
    is_top = a["supervisor_id"] is None
    step = _Step()

    children = {c["actor_id"]: c for c in store.actors(ctx.run_id, tid)
                if c["supervisor_id"] == me}

    def _live_children():
        return [cid for cid, c in children.items() if c["status"] not in TERMINAL]

    def _broadcast(payload, note):
        """Correct EVERY live child (the parked one last, so its resume lands after siblings)."""
        order = sorted(_live_children(), key=lambda cid: cid == blocked_child)
        for cid in order:
            step.emits.append((me, cid, "context_update", {"context": payload, "note": note}, None))

    for ev in evs:
        k, p, corr, frm = ev["kind"], (ev["payload"] or {}), ev["corr_id"], ev["frm"]

        # ---- kickoff: decompose + hire ---------------------------------------------------
        if k == "task" and phase == "new":
            task = p.get("task") or a["assignment"] or ""
            specs = _decompose_specs(ctx, a, task)
            outcome = _hire_or_request(ctx, a, specs, step, corr)
            if outcome == "hired":
                phase = "delegating"
                children = {c["actor_id"]: c for c in store.actors(ctx.run_id, tid)
                            if c["supervisor_id"] == me}
            elif outcome == "requested":
                phase = "hiring"
                mem["pending_specs"] = specs
            else:                                # top of tree and still denied -> hard stop
                step.status = "dead"
                step.result = {"failed": True, "reason": "spawn denied by governance at the root"}
                store.finish_run(ctx.run_id, "failed", step.result, tenant_id=tid)
            handled.append({"frm": frm, "kind": k, "action": f"decompose:{outcome}",
                            "children": len(specs)})
            continue

        # ---- a hire REQUEST from a governance-denied lead below --------------------------
        if k == "need_agent" and p.get("specs") and p.get("for"):
            deny = _spawn_gate(a["role"])
            if deny is None:
                hired = [_hire(ctx, p["for"], s) for s in p["specs"]]
                step.emits.append((me, frm, "resolve",
                                   {"hired": [h for h in hired if h], "note": "hired on your behalf"},
                                   corr))
                act = "hire_on_behalf"
            elif a["supervisor_id"]:            # can't hire either -> keep escalating up
                step.emits.append((me, a["supervisor_id"], "need_agent", p, corr))
                act = "escalate_hire"
            else:
                escalations.append({"frm": frm, "reason": deny, "specs": p["specs"]})
                act = "hire_denied"
            handled.append({"frm": frm, "kind": k, "action": act,
                            "outstanding": _live_children()})
            continue

        # ---- a resolution coming DOWN from our own supervisor -----------------------------
        if k == "resolve" and frm == a["supervisor_id"]:
            if p.get("hired"):                  # our hire request was fulfilled by the tier above
                phase = "delegating"
                mem.pop("pending_specs", None)
                children = {c["actor_id"]: c for c in store.actors(ctx.run_id, tid)
                            if c["supervisor_id"] == me}
            else:                               # an escalation grant -> team-wide broadcast
                grant = p.get("grant", p)
                _broadcast({"grant": grant}, "resolution from above")
                blocked_child = None
            handled.append({"frm": frm, "kind": k, "action": "apply_resolution",
                            "outstanding": _live_children()})
            continue

        # ---- child events: the interrupt-driven core --------------------------------------
        if frm in children:
            if k == "done":
                results[str(frm)] = p
                children[frm] = store.actor(frm, tid) or children[frm]
                if (a.get("role") or "").lower() == "qa-coordinator":
                    childrole = (children.get(frm) or {}).get("role")
                    if childrole == "qa-explorer" and p.get("story") is not None:
                        # authoritative LATEST status for this story (a re-tested-fixed story flips to clean).
                        ss = dict(mem.get("story_status") or {})
                        ss[str(p["story"])] = "blocking" if p.get("blocking_found") else "clean"
                        mem["story_status"] = ss
                        # GAP-FILL: an explorer that stopped with INCOMPLETE coverage (not a bug) gets another
                        # qa-explorer hired to CONTINUE that story — bounded per story so it always terminates.
                        stop = ((p.get("result") or {}).get("stop_reason") or "").lower()
                        incomplete = any(x in stop for x in ("incomplete", "stalled", "stuck", "cap", "deadline"))
                        gf = dict(mem.get("gapfills") or {})
                        if (incomplete and not p.get("blocking_found")
                                and gf.get(str(p["story"]), 0) < _MAX_GAPFILL):
                            gf[str(p["story"])] = gf.get(str(p["story"]), 0) + 1
                            mem["gapfills"] = gf
                            cc = dict((a.get("memory") or {}).get("context") or {})
                            sobj = next((s for s in (cc.get("stories") or [])
                                         if (s.get("id") or s.get("title")) == p["story"]), None)
                            if sobj is not None:
                                _hire_or_request(ctx, a, [{
                                    "name": f"{a['name']}.gapfill-{p['story']}-{gf[str(p['story'])]}",
                                    "role": "qa-explorer", "kind": "worker",
                                    "task": f"GAP-FILL untested aspects of story {p['story']}",
                                    "tool": "qa_explore", "tool_args": {"target_url": cc.get("target_url"),
                                        "vision": cc.get("vision"), "token": cc.get("token"),
                                        "org": cc.get("org", "0"), "story": sobj}}], step, corr)
                                children = {c["actor_id"]: c for c in store.actors(ctx.run_id, tid)
                                            if c["supervisor_id"] == me}
                                _audit(a["name"], "QaGapFill", "executed",
                                       {"story": p["story"], "attempt": gf[str(p["story"])]})
                    elif childrole == "dev-coordinator":
                        # RE-TEST LOOP (closed loop): a fix finished -> hire a FRESH qa-explorer to re-verify
                        # the fixed story, bounded per story so a bad fix can't cycle find<->fix forever.
                        cc = dict((a.get("memory") or {}).get("context") or {})
                        bug = ((children[frm].get("memory") or {}).get("context") or {}).get("bug") or {}
                        story = bug.get("story")
                        sobj = next((s for s in (cc.get("stories") or [])
                                     if (s.get("id") or s.get("title")) == story), None)
                        rt_ = dict(mem.get("retests") or {})
                        if sobj is not None and rt_.get(str(story), 0) < _MAX_RETEST:
                            rt_[str(story)] = rt_.get(str(story), 0) + 1
                            mem["retests"] = rt_
                            _hire_or_request(ctx, a, [{
                                "name": f"{a['name']}.retest-{story}-{rt_[str(story)]}", "role": "qa-explorer",
                                "kind": "worker", "task": f"RE-TEST story {story} after fix",
                                "tool": "qa_explore", "tool_args": {"target_url": cc.get("target_url"),
                                    "vision": cc.get("vision"), "token": cc.get("token"),
                                    "org": cc.get("org", "0"), "story": sobj}}], step, corr)
                            children = {c["actor_id"]: c for c in store.actors(ctx.run_id, tid)
                                        if c["supervisor_id"] == me}
                            _audit(a["name"], "QaRetest", "executed", {"story": story, "attempt": rt_[str(story)]})
                handled.append({"frm": frm, "kind": k, "action": "record",
                                "outstanding": _live_children()})
                continue
            if k == "need_agent":               # a child wants a helper -> org_decider gates it
                dec = org_decider.should_expand(
                    p, {"children": [{"id": cid, "role": c["role"], "status": c["status"]}
                                     for cid, c in children.items()]},
                    {"event": "need_agent", "from": frm})
                if dec.get("expand"):
                    spec = {"name": f"{a['name']}.h{len(children)}",
                            "role": p.get("role") or a["role"], "kind": "worker",
                            "task": p.get("task") or dec.get("detail") or "assist the team"}
                    _hire_or_request(ctx, a, [spec], step, corr)
                    act = "spawn_helper"
                else:
                    step.emits.append((me, frm, "context_update",
                                       {"note": f"expansion declined: {dec.get('rationale')}"}, corr))
                    act = "declined"
                handled.append({"frm": frm, "kind": k, "action": act,
                                "outstanding": _live_children()})
                continue

            # DISAGREEMENT: a child professionally objects to the directive. NEVER a local supervisor decide —
            # it always goes UP to the CEO's tier to rule on (proceed / revise); the child parks until the
            # ruling flows back (via resolve -> broadcast). This is the human-pattern "an agent won't just
            # execute a bad directive; it pushes back and the boss decides".
            if k == "disagree":
                blocked_child = frm
                reason = p.get("reason") or p.get("note") or "objects to the assignment"
                if is_top:                          # CEO tier -> consult the human, then resolve the ruling down
                    ruling = (ctx.human_hook(ev, {"disagreement": reason})
                              if callable(getattr(ctx, "human_hook", None)) else "proceed as directed")
                    step.emits.append((me, frm, "resolve",
                                       {"note": f"CEO ruling on the objection: {ruling}",
                                        "grant": {"ruling": ruling}, "context": {"ruling": ruling}}, corr))
                    escalations.append({"frm": frm, "kind": "disagree", "resolved_with": str(ruling)[:200]})
                elif a["supervisor_id"]:            # route the objection further up toward the CEO
                    step.emits.append((me, a["supervisor_id"], "disagree",
                                       {"reason": reason, "from": frm, "payload": p}, corr))
                _audit(a["name"], "Disagreement", "escalated", {"frm": frm, "reason": str(reason)[:160]})
                handled.append({"frm": frm, "kind": k, "action": "disagreement_up",
                                "outstanding": _live_children()})
                continue

            # QA-COORDINATOR records every finding (for its honest aggregate verdict), then hands blocking ones off.
            if k == "finding" and (a.get("role") or "").lower() == "qa-coordinator":
                mem.setdefault("qa_findings", []).append(
                    {"title": p.get("title") or p.get("bug"), "story": p.get("story"),
                     "blocking": bool(p.get("blocking"))})

            # QA-COORDINATOR dev-handoff (phase 4b): a BLOCKING finding from an explorer is handed to a
            # dev-coordinator — the coordinator-to-coordinator conversation. Deterministic: hire a
            # dev-coordinator with the bug + repo/vision in its context; it spawns a dev-fixer, fixes on the
            # real git diff, and emits `done` back up, which the qa-coordinator aggregates. (Re-test of the
            # fixed story after the fix is a further refinement — see HANDOFF phase 4b.)
            if k == "finding" and (a.get("role") or "").lower() == "qa-coordinator" and p.get("blocking"):
                cc = dict((a.get("memory") or {}).get("context") or {})
                _hire_or_request(ctx, a, [{
                    "name": f"{a['name']}.dev{len(children)}", "role": "dev-coordinator", "kind": "supervisor",
                    "task": f"Fix blocking bug: {p.get('title') or p.get('bug') or 'defect'}",
                    "context": {"bug": p, "vision": cc.get("vision"), "repo": cc.get("repo"),
                                "target_url": cc.get("target_url"), "stories": cc.get("stories"),
                                "token": cc.get("token"), "org": cc.get("org"),
                                "restart_cmd": cc.get("restart_cmd"), "health_url": cc.get("health_url")}}],
                    step, corr)
                children = {c["actor_id"]: c for c in store.actors(ctx.run_id, tid)
                            if c["supervisor_id"] == me}          # include the new dev-coordinator as a child
                _audit(a["name"], "QaDevHandoff", "executed", {"bug": (p.get("title") or p.get("bug"))})
                handled.append({"frm": frm, "kind": k, "action": "dev_handoff",
                                "outstanding": _live_children()})
                continue

            # blocked / escalate / question / finding / need_context / next
            if is_top:                          # TOP TIER: resolve it or consult the human
                d = _ai_json(a["role"], ctx.repo,
                             _CONTROLLER_PROMPT.format(payload=json.dumps(p, default=str)[:800]),
                             spawner=a["role"])
                action = (d.get("action") or "resolve").lower()
                if action in ("consult_human", "ask_human", "human") and callable(ctx.human_hook):
                    grant = ctx.human_hook(ev, d)
                else:
                    grant = d.get("grant") or d.get("message") or "capability granted"
                step.emits.append((me, frm, "resolve", {"grant": grant, "for": p}, corr))
                escalations.append({"frm": frm, "kind": k, "resolved_with": str(grant)[:200]})
                handled.append({"frm": frm, "kind": k, "action": "resolve",
                                "outstanding": _live_children()})
                _audit(a["name"], "ControllerResolve", "executed", {"frm": frm, "grant": str(grant)[:160]})
                continue

            d = _ai_json(a["role"], ctx.repo, _DECIDE_PROMPT.format(
                kind=k, frm=frm, payload=json.dumps(p, default=str)[:800]), spawner=a["role"])
            action = "escalate" if d.get("_blocker") else (d.get("action") or "ack").lower()
            if action == "escalate":
                blocked_child = frm             # keep the child PARKED (it resumes on the grant)
                reason = d.get("reason") or d.get("_blocker") or "beyond supervisor capability"
                escalations.append({"frm": frm, "kind": k, "reason": reason})
                step.emits.append((me, a["supervisor_id"], "escalate",
                                   {"reason": reason, "from": frm, "payload": p}, corr))
                _audit(a["name"], "Escalate", "escalated", {"frm": frm, "reason": reason})
            elif action == "broadcast":
                _broadcast({"correction": d.get("message") or d.get("context")}, "team correction")
            elif action == "spawn_helper":
                spec = dict(d.get("spec") or {"role": a["role"], "task": d.get("task") or "assist"})
                spec.setdefault("name", f"{a['name']}.h{len(children)}")
                _hire_or_request(ctx, a, [spec], step, corr)
            elif action == "hand_next":
                step.emits.append((me, frm, "task", {"task": d.get("task") or "continue"}, corr))
            elif action in ("unblock", "rebrief"):
                step.emits.append((me, frm, "resolve",
                                   {"note": d.get("message") or "proceed", "context": {}}, corr))
            # 'ack' -> recorded only
            handled.append({"frm": frm, "kind": k, "action": action,
                            "outstanding": _live_children()})
            continue

        handled.append({"frm": frm, "kind": k, "action": "ignored"})

    # ---- aggregate: the ONLY join point, reached by events (never a barrier) --------------
    if (phase == "delegating" and children and not mem.get("aggregated")
            and all(c["status"] in TERMINAL for c in children.values())):
        if (a.get("role") or "").lower() == "qa-coordinator":
            # HONEST agentic QA verdict — deterministic, not an AI aggregate. A run is only 'passed' with NO
            # blocking bugs; blocking bugs handed to dev report the fix as CLAIMED (re-verification after fix
            # is the phase-4b refinement, so we say so plainly rather than pretend it's confirmed).
            qf = mem.get("qa_findings") or []
            ss = mem.get("story_status") or {}          # LATEST status per story (re-tested-fixed -> clean)
            blocking_stories = [s for s, st in ss.items() if st == "blocking"]
            devs = [c for c in children.values() if c.get("role") == "dev-coordinator"]
            passed = not blocking_stories               # honest: passed only if NO story is still blocking
            verdict = (f"AGENTIC QA — ALL CLEAR: {len(ss)} stories, no blocking bugs remain "
                       f"(after {len(devs)} fix hand-off(s) + re-test)" if passed else
                       f"AGENTIC QA — {len(blocking_stories)} story(ies) STILL blocking after "
                       f"{len(devs)} fix hand-off(s): {', '.join(map(str, blocking_stories))}")
            agg = {"result": verdict, "ok": True, "passed": passed, "stories": len(ss),
                   "bugs": len(qf), "blocking_stories": blocking_stories, "handed_to_dev": len(devs)}
            _audit(a["name"], "QaVerdict", "executed", agg)
        else:
            d = _ai_json(a["role"], ctx.repo, _AGGREGATE_PROMPT.format(
                results=json.dumps(results)[:2000], escalations=json.dumps(escalations)[:800]),
                spawner=a["role"])
            agg = {"result": d.get("result", ""), "ok": bool(d.get("ok", True)),
                   "children": len(children), "escalations": len(escalations)}
            _audit(a["name"], "Aggregate", "executed", agg)
        mem["aggregated"] = True
        step.status, step.result = "done", agg
        if a["supervisor_id"]:
            step.emits.append((me, a["supervisor_id"], "done",
                               {"task": a["assignment"], "result": agg}, None))
        else:                                   # the root finished -> the RUN finishes
            store.finish_run(ctx.run_id, "done" if agg["ok"] else "failed", agg, tenant_id=tid)

    step.memory = {"phase": phase, "results": results, "handled": handled[-60:],
                   "escalations": escalations, "blocked_child": blocked_child,
                   "aggregated": mem.get("aggregated", False),
                   "qa_findings": mem.get("qa_findings", []),   # qa-coordinator's honest-verdict tally
                   "story_status": mem.get("story_status", {}),  # latest pass/blocking per story
                   "retests": mem.get("retests", {}),           # re-test attempts per story (bounded loop)
                   "gapfills": mem.get("gapfills", {})}         # gap-fill attempts per story (bounded loop)
    if mem.get("pending_specs") and phase == "hiring":
        step.memory["pending_specs"] = mem["pending_specs"]
    if step.status is None and phase != "new" and a["status"] == "idle":
        step.status = "working"
    _persist(ctx, a, step, evs)


# ================================================================================ the worker pool
class _Ctx:
    def __init__(self, run_id, tenant, repo, human_hook, lease_s, max_steps):
        self.run_id, self.tenant, self.repo = run_id, tenant, repo
        self.human_hook = human_hook
        self.lease_s = lease_s
        self.max_steps = max_steps
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.steps_done = 0
        self.active = 0
        self.max_concurrency = 0
        self.last_progress = time.time()
        self.halted = False
        self.errors = []
        self._roles = {}

    def spawner_role(self, actor):
        """The supervisor's role, for factory.agent's spawner= threading (cached per parent)."""
        sup = actor.get("supervisor_id")
        if sup is None:
            return None
        if sup not in self._roles:
            p = store.actor(sup, self.tenant)
            self._roles[sup] = p["role"] if p else None
        return self._roles[sup]


def _execute_step(ctx, a, evs, wname):
    """ONE durable unit: gates -> decide (AI) -> persist -> heartbeat. Any exception leaves the
    claimed events leased (lease-reclaimed later) — a bad step can repeat, never vanish."""
    try:
        if a["kind"] in ("supervisor", "controller"):
            _supervisor_step(ctx, a, evs)
        else:
            _worker_step(ctx, a, evs)
    except Exception:
        ctx.errors.append(traceback.format_exc())
        _audit(a.get("name", "?"), "StepError", "error", {"worker": wname,
                                                          "trace": traceback.format_exc()[-800:]})


def _pool_loop(ctx, wname, poll_s, stall_s):
    while not ctx.stop.is_set():
        r = store.run(ctx.run_id, ctx.tenant)
        if not r or r["status"] != "running":
            ctx.stop.set()
            return
        if _halted():                            # KILLSWITCH: no step starts while halted
            ctx.halted = True
            ctx.stop.set()
            return
        progressed = False
        for a in store.actors(ctx.run_id, ctx.tenant):
            if ctx.stop.is_set():
                return
            with ctx.lock:                       # step-budget gate (crash-sim / test knob)
                if ctx.max_steps is not None and ctx.steps_done >= ctx.max_steps:
                    ctx.stop.set()
                    return
            evs = store.claim_events(a["actor_id"], ctx.tenant, claimed_by=wname,
                                     lease_s=ctx.lease_s)
            if not evs:
                continue
            if a["status"] in TERMINAL:          # stale mail to a finished actor -> drain
                for ev in evs:
                    store.complete_event(ev["id"], ctx.tenant)
                continue
            with ctx.lock:
                ctx.steps_done += 1
                ctx.active += 1
                ctx.max_concurrency = max(ctx.max_concurrency, ctx.active)
            try:
                fresh = store.actor(a["actor_id"], ctx.tenant) or a
                _execute_step(ctx, fresh, evs, wname)
            finally:
                with ctx.lock:
                    ctx.active -= 1
                ctx.last_progress = time.time()
            progressed = True
        if not progressed:
            if time.time() - ctx.last_progress > stall_s:
                ctx.stop.set()                   # stalled (no claimable work) -> stop, resumable
                return
            time.sleep(poll_s)


def run_org(run_id, tenant_id, repo=".", workers=2, human_hook=None, max_steps=None,
            lease_s=store.CLAIM_LEASE_S, poll_s=0.15, stall_s=20.0):
    """THE engine entry: a pool of N worker threads each repeatedly claiming ANY actor's pending
    events (SKIP LOCKED — no two workers ever process the same event) and executing ONE durable
    step. Returns {"run", "steps", "max_concurrency", "halted", "errors"}. Idempotent + resumable:
    call it again after ANY interruption (crash, kill-switch, max_steps) and it picks up every
    non-terminal actor from its persisted rows; events claimed by dead workers reappear after
    `lease_s`."""
    store.ensure()
    # CRASH-RESUME for tool jobs: re-dispatch any tool-worker that's parked with a dispatched tool but whose
    # background job died with a previous process. Idempotent; fail-open (never blocks the engine start).
    try:
        import jobrunner
        jobrunner.reconcile_parked(store, run_id, tenant_id)
    except Exception:
        pass
    ctx = _Ctx(run_id, tenant_id, repo, human_hook, lease_s, max_steps)
    threads = [threading.Thread(target=_pool_loop, args=(ctx, f"orgw-{i}", poll_s, stall_s),
                                daemon=True, name=f"orgw-{i}")
               for i in range(max(1, int(workers)))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return {"run": store.run(run_id, tenant_id), "steps": ctx.steps_done,
            "max_concurrency": ctx.max_concurrency, "halted": ctx.halted, "errors": ctx.errors}


# ============================================================================================
# OFFLINE SELFTEST — factory.agent stubbed (no model/network/spend); REAL local Postgres.
# Proves, all through Postgres rows: 2-worker pool / supervisor + 2 children / blocked ->
# escalate -> resolve -> resume -> aggregate; then a CRASH mid-run (step budget + a dead
# worker's stranded claim) resumed to completion by a fresh run_org; then the kill-switch and
# governance hire-request gates. Touches only its own throwaway tenant's rows; cleans up.
# ============================================================================================
GRANT = "serviceX-cred=LIVE-OK (use v3 endpoint)"


def make_offline_agent():
    """Deterministic offline stand-in for factory.agent driving the canonical storyline:
    payments domain -> lead -> charge worker (blocks on serviceX creds) + refunds worker."""
    def agent(role, repo, task, **kw):
        t = task or ""
        if t.startswith("You are the ORG ARCHITECT"):
            sup = "tech-lead" if "hire-request" in t else "eng-director"
            tree = {"vision": "payments", "root": {"kind": "controller", "title": "Controller",
                    "children": [{"kind": "domain", "name": "payments", "supervisor": sup,
                                  "teams": []}]}}
            return {"rc": 0, "out_full": json.dumps(tree)}
        if t.startswith("You are a LEAD decomposing"):
            kids = [{"role": "backend-engineer", "kind": "worker",
                     "task": "Build the charge endpoint (needs serviceX API creds)"},
                    {"role": "backend-engineer", "kind": "worker",
                     "task": "Build the refunds endpoint"}]
            if "hire-request" in t:
                kids = kids[1:]                  # governance scenario: one simple worker
            return {"rc": 0, "out_full": json.dumps({"org_note": "split", "children": kids})}
        if t.startswith("You are an interrupt-driven LEAD"):
            if "kind=blocked" in t:
                return {"rc": 0, "out_full": json.dumps(
                    {"action": "escalate",
                     "reason": "no API creds for serviceX — needs the controller to provision"})}
            return {"rc": 0, "out_full": json.dumps({"action": "ack"})}
        if t.startswith("You are the CONTROLLER"):
            return {"rc": 0, "out_full": json.dumps(
                {"action": "resolve", "grant": GRANT, "message": "serviceX creds provisioned"})}
        if t.startswith("You are the LEAD. Synthesize"):
            return {"rc": 0, "out_full": json.dumps(
                {"result": "charge + refunds endpoints integrated", "ok": True})}
        if "autonomous AI employee" in t:
            if "serviceX" in t:
                if GRANT in t:                   # the resolution grant reached our memory-context
                    return {"rc": 0, "out_full": json.dumps(
                        {"action": "finish", "result": "charge endpoint built (serviceX v3 OK)"})}
                return {"rc": 0, "out_full": json.dumps(
                    {"action": "emit", "kind": "blocked",
                     "payload": {"reason": "no API creds for serviceX"}})}
            # The SIBLING is deliberately slow (ONE long step, then finishes) so it is provably LIVE while the
            # lead processes the escalate->resolve chain and broadcasts the correction — the broadcast (a
            # context_update event) is created while the sibling is a live child. 2.0s comfortably exceeds the
            # ~1s escalation chain, so the interrupt-broadcast-to-sibling assertion can't flake; and because it
            # FINISHES (not loops), standalone scenarios with no broadcast stay fast.
            time.sleep(2.0)
            return {"rc": 0, "out_full": json.dumps(
                {"action": "finish", "result": "refunds endpoint built"})}
        return {"rc": 0, "out_full": json.dumps({"action": "finish", "result": f"done: {t[:40]}"})}
    return agent


def _selftest():
    import types
    import uuid
    import psycopg

    tid = f"orchestra-runtime-selftest-{uuid.uuid4().hex[:8]}"
    real_agent = factory.agent
    ok = True

    def check(cond, label):
        nonlocal ok
        print(("PASS" if cond else "FAIL") + f": {label}")
        ok = ok and bool(cond)

    factory.agent = make_offline_agent()
    try:
        # ================= scenario 1: the full chain, 2-worker pool, all Postgres ==========
        org = create_org(tid, "A payments service: charge + refund endpoints", repo="/tmp/x")
        res = run_org(org["run_id"], tid, repo="/tmp/x", workers=2, stall_s=10)
        r = res["run"]
        check(not res["errors"], f"no step errors ({res['errors'][:1]})")
        check(r and r["status"] == "done" and (r["result"] or {}).get("ok"),
              f"run finished done+ok through Postgres (result={r and r['result']})")

        acts = store.actors(org["run_id"], tid)
        by_kind = {}
        for a in acts:
            by_kind.setdefault(a["kind"], []).append(a)
        lead = by_kind.get("supervisor", [None])[0]
        wrk = by_kind.get("worker", [])
        check(len(acts) == 4 and lead and len(wrk) == 2
              and all(a["status"] == "done" for a in acts),
              f"org tree rows: controller + supervisor + 2 children, all done "
              f"({[(a['name'], a['status']) for a in acts]})")
        tree = store.org_tree(org["run_id"], tid)
        check(len(tree["tree"]) == 1 and len(tree["tree"][0]["reports"]) == 1
              and len(tree["tree"][0]["reports"][0]["reports"]) == 2,
              "org chart nests controller -> lead -> 2 workers from supervisor_id rows")

        evs = store.events(org["run_id"], tid)
        kinds = [e["kind"] for e in evs]
        chain = all(k in kinds for k in ("task", "blocked", "escalate", "resolve",
                                         "context_update", "done"))
        check(chain, f"durable event chain blocked->escalate->resolve->context_update->done "
                     f"(kinds={sorted(set(kinds))})")
        check(all(e["processed_at"] for e in evs), f"every one of {len(evs)} events processed")
        claimers = {e["claimed_by"] for e in evs if e["claimed_by"]}
        check(len(claimers) >= 2 and res["max_concurrency"] >= 2,
              f"REAL parallelism: {len(claimers)} pool workers claimed steps "
              f"({sorted(claimers)}), max in-flight={res['max_concurrency']}")

        blocked_w = next(w for w in wrk if "serviceX" in (w["assignment"] or ""))
        sibling = next(w for w in wrk if w is not blocked_w)
        check(blocked_w["memory"].get("steps", 0) >= 2
              and GRANT in json.dumps(blocked_w["memory"].get("context")),
              f"parked child RESUMED from persisted memory with the grant "
              f"(steps={blocked_w['memory'].get('steps')})")
        blk = next((h for h in lead["memory"]["handled"] if h["kind"] == "blocked"), None)
        check(bool(blk) and blk["action"] == "escalate"
              and sibling["actor_id"] in blk.get("outstanding", []),
              f"interrupt semantics: lead escalated the block WHILE the sibling was still "
              f"working (outstanding={blk and blk['outstanding']})")
        sib_upd = [e for e in evs if e["kind"] == "context_update"
                   and e["to_actor"] == sibling["actor_id"]]
        check(len(sib_upd) >= 1, "the correction was broadcast to the sibling too")
        check(lead["result"] and lead["result"]["ok"] and lead["result"]["children"] == 2,
              "supervisor aggregated only after every child was terminal")

        # ================= scenario 2: CRASH mid-run -> fresh run_org resumes ================
        org2 = create_org(tid, "crash-sim: payments service again", repo="/tmp/x")
        res_a = run_org(org2["run_id"], tid, repo="/tmp/x", workers=2, max_steps=3, stall_s=10)
        r2 = store.run(org2["run_id"], tid)
        acts2 = store.actors(org2["run_id"], tid)
        nonterm = [a for a in acts2 if a["status"] not in TERMINAL]
        check(r2["status"] == "running" and nonterm,
              f"stopped mid-run after {res_a['steps']} steps: run still 'running', "
              f"{len(nonterm)} actors non-terminal")
        # a DEAD worker's stranded claim: claim events, never complete, backdate the lease
        doomed = 0
        for a in acts2:
            doomed += len(store.claim_events(a["actor_id"], tid, claimed_by="doomed-worker"))
        with psycopg.connect(store.DB) as c, c.cursor() as cur:
            cur.execute("""UPDATE orchestra_events SET claimed_at = claimed_at - interval '2 hours'
                           WHERE claimed_by='doomed-worker' AND tenant_id=%s""", (tid,))
            c.commit()
        check(doomed >= 1, f"simulated crash: {doomed} in-flight events stranded by a dead worker")
        res_b = run_org(org2["run_id"], tid, repo="/tmp/x", workers=2, stall_s=10)
        r2 = store.run(org2["run_id"], tid)
        acts2 = store.actors(org2["run_id"], tid)
        check(r2["status"] == "done" and (r2["result"] or {}).get("ok")
              and all(a["status"] == "done" for a in acts2)
              and all(e["processed_at"] for e in store.events(org2["run_id"], tid)),
              f"fresh run_org picked the org up and FINISHED it (lost nothing; "
              f"{res_b['steps']} resumed steps)")
        w2 = next(a for a in acts2 if "serviceX" in (a["assignment"] or ""))
        check(w2["memory"].get("steps", 0) >= 2 and GRANT in json.dumps(w2["memory"]),
              "the blocked child still went through escalate->resolve->resume after the crash")

        # ================= scenario 3: kill-switch gates every step ==========================
        org3 = create_org(tid, "killswitch-sim: tiny org", repo="/tmp/x")
        fake_ks = types.SimpleNamespace(
            is_halted=lambda scope="global": {"halted": True, "scope": scope, "reason": "test"})
        real_ks_rt, real_ks_store = globals()["killswitch"], store.killswitch
        globals()["killswitch"], store.killswitch = fake_ks, fake_ks
        try:
            res3 = run_org(org3["run_id"], tid, repo="/tmp/x", workers=2, stall_s=5)
        finally:
            globals()["killswitch"], store.killswitch = real_ks_rt, real_ks_store
        check(res3["halted"] and res3["steps"] == 0
              and store.run(org3["run_id"], tid)["status"] == "running",
              "HALT: zero steps execute while the kill-switch is down; the run stays resumable")
        res3b = run_org(org3["run_id"], tid, repo="/tmp/x", workers=2, stall_s=10)
        check(store.run(org3["run_id"], tid)["status"] == "done",
              f"after resume from HALT the same org completes ({res3b['steps']} steps)")

        # ================= scenario 4: governance spawn gate -> hire request =================
        # 'tech-lead' HAS a manifest with can_spawn false -> its decompose may NOT hire directly;
        # the runtime files need_agent up to the controller (can_spawn true), which hires for it.
        org4 = create_org(tid, "hire-request governance sim", repo="/tmp/x")
        res4 = run_org(org4["run_id"], tid, repo="/tmp/x", workers=2, stall_s=10)
        evs4 = store.events(org4["run_id"], tid)
        acts4 = store.actors(org4["run_id"], tid)
        lead4 = next((a for a in acts4 if a["kind"] == "supervisor"), None)
        req = [e for e in evs4 if e["kind"] == "need_agent"
               and (e["payload"] or {}).get("specs")]
        hired_w = [a for a in acts4 if a["kind"] == "worker"]
        check(lead4 and lead4["role"] == "tech-lead" and len(req) == 1
              and req[0]["frm"] == lead4["actor_id"]
              and len(hired_w) == 1 and hired_w[0]["supervisor_id"] == lead4["actor_id"]
              and store.run(org4["run_id"], tid)["status"] == "done",
              f"governance: can_spawn=false lead filed a hire request; the controller hired the "
              f"worker ON ITS BEHALF under the lead; run completed ({res4['steps']} steps)")

    finally:
        factory.agent = real_agent
        import psycopg as _pg
        with _pg.connect(store.DB) as c, c.cursor() as cur:   # only THIS tenant's throwaway rows
            cur.execute("DELETE FROM orchestra_events WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM orchestra_actors WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM orchestra_runs WHERE tenant_id=%s", (tid,))
            c.commit()

    print("\n" + ("PASS: durable runtime — every decide-step a Postgres-backed unit; parallel "
                  "pool; escalate->resolve->resume->aggregate; crash-resume lossless; killswitch "
                  "+ governance gates enforced ✅" if ok else "FAIL"))
    return 0 if ok else 1


def _main(argv):
    if not argv or argv[0] == "selftest":
        return _selftest()
    print("usage: runtime.py selftest", file=sys.stderr)
    return 2


__all__ = ["create_org", "run_org", "make_offline_agent", "TERMINAL", "MAX_ACTOR_STEPS", "GRANT"]


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
