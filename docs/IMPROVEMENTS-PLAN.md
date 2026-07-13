# Improvements plan — research findings → shipped code

The execution tracker for the 18 prioritized improvements from [`AGENTIC-AI-RESEARCH.md`](AGENTIC-AI-RESEARCH.md).
Every item: **status**, the **approach** (how we'll actually build it), and — where the "how" wasn't obvious —
a **research** note (what we dug up, or that a deeper dig is still owed). Anchored to
[`NORTH-STAR.md`](NORTH-STAR.md). Confidence tags from the research doc: **[V]** = 3-vote verified finding,
**[E]** = extracted single-source lead (validate before heavy investment).

**Status key:** ✅ done · 🟡 in progress · ⛳ next · ⬜ not started · 🔬 needs research first

---

## Tier 0 — verdict integrity (auditor) — *"zero bugs reach a human / astonish a skeptic"*

### 1. [V] Harden `review.py` into a true Agent-as-a-Judge — ✅ done (one optional deepening left)
**Done:** master-key **sanitization** (`_sanitize` neutralizes verdict-priming openers like "Thought process:"
and symbol-only fields in agent-written evidence before the auditor reads them); a configurable mid-sized judge
model via `AOS_AUDITOR_MODEL`. **Grounded in the REAL dev work:** the dossier now surfaces the `fix-round-*.json`
artifacts — files actually changed, dev's fixed-claim, and the **residual bugs still open** after each fix — and
the auditor is told to cross-check claimed fixes against residuals (catches "fixed!" over a nonzero residual).
Selftest covers all of it; wired into `scripts/selftest.sh`.
**Optional deepening (not blocking):** let the auditor **run its own checks** (re-open a screenshot, re-run a
named assertion) rather than only reading — deferred; the evidence-grounding above already moves us off narrative.
**Research:** master-key list + "larger judges more vulnerable" (2507.08794); Agent-as-a-Judge survey (2601.05111).

### 2. [V] Auditor VALIDATION harness — ✅ done (needs a real labeled set to run in anger)
**Done:** `scripts/qa/auditor_validate.py` implements the Minimum Viable Validation Protocol from *Reliability
without Validity* (2606.19544): **Cohen's kappa** vs a human-labeled set (not raw agreement), a **position-bias**
probe (`_swap_order` reorders the evidence sections — a verdict that flips is biased), **test-retest** over ≥3
runs, and the killer **high-stability + high-bias → FAIL** flag surfaced on `pulse`. Selftest proves all four on
an order-sensitive stub auditor. Wired into `scripts/selftest.sh`.
**Remaining:** assemble a real **human-labeled fixture set** (past QA evidence dirs + accept/reject labels) and
run it against the live jury auditor to get a real kappa; feed that into CI as a periodic auditor health check.

### 3. [E] Ensemble auditor for close calls — ✅ done
**Done:** `review.review()` now runs a **perspective-diverse jury** (lenses: skeptic / user-flow / evidence),
`AOS_AUDITOR_ENSEMBLE` default 3. **Unanimous accept → accept; any split → `close_call` → ESCALATE** (never
auto-accept a split), findings unioned across jurors. Both callers (`qa_run.py`, `qa_agentic.py`) treat a close
call as not-a-pass. Selftest proves the split-escalates + unanimous-accepts paths.
**Research:** 3-LLM ensemble kappa 0.432 vs 0.049 single-heuristic (2604.16706); panel juries +15%, judges
degrade ~200% on close calls (2512.16041) — motivated the design.

### 4. [E] Human-labeled hold-out the optimizer never sees — ⬜ not started
**Approach:** when we add any agent selection/tuning against auditor score, reserve a labeled hold-out set the
optimizer can't observe (reuse item 2's fixture set, partitioned). Pairs with item 2. Low urgency until we
actually optimize against the auditor.
**Research:** reward-hacking-against-a-judge mechanism (2606.04923) — motivation clear; no dig needed.

---

## Tier 1 — coordination integrity (structural multi-agent failures)

### 5. [V] Mandatory task CONTRACT in the message-bus schema — ✅ done (fail-open at `_hire`)
**Done:** enforcement point resolved to `runtime._hire` — the single choke point every child `task` event flows
through. `_task_contract(spec)` ALWAYS populates `{objective, output_format, allowed_tools, boundaries}` (filling
sensible defaults where the coordinator under-specified), rides the contract in both the task event payload and
the worker's `memory.context`, and **journals** (`TaskContractDefaulted` audit) any defaulted field so
under-specifying coordinators are visible — without ever blocking a hire (fail-open). Runtime selftest green;
logic guard in the suite.
**Optional:** surface the contract fields explicitly in `_WORKER_PROMPT` (today they ride in context, which the
prompt already prints).

### 6. [V] Share FULL traces across coordinators — 🔬 needs design
**Approach:** propagate full agent traces (not just summarized `done`/`finding`) to any coordinator/worker whose
decisions could conflict with a sibling's; force a shared upfront decision-spec for parallel work.
**Research owed:** how much trace to propagate without blowing context (ties into Tier 2 compaction) — design dig.

### 7. [V] Cost/value gate on fan-out — ✅ done (visibility gate)
**Done:** `runtime._fanout_gate` at the `_hire_or_request` choke point journals a **WideFanout** signal (width +
the ~15× token-cost note) when a single hire batch is unusually wide (`AOS_FANOUT_WARN`, default 8), so a runaway
fan-out is VISIBLE (nothing fails invisibly). Deliberately does NOT hard-cap — that would kill legitimate
breadth-first fan-out; governance + `MAX_ACTOR_STEPS` remain the hard backstops, and item-5 task boundaries
discourage coupled siblings. Runtime selftest green.
**Optional (follow-up):** a coordinator-declared `parallelizable` hint to auto-single-thread decision-coupled
work — needs coordinator-prompt cooperation; deferred (can't safely infer coupling programmatically).

### 8. [V] MAST failure-mode checklist in spawn gates + auditor — ✅ done (auditor); spawn-gate consult available
**Done:** `scripts/orchestra/mast.py` encodes the 14 MAST modes in 3 categories with a promptable `checklist()`.
The auditor now consults the **task-verification** category explicitly in its prompt (it's the backstop for
exactly those modes). Selftest + suite wiring.
**Optional:** a runtime spawn-time pre-flight consulting the **coordination** category on a hand-off (the module
already exposes `checklist(['inter-agent misalignment (coordination)'])`); deferred — the auditor is the primary
verification backstop and the AI-call-in-hot-path risk isn't worth it yet.

---

## Tier 2 — memory & context (the biggest architectural gap)

### 9. [E] Explicit agent-MEMORY layer — 🟡 first slice SHIPPED, later tiers pending
**Shipped:** `52-memory.sql` + `companymemory.py` extended — two-tier visibility (`shared`/`private`), per-role
scope, provenance (`author_actor`/`run_id`/`sources`) written into the tamper-evident audit chain, and
`memory_checkpoints` + `checkpoint()`/`plan()`/`summaries()` (item 10). **The dead experiential writer is now
ACTIVATED**: `learn_from_fix()` distills a role lesson at the QA fix-loop post-mortem (`qa_run.py`), env-gated +
best-effort. All selftested + suite-wired.
**Later tiers (deferred, per design doc §5):** `note()`/`share()` two-tier promotion UX; `memory_directory` +
`who_knows()` transactive index; semantic `recall` (pgvector). Plus the open questions in the design doc.
**Design:** [`docs/MEMORY-LAYER-DESIGN.md`](MEMORY-LAYER-DESIGN.md) (from a grounded research+audit pass). Key
finding: agent-os already has the substrate + 2 of 3 memory functions — `companymemory.py` (factual + experiential
tables) and `orchestra_actors.memory` (working). It's an **extend, not greenfield**. The gap: provenance,
per-role/two-tier access control, an experiential writer that's built but **never called in prod**, and
plan/summary checkpoints. Build order: (1) `52-memory.sql` migration capturing current tables + additive columns
w/ safe defaults; (2) provenance + `audit.append` on every write; (3) **activate the dead experiential writer**
(`distill_lesson`/`add_lesson` at the fix-loop post-mortem — highest ROI); (4) item-10 checkpoints.
**Research owed:** open questions in the design doc (global-vs-tenant lessons; whether coordinators actually
truncate today → gates item 11; MemAct RL curation deferred to item 17; retention/GDPR).

### 10. [V] External-memory checkpoint of coordinator PLAN + per-phase SUMMARY — ✅ done
**Done:** storage + API shipped with item 9; now WIRED into `loopcontroller` — a **PLAN checkpoint** is written
the moment the plan is set (line ~709), and a **phase_summary checkpoint** at every `_to()` phase transition, so
the plan + progress survive a context truncation. Best-effort (never breaks a transition); suite wiring guard +
selftest green.
**Optional:** have the controller decide-prompt READ `plan()` back each turn instead of re-deriving (small follow-up).

### 11. [E] Context COMPACTION against "context rot" — ⬜ not started
**Approach:** when a coordinator's context nears the limit, summarize completed phases out of the live window and
reinitiate; keep each `decide` prompt scoped to the step. Uses item 10's summaries as the compaction unit.
**Research:** compaction + note-taking + just-in-time retrieval all from Anthropic context-engineering — enough.

---

## Tier 3 — observability & durability hardening

### 12. [E] Adopt OpenTelemetry GenAI span conventions — ✅ done (mapper) → exporter wiring is the follow-up
**Done:** `scripts/otel.py` — a pure mapper from our work kinds → OTel GenAI operations
(`create_agent`/`invoke_agent`/`invoke_workflow`), the `gen_ai.*` attribute namespace (operation.name / agent.name
/ provider.name / request.model / usage.*), and CLIENT vs INTERNAL span kind. `pulse_to_span(row)` +
`active_spans()` turn the live pulse plane into portable spans; `python scripts/otel.py active` dumps in-flight
work as OTel spans. Selftest + suite wiring.
**Remaining (follow-up):** an actual OTel EXPORTER/bridge that ships `active_spans()` to a collector (Tempo/Jaeger),
and stamping `model`/token usage into pulse `meta` so the spans carry them. The mapping (the hard/portable part) is done.

### 13. [E] Provable side-effects (durable EFFECT records) — 🟡 core shipped
**Done:** `tools.effect_record(action, resource, content=…)` writes a tamper-evident EFFECT into the audit chain
with a **sha256 content hash** + an **idempotency key**, so the org can *prove* what actually happened and a
retried step is recognisable. Wired into `produce_artifact` (file writes) and `connector_ingest` (egress); the
effect rides back on the tool result. Selftest binds the hash to the real bytes.
**Remaining:** wire git-commit effects (the build/dev path) + actually USE the idempotency key to short-circuit a
proven-duplicate side-effect (today it's recorded, not yet enforced). Extends the durable-execution model.
**Research:** durable-execution guidance (Temporal/Inngest/Zylos; claims 27/40/103) — enough to build.

---

## Tier 4 — keep / don't-regress / evaluate

### 14. [V] Keep control-flow deterministic in code — ✅ already true (guard against regress)
The coordinator/tool-worker structure + durable bus + dispatch-and-park are deterministic code control-flow, and
QA-loop termination/gating live in code. **Action = a wiring guard** so this can't silently regress. Low effort.

### 15. [E] Sign messages IF we expose cross-org/external comms — ⬜ deferred
Not needed on the single-box internal bus today. If we ever expose agent-to-external comms, sign messages (don't
inherit A2A's unsigned-SSE MITM weakness, claim 51/52); keep MCP as the tool layer. Deferred until there's a
cross-org surface.

### 16. [E] Evaluate a blackboard (worker-pull) variant of `need_agent` — ⬜ evaluate
Capability-advertised, worker-pull spawning for cases where the coordinator shouldn't need the full org skill-map
(claims 64/120: blackboard beat RAG + master-slave 13–57%). Optional; governed-push may still win. Evaluate, don't
commit.

---

## Follow-up research (owed digs)

### 17. Verify the Tier-2/3 leads (3-vote) — 🔬 owed
The memory/OTel/durable-effect items are single-source **[E]**. Item 9 is a big commitment — run a second,
deeper research pass to 3-vote-verify agent-memory architectures (MemAct, Collaborative Memory, LLM-MAS memory),
context-compaction specifics, and OTel-GenAI field names before building.

### 18. Mine the full claim set — ✅ done
The full **133 unique claims** are now mined from the deep-research subagent transcripts into the Appendix of
[`AGENTIC-AI-RESEARCH.md`](AGENTIC-AI-RESEARCH.md), themed by area.

---

## Session log
- **2026-07-13** — Mined all 133 claims → research-doc Appendix (item 18 ✅). Shipped Tier-0 auditor hardening:
  master-key sanitization + mid-sized-judge knob (item 1 core), perspective-diverse jury with
  disagreement→escalate (item 3 ✅); both QA callers treat a close call as not-a-pass. Built the auditor
  **validation harness** (item 2 ✅): kappa / position-bias / test-retest / confidently-biased FAIL flag, with
  selftest + suite wiring. **Item 1 completed**: auditor dossier now grounds in the real `fix-round-*.json` dev
  work (files changed + residual bugs) and cross-checks claimed fixes against residuals. **Item 9 design landed**
  ([`MEMORY-LAYER-DESIGN.md`](MEMORY-LAYER-DESIGN.md)) from a grounded research+audit pass — it's an extend of
  `companymemory.py`, not greenfield.
- **2026-07-13 (cont.)** — Shipped the item-9 first slice (memory core + provenance + two-tier visibility +
  role-scope + plan/summary checkpoints), **activated the dead experiential writer** (item 9), **wired coordinator
  PLAN + phase-summary checkpoints into loopcontroller** (item 10 ✅), and shipped the **provable EFFECT-record
  core** (item 13 🟡: sha256 hash + idempotency key on `produce_artifact`/`connector_ingest`). **Done: items
  1,2,3,10,18 ✅; 9,13 🟡.** Next candidates: item 11 compaction (CONDITIONAL — measure coordinator context first
  per design §6), Tier-1 (item 5 task contracts — needs bus-schema design; item 8 MAST checklist — low-risk),
  item 13 remainder (git-commit effects + enforce idempotency). Codex: run `bash scripts/selftest.sh` to confirm
  the new checks (auditor sanitize/jury/validation, memory spine, effect records, wiring guards) are green e2e.
