# Design: the agentic QA/dev org

**Goal (North Star):** QA and dev must be a *real org of agents conversing*, not Python `for`-loops.
A **QA-coordinator** and **dev-coordinator** become first-class actors in the orchestra org: they hold
identity/tenure/memory, spawn worker actors, hand off work to each other over the durable message bus,
escalate to a human when stuck, and survive crashes — exactly how an elite org's QA and Eng leads
collaborate. Every decision is already an AI decision; this makes the *coordination* agentic too.

This doc answers to [`NORTH-STAR.md`](NORTH-STAR.md) and is implemented incrementally (phases below), each
phase independently tested, so the crash-resumable runtime is never left broken.

## 1. The substrate we build on (do NOT duplicate it)

`scripts/orchestra/runtime.py` + `store.py` already give a durable, crash-resumable recursive agent org:

- **Actors** (`orchestra_actors`): controller → supervisors → workers, each a durable row with
  memory/status/assignment. Dispatch by `kind` in `_execute_step` (supervisor/controller → `_supervisor_step`,
  else `_worker_step`).
- **Step contract** (`_Step`): a step mutates `emits`/`status`/`result`/`assignment`/`memory` (memory MERGES),
  then `_persist` writes actor → emits events → completes claimed events LAST (at-least-once).
- **Event bus** (`orchestra_events`): kinds `task, next, done, blocked, finding, question, need_agent,
  need_context, escalate, resolve, context_update`. Claimed `FOR UPDATE SKIP LOCKED`, **lease-reclaimable
  after `CLAIM_LEASE_S = 900s`** (a dead worker strands nothing).
- **Driver** (`run_org`): a thread pool claims events per actor and runs steps; **stalls/stops after ~20s of
  no progress**; re-invoking `run_org` resumes from the persisted rows.
- **Org shape**: `org_decider.plan_org(vision)` (controller) / `_decompose_specs` (supervisor) plan children;
  roles are an **open vocabulary**; `can_spawn` is governance-gated.

## 2. The hard problem (why this is a *careful* design, not a quick hook)

A worker step is assumed **short** (an AI call ~1 min). But a real `qa_explore` of one story:
- **drives a live browser for 10–30 min** (a `BrowserBridge` subprocess that must stay alive for the whole
  story — it CANNOT be sliced across separate decide-steps run by different pool threads), and
- would hold its claimed `task` event far past the **900s lease** → another pool worker reclaims it →
  **duplicate browser sessions / double work**, and would stall the 20s-idle pool.

So: **long-running tool work must be decoupled from the short lease-based decide-step.** Naively calling
`Explorer(...).explore()` inside `_worker_step` (as a first instinct) is WRONG on this runtime.

## 3. The solution: dispatch-and-park

A **tool-worker** never runs the tool inline. Its decide-step:

1. On `task`: **dispatch** the tool as a tracked background job and **PARK** (`status="blocked"`,
   `memory.tool_job = <job_id>`). The claimed `task` event is **completed immediately** (so no lease
   pressure); the actor is parked with its resume handle in memory. The pool moves on / stalls harmlessly.
2. A **job runner** (a managed executor, one per `run_org` process, plus a standalone reconciler) runs the
   actual tool (browser stays alive there), and on completion **emits `done` (+ `finding` per bug) from the
   worker to its supervisor** and flips the worker to `done`. The supervisor's normal interrupt-driven step
   reacts — exactly the existing `done`/`finding` machinery, no new supervisor code.
3. **Crash-safety** reuses the `pulse` plane: every dispatched tool job IS a pulse (`kind=tool-job`) that
   beats while running. If the job's process dies, its pulse goes silent → the reconciler (or watchdog)
   detects the stall and **re-dispatches** it (idempotent: keyed by actor_id+task hash). No lost or double
   work — the same guarantee the event lease gives, at the job layer.

This keeps every decide-step short (dispatch or react), preserves crash-resume, and reuses `done`/`finding`
+ `pulse` + the watchdog. It is the minimal correct shape.

## 4. The actors

- **qa-coordinator** (`supervisor`, `can_spawn`): given the vision, `_decompose_specs` → one **qa-explorer**
  tool-worker per story. Collects their `finding`/`done`. On a blocking bug → emits a **handoff** (`need_agent`
  for a dev-coordinator, or a `task` to the existing one). On incomplete coverage → spawns more explorers
  (gap-fill, now as real hires). Runs the auditor sign-off, then `done` (the QA verdict) to the controller.
- **dev-coordinator** (`supervisor`, `can_spawn`): on a bug handoff → spawns **dev-fixer** tool-workers →
  each runs the existing `dev_loop` fix+judge (on the REAL git diff) → `done` back to the qa-coordinator,
  which re-tests. The continuous loop is now literally two leads messaging each other over the bus.
- **qa-explorer** (tool-worker, `can_spawn:false`): dispatch-and-park around `tools.qa_explore` (wraps the
  proven `qa_explorer.Explorer` — coverage-driven, checkpointed, video).
- **dev-fixer** (tool-worker, `can_spawn:false`): dispatch-and-park around `tools.dev_fix` (wraps
  `dev_loop.fix_bug` — plan agents → judge on git diff).

## 5. Runaway guards (liberal, but never infinite)

`MAX_ACTOR_STEPS` per actor; event leases; `run_org` stall stop; the coverage stall/dead guards inside the
explorer; a per-run bug-fix round cap; and the pulse silence-detector on every tool job. Coverage-complete
+ auditor-accept is the *quality* terminator; these are only runaway backstops.

## 6. Incremental plan (each phase independently tested; nothing ships broken)

1. **Tool layer** — `scripts/orchestra/tools.py`: `run_tool(name, args) -> {status, findings, result, ...}`
   wrapping `qa_explore` and `dev_fix`. Isolated, unit-tested with stubs. *(no runtime change — safe first)*
2. **Role manifests** — qa-coordinator / dev-coordinator (`can_spawn`) + qa-explorer / dev-fixer.
3. **Dispatch-and-park worker hook** — a small branch in `_worker_step` for tool-workers + the job runner +
   pulse-tracked reconciler. Guarded by role; the text-worker path is untouched. Runtime selftest must stay
   green (it is timing-sensitive — verify the sibling-broadcast race still passes).
4. **Coordinator spawning** — qa/dev coordinators spawn tool-workers (extend `_decompose_specs` to allow a
   child `tool` spec) and route findings/handoffs.
5. **Entry** — `qa_run(..., agentic=True)` creates the QA org via `create_org` + `run_org` instead of the
   procedural loop; the procedural loop stays as the default/fallback until the agentic path is proven at
   parity (same honest verdict + auditor gate + findings).

Phase 1 is implemented alongside this doc; phases 2–5 follow with the same test discipline.
