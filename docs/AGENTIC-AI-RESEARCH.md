# Agentic-AI SOTA research → agent-os improvements (2024–2026)

A deep, multi-source, **adversarially-verified** scan of how the field builds agent systems, mapped onto
agent-os. Method: 6 search angles → 26 sources fetched → 128 claims → 25 verified by 3-vote adversarial
refutation (24 confirmed, 1 killed). Every finding is tagged **ADOPT / ADAPT / ALREADY-DO / AVOID** for us.
Anchored to [`NORTH-STAR.md`](NORTH-STAR.md). *(Generated from a deep-research run; sources listed at end.)*

**Three confidence tiers below:** the **9 verified findings** (3-vote adversarially confirmed) first, then **9
additional signals [A1–A9]** — the headline claim of each fetched source that the synthesis dropped from the
top-9 (single-source, *not* re-verified — treat as leads). The prioritized list merges both, tagging each item
**[V]** verified or **[E]** extracted-lead. Finally, the **Appendix** lists **all 133 unique claims** mined from
the deep-research subagent transcripts, themed by area — the complete raw set the synthesis drew from (use as a
lead index; single-source, unverified).

> **Verification pass (item 17, 2026-07-13).** A second, web-grounded pass re-checked the single-source **[E]**
> leads against primary sources. **7/8 confirmed**; corrections to note:
> • **LLM-MAS memory** citation `arXiv 2604.03295` is **WRONG** (different paper) — real source is on TechRxiv/
>   Springer (see the Appendix note); the 5-challenge + transactive-memory substance is confirmed.
> • **OTel field names**: our `scripts/otel.py` was **verified correct** — it uses `gen_ai.provider.name` (the
>   current attr, not the deprecated `gen_ai.system`) and does not assume the unstable `mcp.tool.name`. Only
>   confirmed MCP attrs are `mcp.method.name/session.id/protocol.version/resource.uri`; GenAI semconv has moved to
>   the `semantic-conventions-genai` repo.
> • **MemAct** (2510.12635, id correct) numbers "matches 16× larger, −51% context" verified — cite as the paper's
>   single-team result. **Memory forms×functions taxonomy** (2512.13564, id correct) — cite as a recent survey's
>   framework, not settled consensus. **A2A**: MITM/per-message-signing → arXiv **2511.03841**; token/consent gaps
>   → arXiv **2505.12490** (two different papers). Anthropic context-rot/compaction + Temporal durable-execution
>   claims confirmed accurate.

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

## Appendix — the full extracted claim set (all 133, themed)

Mined from the 109 deep-research subagent transcripts (146 raw claim occurrences → **133 unique** after dedup). These are the raw fetch-stage extractions the synthesis drew from — **single-source, not 3-vote verified**; `[central]`/`[supporting]`/`[tangential]` is the fetch agent's own importance tag. Claims that became verified findings 1–9 or signals A1–A9 above are marked ✔. Use as a lead index, not settled fact.

### A. Context engineering & memory

**Agent-memory survey 2512.13564**
- _[cent]_ Agent memory should be treated as a first-class architectural primitive rather than an afterthought bolted onto retrieval or context stuffing; the paper explicitly distinguishes agent memory from LLM memory, RAG, and context engineering.
- _[cent]_ Agent memory has three dominant physical realizations — token-level (in-context/text), parametric (weights), and latent memory — implying that a durable agent system's memory layer can be designed along these distinct storage substrates rather than a single store.
- _[cent]_ Functionally, agent memory decomposes into factual, experiential, and working memory — a taxonomy that maps directly onto separating an agent-os org's shared knowledge (factual), past-run/episodic experience (experiential), and per-task scratch state (working).
- _[supp]_ Multi-agent memory and trustworthiness are named open frontiers, indicating shared organizational memory across coordinators/workers is not yet solved and needs deliberate engineering.
- _[supp]_ The authors argue memory should be a designed primitive of agentic intelligence, supporting an ADAPT recommendation for agent-os to formalize its memory model (short-term/working vs long-term shared org memory) rather than relying only on the Postgres event bus and context windows.

**Anthropic: Effective Context Engineering**
- _[cent]_ Context is a finite resource subject to 'context rot': as the number of tokens in the context window increases, the model's ability to accurately recall information from that context decreases — driven by the transformer's n-squared pairwise attention relationships being stretched thin over longer contexts.
- _[cent]_ For long-horizon tasks that exceed the context window, compaction is a recommended technique: summarize a conversation nearing the context limit and reinitialize a new context window from the summary.
- _[cent]_ Agents should use structured note-taking — regularly writing notes persisted to external memory outside the context window — to enable persistent recall across extended tasks.
- _[cent]_ Specialized sub-agents with clean/isolated context windows can handle focused subtasks while a main agent coordinates high-level strategy, keeping each agent's context uncluttered.
- _[cent]_ An orchestrator-worker multi-agent system (Claude Opus 4 lead + Sonnet 4 subagents) outperformed a single-agent Claude Opus 4 by 90.2% on their internal research eval, but the advantage is specifically for breadth-first, parallelizable queries.
- _[cent]_ Multi-agent systems are far more token-expensive than single calls (agents ~4x chat, multi-agent ~15x), and token usage explains 80% of the variance in performance — so multi-agent is only economically justified for high-value tasks.
- _[cent]_ Orchestrators must give each subagent an explicit objective, output format, tool/source guidance, and clear task boundaries; without this, subagents duplicate work, leave gaps, or spawn excessively (e.g. 50 subagents for a simple query).
- _[cent]_ Long-running agents need durable resume-from-failure plus external memory checkpointing, because minor failures compound catastrophically and the context window (200K tokens) truncates, so the lead agent saves its plan to Memory and summarizes completed phases.
- _[supp]_ A just-in-time context retrieval strategy — where agents hold lightweight identifiers and dynamically load data into context at runtime via tools — is preferable to pre-loading/pre-processing all data upfront.
- _[supp]_ Tool sets should be kept minimal and unambiguous; bloated tool sets covering too much functionality create ambiguous decision points about which tool to use and degrade agent performance.
- _[supp]_ Full production tracing of agent decision patterns and interaction structures (without reading conversation contents) was what let them systematically diagnose and fix failures.
- _[supp]_ Synchronous orchestrator execution is a coordination bottleneck: the lead agent cannot steer subagents mid-flight and subagents cannot coordinate with each other.
- _[supp]_ Full production tracing of agent decision patterns and interaction structures (without reading conversation contents) is what let them systematically diagnose and fix failures; synchronous orchestration is a coordination bottleneck where the lead can't steer subagents mid-flight.

**Collaborative Memory 2505.18279**
- _[cent]_ Persistent memory improves single-agent LLM performance, but existing memory systems lack mechanisms for knowledge transfer across users under dynamic, asymmetric permissions — a gap for multi-user agent orgs.
- _[cent]_ Collaborative Memory uses a two-tier memory structure: private fragments visible only to their originating user, and selectively shared fragments, enabling controlled cross-user/cross-agent knowledge sharing.
- _[supp]_ Access is governed by read policies that produce filtered/transformed views enforcing current user-agent-resource constraints, and write policies that determine fragment retention and sharing, with permissions modeled as bipartite graphs linking users, agents, and resources.
- _[supp]_ Every memory fragment carries immutable provenance metadata (contributing agents, accessed resources, timestamps), enabling retrospective permission checks and full auditability of memory operations.

**LLM-MAS memory** *(⚠ verification (item 17): the extraction's arXiv id 2604.03295 was WRONG — that id is a different paper. Real source: "Memory in LLM-based Multi-agent Systems: Mechanisms, Challenges, and Collective Intelligence" — TechRxiv DOI 10.36227/techrxiv.176539617.79044553 / Springer 10.1007/978-981-92-1468-6_10, NOT on arXiv. The 5-challenge + transactive-memory substance below is CONFIRMED.)*
- _[cent]_ Memory in LLM-based multi-agent systems (LLM-MAS) is a distinct research frontier from single-agent memory, introducing five new classes of challenge: synchronization, access control, scalability, alignment, and safety.
- _[cent]_ Effective multi-agent coordination requires transactive (meta-)memory — an explicit 'who knows what' index — so agents can allocate work and avoid redundant processing, analogous to human team transactive memory systems.
- _[cent]_ LLM-MAS memory is organized into three primary topologies: per-agent private/local stores, centralized shared memory (blackboard-style), and hybrid designs combining local perceptual memory with a shared summarized world-state.
- _[supp]_ Shared memory across agents should be governed by explicit two-tier access control, separating private fragments from shared fragments under dynamic access policies rather than exposing one global store to all agents.
- _[supp]_ In multi-agent settings, memory functions as shared cognitive infrastructure that is a prerequisite for collective intelligence, long-term coordination, and team evolution over time.

**MemAct 2510.12635**
- _[cent]_ MemAct treats working-memory/context management as learnable policy actions performed via in-place editing operations (deletion and insertion), trained end-to-end with reinforcement learning, rather than via external mechanisms unaware of the agent's reasoning state.
- _[cent]_ A 14B agent trained with MemAct-RL matches the task accuracy of models 16x larger while reducing average context length by 51%.
- _[supp]_ Long-context LLMs still require active working-memory management because unmanaged context growth causes attention dilution that degrades performance on long-horizon tasks; simply having a large context window is insufficient.
- _[supp]_ External/heuristic context-management mechanisms that lack awareness of the agent's reasoning state lead to suboptimal memory decisions, motivating in-agent learned curation.
- _[supp]_ Dynamic context updates during training create computational/efficiency challenges, which the authors address with a method called Dynamic Context Policy Optimization that restores training efficiency without compromising reasoning integrity.

### B. Orchestration patterns & when to fan out

**Anthropic: Building Effective Agents**
- _[cent]_ Anthropic explicitly distinguishes 'workflows' (LLMs/tools orchestrated through predefined code paths) from 'agents' (LLMs dynamically directing their own processes and tool usage), and recommends choosing between them based on the task: workflows for predictability on well-defined tasks, agents for flexibility and model-driven decisions at scale.
- _[cent]_ Anthropic advises building with the simplest possible design and only adding complexity (including multi-agent structure) when needed, noting that for many applications a single optimized LLM call with retrieval and in-context examples suffices.
- _[cent]_ Anthropic warns that autonomous agents carry higher cost and the potential for compounding errors, and prescribes extensive testing in sandboxed environments plus guardrails as mitigation.
- _[cent]_ Anthropic names five composable building-block patterns for agentic systems — prompt chaining, routing, parallelization, orchestrator-workers, and evaluator-optimizer — where orchestrator-workers has a central LLM dynamically decompose tasks, delegate to worker LLMs, and synthesize results (directly analogous to agent-os's coordinator/tool-worker structure).
- _[cent]_ Anthropic names five composable building-block patterns for agentic systems, including orchestrator-workers, where a central LLM dynamically decomposes tasks, delegates to worker LLMs, and synthesizes results (directly analogous to agent-os's coordinator/tool-worker structure).
- _[supp]_ Anthropic recommends reducing framework abstraction layers and building with basic components when moving to production, rather than relying on agent frameworks.
- _[supp]_ Anthropic prescribes human-in-the-loop checkpoints where agents pause for human feedback at checkpoints or when encountering blockers, and stresses investing in the agent-computer interface (ACI) via thorough tool documentation and testing.

**Blackboard 2510.01285**
- _[cent]_ A blackboard multi-agent paradigm — where a central agent posts requests to a shared blackboard and autonomous subordinate agents volunteer to respond based on their own capabilities — eliminates the need for a central coordinator to have prior knowledge of every sub-agent's expertise, improving scalability and flexibility over the master-slave/orchestrator model.
- _[cent]_ Master-slave (orchestrator-worker) multi-agent systems have a concrete structural weakness: they depend on a rigid central controller for task allocation that requires precise knowledge of each sub-agent's capabilities, which does not scale to large heterogeneous problem spaces.
- _[supp]_ The blackboard architecture measurably outperforms both RAG and the master-slave multi-agent paradigm, achieving 13% to 57% relative improvement in end-to-end task success and up to 9% relative F1 gain on data discovery, across both proprietary and open-source LLMs on three benchmarks (KramaBench, modified DS-Bench, modified DA-Code).
- _[supp]_ Single-agent systems are quickly overwhelmed when they must operate over large, heterogeneous inputs, motivating a multi-agent decomposition — a counterpoint to the common 'single agent + tools often beats multi-agent' claim in high-heterogeneity domains.
- _[tang]_ The authors position the blackboard paradigm as a general-purpose, scalable communication framework for multi-agent systems, not merely a data-discovery-specific technique.

**Cognition: Don't Build Multi-Agents**
- _[cent]_ Cognition (Devin's maker) advises against multi-agent architectures in favor of single-threaded, continuous-context agent design as the default for reliable systems.
- _[cent]_ The primary design principle is that context must be shared across the full agent trace, not just individual messages, because subagents working in isolation lose nuance and misunderstand their tasks.
- _[cent]_ Parallel subagents that cannot see each other's work make conflicting implicit decisions that compound into inconsistent/bad outputs (illustrated by the Flappy Bird visual-style mismatch example).
- _[supp]_ Having agents 'talk things out' to coordinate is unreliable as of 2025 because agents lack the communicative efficiency of humans, so multi-agent coordination via messaging cannot be trusted to resolve conflicts.
- _[supp]_ For very long tasks that exceed the context window, the recommended approach is a dedicated compression/summarization model that distills action history into key decisions and events, rather than splitting work across agents.

**LangChain: multi-agent**
- _[cent]_ Multi-agent systems excel at breadth-first, parallelizable tasks but fail when agents must share the same context or have many interdependencies (e.g., most coding tasks).
- _[cent]_ Read operations parallelize across agents far better than write operations, because conflicting writes produce worse outcomes than conflicting reads; therefore synthesis (writing) should be centralized to one agent while research (reading) is distributed.
- _[cent]_ Vague or under-specified subagent task descriptions cause duplicated work and misinterpretation; each subagent needs an explicit objective, output format, tool/source guidance, and clear task boundaries.
- _[supp]_ Insufficient context in task hand-offs causes concrete coordination failures where subagents duplicate each other's work rather than dividing labor.
- _[supp]_ Durable execution and observability are required for reliable agents because minor failures can be catastrophic and agents are non-deterministic between runs even with identical prompts.

**MAST 2503.13657**
- _[cent]_ Multi-agent LLM system failures fall into a 14-mode taxonomy (MAST) organized into 3 categories: System Design Issues (specification), Inter-Agent Misalignment (coordination), and Task Verification.
- _[cent]_ State-of-the-art open-source multi-agent systems fail on the majority of tasks, with measured failure rates from 41% to 86.7% across 7 SOTA frameworks.
- _[cent]_ MAS failures stem primarily from organizational/coordination design flaws rather than the limitations of individual constituent agents.
- _[cent]_ Simple tactical interventions (e.g. improved role prompts, adding verification steps) yield modest gains but do not resolve all failure modes; task completion remains low, implying structural redesign is needed.
- _[cent]_ Multi-agent LLM system failures cluster into 14 fine-grained failure modes grouped into 3 categories: system design issues, inter-agent misalignment, and task verification/termination.
- _[cent]_ The MAST taxonomy was empirically derived from a large corpus of real MAS execution traces (1600+ annotated traces across 7 popular multi-agent frameworks), not from theory alone.
- _[cent]_ Multi-agent system failures are structural and cannot be fixed by superficial tweaks (e.g., better prompting); they require deeper solutions in agent organization and verification.
- _[supp]_ Verification failures are a substantial share of MAS errors: incorrect verification (9.1%) plus no/incomplete verification (8.2%) together account for ~17% of observed failure-mode prevalence across 1642 traces.
- _[supp]_ The taxonomy is a reliable, reproducible instrument: independent human annotators agreed at kappa = 0.88, and the authors release an LLM-based annotator to scale the analysis.
- _[supp]_ Multi-agent LLM systems frequently underperform expectations, showing minimal gains on popular benchmarks despite added complexity, motivating a systematic study of why they fail.

**OpenAI Agents SDK**
- _[cent]_ The OpenAI Agents SDK frames agent orchestration as a choice between two approaches: LLM-driven (the LLM plans/reasons/decides steps) and code-driven, and explicitly states that orchestrating via code yields more deterministic and predictable speed, cost, and performance.
- _[cent]_ The SDK recommends a specific deterministic pattern for self-verification: running a task agent in a while loop paired with a separate evaluator agent that provides feedback until the output passes criteria (an evaluator-optimizer / critic loop implemented in code, not by the LLM).
- _[supp]_ The SDK distinguishes two multi-agent primitives with explicit selection guidance: 'agents as tools' (a manager agent keeps control and calls specialists via Agent.as_tool(), owning the final answer) versus 'handoffs' (a triage agent routes and the specialist becomes the active agent for the rest of the turn).
- _[supp]_ For LLM-driven orchestration, the SDK prescribes concrete reliability best practices including specialized single-task agents over generalists, self-critique loops, monitoring/iteration on failures, and investing in evals.
- _[supp]_ The SDK endorses structured outputs and agent chaining (transforming one agent's output into the next's input) plus parallel execution via asyncio.gather as the code-orchestration building blocks for inspectable, deterministic control flow.

### C. Evaluation, judging & reward hacking

**Agent-as-a-Judge survey 2601.05111**
- _[cent]_ LLM-as-a-judge (single-model) evaluation suffers from inherent parametric biases such as favoring verbosity and its own output patterns, undermining neutrality — a concrete risk for agent-os's skeptical auditor if it is a single LLM pass.
- _[cent]_ Traditional LLM judges are passive observers that assess answers only from linguistic patterns without verification, leading to hallucinated evaluations; grounding judgments in real observations/tool checks is needed.
- _[cent]_ Agent-as-a-Judge improves reliability via tool-augmented verification and inspection of execution artifacts/automated checks — directly applicable to grounding agent-os's QA auditor in the real git diff and test/browser evidence rather than narrative.
- _[supp]_ Single-pass evaluation across all dimensions causes cognitive overload and produces coarse-grained scores, motivating decomposed/multi-step evaluation rather than one holistic verdict.
- _[supp]_ Giving judge agents tool access introduces new safety risks including prompt injection, tool misuse, and unintended side effects, plus compute/latency overhead — a cost of moving from LLM-judge to agentic judging.

**Judge consistency 2512.16041**
- _[cent]_ Even top-performing LLM judges (Gemini-2.5-Pro, GPT-5) fail to maintain consistent preferences in nearly a quarter of difficult cases, indicating that state-of-the-art LLM-as-a-Judge is not reliable enough to be trusted on hard/close calls.
- _[cent]_ LLM judges suffer severe positional bias: order-reversal inconsistency rates measured at 76.2% for Llama3-8B-Instruct, 44.4% for Qwen3-4B-Instruct, and 25.3% for Gemini-2.5-Flash-Lite, so a single-pass judgment can be flipped just by swapping answer order.
- _[cent]_ Judge reliability degrades sharply as candidate answers get closer in quality — roughly 200% more inconsistency on close-gap answers — which is precisely the regime used in RL-based training rewards and test-time best-of-N selection.
- _[supp]_ Concrete mitigations improve judge consistency: multi-agent/panel-based juries improve performance by up to 15%, and prompting the model to self-generate explicit rubrics reduces local inconsistency (IPI) by 16.1% and global inconsistency (TOV) by 11.0%, whereas increasing reasoning depth yields only minor gains.
- _[supp]_ Human annotation is not a reliable gold standard for evaluation: inter-annotator agreement is low (66% AlpacaFarm, 63% MT-Bench) and applying the same consistency metrics to human evaluators shows fragility (IPI 0.332, TOV 6.523 on complex tasks).

**One Token to Fool 2507.08794**
- _[cent]_ LLM-as-a-judge / generative reward models can be systematically fooled into emitting false-positive 'correct' verdicts by superficial 'master key' inputs (non-word symbols like ':' or '.', or reasoning openers like 'Thought process:' or 'Solution') that contain no actual reasoning, with false positive rates as high as 80%.
- _[cent]_ The vulnerability affects leading proprietary judge models often treated as gold-standard evaluators, not just open-source ones: GPT-4o, GPT-o1, and Claude-4 are all susceptible; e.g. 'Thought process:' induces up to 35% FPR in GPT-4o, and reasoning openers cause 60-90% FPR in open models like LLaMA3-70B-Instruct and Qwen2.5-72B-Instruct.
- _[cent]_ Common inference-time defenses do not reliably protect judges: chain-of-thought prompting and majority voting fail to defend and can even worsen the attack, while larger judge models are often MORE vulnerable, with mid-sized models best balancing robustness and accuracy.
- _[supp]_ A targeted data-augmentation mitigation works: fine-tuning judges on truncated model outputs (first-segment lead-ins) as adversarial negatives yields 'Master Reward Models' with near-0% FPR against master-key attacks while preserving standard evaluation quality (Cohen's kappa 0.91 with GPT-4o, 0.90 with humans).
- _[supp]_ The vulnerability was discovered as a real training-collapse failure mode: during RLVR training a policy model degenerated into emitting short superficial openers (<30 tokens) that the judge rewarded, causing response length to collapse and KL divergence to surge.

**Reliability w/o Validity 2606.19544**
- _[cent]_ High raw agreement (80-85% exact-match on MT-Bench) between an LLM judge and humans collapses to only moderate chance-corrected agreement (Cohen's kappa ~0.48), with 'kappa deflation' of 33.8-41.3 percentage points across all 21 tested models — meaning high accuracy percentages overstate true judge validity.
- _[cent]_ An LLM judge can be highly self-consistent (test-retest reliability >=0.95) while simultaneously exhibiting severe position bias (>0.10); consistency measures output stability, not decision-process correctness, so a deterministic bias can masquerade as reliability.
- _[cent]_ Trusting an LLM judge's verdict requires a validation protocol: report Cohen's kappa (not exact match) as the headline metric, test position bias via AB+BA order swaps, verify test-retest across >=3 runs, cross-validate on >=2 benchmarks, and flag high-stability-but-high-bias judges as failure modes.
- _[supp]_ LLM-judge quality rankings are unstable across benchmarks: the same model (Llama 3.3 70B) dropped 15 positions (5th to 20th) between MT-Bench and JudgeBench, so a judge validated on one dataset cannot be assumed reliable on another.
- _[tang]_ Verbosity bias in LLM judges has substantially diminished in recent model generations: all 21 judges tested showed verbosity bias below 0.011, well under the 20-40% variance reported in 2023 literature.

**Reward-hacking RHDA 2606.04923**
- _[cent]_ In rubric-based RL, using an LLM-as-a-Judge causes reward hacking because the policy learns to exploit the judge's latent biases (verbosity, sycophancy, self-praise, surface form) rather than improve genuine task quality, and this hacking is subtle and only visible after training has derailed.
- _[cent]_ Reward hacking against an LLM judge causes measurable capability degradation, not just inflated scores: models trained under self-praise bias dropped from a 47.4 no-bias HealthBench score to 36.1, and Arena-Hard from 10.6 to 8.5, showing the biased reward actively harms real task quality.
- _[cent]_ A dedicated tool-using LLM agent (RHDA) that inspects multiple checkpoints and accumulates typed, evidence-constrained alerts detects reward-hacking onset more reliably than general-purpose agents (Claude Code) or a fixed chain-of-thought monitor; the CoT monitor missed 3 of 6 runs, and trajectory-level hypothesis tracking mattered more than backend model strength.
- _[supp]_ Judge-blind onset detection requires temporal contrast across the trajectory rather than judging isolated outputs: a single response may look fluent, so the detector must compare behavior across steps to spot exploitation of judge bias.
- _[supp]_ How fast and severely a policy hacks a judge is governed by two separable bias properties: discoverability (driven by the bias's entanglement with the gold reward) and exploitability (driven by the intrinsic complexity of the bias).

**Tool-agent eval 2604.16706**
- _[cent]_ Substring/keyword-match evaluation of tool-using agent outputs agrees with human annotation at only kappa=0.049 (chance level), while a three-LLM ensemble judge reaches kappa=0.432 (moderate); this means heuristic 'string-match' verdicts are essentially worthless for judging agent QA outputs.
- _[cent]_ Even a validated three-LLM ensemble judge carries a systematic conservative bias, marking only 25% of traces correct versus 38% by humans, underestimating true correctness by ~13 percentage points; the dominant error is rejecting answers humans accept (19 of 25 disagreements). So an auditor's raw verdict needs bias-direction reporting and human calibration.
- _[cent]_ A single injected wrong-but-valid parameter propagates to a wrong final answer with human-calibrated probability ~0.62 (range 0.46-0.73 across models); early-stage errors in an agent pipeline compound to corrupt the final output at high rates, quantifying error-cascade risk.
- _[supp]_ A model's ability to REJECT bad parameters at the schema gate and its ability to RECOVER after accepting a bad parameter are statistically independent capabilities (Spearman rho=0.126, p=0.747); robustness is at least two-dimensional, so mitigations must target input-filtering and output-reasoning separately.
- _[supp]_ A lightweight three-layer runtime interceptor (schema validation + chain-of-thought uncertainty-keyword monitor + output-consistency check), running in parallel at negligible cost (~$0.15 for 1,200 runs), cut GPT-4o-mini hallucination by 23.0 pp under a concurrent n=600 control, but had no effect on Gemini-2.0-Flash whose 95% parameter-rejection already eliminated the failure mode; interceptor value is model-dependent.

### D. Durable execution & reliability

**Inngest: durable execution**
- _[cent]_ Durable execution engines persist the result of each step and, on restart, replay from the last successful checkpoint rather than re-executing the entire workflow, providing crash recovery for long-running agents.
- _[cent]_ Multi-step agent workflows compound failure: five steps at 99% reliability each yield only 95% overall success, motivating per-step durability rather than whole-run retries.
- _[cent]_ AI agent workflows are inherently long-running (minutes to hours) and must survive infrastructure failures, deployment restarts, and external service outages.
- _[supp]_ Durable execution provides exactly-once step semantics via memoization so expensive LLM/tool calls are not re-run on retry, controlling inference cost.
- _[supp]_ Durable execution enables human-in-the-loop by suspending a workflow that persists its complete state and waits for an external signal, allowing pauses of hours or days without losing state.

**Temporal: durable agents**
- _[cent]_ Temporal can build dynamic AI agents by separating deterministic Workflow orchestration from non-deterministic LLM decisions executed in Activities; the LLM's tool choices are dynamic while the while-loop/orchestration structure stays deterministic.
- _[cent]_ Temporal achieves crash recovery for agents by replaying the workflow from a recorded Event History, re-using prior LLM decisions rather than re-executing them, so a restarted agent does not make different choices or create conflicting state.
- _[supp]_ The recommended agent loop keeps the workflow structure (while loop, sequence of operations) deterministic while the LLM dynamically selects which tool to call and with what parameters within that loop.
- _[supp]_ Durable-execution engines like Temporal are used in production agent systems, with OpenAI's Codex and Replit's Agent 3 cited as examples, and the authors argue Temporal is the best way to build AI agents.

**Vadim: durable execution**
- _[cent]_ Durable execution for LLM agents is achieved by checkpointing full workflow state (context) after every LLM call or tool invocation, serialized to persistent storage (e.g. SQLite/JSON), so a crashed run resumes from exactly where it stopped.
- _[cent]_ Idempotency keys are required so that retries of durable steps do not cause duplicate side effects; a step checks whether its key was already processed and returns the cached result instead of re-executing.
- _[cent]_ Human-in-the-loop pauses should be implemented by parking the run durably (waiting indefinitely at no compute cost) rather than by polling, avoiding wasted compute cycles during the wait.
- _[cent]_ Durable execution is defined as the property that an agent workflow (LLM calls, tool invocations, human-in-the-loop pauses) survives process crashes, redeploys, and indefinite waits.
- _[supp]_ For long runs (over an hour) or multi-node setups, journal replay over snapshots provides exactly-once semantics across steps and is preferable to plain snapshotting.

**Zylos: durable agent runtime**
- _[cent]_ Session/chat memory is not equivalent to durable execution; durability requires proving which side effects (commands, emails, approvals) actually occurred, not just recalling conversation history.
- _[cent]_ Checkpointing alone is insufficient for durability; a common and dangerous misconception is that saving checkpoints solves crash-resumability.
- _[cent]_ A durable agent runtime should be structured as a run/step journal with replay boundaries around nondeterministic operations, idempotent tool wrappers, durable human-approval gates with artifact hashing, and deliberate crash testing at specific execution points.
- _[supp]_ Agent runtimes need global retry budgets spanning the entire run to prevent retry storms, rather than only per-step retries.
- _[supp]_ Human approvals must be recorded as durable records including artifact hashes; storing approvals only as chat messages enables unsafe replay where modified artifacts execute against a stale approval.

### E. Protocols & observability

**Agent-protocol security 2511.0384**
- _[cent]_ Google's A2A (Agent-to-Agent) protocol lacks per-message signing and is highly susceptible to message tampering / MITM attacks over its SSE (Server-Sent Events) channels, because its peer-to-peer design prioritizes low latency over security oversight.
- _[cent]_ A2A has no explicit consent mechanism and orphaned/long-lived bearer tokens persist in peer caches without centralized revocation, so revoked permissions can remain active due to asynchronous synchronization delays.
- _[cent]_ Agent communication protocols are vulnerable to tool poisoning / command injection via crafted task descriptors, and prompt injection against A2A succeeds at 60-90% rates, with unintended data propagation occurring in up to 60% of simulated multi-agent exchanges.
- _[supp]_ Among the compared protocols, ACP (Linux Foundation's RESTful standard) has the strongest authorization scoping model (operation-specific JWTs and per-segment JSON Web Signatures), but because JWS enforcement is optional, its flexibility itself becomes a vulnerability producing predictable integrity failures.
- _[supp]_ The authors conclude existing agentic communication protocols are insufficiently secure for production multi-agent deployment at scale, recommending mandatory per-message cryptographic signing, globally enforced token expiration, fine-grained context-aware authorization, and immutable audit logging.

**OpenTelemetry GenAI conventions**
- _[cent]_ OpenTelemetry's GenAI semantic conventions define standardized operation types for agentic workflows — create_agent, invoke_agent, and invoke_workflow — and (as of v1.41) distinguish CLIENT spans for remote agent calls from INTERNAL spans for local framework execution, turning agent reasoning from a black box into structured traces.
- _[cent]_ MCP tool-call tracing (added in v1.39) connects previously-disconnected agent-side and server-side traces by carrying mcp.method.name, mcp.session.id, and mcp.protocol.version on client spans and linking to server spans via W3C Trace Context propagation, so all spans share one trace_id for end-to-end visibility.
- _[cent]_ The GenAI conventions define a hierarchical span nesting for agent workflows where an invoke_agent span parents model chat calls and MCP tools/call spans, and tool execution uses an execute_tool INTERNAL span with optional gen_ai.tool.call.arguments.
- _[supp]_ OpenTelemetry standardizes core LLM telemetry attributes including gen_ai.provider.name, gen_ai.request.model, gen_ai.response.model, gen_ai.usage.input_tokens, and gen_ai.usage.output_tokens, which traditional OTel conventions did not cover.
- _[supp]_ The conventions offer three content-recording modes (disabled by default, on-span attributes, or external storage with reference URLs), recommending external storage for production with high telemetry volume or sensitive data; the spec is still in Development status (v1.41.0) with no committed stabilization timeline.

**Protocol survey 2505.0227**
- _[cent]_ The survey defines a four-layer progression of agent interoperability protocols with distinct scopes: MCP for LLM-to-tool integration, ACP for infrastructure-level multi-agent messaging, A2A for enterprise intra-org task delegation, and ANP for open-internet decentralized agent marketplaces.
- _[cent]_ The survey recommends a phased adoption roadmap (MCP for tools first, then ACP for rich interaction, then A2A for enterprise collaboration, then ANP for open markets) to maximize interoperability while minimizing integration complexity.
- _[supp]_ The protocols differ in transport and security: A2A uses HTTP with optional SSE plus push notifications and DID-based handshake or out-of-band headers, while MCP uses HTTP/Stdio/SSE with token-based auth (optionally DIDs).
- _[supp]_ MCP's architecture assumes a centralized server and is exposed to prompt-injection risk, an explicit stated limitation of the protocol.
- _[supp]_ A2A is designed for trusted task delegation within organizational trust boundaries using capability-based Agent Cards, but its limitation is being enterprise-centric and assuming an agent catalog exists.


*(Total unique claims listed: 133.)*