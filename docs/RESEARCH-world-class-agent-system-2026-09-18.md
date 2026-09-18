# Research synthesis: a world-class agent operating system

**Research date:** 2026-09-18
**Question:** What would make Agent OS materially more useful than a direct frontier-model session, reliable
enough for consequential work, and adaptable across founders, product teams, engineers, reviewers, operators,
clients, and future domains?
**Decision status:** accepted direction; implemented items and remaining evidence are separated below.

## Executive conclusion

The winning product is not “more agents.” OpenAI, Anthropic, GitHub, IDEs, and many open-source frameworks
already provide model loops, tools, subagents, and coding assistance. Agent OS earns a price only when it makes
a valuable outcome **more likely, less labor-intensive, more recoverable, and easier to govern** than the
simpler alternative.

The long-term architecture therefore has four product-level differentiators:

1. **A durable outcome contract:** objective, feasibility, assumptions, measures, resources, authority,
   workstreams, verification, and replanning survive model calls, deploys, outages, and human delays.
2. **An accountable organization:** people, agents, services, and vendors have explicit owners, budgets,
   permissions, escalation routes, and evidence obligations. The UI presents work and decisions, not agent
   theater.
3. **An evidence-backed product council:** synthetic customers, product/UX agents, browser explorers, and model
   judges discover issues cheaply, but only blinded studies, deterministic checks, representative humans, and
   production telemetry can justify adoption.
4. **A customer value receipt:** every expensive topology or paid tier can be compared with a named simpler
   baseline on matched tasks: success, quality, reliability, latency, model cost, human time, and interventions.

This changes a previous assumption materially: elastic agent organizations remain important, but fan-out is
not the default. The planner must choose the smallest sufficient topology and prove why extra coordination is
worth its context, latency, cost, and failure surface.

## Research method and limits

The review prioritized primary sources: peer-reviewed papers, project specifications, official engineering
reports, official courses, standards, and first-party product documentation. Vendor claims were treated as
evidence of supported mechanisms, not proof of Agent OS outcomes. No finite search can cover “the whole
internet”; the stopping rule was that additional sources no longer changed the architecture decision, only
reinforced it.

The corpus covered:

- multi-agent architecture, context engineering, durable execution, and model/tool protocols;
- agent evaluation, LLM-judge bias, human calibration, task horizons, and workplace benchmarks;
- human-AI interaction, uncertainty, feedback/control, accessibility, and role-specific experience;
- runtime debugging, traces, artifact inspection, recovery, versioning, and security;
- current coding-agent and app-builder products, their interaction models, and their pricing boundaries;
- design-system integration, especially Figma MCP and Code Connect.

## Findings that changed the design

### 1. Multi-agent systems are a conditional optimization

Anthropic reports a large improvement from a lead-agent/parallel-subagent architecture for breadth-first
research, but also reports roughly fifteen times the token use of chat and warns that multi-agent delegation is
not a good fit for many tightly coupled coding tasks. Its broader agent guidance recommends starting with the
simplest design and adding workflow or agent complexity only where measurable results justify it.

**Agent OS decision:** every workstream declares one of `deterministic_workflow`, `single_agent`,
`parallel_agents`, `evaluator_optimizer`, or `subworkflow`. It also records coupling, parallelism, a comparison
baseline, expected measured benefit, model-cost estimate, latency budget, and fallback. Parallel agents are
admitted only for low-coupling work with a registered success measure.

Sources: [Anthropic multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system),
[Anthropic building effective agents](https://www.anthropic.com/engineering/building-effective-agents).

### 2. Context is a finite engineering resource

Long prompts and accumulated traces do not produce a monotonic quality improvement. Context must remain
high-signal, task-scoped, and reconstructable. Each worker needs a local cursor plus durable references to the
authoritative mission, not a copy of every prior transcript.

**Agent OS decision:** preserve the complete program as an immutable artifact, give workers compact mission
authority plus their workstream coordination contract, keep evidence reference-only, and use deliberate
handoffs/summaries. Never solve context overflow by silently deleting requirements or by restarting completed
work.

Source: [Anthropic context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents).

### 3. An eval is a request, environment, stopping rule, and scorer

Agent evaluation must test multi-turn trajectories in an environment, not only final prose. The Stanford
CS329Z formulation—request, environment, stopping criteria, scorer—is a useful minimum contract. Anthropic's
eval guidance adds representative tasks and inspecting the transcript/trace. METR's time-horizon work shows
why task duration and completion probability should be measured together instead of reporting an undifferentiated
pass rate.

**Agent OS decision:** version canonical tasks, environments, budgets/stopping rules, measures, and evidence.
Report completion, first-failure location, quality, reliability, latency, cost, intervention, and recovery—not
one aggregate “agent score.”

Sources: [Stanford CS329Z](https://cs329z.stanford.edu/),
[Anthropic agent evals](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents),
[METR time horizons](https://metr.org/time-horizons/),
[Hugging Face agent observability and evaluation](https://huggingface.co/learn/agents-course/bonus-unit2/what-is-agent-observability-and-evaluation).

### 4. Synthetic users are instruments, not customers

Research systems such as UXAgent show that browser-capable simulated users can scale usability exploration.
They are useful for finding confusing flows, accessibility failures, and edge cases before involving people.
But persona-prompt research finds that prompt construction changes behavior and can amplify stereotypes; one
ACL study found demographic personas explain less than ten percent of variance in many subjective datasets.

**Agent OS decision:** a product-manager agent may propose a study; a synthetic council may explore; an
independent evaluator may score. Synthetic preference alone cannot adopt a design or claim market demand.
Studies must be pre-registered, blinded, repeated, position-counterbalanced, tied to durable browser/artifact
evidence, and calibrated with representative humans and production behavior.

Sources: [UXAgent](https://arxiv.org/abs/2504.09407),
[ACL persona attributes and subjective tasks](https://aclanthology.org/2024.acl-long.554/),
[EMNLP findings on persona prompting](https://aclanthology.org/2025.findings-emnlp.1261/).

### 5. Model judges need bias controls and hard vetoes

LLM judges exhibit position and self-preference bias. Shared, task-specific rubrics improve agreement, but a
judge vote must never override deterministic security, accessibility, data integrity, authorization, or
functional failures.

**Agent OS decision:** compare both variants, blind identity, counterbalance position across repetitions, pin
the rubric, retain raw evidence, separate maker/checker, and reject a candidate on critical deterministic
failure even if every model prefers it. Human evidence admits only a reversible experiment; representative
production evidence is required for automatic adoption under the current contract.

Sources: [position bias in LLM judges](https://arxiv.org/abs/2406.07791),
[self-preference bias](https://arxiv.org/abs/2410.21819),
[Google human-in-the-loop patch evaluation](https://research.google/pubs/towards-a-human-in-the-loop-framework-for-reliable-patch-evaluation-using-an-llm-as-a-judge/),
[OpenAI GDPval](https://openai.com/index/gdpval/).

### 6. Inspect the artifact and the trajectory

Microsoft's CORPGEN work reports that inspecting output artifacts aligned with human judgment far better than
using screenshots or action logs alone in its studied setting. Screenshots remain essential for visual,
interaction, and accessibility evidence; traces remain essential for diagnosis. Neither is a substitute for
opening, executing, and evaluating the produced artifact. AgentRx similarly motivates guarded executable
constraints and step-level evidence for locating root causes.

**Agent OS decision:** require both outcome evidence and process evidence. The release packet contains the
artifact, its digest/provenance, deterministic checks, representative task results, browser evidence where
applicable, first-failure diagnostics, cost/latency, and the exact decision that admitted it.

Sources: [Microsoft CORPGEN](https://www.microsoft.com/en-us/research/blog/corpgen-advances-ai-agents-for-real-work/),
[Microsoft AgentRx](https://www.microsoft.com/en-us/research/blog/systematic-debugging-for-ai-agents-introducing-the-agentrx-framework/).

### 7. Human control must be efficient, specific, and durable

Microsoft HAX and Google's People + AI Guidebook emphasize making capabilities/limitations clear, supporting
efficient correction, and giving people feedback and control. GitHub distinguishes immediate steering from
queued follow-up; Linear keeps a human owner when work is delegated to an agent.

**Agent OS decision:** people can steer now, queue next, request changes, delegate, pause, resume, cancel, or
replan. A question states why it matters, who owns it, what it blocks, safe default, cost/deadline, and whether
work continues. Agents never gain standing authority from assignment. Human response is durable work, not a
chat message that disappears on refresh.

Sources: [Microsoft HAX guidelines](https://www.microsoft.com/en-us/haxtoolkit/ai-guidelines/),
[Google PAIR feedback and control](https://pair.withgoogle.com/chapter/People%20%2B%20AI%20Guidebook%20-%20Feedback%20%2B%20Control.pdf),
[GitHub steering and queueing](https://docs.github.com/en/copilot/how-tos/copilot-sdk/features/steering-and-queueing),
[Linear agent delegation](https://linear.app/docs/assigning-issues).

### 8. Figma is an adapter, not product authority

Figma's MCP server can expose design context and write to the canvas, while Code Connect can map design-system
components to code. This can materially improve designer collaboration and fidelity for customers already
using Figma. It cannot determine whether a flow is understandable, accessible, useful, or commercially sound.

**Agent OS decision:** add a scoped Figma MCP/Code Connect adapter after customer demand and credentials. Store
file/node/version references and generated artifacts in the mission evidence graph. Keep product studies,
accessibility checks, source code, and release authority inside Agent OS.

Sources: [Figma MCP server](https://developers.figma.com/docs/figma-mcp-server/),
[Figma Code Connect](https://developers.figma.com/docs/figma-mcp-server/code-connect-integration/).

### 9. Governance is continuous, not a pre-release ceremony

NIST AI RMF organizes work around govern, map, measure, and manage, with monitoring and feedback continuing
after deployment. OWASP's agentic threat work makes clear that tool use, memory, identity, and inter-agent
communication add attack surfaces beyond prompt injection.

**Agent OS decision:** policy/effect authorization, identity, tenancy, secret boundaries, evidence provenance,
runtime health, canary/rollback, and production feedback remain active throughout the lifecycle. Observability
helps explain behavior but cannot become a second source of product truth.

Sources: [NIST AI RMF core](https://airc.nist.gov/airmf-resources/airmf/5-sec-core/),
[OWASP agentic AI threats and mitigations](https://genai.owasp.org/resource/agentic-ai-threats-and-mitigations/),
[OpenTelemetry semantic conventions](https://opentelemetry.io/docs/specs/semconv/).

## Product architecture after the review

### The product-evidence council

The council is a governed process, not one omniscient agent:

| Stage | Owner | Output | Authority |
|---|---|---|---|
| Frame | product manager agent + accountable human | versioned hypothesis, baseline, candidate, segments, canonical tasks, measures | may propose |
| Explore | browser/synthetic personas | observations with screenshots, traces, artifacts, failures | may discover |
| Check | deterministic test/security/accessibility tools | pass/fail evidence and critical failures | may veto |
| Judge | independent evaluators | blinded, counterbalanced rubric scores | advisory |
| Calibrate | representative humans | task completion, friction, correction, qualitative evidence | may admit bounded experiment |
| Learn | production telemetry + feedback | outcome, reliability, cost, intervention, cohort evidence | may satisfy adoption gate |
| Decide | attributable product/release authority | adopt, experiment, reject, rollback, or request evidence | commits |

No agent may manufacture a human or production observation. No aggregate score may hide a hard-gate failure.
Every decision names the study revision and durable evidence IDs.

### Role-specific experience

One interface should not force every person into an engineer's graph or trace view.

| User | Default question | Default workspace | Progressive detail |
|---|---|---|---|
| Founder/CEO | “Are we on course, what needs me, and what value did this create?” | outcomes, decisions, risks, spend, next milestones | organization, evidence, traces |
| Product leader | “What customer problem and evidence justify this change?” | hypotheses, segments, task studies, feedback, experiments | judge runs, raw sessions |
| Designer | “What flow/component needs revision and why?” | journey, variant, accessibility, Figma/code linkage | browser evidence and implementation |
| Engineer | “What contract failed and where?” | owned work, failing gate, artifact diff, reproducible command | trace spans/model/tool calls |
| Reviewer/security | “Can this claim be trusted?” | claims, provenance, policy, independence, exceptions | immutable audit and raw evidence |
| Operator | “Is execution healthy and recoverable?” | queues, leases, heartbeats, provider health, recovery actions | bounded diagnostic payloads |
| Client/stakeholder | “What was delivered and what decision is requested?” | scoped milestones, preview, review packet, requests | only authorized evidence |

The primary interaction remains conversational but not chat-only. Chat creates and steers durable missions;
the inbox owns decisions; the mission view owns progress; the review packet owns claims/evidence; push/email/
Slack/Teams are recipient-controlled attention routes. The installable PWA is the correct first mobile product.
A native app is justified only by measured PWA limitations.

### The skeptical-customer test

For every release and price tier, run matched tasks against:

1. a direct frontier-model session;
2. the best single-agent workflow available to the customer;
3. the customer's current human/tool process, when measurable.

Report, without a synthetic dollar conversion:

- completed tasks / admitted tasks;
- quality and critical-failure rate;
- recovery rate after injected failure;
- p50/p95 outcome latency;
- model/tool/infrastructure cost;
- human minutes and number of interventions;
- deployable artifact ownership/exportability;
- evidence completeness and decision auditability.

Only measured human time is monetized in the value receipt. Quality, safety, market adoption, and “better
ideas” are not converted into invented revenue. If the customer cannot see a favorable receipt, Agent OS should
recommend the simpler alternative instead of defending its own complexity.

## Repository impact

### Implemented in this change

- Added a provider/UI-independent product-study contract with versioned tasks, segments, metrics, evidence
  kinds, durable evidence references, bias controls, deterministic vetoes, and adoption dispositions.
- Added a matched-system value receipt covering success, quality, reliability, latency, model cost, customer
  price, human time, and interventions.
- Added a coordination plan to every mission workstream: strategy, coupling, parallelism, comparison baseline,
  expected benefit, measures, estimated model cost, latency budget, and fallback.
- Added deterministic admission rules for low-coupling parallelism, real worker count, evaluator-optimizer
  rubrics, executable subworkflows, deterministic workflows, known measures, and budget authority.
- Kept workstream/coordination authority in compact agent run context.
- Replaced legacy “bias toward expanding; cost is not a constraint” prompts with smallest-sufficient-team
  guidance.
- Added focused tests for synthetic-evidence validity, human/production gates, deterministic failures,
  customer value, topology coherence, and coordination-budget oversubscription.

### Already present and retained

- durable mission intent, lifecycle reconciliation, arbitrary mission programs, scoped human waits, resource
  acquisition, capability expansion, subworkflows, verification, and atomic plan revision;
- independent assurance, improvement holdouts/canaries, browser-based QA evidence, security scanning,
  tenant/role/authority controls, spend policy, audit, OpenTelemetry mapping, crash recovery, and health gates;
- role-aware console projections, human-request ledger, conversational steering, PWA shell, notification
  preferences, external attention routes, and customer-safe evidence linkage.

### Not complete merely because this document exists

The system is not entitled to claim world-class product fit or public-production readiness until it has:

1. persisted and exposed product studies/observations/decisions in a tenant-scoped Evidence Lab;
2. run a versioned benchmark against direct Codex/Claude-class baselines on representative paid-customer
   tasks, publishing value receipts including failures;
3. calibrated synthetic personas/judges with real target users and maintained disagreement statistics;
4. exercised fault injection for provider throttling, partial tool failure, process death, stale leases,
   duplicate events, and artifact corruption in the production-shaped environment;
5. connected the production telemetry exporter and tested alert-to-diagnosis-to-recovery workflows;
6. run external staging with real OIDC, secrets, model/provider credentials, storage, email/push, billing,
   sandbox, deployment, backup/restore, and domain/TLS configuration;
7. completed accessibility and security review on representative end-to-end journeys;
8. obtained at least one real-customer outcome receipt showing why the product was worth its price.

## Sequenced backlog

### P0 — prove the product, not only the runtime

1. Persist the new Evidence Lab contracts behind tenant-scoped ports and RLS; add API projections and a
   role-aware UI for product, design, reviewer, and executive views.
2. Build the direct-model/single-agent/Agent-OS benchmark harness with matched tasks and blinded artifacts.
3. Add a failure taxonomy and first-failure localization to eval packets; run deterministic fault campaigns.
4. Add real-user consent, recruitment, session retention/redaction, and calibration reports.
5. Turn the value receipt into the executive outcome card and usage invoice explanation.

### P1 — deepen collaboration and operations

1. Add optional Figma MCP and Code Connect adapters with exact scopes and evidence references.
2. Add SLA timers, delegation, and escalation to the authoritative human-request ledger.
3. Export complete trace/metric/log correlation through an OpenTelemetry Collector while preserving bounded
   in-product diagnostics.
4. Add experiment assignment, cohort isolation, automatic rollback thresholds, and attributable promotion.
5. Measure context quality and handoff loss; add compression/retrieval only where benchmark evidence supports
   it.

### P2 — scale only when the workload proves the need

1. Run the durable-engine bakeoff and migration corpus under outage/version/replay load.
2. Add regional/cell isolation, fairness, noisy-neighbor controls, and disaster-recovery exercises.
3. Add native mobile only if the PWA fails measured decision/attention journeys.
4. Add specialized runtimes or languages only for measured control-plane or sandbox bottlenecks.

## Rejected directions

- **A permanent council of personas that votes on UI:** cheap consensus theater; it measures prompts and model
  taste unless calibrated against real people.
- **Maximum fan-out by default:** increases token use, coordination, context loss, and failure modes without
  guaranteeing task progress.
- **One framework owns product semantics:** SDKs and models will change; Agent OS must own mission, authority,
  evidence, and value contracts.
- **Figma as design truth:** useful collaboration surface, not evidence of usability or value.
- **Screenshots or traces as completion:** useful diagnostic evidence, not proof the artifact works.
- **A single magic quality score:** hides regressions and makes hard safety/security/accessibility failures
  tradable against superficial gains.
- **Claiming “production-ready” from local green tests:** production readiness requires external identity,
  infrastructure, recovery, security, accessibility, operations, and representative-user evidence.
