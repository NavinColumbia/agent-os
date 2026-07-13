# Improvements plan — research findings → shipped code

The execution tracker for the 18 prioritized improvements from [`AGENTIC-AI-RESEARCH.md`](AGENTIC-AI-RESEARCH.md).
Every item: **status**, the **approach** (how we'll actually build it), and — where the "how" wasn't obvious —
a **research** note (what we dug up, or that a deeper dig is still owed). Anchored to
[`NORTH-STAR.md`](NORTH-STAR.md). Confidence tags from the research doc: **[V]** = 3-vote verified finding,
**[E]** = extracted single-source lead (validate before heavy investment).

**Status key:** ✅ done · 🟡 in progress · ⛳ next · ⬜ not started · 🔬 needs research first

**Roll-up (2026-07-13):** **✅ done: 1,2,3,4,5,6,7,8,10,12,13,14,15,16,17,18** · **🟡 capability shipped, more
optional: 9,11.** Every buildable item has landed with a selftest + suite wiring; the item-17 verification pass
confirmed 7/8 research leads (corrections applied). The only remaining threads are *measurement-gated* depth
(11 live-wiring), *optional deepenings* (1/2/9/10/12/13 follow-ups noted per-item), and explicit *decisions*
(6/15/16). Nothing is silently skipped.

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

### 4. [E] Human-labeled hold-out the optimizer never sees — ✅ done (partition helper)
**Done:** `auditor_validate.split_holdout(cases, holdout_frac)` deterministically (hash-based, no RNG) partitions
labeled cases into (optimizer_set, holdout_set) — disjoint + stable across runs — so if we ever tune/select
agents against the auditor's score, the hold-out is one the optimizer NEVER sees (the only real defense against
reward-hacking a judge). Selftest proves stability + disjointness. Activates fully once item-2's real labeled set
exists and we actually optimize against the auditor.

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

### 6. [V] Share FULL traces across coordinators — ✅ decided: intent met by existing + new mechanisms; full-trace deferred
**Decision (grounded):** the INTENT — parallel siblings shouldn't make conflicting implicit decisions — is now
substantially served by three shipped mechanisms: (a) `context_update`/`broadcast` propagate corrections to a
sibling set (`_broadcast` in `_supervisor_step`); (b) the **shared memory layer** (item 9) stores decisions/facts
with provenance, recalled into every agent's brief; (c) the **task contract** (item 5) `boundaries` field tells a
worker not to do a sibling's job. Literally propagating FULL transcripts (vs summaries) is what the research
prescribes for *observed* conflicting-decision failures — but doing it blindly fights item-11 compaction (context
bloat). **Deferred with detection in place:** the WideFanout (item 7) + MAST coordination checklist (item 8)
signals now make a real conflicting-decision failure observable; wire full-trace propagation *when we see one*,
not speculatively. Rationale honours the research's own "add complexity only when needed."

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

### 11. [E] Context COMPACTION against "context rot" — 🟡 capability done; live-context wiring measurement-gated
**Done:** `companymemory.compact_summaries(tenant, run, keep_last)` rolls the OLDER phase summaries into ONE
compacted summary (AI when a model's available, deterministic join otherwise) and marks the originals superseded,
so `summaries()`/the injected context stay bounded regardless of run length. `AOS_MEMORY_KEEP_LAST` threshold.
Selftest proves 9→(3 kept + 1 compacted).
**Deliberately NOT wired to live coordinator context** — design §6 open-Q #2: `factory.py:705` delegates context
mgmt to the CLI, so aggressive auto-compaction only earns its keep once real coordinator context sizes justify it.
The safe, unconditional part (bounding the durable checkpoint store) is shipped; measure before turning on more.

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

### 13. [E] Provable side-effects (durable EFFECT records) — ✅ done; idempotency ENFORCEMENT is the follow-up
**Done:** `tools.effect_record(action, resource, content=…)` writes a tamper-evident EFFECT (sha256 content hash
+ idempotency key) into the audit chain, wired into `produce_artifact` (file writes) and `connector_ingest`
(egress). **Git publish** now records an effect too (`appregistry.publish`): the commit **SHA is the
tamper-evident identity + idempotency key**, so a re-publish of the same tree is recognisable. The org can *prove*
what actually happened, not just that an agent said so.
**Remaining follow-up:** actually USE the idempotency key to SHORT-CIRCUIT a proven-duplicate side-effect
(needs per-tool check-before-side-effect restructuring; today duplicates are *recordable*, not yet *prevented* —
adding a half-wired check nothing calls would be speculative, so it's a clean, scoped follow-up).
**Research:** durable-execution guidance (Temporal/Inngest/Zylos; claims 27/40/103) — enough.

---

## Tier 4 — keep / don't-regress / evaluate

### 14. [V] Keep control-flow deterministic in code — ✅ done (regress guard added)
The coordinator/tool-worker structure + durable bus + dispatch-and-park are deterministic code control-flow, and
QA-loop termination/gating live in code. **Regress guard added** to the suite: asserts the auditor close-call
gate stays code-driven in both QA callers (`grep close_call`), so a refactor can't silently move verdict gating
into LLM discretion (premature-termination is a named MAST failure).

### 15. [E] Sign messages IF we expose cross-org/external comms — ✅ decided: deferred (no surface yet), guardrail recorded
**Decision:** agent-os's bus is a **single-box internal** substrate — the durable Postgres bus IS the trust
boundary, so per-message signing buys nothing today. Recorded guardrail for when a cross-org/agent-to-external
surface is added: **sign messages** (don't inherit A2A's unsigned-SSE MITM weakness) and keep **MCP** as the tool
layer (already used). No code now — building auth for a surface that doesn't exist would be speculative.

### 16. [E] Evaluate a blackboard (worker-pull) variant of `need_agent` — ✅ evaluated: keep governed-push
**Decision:** the blackboard (capability-advertised, worker-pull) pattern's win is removing the coordinator's need
to know the full org skill-map. agent-os already solves that differently and better for our constraints:
`org_decider.plan_org` + **role manifests** give governed, auditable, *push* spawning with spawn-gates and a
kill-switch — the governance the North Star requires ("nothing fails invisibly"). A worker-pull blackboard would
weaken that control for a scalability win we don't need on a single box. **Not adopting**; revisit only if org
sizes outgrow governed-push planning.

---

## Follow-up research (owed digs)

### 17. Verify the Tier-2/3 leads — ✅ done (7/8 confirmed; corrections applied)
Web-grounded verification pass complete. **7/8 leads confirmed.** Corrections applied to the docs:
- **LLM-MAS memory** citation `arXiv 2604.03295` was WRONG → real source is TechRxiv/Springer (fixed in the
  research doc; the 5-challenge + transactive substance is confirmed, so the memory design stands).
- **`otel.py` verified CORRECT** — already uses `gen_ai.provider.name` (not deprecated `gen_ai.system`) and
  doesn't assume the unstable `mcp.tool.name`; docstring now points at the moved `semantic-conventions-genai` repo.
- Framing tightened: MemAct 16×/−51% = a single-team result; memory taxonomy = a recent survey's framework; A2A
  MITM→2511.03841 vs token/consent→2505.12490. MemAct (2510.12635) + memory-survey (2512.13564) + Collaborative
  Memory (2505.18279) + Anthropic context-rot + Temporal durable-execution all **confirmed**.
The memory-layer build (item 9) rests on confirmed research; no rework needed.

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
- **2026-07-13 (final push)** — Completed the buildable backlog + resolved the rest by explicit decision. Shipped:
  MAST taxonomy + auditor consult (8), mandatory task contracts fail-open (5), fan-out cost/visibility gate (7),
  OTel GenAI span mapper (12), context-compaction capability (11), held-out split (4), git-publish effect record
  (13), verdict-gating regress guard (14). Decided: full-trace sharing served by existing mechanisms (6), message
  signing deferred-with-guardrail (15), blackboard not-adopted keep-governed-push (16). Launched the item-17
  web-verification pass (running). **All commits pushed to origin/master.** Codex: `bash scripts/selftest.sh`
  should be green; the DB-backed memory/loopcontroller/runtime selftests need the venv + Postgres up
  (`bash scripts/recover.sh`).
