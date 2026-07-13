# Improvements plan — research findings → shipped code

The execution tracker for the 18 prioritized improvements from [`AGENTIC-AI-RESEARCH.md`](AGENTIC-AI-RESEARCH.md).
Every item: **status**, the **approach** (how we'll actually build it), and — where the "how" wasn't obvious —
a **research** note (what we dug up, or that a deeper dig is still owed). Anchored to
[`NORTH-STAR.md`](NORTH-STAR.md). Confidence tags from the research doc: **[V]** = 3-vote verified finding,
**[E]** = extracted single-source lead (validate before heavy investment).

**Status key:** ✅ done · 🟡 in progress · ⛳ next · ⬜ not started · 🔬 needs research first

---

## Tier 0 — verdict integrity (auditor) — *"zero bugs reach a human / astonish a skeptic"*

### 1. [V] Harden `review.py` into a true Agent-as-a-Judge — 🟡 (core landed)
**Done:** master-key **sanitization** (`_sanitize` neutralizes verdict-priming openers like "Thought process:"
and symbol-only fields in agent-written evidence before the auditor reads them); a configurable mid-sized judge
model via `AOS_AUDITOR_MODEL` (honours Opus default, exposes the research-recommended mid-sized judge without a
deploy). Wired into `scripts/selftest.sh`.
**Remaining:** ground the verdict in the **actual git diff + actual test-run output** (today it reads step
records + screenshots + coverage; add the diff/test artifacts to the dossier), and let the auditor **run its own
checks** (re-open a screenshot, re-run a named assertion) rather than only reading. → next sub-step.
**Research:** master-key token list + "larger judges are MORE vulnerable, mid-sized best" from *One Token to
Fool* (2507.08794) and the Agent-as-a-Judge survey (2601.05111) — enough to build; no further dig needed.

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

### 5. [V] Mandatory task CONTRACT in the message-bus schema — 🔬 needs design
**Approach:** every `task`/`need_agent` event must carry `{objective, output_format, allowed_tools/sources,
boundaries}`; the runtime **refuses/flags** a spawn whose task lacks them. Touches `orchestra/bus.py` (schema),
`store.emit`/`spawn_actor`, and the coordinator prompts that mint tasks.
**Research owed:** confirm the least-disruptive enforcement point (validate at `emit` vs at `_hire`) and a
fail-open default so a missing field degrades to a warning, not a dead org. *Spawn a design agent before coding.*

### 6. [V] Share FULL traces across coordinators — 🔬 needs design
**Approach:** propagate full agent traces (not just summarized `done`/`finding`) to any coordinator/worker whose
decisions could conflict with a sibling's; force a shared upfront decision-spec for parallel work.
**Research owed:** how much trace to propagate without blowing context (ties into Tier 2 compaction) — design dig.

### 7. [V] Cost/value gate on fan-out — ⬜ not started
**Approach:** in the role-manifest governance layer, gate a spawn on breadth-first-parallelizable vs
decision-coupled; single-thread the latter. Fan-out ≈ 15× tokens, so the gate is also a cost control.
**Research:** motivation solid (Anthropic 90.2%/15×); heuristic for "is this task decomposable?" needs a small dig.

### 8. [V] MAST failure-mode checklist in spawn gates + auditor — ⬜ not started
**Approach:** encode the 14 MAST modes (3 categories: system-design / inter-agent-misalignment /
task-verification) as a checklist the spawn gate + auditor consult. Mostly prompt/rule work.
**Research:** the 14-mode taxonomy is in MAST (2503.13657); enough to build.

---

## Tier 2 — memory & context (the biggest architectural gap)

### 9. [E] Explicit agent-MEMORY layer — 🔬 RESEARCH FIRST (biggest new build)
**Approach (draft):** an org-level + role-level memory store, distinct from the event bus and from RAG,
permissioned per tenant, with cross-run learning. Note: a `scripts/companymemory.py` "memory spine" already
exists (in the selftest) — **first task is to audit what it already does** vs the gap.
**Research owed — YES, deep:** the mined claims point at concrete designs — three realizations (token/parametric/
latent) and factual/experiential/working taxonomy (2512.13564); two-tier private/shared + provenance metadata
(Collaborative Memory 2505.18279); transactive "who knows what" index + 5 challenge classes (LLM-MAS 2604.03295);
MemAct learnable delete/insert (2510.12635). *A focused research run + a design doc precede any code.*

### 10. [V] External-memory checkpoint of coordinator PLAN + per-phase SUMMARY — ⬜ not started
**Approach:** persist each coordinator's plan + phase summaries as distinct rows (not buried in event history) so
a context truncation can't lose the plan. Small, well-scoped; can land before the full item-9 layer.
**Research:** Anthropic multi-agent — enough to build.

### 11. [E] Context COMPACTION against "context rot" — ⬜ not started
**Approach:** when a coordinator's context nears the limit, summarize completed phases out of the live window and
reinitiate; keep each `decide` prompt scoped to the step. Uses item 10's summaries as the compaction unit.
**Research:** compaction + note-taking + just-in-time retrieval all from Anthropic context-engineering — enough.

---

## Tier 3 — observability & durability hardening

### 12. [E] Adopt OpenTelemetry GenAI span conventions — 🔬 light research
**Approach:** map `pulse`/audit events onto OTel GenAI spans (`create_agent`/`invoke_agent`/`invoke_workflow`,
CLIENT vs INTERNAL) + core attrs (`gen_ai.*`), so traces are portable (Grafana/Jaeger) instead of bespoke.
**Research owed:** exact v1.41 attribute names + the three content-recording modes (mined claims 74/128/129 give
the shape; confirm current field names before wiring). *Light dig, then map the schema.*

### 13. [E] Provable side-effects (durable EFFECT records) — ⬜ not started
**Approach:** every external action (`produce_artifact` write, `connector_ingest` egress, git commit) writes a
tamper-evident effect record (with artifact hash), so the org can **prove** what happened. Idempotency keys so a
retried step doesn't double-fire. Extends the existing `audit.py` tamper-evident chain.
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
  selftest + suite wiring. Launched a background design-research agent for item 9 (memory layer) to audit the
  existing `companymemory.py` spine + turn the mined memory claims into a concrete design before any code.
  Next: item 1's diff/test-grounding sub-step; land item-9 design → smallest useful memory slice; then items
  10/11 (PLAN/SUMMARY checkpoint + compaction).
