# Live e2e dogfood — findings & fix plan

Dogfooding: driving agent-os's own fleet to build agent-os's own **CEO Cockpit** app (loopcontroller
DISCOVER→…→DELIVER) surfaced real bugs. Two are *fundamental design gaps*; the rest are contained. Tracking +
fixing all of them (owner directive: fix them all, effort is fine). Testing uses the host's **Claude
subscription** (tenant `ceo-e2e` on subscription mode + enterprise plan).

## Root-cause design gaps

### G1 — No central execution service (jobs die with their caller)
**Symptom:** `loopcontroller._dispatch(thread_id, kind, fn)` runs the heavy phase work (research fleet, prototype,
build, QA) in an in-process `threading.Thread(daemon=True)`. That thread lives only as long as *whatever process
called* `advance()`/`say()`. In prod that's the long-lived `console.py`, so it usually works — but it's fragile
(a console restart mid-job orphans the thread; only research is reconciled by `resume_stalled`), it centralises
nothing (every caller spawns its own threads + `claude` subprocesses, no shared concurrency/resource control),
and it silently stalls at `awaiting='fleet'` for any short-lived caller.
**Fix — a central Job Runner daemon (`jobd`):** heavy controller jobs become durable `controller_jobs` rows with
a lifecycle `queued → running(leased) → done/failed` + a `spec` (thread_id, phase, kind) sufficient to
RECONSTRUCT the work from thread state. `advance()` ENQUEUES instead of spawning a thread. A long-lived `jobd`
daemon claims queued jobs (`FOR UPDATE SKIP LOCKED` + lease), runs the phase work via a `PHASE_WORKERS`
dispatch table, and calls `advance(result)` on completion — a bounded worker pool, the ONE place `claude`
subprocesses get spawned for controller jobs. A leased job whose worker dies is reclaimed (crash-resume);
`resume_stalled` stays as the higher-level reconciler. Coordinators/controllers now *communicate with* this
central runner (enqueue + durable result) instead of owning execution threads. Wired into `recover.sh`.

### G2 — The controller babysits agents instead of agents deciding
**Symptom:** in DISCOVER the LLM keeps drafting plans/options as prose instead of emitting the `[[RESEARCH]]`
marker that advances the phase, so the controller never moves without a human/driver forcing it. It's
over-conversational ("perky") where it should judge "do I know enough? then GO."
**Fix:** rewrite the chat-gate prompts so the AGENT self-decides when it has *enough* and emits the transition
marker decisively — at most ONE clarifying question, then proceed. The controller shouldn't force transitions;
the agent should exit the gate when good-enough. Applies to DISCOVER (→RESEARCH) and the plan-draft gate.

## Contained findings
- **F1 — `say()`/`_llm` timeout strands the thread.** A transient CLI timeout left a bare "timeout" message to
  the CEO, no auto-retry, phase stuck. Fix: retry the LLM call (bounded, backoff) inside `_llm`; on exhaustion
  surface an honest, actionable, RESUMABLE message — never a bare "timeout". Bump the per-call timeout for the
  big scoping prompts.
- **F3 — misleading error mapping.** `quota_check_failed: no such tenant` (an integrity error) is rendered to
  the CEO as *"You've hit your plan's build quota — upgrade your plan"* because the mapper matches the substring
  "quota". Fix: match the SPECIFIC quota-exceeded signal (e.g. `within_quota == False` / an explicit
  `quota_exceeded` marker), and treat "no such tenant" / unknown errors as internal errors, not billing.
- **F5 (setup, resolved) — a build needs a registered tenant + plan.** A synthetic tenant fails the spend gate;
  registered `ceo-e2e` (enterprise plan, subscription provider) for testing.

## Status
- [x] **G1 — central `jobd` execution service.** `scripts/jobd.py` — a long-lived daemon that each tick (a)
  runs `resume_stalled()` (recover orphans, now IN a persistent process so re-dispatched work survives), and
  (b) drives every *runnable* thread (awaiting IS NULL, not DELIVER, settled ≥5s) via `advance()`, so the fleet
  work it dispatches lives in jobd and cascades to completion — instead of dying with a short-lived caller or
  the old 10-min `loopcontroller.py resume` subprocess. Per-thread advisory lock prevents double-drive. Wired
  into `recover.sh` (persistent daemon) + `selftest.sh`; running live (pid confirmed, driving threads).
  *Follow-up:* fully route dispatch through jobd (enqueue-only `_dispatch`, drop the inline thread) once proven
  in prod — jobd already guarantees progress as the durable driver, so this is an optimization, not a gap.
- [x] G2 — decisive chat-gate prompts (agent self-advances; DISCOVER no longer perky).
- [x] F1 — `_llm` timeout retry (3× backoff, longer timeout) + honest resumable fallback (never bare "timeout").
- [x] F3 — precise quota-vs-integrity error mapping (only a real "quota reached" → billing message).
- [x] F5 — tenant/plan/provider set up for the e2e.

## Second wave (surfaced after the pipeline ran end-to-end)
- **F6 — product-name mismatch / F7 — build-error-ignored → ROOT CAUSE found.** The IMPLEMENT phase called
  `qualityloop.run` (a quality-IMPROVE loop) on a product **that was never scaffolded**. Both `verify.verify` and
  `improve.improve_once` return `"no such product"` when the repo doesn't exist — so the build errored (score 0.0)
  and QA had nothing to test. *Same disease:* a phase assumed a precondition ("product exists") that no prior
  step guaranteed. **Fixed** two ways: (1) `productregistry` + boundary contracts surface it honestly instead of
  "QA found 0 stories"; (2) `_do_build` now **scaffolds via `factory.build_product` at the registered path if the
  repo is missing, THEN runs the quality loop** — so the product actually gets built before it's improved/verified.
- **QA→human instead of dev → FIXED.** `_autoloop_build`: a failed/unverifiable build routes back to the builder
  automatically (bounded `AOS_MAX_BUILD_RETRY=3`), escalating to the CEO only when exhausted.

## Architecture
See [`ARCHITECTURE-ROOT-CAUSE.md`](ARCHITECTURE-ROOT-CAUSE.md): all of these are one disease — the older pipeline
re-derived shared truth and trusted handoffs. The cure (single-source-of-truth registry + validated boundary
contracts + auto-loop) is now shipped and is the law for the pipeline going forward.

## Live run
Baseline build (pre-fix): thread 1561, tenant `ceo-e2e` — research fleet ran green after the tenant fix; used
to surface downstream (design/dev/QA) findings while the fixes land.
