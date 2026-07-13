# Architecture overhaul — durable execution for the CEO pipeline

Research-backed (Temporal, DBOS, Inngest, Restate, LangGraph, Kleppmann on distributed locking) diagnosis +
target + migration. This supersedes the piecemeal patches: it names the ONE root flaw and the correct fix.

## The single biggest flaw (plainly)
> **We infer liveness from the absence of OUTPUT, and let multiple uncoordinated drivers act on that inference —
> so a legitimately slow, quiet, still-running build is falsely declared dead and its rows are grabbed by a
> racing driver, with no fencing to stop the still-alive original from continuing.**

There is no durable, **single-owner** execution record with an **output-independent** liveness signal. Everything
else — daemon threads that die with their caller (G1), the working build reaped as "silent" (F8), the tangle of
drivers racing rows (the demo mess), no single-source-of-truth/handoff contracts (F6/F7) — flows from that root.
The loopcontroller pipeline **hand-rolls a durable-execution engine** out of the three ingredients engine
designers explicitly warn against: process-bound daemon threads, DB-polling as the driver, and output-gap-based
liveness.

## "Is it because claude isn't async?" — no (sharpened)
Async solves *in-process concurrency* (not freezing an event loop during a blocking call). It does **not** buy
durability: a crashed async process loses exactly as much as a crashed sync one. Proof: Temporal Activities can be
sync **or** async — durability comes from the workflow/worker/queue persistence model, not the `async` keyword.
Our `claude` CLI is already a separate OS process (free crash isolation — a plus). **The issue is the
ORCHESTRATION model (activity / worker / lease / durable-queue), not sync-vs-async.** Making the code async fixes
none of the four bugs.

## What every mature system does
1. **Deterministic orchestration** (the phase state machine) is separate from **non-deterministic activities**
   (LLM / `claude` subprocess calls). Activities run outside the replay path; their results are checkpointed.
   (Temporal, DBOS, Inngest, Restate, LangGraph all enforce this split.)
2. **Workers PULL from a durable queue** (`FOR UPDATE SKIP LOCKED`) — work persists independent of any worker's
   lifetime; a crashed worker's task reverts to pending and another picks it up. (Not: work living inside the
   thread that spawned it.)
3. **Liveness = an output-INDEPENDENT heartbeat + a hard ceiling.** Two independent clocks:
   a short **heartbeat timeout** (worker → store, on a background timer, regardless of output) detects a *dead
   worker* in one interval; a generous **hard duration ceiling** (sized to true worst case) catches runaways.
   **Never reap on output gaps.** (Temporal: short Heartbeat-Timeout + long Start-To-Close + an `auto_heartbeater`
   that pings on a timer; Step Functions: `HeartbeatSeconds` decoupled from `TimeoutSeconds`.) *This one principle
   eliminates F8.*
4. **Fencing tokens** — every lease mints a monotonically increasing token; writes carry it; stale tokens are
   rejected. Makes reaping *safe* even if a wrongly-reaped worker is still alive. (Kleppmann.) *Eliminates the
   double-driver hazard.*
5. **Idempotency** — activities are at-least-once, so each phase must be safe to re-run (guard on
   build+phase+attempt).

## The kicker: we already own the right engine (twice)
- **DBOS** (Postgres-backed durable workflows) powers `controller.py` — correct for a *linear* phase sequence.
- The **orchestra runtime** — durable actors on Postgres, leases, a worker pool, **dispatch-and-park** — is our
  own correct engine. Research runs on it and never hit any of these bugs.
The CEO pipeline (`loopcontroller`) is a **third, hand-rolled, inferior** durability layer. The overhaul is to
**retire it and run the CEO build on the engine we already trust** — not to invent anything.

## Target architecture
**One durable owner per build, output-independent liveness, dispatch-and-park.**
- **One owner per build** = an orchestra actor holding a Postgres lease (single-writer). Exactly one driver may
  mutate a build's rows. `jobd` / scheduler / resume-sweeper become dumb enqueuers, never row-mutators.
- **Deterministic phase loop; each `claude` phase = an idempotent, at-least-once activity** with a checkpointed
  result and a **typed handoff contract** (the product registry + boundary contracts already built are this).
- **Dispatch-and-park** for the long subprocess: the owner starts `claude`, durably records its handle, releases
  its worker slot, and resumes on completion (exit hook flips a row) — no thread held hostage for 40 min.
- **Liveness:** a background heartbeat (every ~45s, decoupled from `claude` output) + a LIBERAL hard ceiling (~6h — Opus builds are slow; the heartbeat catches real deaths in ~3 min regardless).
  Reap only on heartbeat-lapse or ceiling. Fencing token on every write.

### Keep vs discard
**Keep:** Postgres as the substrate; `claude` subprocess isolation; the orchestra durable-actor runtime; DBOS for
linear sub-pipelines; the phase decomposition; the product registry + boundary contracts (SSOT/handoff — already
engine-agnostic and correct).
**Discard:** daemon threads as the execution vehicle; output-gap / heartbeat-*frequency* reaping; the multiple
ad-hoc drivers racing rows; inferring "process alive" from work progress.

## Migration path (staged — each step ships value on its own)
1. **[Step 1 — stop the bleeding, days] Correct the liveness model in place.** Output-independent background
   heartbeat on the running job + reaper fires ONLY on heartbeat-lapse or hard-ceiling (not output silence) +
   fencing token on build-row writes. Kills F8 and the double-driver half of the tangle **without re-architecting.**
2. **[Step 2 — 1–2 wks] One owner per build.** Route all state mutations through a single driver (an orchestra
   actor lease, or a DBOS workflow keyed by build id). Convert `jobd`/scheduler/resume-sweeper into enqueuers;
   strip their direct row-writes. Kills the rest of the racing bug + cements the SSOT.
3. **[Step 3 — 2–4 wks] Move phases onto the engine, one at a time** (start BUILD/QA): each `claude` phase becomes
   an idempotent activity with dispatch-and-park. Each migrated phase survives process exit — retiring G1 incrementally.
4. **[Step 4] Consolidate on the primary engine** (lead with actor + dispatch-and-park for the CEO pipeline —
   it targets the single-writer/handoff bugs directly; keep DBOS for linear sub-pipelines). Never run two as
   *drivers* of the same build.
5. **[Step 5] Delete the hand-rolled loopcontroller durability** once every phase runs on the engine.

## Concurrency & liveness of `claude` calls (F12 — shipped)
The single Claude subscription was being over-subscribed and calls hung:
- **Reaper** (`clauded.py`): kills orphaned/hung headless `claude` calls (a dead parent orphans its child, which
  runs forever) older than 15 min — safe (never touches an interactive session); runs in jobd + the scheduler.
- **Cross-process gate** (`claude_gate.py`): the per-process semaphore (8) couldn't cap across the many driver
  processes (8×N processes → throttle). A Postgres-backed **global N-slot pool** (default 6) now caps TOTAL
  concurrent `claude` calls across the whole box; every `factory._run_once` holds a slot for its duration.
  Lease-based (a crashed holder's slot is reclaimed after 20 min); fail-open (a DB hiccup never deadlocks). This
  is the concurrency half of Step 2 (the "central way to spawn claude" the owner asked for) — done ahead of the
  full single-owner refactor.

## Status
- [x] **Step 1 — DONE** — output-independent heartbeat + reaper-on-lapse/ceiling (not output-silence) + fencing
  token + a LIBERAL 6h runaway ceiling (Opus builds are slow). Proven by `loopcontroller.py liveness`. Kills F8.
- [ ] Step 2 — single owner per build
- [ ] Step 3 — phases as dispatch-and-park activities
- [ ] Step 4 — consolidate engine
- [ ] Step 5 — delete hand-rolled durability

Sources: Temporal (activity-timeouts, detecting-activity-failures, task-queue, async-completion, durable-AI-agent),
DBOS (architecture, making-postgres-queues-scale), AWS Step Functions (HeartbeatSeconds vs TimeoutSeconds),
Restate, Inngest AgentKit, LangGraph persistence, Kleppmann "How to do distributed locking" (fencing tokens),
Dapr/Orleans single-activation, Python threading/asyncio docs. Full citation list in the research transcript.
