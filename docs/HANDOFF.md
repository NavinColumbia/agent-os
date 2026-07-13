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
bash scripts/selftest.sh                 # full suite — MUST stay "137 passed, 0 failed"
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
2. **Phase 4b remaining refinements (NEXT).** Done: (a) blocking-finding → dev-coordinator hand-off
   (`ef0253b`); (d) honest deterministic verdict at aggregate (`acb1ac1`). Still to add as `_supervisor_step`
   qa-coordinator branches: (b) on incomplete coverage in an explorer's `done` (its result carries the
   coverage ledger + stop_reason), hire more qa-explorers with `resume_covered` (gap-fill as real hires);
   (c) after a dev-coordinator's `done`, RE-TEST the fixed story (hire a fresh qa-explorer for it) — the
   closed loop; bound re-tests per story (e.g. 3) to avoid a fix↔find cycle; (e) at aggregate also run
   `review.review` (auditor) — needs the agentic org to assemble an evidence dossier from the explorers'
   results first. Role manifests (governance hygiene; fail-open works without them):
   `~/projects/control-plane/roles/{qa-coordinator,dev-coordinator}.yaml` (`can_spawn:true`),
   `{qa-explorer,dev-fixer}.yaml` (`can_spawn:false`).

   **Gotcha for (b)/(c):** these HIRE more actors, growing the children set that the aggregate join waits on
   — fine (the loop settles), but bound them so it terminates; findings arrive as separate `finding` events
   (not in child `done` payloads), so track state in the coordinator's `mem` and persist it in `step.memory`
   (see how `qa_findings` is threaded in `acb1ac1`).
3. **Phase 5 — agentic entrypoint.** `qa_run(..., agentic=True)` creates the QA org: `create_org(tenant,
   vision)` → the controller/plan spawns a qa-coordinator whose memory.context carries
   {vision, target_url, token, org, product, stories (from story_gen), artifact_dir, repo, restart_cmd,
   health_url} → `run_org(...)`. **Keep the procedural loop as the default** until the agentic path is proven
   AT PARITY (same honest verdict + auditor gate + findings on the same target).

**KNOWN FLAKY TEST (fix me):** `scripts/orchestra/runtime.py selftest` → "correction was broadcast to the
sibling too" fails ~1/3 of runs on a clean tree (a 2-worker-pool timing race in the escalate→broadcast path,
NOT caused by the agentic-org changes). De-flake it (e.g. deterministic step ordering or a barrier in the
test) so `selftest.sh` is reliably 0-fail.

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

*Last updated at handoff. Session commits: `fbb51bf` … `de7d9f7`. Working tree clean; suite 137/0 green.*
