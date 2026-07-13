# Agentic-AI SOTA research → agent-os improvements (2024–2026)

A deep, multi-source, **adversarially-verified** scan of how the field builds agent systems, mapped onto
agent-os. Method: 6 search angles → 26 sources fetched → 128 claims → 25 verified by 3-vote adversarial
refutation (24 confirmed, 1 killed). Every finding is tagged **ADOPT / ADAPT / ALREADY-DO / AVOID** for us.
Anchored to [`NORTH-STAR.md`](NORTH-STAR.md). *(Generated from a deep-research run; sources listed at end.)*

## The one-paragraph takeaway
The field's biggest unresolved tension is **Anthropic** (orchestrator-worker multi-agent beat single-agent by
90.2% on breadth-first research) vs **Cognition/Devin** ("Don't Build Multi-Agents"; default to single-threaded
continuous-context). Both are right *within scope*: parallel subagents win for **read-heavy, decomposable,
independent-direction** work but fail when tasks need **shared decisions** (isolated context → conflicting
implicit choices that compound). Academic consensus (**MAST**, 1600+ traces) says multi-agent failures are
**structural**, not prompt-fixable. **The strongest signal for us is in evaluation:** a single-pass LLM
"auditor" is *fundamentally unsafe* as a verdict authority — trivially gamed, and its self-consistency is not
correctness. **Good news:** agent-os's durable-resume, dispatch-and-park, orchestrator-worker, and
evaluator-optimizer QA loop are all **validated as ALREADY-DO**. **The highest-value work** is hardening the QA
auditor into a true *Agent-as-a-Judge* grounded in real artifacts, validating it, making task contracts
mandatory, and sharing full traces across coordinators.

## Verified findings (each mapped to agent-os)

### 1. Fan-out is a ~15× cost decision, not a default — ADAPT
Anthropic: multi-agent (Opus 4 lead + Sonnet 4 subagents) beat single-agent Opus 4 by **90.2%** on their
research eval, but only "for breadth-first queries pursuing multiple independent directions." Multi-agent uses
**~15× the tokens** of chat; token usage alone explains **80% of variance** on BrowseComp.
→ **agent-os:** the CEO-coordinator should fan out only for **breadth-first, independently parallelizable** work
(multi-source research, coverage-driven QA exploration) and keep **decision-coupled/depth-first** work
single-threaded. **Add a cost/value gate to spawn decisions in the role-manifest governance layer.** (Src: Anthropic multi-agent)

### 2. Context isolation is the core danger — share FULL traces — ADOPT
Cognition ("Don't Build Multi-Agents"): default to a single-threaded linear agent; **"share context, and share
full agent traces, not just individual messages"**; "actions carry implicit decisions, and conflicting decisions
carry bad results" (parallel subagents built a game with mismatched art styles). MAST: 14 failure modes in 3
categories — system design, inter-agent misalignment, task verification/termination — needing **structural**
redesign; prompt/topology tweaks gave only single-digit gains.
→ **agent-os:** on the **durable message bus**, propagate **full agent traces** (not just summarized `done`/
`finding`) to any coordinator/worker whose decisions could conflict with a sibling's; where work must stay
parallel, force a **shared upfront spec of the decisions** that would otherwise be implicit. Use the 3 MAST
categories as a **failure-mode checklist** for spawn gates + the auditor. **AVOID** prompt-only fixes for
coordination bugs. (Src: Cognition; MAST arXiv 2503.13657)

### 3. Durable resume + external-memory checkpointing — ALREADY-DO (+ one gap to ADOPT)
Anthropic: "systems that can resume from where the agent was when the errors occurred"; "agents summarize
completed work phases and store essential information in **external memory** before proceeding"; the lead saves
its **plan to memory** because a >200k-token context truncates; "minor system failures can be catastrophic."
→ **agent-os: ALREADY-DO** — crash-resume via persisted Postgres rows + lease reclaim, dispatch-and-park, and
heartbeats *are* these mitigations. **ADOPT the missing piece:** explicit external-memory checkpointing of each
coordinator's **PLAN** and **per-phase SUMMARY** (distinct from event rows) so context truncation can't lose the
plan. (Src: Anthropic multi-agent + Building Effective Agents)

### 4. Every spawned task needs an explicit CONTRACT — ADOPT
Anthropic: "Each subagent needs an **objective, an output format, guidance on the tools and sources to use, and
clear task boundaries**." Without it, agents "duplicate work, leave gaps, or fail to find information"; early
versions spawned **50 subagents for a simple query**.
→ **agent-os:** make the task-issuing contract **structurally mandatory in the message-bus schema** — every
`task`/`need_agent` carries objective, expected output format, allowed tools/sources, and boundaries. This also
caps runaway spawning (complements spawn gates + kill-switch). (Src: Anthropic multi-agent)

### 5. Deterministic CODE orchestration vs LLM-driven — keep control-flow in code — ALREADY-DO/ADAPT
Anthropic distinguishes **workflows** (LLMs orchestrated through predefined code paths — predictable) from
**agents** (LLMs direct their own process — flexible); "find the simplest solution, add complexity only when
needed." OpenAI Agents SDK: "orchestrating via code makes tasks more deterministic and predictable in speed,
cost and performance."
→ **agent-os: ALREADY-DO** — the coordinator/tool-worker structure *is* orchestrator-workers, and the durable
bus + leasing + dispatch-and-park + heartbeats are deterministic **code** control-flow. **Keep it that way**;
reserve LLM-driven decisions for genuinely open-ended steps (minimizes cost/latency variance + compounding
errors). (Src: Anthropic; OpenAI Agents SDK)

### 6. Evaluator-optimizer (critic) loop in code — ALREADY-DO
OpenAI/Anthropic both name the pattern: a task agent in a **while-loop** with a separate **evaluator** until
criteria pass — implemented in code, not LLM discretion.
→ **agent-os: ALREADY-DO** — the QA loop (explore → blocking-bug hand-off → fix on real git diff → re-test →
verdict) *is* a code-orchestrated evaluator-optimizer. **Reinforce that termination + pass/fail gating live in
CODE** (the runtime), never LLM discretion — premature termination is a named MAST failure. (Src: OpenAI; Anthropic)

### 7. A single-pass LLM auditor is fundamentally UNSAFE as a verdict authority — AVOID
"One Token to Fool LLM-as-a-Judge" (NeurIPS 2025): trivial **"master keys"** — symbols (`:` `.`) or openers
("Thought process:", "Let's solve step by step") — elicit **false-positive "correct" verdicts, FPR up to 80%**,
with *no* real reasoning. Crucially: **chain-of-thought and majority voting DON'T defend and can make it WORSE;
larger judges are MORE vulnerable**; mid-sized models best balance robustness. Single-pass judges are "passive
observers... assessing linguistic patterns without verification → hallucinated evaluations."
→ **agent-os: AVOID** letting a single-LLM auditor trust its own narrative; **AVOID** CoT/majority-voting as the
primary hardening. **ADAPT:** strip verdict-eliciting tokens from evidence before judging; prefer a **mid-sized**
model for the judge; judge **artifacts, not prose** (finding 8). (Src: arXiv 2507.08794; survey 2601.05111)

### 8. Agent-as-a-Judge grounded in REAL execution artifacts — ADOPT (HIGHEST PRIORITY)
The prescribed fix: judges that **inspect execution artifacts and run their own automated checks**, not review
narratives. The foundational Agent-as-a-Judge paper: it "dramatically outperforms LLM-as-a-Judge and is as
reliable as human evaluation."
→ **agent-os:** our `review.py` auditor already reads the run's **evidence** (screenshots + per-step
reasoning/action/**actual**/verdict) — that is *moving toward* Agent-as-a-Judge and is the right instinct.
**Push it all the way:** ground the verdict in the **actual git diff, actual test-run output, actual
headless-browser observations**, and let the auditor **run its own checks** — never the worker's written
narrative. This directly serves "zero bugs reach a human / astonish a skeptic." (Src: survey 2601.05111; arXiv 2410.10934)

### 9. High agreement ≠ validity; self-consistency ≠ correctness — validate the judge — ADOPT
"Reliability without Validity" (21 judges, ~541k judgments): 85% exact-match ≈ Cohen's **kappa ~0.48**
(moderate), with **kappa deflation of 34–41pp** across *all* models. Two production judges had test-retest ≥0.95
*while* showing severe position bias — "test-retest measures output stability, not decision-process
correctness." Prescribes a **Minimum Viable Validation Protocol:** report **kappa** (not exact match), test
**position bias** via AB+BA order swaps, verify **test-retest across ≥3 runs**, cross-validate on ≥2 benchmarks,
and **flag high-stability/high-bias judges as failures**.
→ **agent-os:** before trusting the auditor, **validate it** — kappa vs human-labeled QA runs, position-bias
probe (swap evidence order), test-retest across repeated runs, and surface a **"high-stability-but-high-bias"
flag in the pulse plane**. Treat a confident, self-consistent auditor as *suspect until validated*. *(Caveat:
a 2026 preprint — treat exact figures as provisional.)* (Src: arXiv 2606.19544)

## Caveats & one refuted claim
- **Refuted (0-3):** specific per-model FPRs ("Thought process:" → 35% FPR in GPT-4o; 60–90% in LLaMA3/Qwen2.5)
  could **not** be verified — the general master-key vulnerability holds, but don't cite those exact numbers.
- Several evaluation-hardening sources are **very recent 2026 arXiv preprints** (survey 2601.05111; "Reliability
  without Validity" 2606.19544) — internally consistent + corroborated, but treat exact stats as provisional.
  MAST (2503.13657) and "One Token to Fool" (2507.08794) are stronger (NeurIPS 2025 / widely reproduced).
- Anthropic's 90.2% is an **internal, unpublished** eval and omits the ~15× cost caveat in headline form.
- The Anthropic-vs-Cognition disagreement is **real and unresolved** — agent-os must decide *per task* which
  regime applies, not adopt one universally.
- Thinner verified coverage on memory-compaction specifics, OpenTelemetry-GenAI conventions, and MCP/A2A
  tradeoffs (in scope but fewer surviving claims) — worth a follow-up research run.

## PRIORITIZED agent-os improvements (what to actually build)
1. **[HIGHEST] Harden `review.py` into a true Agent-as-a-Judge.** Ground the QA verdict in the **real git diff +
   real test output + real browser observations** and let the auditor **run its own checks**; **strip
   verdict-eliciting "master-key" tokens** from evidence before judging; use a **mid-sized** judge model, not
   the largest. (Findings 7, 8)
2. **[HIGH] Add an auditor VALIDATION harness.** Cohen's kappa vs human-labeled QA runs, position-bias probe
   (swap evidence order → must not flip), test-retest ≥3 runs, and a **high-stability+high-bias → FAIL** flag
   surfaced in `pulse`. A confident auditor is suspect until validated. (Finding 9)
3. **[HIGH] Make the task CONTRACT mandatory in the message-bus schema.** Every `task`/`need_agent` must carry
   {objective, output_format, allowed_tools/sources, boundaries}. Caps runaway spawning; kills dup/gap failures.
   (Finding 4)
4. **[HIGH] Share FULL traces across coordinators** (not just summarized `done`/`finding`) wherever sibling
   decisions could conflict; force a shared upfront decision-spec for parallel work. (Finding 2)
5. **[MED] Cost/value gate on fan-out** in governance — spawn coordinators/tool-workers only for breadth-first
   parallelizable work; single-thread decision-coupled work. (Finding 1)
6. **[MED] External-memory checkpoint of each coordinator's PLAN + per-phase SUMMARY** (distinct from event
   rows) so context truncation can't lose the plan; add **context compaction** (summarize near-full context,
   reinitiate a fresh window) against "context rot". (Findings 3, + Anthropic context-engineering)
7. **[MED] MAST failure-mode checklist** baked into spawn gates + the auditor (system-design / inter-agent
   misalignment / task-verification). (Finding 2)
8. **[LOW/keep] Keep control-flow deterministic in code** and keep QA loop termination/gating in code — already
   true; don't regress. (Findings 5, 6)
9. **[follow-up] A second research run** on memory-compaction, OpenTelemetry-GenAI tracing, and MCP/A2A adoption
   (thin verified coverage this pass).

## Sources (primary first)
- Anthropic — *How we built our multi-agent research system* · *Building Effective Agents* · *Effective Context Engineering for AI Agents*
- Cognition — *Don't Build Multi-Agents* (Walden Yan)
- OpenAI — *Agents SDK: multi-agent orchestration*
- MAST — *Why Do Multi-Agent LLM Systems Fail?* (arXiv 2503.13657, NeurIPS 2025)
- *One Token to Fool LLM-as-a-Judge* (arXiv 2507.08794)
- *A Survey on Agent-as-a-Judge* (arXiv 2601.05111) · *Agent-as-a-Judge* foundational (arXiv 2410.10934)
- *Reliability without Validity* (arXiv 2606.19544)
- Durable execution: Temporal, Inngest, and practitioner write-ups (durable-execution for LLM agents)
- Full source list + per-claim verification votes: the deep-research run output (`tasks/w1esxgseq.output`).
