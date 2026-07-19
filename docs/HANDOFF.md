# agent-os — Handoff & Roadmap

**Purpose:** everything an engineer (human or an AI coding agent like Codex) needs to continue this work
without prior context — what's DONE, what's IN PROGRESS, what's REMAINING, plus long-term vs short-term
priorities. Provider-neutral (nothing here depends on which model you use to develop).

**The one rule above all:** every change answers to [`NORTH-STAR.md`](NORTH-STAR.md) — every user is a CEO
running AI-agent companies; the bar is *astonish a skeptic, zero bugs reach a human, nothing fails
invisibly*. When in doubt, re-read it.

**How to verify you haven't broken anything (run before AND after any change):**
```bash
cd ~/projects/agent-os
bash scripts/selftest.sh                 # full suite of module selftests — MUST stay "0 failed" (the pass
                                         # COUNT grows as modules are added; never gate on a fixed number)
.venv/bin/python -m pytest tests/ -q     # the offline pytest suite (one of selftest.sh's checks) — 0 failed
bash scripts/recover.sh                  # bring all services up (idempotent)
```
Individual module selftests (fast, offline, no API/browser — run the one you touched):
```bash
.venv/bin/python scripts/qa/qa_explorer.py        # coverage-driven explorer
.venv/bin/python scripts/qa/qa_run.py selftest    # QA loop end-to-end (stubbed)
.venv/bin/python scripts/qa/qa_report.py selftest  # verdict logic
.venv/bin/python scripts/pulse.py selftest         # observability plane (needs DB)
.venv/bin/python scripts/orchestra/runtime.py selftest   # the agent org runtime (TIMING-SENSITIVE)
.venv/bin/python scripts/orchestra/tools.py         # agentic tool layer
.venv/bin/python scripts/orchestra/jobrunner.py     # dispatch-and-park executor
```

---

## Part 1 — What's DONE (this session, committed on `master`)

Newest → oldest. Each is committed, tested, and green.

### SOTA research → improvement backlog → execution (NEW)
- **Deep-research** of the 2024–26 agentic-AI field → [`docs/AGENTIC-AI-RESEARCH.md`](AGENTIC-AI-RESEARCH.md):
  9 three-vote-verified findings, 9 extracted signals, and an **Appendix mining all 133 claims** from the
  research subagent transcripts (themed). Every finding tagged ADOPT/ADAPT/ALREADY-DO/AVOID for us.
- **[`docs/IMPROVEMENTS-PLAN.md`](IMPROVEMENTS-PLAN.md)** — the **live execution tracker** for the 18
  prioritized improvements: per-item status / approach / research-owed. **Start here to continue the research-
  driven work.** Items flagged 🔬 need a design/research dig before coding (called out in the doc).
- **Tier-0 auditor hardening shipped** (`scripts/review.py`): (1) **master-key sanitization** — neutralizes
  verdict-priming tokens ("Thought process:", symbol-only fields) in agent-written evidence before the auditor
  reads them; (2) **perspective-diverse JURY** (skeptic / user-flow / evidence lenses, `AOS_AUDITOR_ENSEMBLE`
  default 3) that **ESCALATES on a split** (`close_call`) instead of auto-accepting — a single-pass judge is
  research-proven unsafe. Both QA callers (`qa_run.py`, `qa_agentic.py`) treat a close call as **not a pass**.
  Mid-sized-judge knob `AOS_AUDITOR_MODEL`. Auditor now **grounds in the real dev work** (`fix-round-*.json`:
  files changed + residual bugs, cross-checked vs claims). Selftest: `python scripts/review.py --selftest`.
- **Auditor VALIDATION harness** (`scripts/qa/auditor_validate.py`): Cohen's kappa vs a human-labeled set,
  position-bias probe (reorder → verdict must not flip), test-retest ≥3, and a **high-stability+high-bias → FAIL**
  flag on `pulse`. Needs a real labeled fixture set to run in anger. `python scripts/qa/auditor_validate.py --selftest`.
- **Memory-layer first slice** (`scripts/companymemory.py`, `postgres/initdb/52-memory.sql`) — design in
  [`docs/MEMORY-LAYER-DESIGN.md`](MEMORY-LAYER-DESIGN.md). Extends the existing spine with two-tier
  `shared`/`private` visibility, per-role scope, provenance (author/run/sources → tamper-evident audit chain), and
  **coordinator PLAN + phase-SUMMARY checkpoints** (`checkpoint()`/`plan()`/`summaries()` — item 10 storage).
  **Activated the previously-dead fleet-learning writer**: `learn_from_fix()` distills a role lesson at the QA
  fix-loop post-mortem (`AOS_MEMORY_LEARN`, default on). `.venv/bin/python scripts/companymemory.py selftest` (needs DB).
- **The live tracker is [`docs/IMPROVEMENTS-PLAN.md`](IMPROVEMENTS-PLAN.md)** — per-item status for all 18.
  **As of 2026-07-13 the backlog is worked through:** ✅ items 1–8,10,12,13,14,15,16,18; 🟡 9 & 11 (capability
  shipped, deeper wiring measurement-gated); 🟡 17 (a web-verification pass is running to confirm the single-source
  memory/OTel/A2A leads and correct any stale field names). New modules this pass: `scripts/orchestra/mast.py`
  (MAST 14-mode taxonomy), `scripts/otel.py` (OTel GenAI span mapper), `scripts/qa/auditor_validate.py`
  (auditor validation + hold-out split). New runtime behaviour: task **contracts** on every hire
  (`runtime._task_contract`, fail-open), a fan-out **cost/visibility gate** (`runtime._fanout_gate`), provable
  **effect records** (`tools.effect_record` + `appregistry` git-publish). Items 6/15/16 were resolved by explicit
  documented **decision** (see the plan) rather than speculative code.

### QA is now coverage-driven and honest
- **`75834fd` coverage-driven QA** — the explorer no longer stops at a hardcoded step count. It enumerates a
  **coverage ledger** ("everything a user would try") and tests until covered, checkpointing every step
  (`checkpoint.json`) so a crash/shutdown loses nothing and can resume. Also: Playwright `.webm` →
  **scrollable H.264 `.mp4`** (per-story + a stitched `qa-session.mp4`); tiered models (fast model for
  navigation, Opus for the bug-judgment). Files: `scripts/qa/qa_explorer.py`, `qa_run.py`, `artifacts.py`,
  `browser_bridge.js`. Principle: **caps are only high safety backstops, never the quality terminator.**
- **`0af78fe` gap-filling loop** — a QA round isn't "done" just because there's no blocking bug; if coverage
  is INCOMPLETE it spawns **gap-fill rounds** re-testing the incomplete stories. Self-terminates on
  coverage-complete or an honest "gap-stalled"; `MAX_ROUNDS` is a runaway cap only. `scripts/qa/qa_run.py`.
- **`7bad85f` honest verdict** — a run that ended incomplete/stuck can **never** report "passed" (it fed the
  LAUNCH gate before). `scripts/qa/qa_report.py` (`_incomplete`).
- **`25ac8c1` grounded coverage** — an aspect is credited as tested only when the **evaluator** (which sees
  the real before→after) confirms it (`demonstrated`), not the decider's optimistic `covers` claim.

### Deterministic scrutiny (the auditor)
- **`7ae8470` work-execution auditor** — `scripts/review.py`: `dossier(run)` reconstructs a run from
  ground-truth evidence (per-step reasoning/action/expected/ACTUAL/verdict + screenshots + video + coverage);
  `review(run, rubric)` runs a **skeptical AI auditor** ("if the evidence doesn't show it, it didn't happen")
  → writes `AUDIT.md`. Catches skipped flows, unbacked coverage claims, vague/dishonest reporting. Run it:
  `python scripts/review.py <evidence_dir|qa-pulse-work-id> [--rubric "..."] [--dossier]`.
- **`aaec394` auditor sign-off gate** — the auditor runs at the end of every `qa_run`; a run it **rejects**
  gets `passed=False` (blocks the LAUNCH gate). Env `AOS_QA_AUDIT_GATE` (off in the offline selftest).
- **`67fdaff` audit → governed findings** — a rejected audit's gaps become owned, SLA-tracked `findings.py`
  items (AI-routed to a dev/QA role), so "the skeptic rejected it" becomes real, tracked dev work.

### Observability — nothing runs blind
- **`a1ca251` pulse plane** — `scripts/pulse.py` + `agent_pulse` table (also `postgres/initdb/51-pulse.sql`):
  every in-flight unit of agentic work beats a heartbeat on a cadence. `pulse.live()` /
  `python scripts/pulse.py` = one-glance "what is every agent doing right now, is anything stuck?".
  `watchdog.check()` escalates silent work (silence = failure signal). QA is fully wired (beats every explore
  step + the finalize phases that used to go dark).
- **`b77aa01` whole-system pulse** — builds beat per lifecycle stage (`controller.stage_step`); the fleet is
  surfaced READ-ONLY by aggregating `orchestra_actors.last_active` (do NOT write on the actor hot path — it
  perturbs the timing-sensitive supervisor/sibling race, learned the hard way).
- **`3b15d31` + `036abd5` dashboard panels** — the mission-control dashboard (`scripts/dashboard.py`, :8092,
  phone-visible over Tailscale) now shows a **Live agent work** panel (pulse) and a **QA quality** panel
  (latest verdict per product: passed / AUDIT REJECTED / incomplete).

### Foundations
- **`32c5058` Opus default, Fable opt-in** — the fleet defaults to `claude-opus-4-8`; Fable is opt-in only
  (too credit-expensive). `scripts/factory.py` (`BUILD_MODEL`/`FALLBACK_MODEL`/`FRONTIER_MODEL`,
  `fable_default_build` flag). Owner directive.
- **`3c5e5cb`** — recovered/checkpointed in-progress Jul-5 work (tenant-aware Codex failover + QA evidence).

---

## Part 2 — What's IN PROGRESS: the agentic QA/dev org

**Goal:** make QA and dev **real actors** in the orchestra org — a QA-coordinator and a dev-coordinator that
converse over the durable message bus, spawn worker actors, hand off to each other, escalate to a human, and
survive crashes — instead of the current procedural Python loops (`qa_run.py`, `dev_loop.py`). This is the
biggest remaining North-Star item. **Full design: [`AGENTIC-QA-ORG.md`](AGENTIC-QA-ORG.md) — read it first.**

> **Beyond QA — the FULL CEO-directed org is now built too** (see [`NORTH-STAR-ROADMAP.md`](NORTH-STAR-ROADMAP.md)).
> The QA/dev pattern was generalized: `orchestra/tools.py` gained `research`, `finance_report`, and a
> catch-all `knowledge_work` (any of the 92 role charters does real work); `runtime._coordinator_specs` gained
> a GENERIC tool-team branch (any coordinator's `context.{tool,items}` staffs a worker team) and a COMPANY
> branch (a CEO-coordinator's `context.functions` spawns one function coordinator per function); and
> `orchestra/company.py` `run_company_org(vision, functions)` drives the whole thing — CEO-coordinator →
> function coordinators → tool-worker teams → reports aggregating up — proven offline (`company.py` selftest).
> Remaining is live activation + specialized external-action tools + deeper console UI (roadmap items 1/2/3-live/4).

**The crux the design solves:** a story's `qa_explore` drives a LIVE browser for 10–30 min. That can't run
inside a short lease-bound decide-step (the 900s event lease would reclaim it → duplicate browser) and can't
be sliced across steps (the browser subprocess must stay alive the whole story). **Solution =
dispatch-and-park:** a tool-worker dispatches the job to a background runner and parks (`blocked`); the runner
runs it and emits `done`/`finding` on completion; crash-safety reuses the pulse plane (each job is a pulse;
silence → reconcile re-dispatches).

**Phases (each independently tested so the runtime is never left broken):**
- ✅ **Phase 1 — tool layer** (`de7…`/`0b5…`): `scripts/orchestra/tools.py` — `run_tool(name, args)` wrapping
  `qa_explore` + `dev_fix`, fail-soft. Unit-tested.
- ✅ **Phase 3a — job runner** (`de7d9f7`): `scripts/orchestra/jobrunner.py` — `dispatch(job)` runs a tool in
  a bg thread, emits `finding`+`done` to the supervisor, flips the parked worker terminal; `reconcile()`
  re-dispatches silent jobs (idempotent). Unit-tested.
- ✅ **Phase 3b — runtime hook** (`52da513`): `_worker_step` detects a tool-worker (`memory.context.tool`) and
  dispatch-and-parks via jobrunner instead of an inline AI call; `run_org` calls `jobrunner.reconcile_parked`
  at startup (crash-resume). Additive + guarded — the text-worker path is unchanged.
- ✅ **Phase 4 — coordinators spawn tool-workers** (`a8aed28`): `_hire` carries a child's `tool`/`tool_args`
  into memory.context (→ tool-worker); `_decompose_specs` → `_coordinator_specs` makes qa-coordinator spawn
  one qa-explorer per story and dev-coordinator spawn one dev-fixer per bug (run params from the
  coordinator's memory.context). Generic supervisor step then reacts to their finding/done + aggregates.
- ✅ **Phase 5 — agentic entrypoint** (`8ad739b`, `f4bbcd0`): `scripts/qa/qa_agentic.py` `run_agentic_qa()`
  creates the QA org (hire qa-coordinator → spawn qa-explorer tool-workers per story → dispatch-and-park →
  findings over the bus → aggregate) and drives `run_org` to completion. **The full ASYNC drive works
  end-to-end, reliably (~3s, 3/3)** — the selftest stubs both seams (the tool AND factory.agent) and asserts
  run=done, 2 explorers terminal, 2 findings over the bus. (An earlier apparent "hang" was an under-stubbed
  test spawning real CLI subprocesses in the coordinator's decide step — not a runtime bug; a stack dump
  found it.) Completion is single-writer-correct: jobrunner emits ONE `tool_result` event to the worker
  (new bus KIND); only the pool writes actor rows.

---

## Part 3 — What's REMAINING (do IN ORDER; test each)

### Short-term (finish the agentic org — the current thrust)
1. ✅ DONE — Phases 3b/4/5 + 4b hand-off (`52da513`/`a8aed28`/`8ad739b`/`f4bbcd0`/`ef0253b`). The agentic org
   WORKS end-to-end incl. **qa-coordinator → dev-coordinator → dev-fixer** hand-off, reliably tested
   (`python scripts/qa/qa_agentic.py`: 2 explorers find bugs → 2 dev-coordinators → 2 dev-fixers → done).
2. **Phase 4b — DONE.** ✅ (a) dev-handoff (`ef0253b`); ✅ (d) honest verdict (`acb1ac1`); ✅ (c) closed-loop
   re-test after fix (`0dd53ab`); ✅ (b) gap-fill on incomplete coverage (`117dbc6`). The qa-coordinator now
   covers the full QA decision set: explore → gap-fill → find → hand-off → fix → re-test → honest verdict.
   Optional remaining: (e) at aggregate also run `review.review` (needs the agentic org to assemble a review
   dossier from the explorers' results). Role manifests (governance hygiene; fail-open works without them):
   `~/projects/control-plane/roles/{qa-coordinator,dev-coordinator}.yaml` (`can_spawn:true`),
   `{qa-explorer,dev-fixer}.yaml` (`can_spawn:false`). ✅ (e) AUDITOR sign-off gate on the agentic path
   (`9d81e09`): per-step evidence is propagated (tools.qa_explore → run-final.json in review's dossier shape)
   and `review.review` runs as a gate (rejected → passed=False). **Phase 4b is now fully DONE.**

   **Pattern for adding coordinator behaviors** (all of the above use it): coordinator state lives in the
   supervisor's `mem` and MUST be persisted in `step.memory` (see `qa_findings`/`story_status`/`retests` in
   `0dd53ab`); hires grow the children set the aggregate join waits on (fine — bound them so it settles);
   findings arrive as separate `finding` events (not in `done`), but a tool-worker's `done` now carries
   `story` + `blocking_found` for per-story tracking.

3. **Phase 6 — parity + flip the default.** ✅ 6a (`5f88a50`): `qa_runs` row + `docs/QA-VERDICT.json`. ✅ 6b
   (`f9ea7d3`): `qa_run(agentic=True)` opt-in entrypoint. ✅ 6-i (`59e0e64`): evidence dir + COVERAGE.md +
   coverage.json + run-final.json + files still-open findings. **The agentic path now leaves the SAME
   artifacts as the procedural loop.** ONLY remaining: (iii) **run BOTH paths against the LIVE console**
   (`.venv/bin/python -c "import sys;sys.path[:0]=['scripts','scripts/qa'];import qa_run,qa_smoke_args;
   qa_run.qa_run(<live console url/vision/token/org/summary>, agentic=True)"` — easiest: copy the seed/ensure
   from `qa_run._smoke`), confirm the agentic verdict + evidence + QA-VERDICT.json match the procedural loop,
   then (iv) flip `qa_run`'s default to `agentic=True`. **This live parity run is Codex's key validation** —
   everything up to it is offline-tested and green. Until (iii) passes, procedural stays default.
3. **Phase 5 — agentic entrypoint.** `qa_run(..., agentic=True)` creates the QA org: `create_org(tenant,
   vision)` → the controller/plan spawns a qa-coordinator whose memory.context carries
   {vision, target_url, token, org, product, stories (from story_gen), artifact_dir, repo, restart_cmd,
   health_url} → `run_org(...)`. **Keep the procedural loop as the default** until the agentic path is proven
   AT PARITY (same honest verdict + auditor gate + findings on the same target).

**~~KNOWN FLAKY TEST~~ FIXED (`f095076`):** the runtime selftest's "correction was broadcast to the sibling"
race is de-flaked — the sibling now does one 2.0s step (> the ~1s escalate→broadcast chain) then finishes, so
it's provably live when the broadcast fires (4/4 reliable, ~19.5s). `selftest.sh` should now be reliably 0-fail.

### Medium-term (polish / breadth — independent of the above)
4. Surface `pulse.live()` + QA quality in the **CONSOLE** (`scripts/console.py`, :8099, the actual CEO app),
   not only the ops dashboard.
5. **Coordinator-to-coordinator as human-pattern comms** — once phases 3b–5 land, the QA↔dev handoffs are
   already bus events; make sure they render in the dashboard's "Agent communication graph".
6. Trace ad-hoc/QA runs into the `traces` replay plane (currently new-builds-only). DEPRIORITIZED — needs
   threading `factory._ctx` global; QA already has richer replay via `review.py`.

### Long-term (the North Star frontier — big, mostly not yet started)
7. **Every FAANG process autonomous** beyond QA/dev: product creation, research, design, security, support,
   ops, incident response, planning, review cycles — as agent orgs on the same runtime. QA/dev is the
   template; generalize it.
8. **Elastic recursive orgs that grow themselves** to whatever scale the work demands (org_decider already
   biases toward expansion — push on `should_expand` signals: backlog, blocked-time, SLA breach).
9. **Multi-company at once** per CEO, each launching real businesses. Multi-tenant scaffolding exists
   (BYO-key, billing, circuit-breakers); harden it.
10. **Proactive human-pattern communication** — the controller briefs the CEO on the calls that matter and
    never on noise; disagreement, clarification, status reporting as first-class agent behaviors.

---

## Part 4 — Map of the code you'll touch most

| Area | Files |
|---|---|
| QA loop / coordinator | `scripts/qa/qa_run.py` (orchestrator), `qa_explorer.py` (browser explorer), `dev_loop.py` (dev-fix), `qa_report.py` (verdict), `artifacts.py` (evidence/mp4), `browser_bridge.js` (Playwright bridge) |
| Scrutiny | `scripts/review.py` (auditor), `scripts/findings.py` (governed findings) |
| Observability | `scripts/pulse.py`, `scripts/watchdog.py`, `scripts/sentinel.py`, `scripts/dashboard.py` |
| Agent org runtime | `scripts/orchestra/runtime.py` (decide-loop), `store.py` (durable org+bus), `org_decider.py` (org shape), `tools.py` (tool layer), `jobrunner.py` (dispatch-and-park) |
| Model policy | `scripts/factory.py` (`BUILD_MODEL`/`FALLBACK_MODEL`/`FRONTIER_MODEL`, resilient `agent()`) |
| Build lifecycle | `scripts/controller.py` (SPEC→BUILD→QA→REVIEW→LAUNCH), `gate_check` (control-plane) |

**Conventions & gotchas (READ before editing):**
- **DB:** `DATABASE_URL=` line in `~/projects/agent-os/.env.local`; `with psycopg.connect(DB) as c, c.cursor() as cur: ... c.commit()`. New tables: a `_ensure()` with `CREATE TABLE IF NOT EXISTS` on demand (see `watchdog.beat`/`pulse._ensure`) AND a `postgres/initdb/NN-*.sql` for fresh installs.
- **Fail-open observability:** a heartbeat/trace/audit write must NEVER break the work it observes — wrap in `try/except: pass`.
- **The orchestra runtime is crash-resumable and TIMING-SENSITIVE.** Never add synchronous/slow work to the hot decide-step path (`_persist`, `_worker_step`, `_pool_loop`). A single stray DB write there broke the sibling-broadcast race this session. Read from `last_active`, don't write.
- **Model:** default is Opus (`claude-opus-4-8`); Fable is opt-in (credit cost). Don't reintroduce Fable as a default.
- **QA is coverage-driven, never step-capped.** Termination = AI coverage judgment + auditor sign-off; caps are only high runaway backstops that must checkpoint + report "incomplete", never a silent "done".
- Every non-trivial module has a `selftest` — add/extend it, and keep `scripts/selftest.sh` at 0 failures.

---

## Part 5 — Quick "prove it works" demo (real run)
```bash
cd ~/projects/agent-os
bash scripts/recover.sh                                   # services up
.venv/bin/python scripts/qa/qa_run.py smoke               # live coverage-driven QA vs the console
python scripts/pulse.py                                   # watch it in flight (run in another shell mid-QA)
# after it finishes, find the evidence dir it printed, then:
python scripts/review.py <that-evidence-dir>              # the skeptical audit of what it did
```
Evidence (video `qa-session.mp4`, `COVERAGE.md`, `AUDIT.md`, screenshots) lands in a Windows-visible folder
under `/mnt/c/Users/<you>/Documents/agent-os-qa-evidence/` when running under WSL.

---

## Part 6 — MOST RECENT WORK (2026-07, newest thrust — read this first to continue)

The single source of truth for the current state is now **[`SYSTEM-AUDIT-2026-07.md`](SYSTEM-AUDIT-2026-07.md)**
(a ground-up audit of all code + docs, with every finding severity-ranked and ✅-marked as fixed). Highlights
landed after the sections above were written:

- **Durability overhaul (Steps 1–3), dispatch-and-park is now the DEFAULT** (`AOS_DISPATCH_PARK=1`). Single-
  owner drive lock, atomic orchestra decide-step, pid-reap of dead workers, crash-transparent resume. See
  [`ARCHITECTURE-OVERHAUL.md`](ARCHITECTURE-OVERHAUL.md) / [`PARK-VALIDATION.md`](PARK-VALIDATION.md).
- **The CEO's requirements agent** (`visionkeeper.py`) — holds the standing vision, self-refines requirements
  incl. the up-front "what I need from you", maintains [`SYSTEM-REQUIREMENTS.md`](SYSTEM-REQUIREMENTS.md), and
  feeds the controller so a vague prompt is enough. Auto-refines daily; surfaced in the console "Requirements".
- **Proactive comms** (`proactivecomms.py`) — pushes the calls that matter to the CEO before they wonder.
- **The living company org** (`orchestra/company.py`) — billing-correct, reliably completes, invocable via
  `company.py run "<directive>"`; research runs on the crash-resumable `run_org` engine
  ([`RESEARCH-RUNORG-REWIRE.md`](RESEARCH-RUNORG-REWIRE.md), flagged). Remaining: owner-gated LIVE run.
- **Security/compliance hardening**: provider-aware consent, Codex spend counted, auth code brute-force cap +
  non-enumerating login, published-repo secret-leak guard, watchdog survives a DB outage, auditor gate fails
  closed + covers re-verification. All in the audit ledger.

**The one true remaining critical path** is a LIVE end-to-end run (dispatch-and-park multi-hour crash test;
agentic-QA parity; the company-org live run) — everything else is offline-green. That run spends real credits
and is owner-gated.

*Session commits through `master`. Verify with `bash scripts/selftest.sh` (0 failed) + `pytest tests/` (0 failed).*
