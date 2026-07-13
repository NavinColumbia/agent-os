# Agentic-AI SOTA research → agent-os improvements (2024–2026)

A deep, multi-source, **adversarially-verified** scan of how the field builds agent systems, mapped onto
agent-os. Method: 6 search angles → 26 sources fetched → 128 claims → 25 verified by 3-vote adversarial
refutation (24 confirmed, 1 killed). Every finding is tagged **ADOPT / ADAPT / ALREADY-DO / AVOID** for us.
Anchored to [`NORTH-STAR.md`](NORTH-STAR.md). *(Generated from a deep-research run; sources listed at end.)*

**Two confidence tiers below:** the **9 verified findings** (3-vote adversarially confirmed) first, then **9
additional signals [A1–A9]** — the headline claim of each fetched source that the synthesis dropped from the
top-9 (single-source, *not* re-verified — treat as leads). The prioritized list at the end merges both, tagging
each item **[V]** verified or **[E]** extracted-lead.

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

## Additional signals (extracted headline claims — lower confidence)
These are the **headline claim of each fetched source** that the synthesis stage did *not* promote into the
top-9 above. They are **single-source, extracted-but-not-3-vote-verified** (the full 128-claim set lives in the
subagent transcripts, not the result JSON) — treat as **leads**, not settled facts. Still mapped to agent-os.

### A1. Ensemble judging beats any single judge — and string-match is worthless — ADOPT
Substring/keyword-match evaluation of tool-using-agent output agrees with humans at **kappa=0.049 (chance)**; a
**three-LLM ensemble judge reaches kappa=0.432** (moderate). Even top judges (Gemini-2.5-Pro, GPT-5) fail to
keep consistent preferences on **~1/4 of hard cases**.
→ **agent-os:** never gate a verdict on heuristic string/regex matching of agent output. For close calls, make
the auditor a **small ensemble (odd N, distinct models/prompts) with a disagreement→escalate rule**, not one
pass. Reinforces findings 7–9. (Src: fetched agent-eval papers; unverified exact kappas)

### A2. Rubric-based RL with an LLM judge → reward hacking — AVOID/ADAPT
When an LLM-as-a-Judge supplies the reward, the policy learns to **exploit the judge's latent biases (verbosity,
sycophancy, self-praise, surface form)** rather than improve real quality; the hacking is subtle and only
visible after training derails.
→ **agent-os:** if agents are ever tuned/selected against the auditor's score (even implicitly, e.g. "keep the
worker whose runs the auditor liked"), that loop **will** be gamed. Keep the auditor grounded in artifacts
(finding 8) and **hold out a human-labeled validation set** the optimizer never sees. (Src: fetched RL-reward paper; unverified)

### A3. "Context rot": recall degrades as the window fills — ADOPT
Context is a **finite** resource: as token count grows, recall of any single fact **drops** (transformer n²
pairwise attention stretched thin). More context ≠ better.
→ **agent-os:** don't let a long-lived coordinator accrete an ever-growing context. **Compact** (summarize
completed phases to external memory, reinitiate a fresh window) and keep each `decide` prompt scoped to what the
step needs. Complements finding 6's PLAN/SUMMARY checkpointing. (Src: Anthropic context-engineering; directional)

### A4. Treat agent MEMORY as a first-class primitive (≠ RAG/context) — ADAPT
Multiple sources: agent memory is a **distinct architectural primitive**, not "RAG + context stuffing." One line
(**MemAct**) treats working-memory management as **learnable delete/insert policy actions** trained with RL;
another flags **knowledge transfer across users under dynamic, asymmetric permissions** as an open gap.
→ **agent-os:** we have durable *event/state* memory (Postgres rows) but **no semantic agent-memory layer** —
no cross-run "what this org/role learned," no compaction policy, no per-tenant knowledge store with access
control. **Design an explicit memory module** (org-level + role-level, permissioned per tenant) rather than
overloading the event bus. This is the biggest *architectural* gap surfaced. (Src: fetched memory survey + MemAct; unverified)

### A5. Multi-agent memory adds 5 NEW hard problems — ADAPT (design constraint)
LLM-MAS memory is its own frontier vs single-agent, introducing **synchronization, access control, scalability,
alignment, and safety** as distinct challenge classes.
→ **agent-os:** whatever memory layer we build (A4) must treat these as first-class: **sync** (single-writer
actor rows — we do this; extend to memory writes), **access control** (per-tenant/per-role read scopes),
**scalability** (compaction), **alignment/safety** (don't let one agent poison shared memory). (Src: fetched LLM-MAS memory survey; unverified)

### A6. Blackboard paradigm: subagents VOLUNTEER by capability — CONSIDER
A **blackboard** pattern — a central agent posts a request to a shared board and autonomous subagents *volunteer*
based on their own capabilities — removes the need for the coordinator to know every subagent's expertise up
front, improving scalability.
→ **agent-os:** our `need_agent`/spawn model is **coordinator-push** (it decides who to hire). A blackboard
(capability-advertised, worker-pull) variant could help when the CEO-coordinator *shouldn't* need the full org's
skill map. **Evaluate as an option** for the `need_agent` path; not obviously better than governed push given our
role-manifest gates. (Src: fetched blackboard-MAS paper; unverified)

### A7. Agent-interoperability protocols are layered — and A2A has a security hole — ADAPT/AVOID
A survey frames four layers: **MCP** (LLM↔tool), **ACP** (infra-level messaging), **A2A** (enterprise intra-org
delegation), **ANP** (open-internet agent marketplaces). Google's **A2A lacks per-message signing** and is
**susceptible to tampering/MITM over its SSE channels** (latency prioritized over security).
→ **agent-os:** our bus is a **single-box internal** substrate — we don't need A2A/ANP now, and adopting A2A
naively would **inherit its unsigned-message weakness**. If we ever expose cross-org/agent-to-external comms,
**sign messages** and keep the durable bus as the trust boundary. For tools, **MCP is the right layer** (we
already speak it). (Src: fetched protocol survey; unverified)

### A8. OpenTelemetry GenAI conventions exist for agent tracing — ADOPT
OTel's **GenAI semantic conventions** standardize agent operations — `create_agent`, `invoke_agent`,
`invoke_workflow` — and (v1.41) split **CLIENT spans** (remote agent calls) from **INTERNAL spans** (local
framework execution), turning agent reasoning into structured, queryable traces.
→ **agent-os:** our `pulse` plane is a bespoke heartbeat table. **Adopt the OTel GenAI span names/attributes** as
the schema for pulse/audit events so traces are portable and tool-compatible (Grafana/Jaeger/etc.) instead of
proprietary. Low-cost, high-leverage for "nothing fails invisibly." Directly fills the finding-list's
observability follow-up. (Src: OpenTelemetry GenAI conventions; solid but not adversarially verified)

### A9. Durable execution = separate deterministic WORKFLOW from non-deterministic ACTIVITIES — ALREADY-DO/ADAPT
Temporal-style guidance: build durable agents by keeping the **orchestration loop deterministic** while the
**LLM/tool calls run as non-deterministic "activities"**; persist each step's result and **replay from the last
checkpoint** on restart. Critically: **session/chat memory ≠ durable execution** — durability means being able to
**prove which side effects actually occurred** (commands, emails, approvals), not just recall the conversation.
→ **agent-os: ALREADY-DO** in shape — our deterministic runtime loop + dispatch-and-park (LLM/tool work as async
"activities") + persisted rows + lease-reclaim replay *is* this pattern. **ADAPT the sharp bit:** make side
effects **provable** — every external action (a `produce_artifact` write, a `connector_ingest` egress, a git
commit) records a durable, tamper-evident **effect record**, so the org can answer "did this actually happen?"
not just "an agent said it did." Ties into the deterministic-auditor goal. (Src: Temporal/Inngest write-ups; directional)

## PRIORITIZED agent-os improvements (what to actually build)
Tiered and granular. Each item tags the confidence of its evidence: **[V]** = 3-vote verified finding, **[E]** =
extracted single-source lead (validate before heavy investment).

**Tier 0 — verdict integrity (do first; directly serves "zero bugs reach a human / astonish a skeptic")**
1. **[V] Harden `review.py` into a true Agent-as-a-Judge.** Ground the QA verdict in the **real git diff + real
   test output + real browser observations** and let the auditor **run its own checks**; **strip verdict-eliciting
   "master-key" tokens** from evidence before judging; use a **mid-sized** judge model, not the largest. (Findings 7, 8)
2. **[V] Add an auditor VALIDATION harness.** Cohen's kappa vs human-labeled QA runs, position-bias probe (swap
   evidence order → must not flip), test-retest ≥3 runs, **high-stability+high-bias → FAIL** flag surfaced in
   `pulse`. A confident auditor is suspect until validated. (Finding 9)
3. **[E] Make the auditor a small ENSEMBLE for close calls**, not a single pass; disagreement → escalate. Never
   gate a verdict on heuristic string/regex matching (kappa≈chance). (Signal A1)
4. **[E] Keep a human-labeled hold-out the optimizer never sees**, so agent selection/tuning can't reward-hack
   the auditor's score. (Signal A2)

**Tier 1 — coordination integrity (the structural multi-agent failures)**
5. **[V] Make the task CONTRACT mandatory in the message-bus schema.** Every `task`/`need_agent` carries
   {objective, output_format, allowed_tools/sources, boundaries}. Caps runaway spawning; kills dup/gap failures. (Finding 4)
6. **[V] Share FULL traces across coordinators** (not just summarized `done`/`finding`) wherever sibling decisions
   could conflict; force a shared upfront decision-spec for parallel work. (Finding 2)
7. **[V] Cost/value gate on fan-out** in governance — spawn only for breadth-first parallelizable work;
   single-thread decision-coupled work (fan-out ≈ 15× tokens). (Finding 1)
8. **[V] MAST failure-mode checklist** baked into spawn gates + the auditor (system-design / inter-agent
   misalignment / task-verification/termination). (Finding 2)

**Tier 2 — memory & context (the biggest architectural gap)**
9. **[E] Design an explicit agent-MEMORY layer** (org-level + role-level), distinct from the event bus and from
   RAG — permissioned per tenant, with cross-run learning. Treat sync/access-control/scalability/alignment/safety
   as first-class. This is the largest new-architecture item surfaced. (Signals A4, A5)
10. **[V] External-memory checkpoint of each coordinator's PLAN + per-phase SUMMARY** (distinct from event rows)
    so context truncation can't lose the plan. (Finding 3)
11. **[E] Context COMPACTION against "context rot"** — summarize completed phases out of the live window,
    reinitiate fresh; keep each `decide` prompt scoped to the step. (Signal A3)

**Tier 3 — observability & durability hardening**
12. **[E] Adopt OpenTelemetry GenAI span conventions** (`create_agent`/`invoke_agent`/`invoke_workflow`, CLIENT
    vs INTERNAL spans) as the schema for `pulse`/audit, so traces are portable + tool-compatible instead of
    bespoke. (Signal A8)
13. **[E] Provable side-effects (durable EFFECT records).** Every external action (`produce_artifact` write,
    `connector_ingest` egress, git commit) writes a tamper-evident effect record, so the org can *prove* what
    happened, not just report it. (Signal A9)

**Tier 4 — keep / don't-regress / evaluate**
14. **[V] Keep control-flow deterministic in code** and QA-loop termination/gating in code — already true; don't
    regress (premature termination is a named MAST failure). (Findings 5, 6)
15. **[E] If we ever expose cross-org/external comms, SIGN messages** — don't inherit A2A's unsigned-SSE MITM
    weakness. Keep MCP as the tool layer (already used); the durable bus stays the trust boundary. (Signal A7)
16. **[E] Evaluate a blackboard (capability-advertised, worker-pull) variant** of the `need_agent` path for cases
    where the coordinator shouldn't need the full org skill-map. Optional; governed-push may still win. (Signal A6)

**Follow-up research**
17. **[follow-up]** A second, deeper run to 3-vote-VERIFY the Tier-2/3 leads (agent-memory architectures, MemAct,
    context-compaction specifics, OTel-GenAI, MCP/A2A) — they're currently single-source [E], and the memory
    layer (item 9) is a big enough commitment to warrant confirmation before building.
18. **[follow-up]** Mine the full **128-claim** set from the subagent transcripts (only ~26 headline claims + the
    9 verified findings reached the result JSON) if we want exhaustive coverage rather than the headline-per-source
    sample captured above.

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
