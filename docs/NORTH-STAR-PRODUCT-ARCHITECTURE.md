# North-Star product and platform architecture

**Decision date:** 2026-09-08
**Scope:** the commercial Agent OS product, from a CEO's first prompt through operating one or more companies
**Status:** normative direction; implementation is incremental and the legacy runtime remains only while runs drain

## Executive decision

Agent OS is a **governed operating system for AI companies**, not a workflow builder, coding chat, local demo,
or collection of autonomous scripts.

The product promise is:

> A customer states a business or product outcome, supplies the genuinely unavoidable prerequisites once,
> and manages an accountable AI organization that researches, builds, verifies, deploys, operates, sells,
> measures, and improves the result.

The ultimate capability target is an **organization generator**, not merely an application generator. The
same control and execution planes must be able to expand from a small product team into portfolios, divisions,
programs, thousands of specialist agents, human leaders, vendors, simulations, and regulated/physical work.
Large capital increases available resources; it never converts uncertainty into a guaranteed outcome. The
system must assemble and govern the institution a world-class mission needs, keep claims evidence-based, and
refuse to disguise a toy deliverable as completion.

Build it as a hosted multi-tenant SaaS first, with a remote CLI and public API using the same control plane.
Also provide a portable enterprise/BYOC distribution, but do not make self-hosting complexity part of the
first-time hosted experience.

Keep the differentiated Agent OS domain semantics and replace the overlapping custom durability machinery:

- Python modular monolith for the product, agents, API, workflow definitions, policy integration, and QA.
- TypeScript/React for the web console.
- A framework-neutral `WorkflowEngine`: DBOS Transact is permitted for the near-zero-fixed-cost bootstrap
  profile; Temporal is the managed growth engine after its explicit revenue/SLO gate. PostgreSQL owns business
  records and projections. A run never has two authoritative workflow engines.
- PydanticAI behind an Agent OS-owned `AgentRuntime`; all model access behind `ModelGateway`.
- MCP for agent-to-tool integration, AG-UI/SSE for agent-to-user events, and A2A only at boundaries with
  independent external agent systems.
- Cloud Run first; GKE Autopilot and KEDA when measured workload or sandbox requirements justify them.
- OpenTofu, OIDC/SCIM, OPA, PostgreSQL RLS, OCI, object storage, and OpenTelemetry as portable foundations.

Do **not** rewrite the product in Go or Rust. A second backend language may be introduced later only for a
measured, narrow data-plane boundary. Go is the preferred future language for a Kubernetes controller or
high-concurrency runner coordinator; Rust is reserved for a privileged host/microVM or other memory-safety-
critical component. Neither may own product lifecycle or policy truth.

## Product boundary

The complete product contains four related systems. Shipping only the first one is not the North Star.

1. **CEO control plane** — portfolio, company creation, directive chat, prerequisite packet, approvals,
   budgets, status, evidence, incidents, economics, and decisions.
2. **AI organization runtime** — persistent identities and roles, hierarchy, delegation, communication,
   memory, capability grants, work contracts, escalation, supervision, and improvement.
3. **Product/company execution plane** — research, specification, implementation, QA, release, marketing,
   sales support, analytics, support, operations, and continuous improvement.
4. **Platform plane** — accounts, organizations, tenancy, billing, metering, model/provider routing,
   integrations, sandboxing, deployment, observability, administration, security, and compliance evidence.

The first sellable vertical is smaller than the whole vision but must cut through all four systems:

```text
sign up -> create company -> state outcome -> prerequisite packet -> approve bounded plan
        -> research/specify -> build -> independently verify -> deploy to public URL
        -> observe evidence/cost/status -> issue a follow-up directive -> safe update/rollback
```

If that path needs SSH, local port forwarding, manual database edits, hidden operator work, or multiple
inconsistent dashboards, it is not a customer-ready vertical.

## Customer experience contract

### Hosted SaaS: the default

The customer should need only a browser:

1. Create an account and company.
2. Describe an outcome in ordinary language.
3. Receive one consolidated prerequisite packet: accounts, credentials, domain, budget, legal choices,
   irreversible approvals, and expected cost/time ranges.
4. Connect required services with hosted OAuth/provider flows. BYOK is optional, not a prerequisite to
   understanding or trying the product.
5. Approve a bounded operating contract.
6. Leave the tab. Receive useful milestone, delay, blocker, recovery, approval, and completion updates.
7. Return to a working URL, evidence, costs, known limitations, rollback, and recommended next action.

The primary UI is an exception-first CEO workspace, not a process monitor. It answers:

- What outcomes changed?
- What is at risk or waiting, and who owns it?
- What decision truly requires me now?
- What did the organization spend and earn?
- What evidence supports “done”?
- What will happen next if I do nothing?

### CLI and API

The CLI is a thin authenticated client of the same versioned API. It is useful to technical customers and
automation, but it is not a second orchestrator.

```text
agent-os login
agent-os company create
agent-os direct "launch ..."
agent-os watch RUN_ID
agent-os decide APPROVAL_ID
agent-os deploy BUILD_ID
agent-os export COMPANY_ID
```

All durable operations also have documented REST/event APIs and idempotency keys. Web, CLI, API, mobile,
and integrations must see the same authoritative state.

### Self-hosted and customer-cloud

Offer three explicit deployment profiles rather than claiming that one Compose file is production-ready:

| Profile | Intended use | Packaging | Truthful guarantee |
|---|---|---|---|
| `local` | development, evaluation, one operator | Docker Compose plus local Temporal | easy and portable, not HA |
| `byoc-gcp` | enterprise customer-controlled cloud | OpenTofu-provisioned managed GCP services | recommended customer-cloud production path |
| `kubernetes` | regulated/portable enterprise | versioned Helm OCI chart with external PostgreSQL, object storage, secrets, and Temporal | portable, but customer owns cluster and data-plane operations |

One bootstrap command should validate prerequisites, render a plan, provision idempotently, run migrations,
install services, create the first admin, and execute a synthetic end-to-end proof. Configuration is typed and
versioned. Secrets live in a secret manager and workloads use federated identity; production setup must not
depend on long-lived credentials in `.env` files.

### Cost and customer-pricing envelope

The launch architecture must scale economically as well as technically. Before `$500` MRR, recurring fixed
infrastructure is capped at `$50/month`; model, sandbox, external API, and generated-app costs are reserved and
metered per customer. Temporal Cloud, HA database tiers, Kubernetes, enterprise identity connections, and paid
observability activate only when revenue or a signed customer requirement covers them.

Customers are not required to buy a four-figure fixed plan. The launch hypothesis is a small platform
subscription plus prepaid, hard-capped usage, with BYOK available. The older `$1,000/month` Release Assurance
offer is a separate optional managed service paid to Agent OS, not Agent OS's infrastructure bill or default
subscription. The full activation thresholds, bootstrap bill, metering contract, and initial retail hypothesis
are defined in [Pricing and cost guardrails](PRICING-AND-COST-GUARDRAILS.md).

## One authoritative lifecycle

The outer lifecycle is the small monotonic aggregate accepted in
[`ADR-001`](ADR-001-single-authoritative-lifecycle.md):

```text
INTAKE -> RESEARCH -> SPECIFY -> BUILD -> VERIFY -> RELEASE
```

`ACTIVE | WAITING | FAILED | SUCCEEDED | CANCELLED` is a separate execution condition. QA repair remains an
iteration inside `VERIFY`; a timeout, worker lease, heartbeat, or model attempt is telemetry, not a product
phase.

After release, the product enters ongoing company loops rather than extending the build state enum forever:

```text
OPERATE <-> SUPPORT <-> MEASURE <-> IMPROVE
    \          MARKET <-> SELL          /
```

These are workflows and objectives attached to a released product. They have their own typed states and can
run concurrently. They do not mutate the authoritative build lifecycle into a hundred-state mega-machine.

### Judgment versus invariants

The North Star's “every decision agentic” means every **open-ended judgment** should be made by a capable,
contextual agent and recorded with its evidence—not that consistency and safety should be left to a model.

Agents decide what to research, how to decompose a goal, which specialists are needed, whether evidence is
convincing, how to repair a defect, and what recommendation to make. Deterministic code enforces tenancy,
authority, budgets, allowed transitions, idempotency, signatures, schema validation, credential boundaries,
release gates, and cancellation. An LLM can propose changing an invariant; it cannot silently bypass one.

This split is required for both human-like behavior and production reliability. A real executive team uses
judgment inside laws, contracts, budgets, accounting controls, and change-management rules.

## AI organization model

An agent is not merely one model call. It is a durable governed identity with:

- `agent_id`, tenant/company, role, supervisor, charter, objectives, and lifecycle;
- model policy, skills, tools, MCP servers, data scopes, spending authority, and action authority;
- private working memory, approved company memory, provenance, retention, and compaction policy;
- current work contracts, inbox, commitments, progress lease, availability, and escalation route;
- quality history, decisions, outcomes, cost, interventions, and capability-review dates.

Organization shape is dynamic but bounded. A supervisor can request or create capacity within policy and
budget. Recursive delegation is allowed, but every work item has one accountable owner, a contract, evidence,
a deadline/review cadence, and an escalation path. Unlimited recursive spawning is not a scalability strategy.

### Adaptive Chief of Staff and human workforce

The CEO front door is a persistent Chief-of-Staff agent, not a static intake form. It maintains a versioned
mission charter and resource inventory, detects only the missing prerequisites relevant to the current
mission, and asks a consolidated blocking-first question packet. When the CEO adds a recruiter, contractor,
executive, vendor, budget, credential, or data source, it proposes the resulting organization/work changes in
plain language and applies them only within granted authority. It should proactively suggest reassignment or
new hiring while keeping one accountable AI manager for every workstream.

Humans, agents, vendors, and software services are first-class work participants with responsibility,
response, quality, permission, and escalation contracts. A late or weak human/vendor result triggers an
agentic diagnosis followed by the appropriate follow-up, manager/vendor escalation, authorized reassignment,
or executive decision request; it is not declared failed merely because a generic timer elapsed.

Agent OS's tenant-scoped work/message/evidence ledger is authoritative. Jira, Linear, email, Slack, and other
systems synchronize through idempotent connectors rather than becoming hidden sources of workflow truth.
Each human may opt into a personal copilot whose access is limited by explicit, revocable scopes; deploying a
copilot never silently grants it all company or personal data.

The Chief of Staff replans after every material resource or authority change. If no recruiter exists, it may
perform bounded sourcing/coordination itself when its capabilities and grants permit, propose an external
recruiter when they do not, or ask the CEO one consolidated choice when human authority is unavoidable. A
confirmed hire is a durable `HUMAN_CHANGED` fact, not a conversational memory: affected work is recomputed,
and reassignment, reporting-line, tool-access, onboarding, and optional copilot changes are proposed together.
Nothing is silently reassigned and nothing depends on the CEO remembering to ask the next question.

Each human-facing copilot is an organizational identity with a separate grant, inbox, work view, and audit
trail. It can help the person understand and complete assigned work, but it cannot impersonate them, approve
its own request, read unrelated data, or expand its own scope. A human can pause or revoke it without stopping
the rest of the company.

### Communication

Internal communication uses typed Agent OS domain events over the selected workflow engine's durable
messages and a transactional outbox/projection, including:

```text
assignment, acknowledgement, progress, finding, question, disagreement,
help-request, delegation, handoff, approval-request, blocked, recovered,
incident, decision, completion, correction, context-update
```

Messages contain sender, recipient/audience, objective/work item, correlation and causation IDs, authority,
priority, content/evidence references, and delivery state. They are persisted before acknowledgement,
deduplicated, bounded, tenant-scoped, and independently observable. Natural-language rendering makes them feel
human; typed semantics make them reliable.

Use [MCP](https://modelcontextprotocol.io/) for an agent's tools and data. Use
[A2A](https://a2a-protocol.org/latest/) only when communicating with an independently operated/opaque agent
system; current A2A guidance distinguishes that boundary from MCP-equipped tools. Do not force every internal
worker exchange through A2A. Use [AG-UI](https://docs.ag-ui.com/introduction) for standardized interactive
agent events to user interfaces. Agent OS domain events remain authoritative even if any protocol changes.

## Logical platform architecture

```text
 Browser / mobile          remote CLI          customer/API integrations
          \                   |                    /
           +------ edge, OIDC, WAF, quotas -------+
                              |
                    stateless control API
                              |
              application services + policy gate
                 /             |              \
        PostgreSQL          Workflow engine     object/OCI stores
    business/audit state   DBOS -> Temporal     artifacts/evidence
                              |
            +-----------------+--------------------+
            |                 |                    |
      agent workers      verification workers  deployment workers
            |                 |                    |
            +------ ephemeral isolated sandboxes --+
                              |
               signed artifact -> staged release
                              |
                  generated-app data plane
              CDN / Cloud Run / customer cloud

 Every boundary -> OpenTelemetry -> metrics/logs/traces + LLM evaluation
 Every material action -> append-only Agent OS accountability ledger
```

The Agent OS control plane and generated applications are different products operationally. Generated-app
traffic never traverses the CEO workflow controller. Each app has independent deployment versions, runtime,
data, domain, quotas, monitoring, rollback, and failure containment.

## Control-plane modules

Keep one release train initially, but enforce these code and data boundaries:

| Module | Owns | Must not own |
|---|---|---|
| identity/tenancy | users, organizations, memberships, tenant context | agent decisions |
| governance | authority, approvals, policy, budgets, consent, kill switch | workflow scheduling |
| companies | company contracts, objectives, portfolio, economics | provider SDK objects |
| organization | agents, hierarchy, capability grants, work contracts, communication | product phase |
| lifecycle | lifecycle state and application commands | retries, leases, infrastructure health |
| runtime | model/tool execution and normalized results | authority or business transitions |
| verification | stories, evidence, findings, adjudication, release verdict | customer-visible completion by assertion |
| release/operations | artifacts, deployments, routes, health, rollback, incidents | source/build truth |
| integrations | OAuth connections, MCP catalog, credential references, connector health | raw secret display |
| metering/billing | usage, costs, entitlements, invoices, margins | arbitrary lifecycle mutation |
| experience | API, AG-UI events, console projections, notifications | direct SQL/process control |

The dependency direction remains:

```text
api / cli / workflows -> application -> domain
infrastructure -----------------------> application ports
```

## Language decision

### Use now

| Concern | Language | Reason |
|---|---|---|
| domain, workflows, agents, QA, API | Python | strongest AI/browser/data ecosystem; workload is dominated by network/model/build waits; current product knowledge and tests are Python |
| web console | TypeScript/React | mature typed browser ecosystem and AG-UI integration |
| infrastructure | OpenTofu HCL plus minimal declarative YAML | repeatable managed infrastructure and portable images |
| SQL | PostgreSQL migrations and explicit repositories | tenant policy, transactional truth, auditability |

Use modern async I/O where it reduces waiting, processes/jobs for untrusted or blocking work, and horizontal
worker scaling for throughput. Changing languages does not repair incorrect state ownership, idempotency,
backpressure, tenant isolation, or slow model prompts.

### Conditions for Go

Introduce Go only for an isolated service when all are true:

1. production profiles show Python CPU, memory, cold start, or connection concurrency—not model/provider/DB
   latency—is a material SLO or gross-margin bottleneck;
2. ordinary optimization and horizontal scaling fail the documented target;
3. the service has a narrow versioned API and owns no lifecycle/business truth; and
4. load tests show the new implementation materially improves cost or capacity after operational overhead.

A future Kubernetes `runner-controller` is the most plausible candidate because the controller-runtime and
container ecosystem are Go-native.

### Conditions for Rust

Use Rust only when Agent OS must operate privileged host code, a Firecracker/microVM supervisor, a native
egress proxy, or another security boundary where memory safety and predictable resource use justify the
complexity. Prefer managed gVisor/GKE Sandbox first so the company does not accidentally become a hypervisor
vendor.

No cross-language rewrite may start from “millions of users someday.” It starts from a reproducible benchmark,
an SLO miss, a cost model, a rollback plan, and a bounded interface.

## Scale architecture

Scale four dimensions separately: registered users, live API/chat sessions, active workflows/model calls, and
untrusted sandboxes/generated-app traffic.

### First 10–50 customers

- One region using multi-zone managed services.
- Cloud Run service for API/streaming, one fixed small worker pool, and Cloud Run Jobs for sandboxes.
- Usage-based managed PostgreSQL with restore history, object storage, OCI registry, managed OIDC, OPA, and
  OTel. Use DBOS Transact in the capped bootstrap profile; activate Temporal Cloud only after its revenue/SLO
  gate.
- Per-tenant concurrency, model, budget, sandbox, and rate quotas with weighted fairness.
- Separate projects/security boundaries for first-party control plane and untrusted customer code.

Cloud Run worker pools do not natively scale from workflow backlog. For a Temporal deployment, initially keep a
small fixed pool. If dynamic scaling becomes worthwhile, use a measured external scaler; Google now documents
its CREMA external-metrics pattern, while GKE can use KEDA's Temporal task-queue scaler. Do not invent another
durable queue merely to trigger serverless compute. [Cloud Run worker-pool scaling](https://docs.cloud.google.com/run/docs/deploy-worker-pools),
[CREMA](https://docs.cloud.google.com/run/docs/configuring/workerpools/crema-autoscaling),
[KEDA Temporal scaler](https://keda.sh/docs/2.20/scalers/temporal/)

### Hundreds to thousands

- Split interactive, research, build, browser/QA, deploy, and low-priority maintenance task queues.
- Autoscale only from queue latency/backlog, saturation, and SLO signals; preserve minimum recovery capacity.
- Apply tenant-weighted admission control so one company cannot exhaust agents, model quotas, or sandboxes.
- Partition append-only audit/evidence/trace data by time and retention class.
- Add read models/caches and replicas only from measured query pressure.
- Move persistent worker and sandbox capacity to GKE Autopilot when KEDA, scheduling, networking, density,
  long-lived workspaces, or sustained cost make it a better fit than Cloud Run.

### Very large / multi-region

- Route each organization to a home regional cell.
- Each cell owns independent API capacity, workers, PostgreSQL, object storage, quotas, and blast radius.
- A small global catalog maps organization to cell; it is not in the hot path of every agent event.
- Replicate only required identity/billing/catalog metadata globally; keep workflow writes single-home.
- Move organizations between cells with an explicit export/import and quiescence protocol.
- Give regulated or high-volume tenants dedicated cells without forking the product.

Do not start with active-active workflow writes, global distributed transactions, a giant shared database,
per-agent microservices, Kafka, or a service mesh. Cells and queues give a clean path without paying that
complexity tax before demand exists.

## Reliability contract

Before public paid access, the thin vertical must prove:

- every consequential request and side effect is idempotent;
- worker/process/zone loss resumes without losing business progress;
- human/external waits survive deploys and resume only with a correlated answer;
- provider throttling and transient outages release work into durable retry/backoff without losing the work
  contract, duplicating a committed side effect, or silently exhausting a hard story timer;
- every running operation has an owned renewable lease, durable checkpoint/result, attempt history, next
  diagnostic action, and accountable escalation path; expired leases are reclaimable after process/zone loss;
- structured model actions enter an immutable tenant/run event stream as proposals, while hallucinated
  recipients, authority, evidence, or completion are rejected before side effects;
- no customer-visible phase is inferred from output silence, elapsed time, or model confidence;
- duplicate events, late results, cancellation races, and stale writers fail safely;
- one tenant cannot read, schedule, spend, or execute as another at API, policy, database, object, sandbox,
  and deployment boundaries;
- provider degradation uses bounded retry, circuit breaking, alternative routing where authorized, and honest
  customer communication;
- QA evidence is immutable and revision-bound; release requires independent verification and rollback proof;
- backup/restore, worker versioning/replay, canary, rollback, incident response, and dependency compromise are
  exercised, not documented only;
- status derives from authoritative workflow/domain state and includes last progress, current owner, reason,
  evidence, expected next update, and customer options.

The initial V2 management projection now implements the read side of the final item: it joins adaptive graph
tokens to tenant-fenced queue attempts and leases, exposes mission-scoped roles and managers, and retains bounded
agent proposals for communication, delegation, staffing, risks, and decisions. It treats “slow but owned” as a
diagnostic condition rather than a timeout. Durable periodic watches now deliver deduplicated nonterminal manager
signals, bounded persistent-condition escalation, and recovery notices. The write side is still incremental:
an immutable standing company stream now preserves authorized AI-role hiring and retirement across directives
and injects that roster into agent context. Human/vendor/team changes, automatic policy-bounded proposal
application, and contextual manager-agent repair/reassignment turns must still be completed before this is
described as the full persistent AI organization runtime. AI staffing proposals now have stable identities and
an authorized, atomic promote/reject path; automatic approval is intentionally separate from self-approval.

Initial external SLOs should be honest rather than copying the aspirational 99.99% North-Star target:

- control API/console availability: 99.9% monthly for the first paid release;
- acknowledged directive durability: no loss after API success;
- stale active-work detection: under 60 seconds for instrumented work;
- tenant-boundary violations and duplicate financial/deployment side effects: zero;
- status projection freshness: p99 under 5 seconds while healthy;
- restore objectives: explicitly measured in staging before launch.

Raise the availability target only when architecture, vendors, staffing, and an on-call rotation can support it.

## Product delivery sequence

### Gate 0 — stop multiplying the legacy

- Freeze new behavior in legacy controllers except security/correctness fixes.
- Keep the living system requirements aligned with the commercial mandate; its 2026-09-08 regeneration
  removed the contradictory “public SaaS is a non-goal” output and the keeper now regression-protects that
  mandate.
- Mark dated roadmap “built” claims as historical until current entrypoint tests prove them.
- Establish a reproducible package, lockfile, migration baseline, and architecture dependency checks.

### Gate 1 — framework-neutral spine

- Complete lifecycle, company, agent, work-contract, message, approval, artifact, and deployment domain types.
- Define `WorkflowEngine`, `AgentRuntime`, `ModelGateway`, `PolicyEngine`, `ArtifactStore`, `SandboxRunner`,
  `Deployer`, `IdentityProvider`, and `Notifier` ports.
- Build an in-memory harness and golden-history translator before connecting a durable workflow adapter.

### Gate 2 — one real vertical

- FastAPI/OIDC create-company and directive endpoints plus AG-UI event streaming.
- One PydanticAI agent family and one V2 lifecycle on the selected bootstrap workflow adapter.
- Consolidated prerequisite packet and one durable human approval/wait.
- Isolated build/QA sandbox, revision-bound evidence, signed artifact, Cloud Run deployment, health check,
  stable URL, and rollback.
- CEO workspace showing truthful progress, communications, cost, evidence, and next decision.

This gate—not a synthetic multi-agent demo—is the proof that the product exists.

### Gate 3 — first paid customer readiness

- Stripe subscription/metering/spend caps, support path, terms/privacy/AUP, data export/deletion.
- Managed production IaC, backups, restore drill, SLOs, paging, runbooks, abuse limits, and operator console.
- Staging tenant isolation, load, chaos, provider-outage, model-output, cancellation, and side-effect tests.
- Onboarding from a new browser to deployed URL without repository/operator access.

### Gate 4 — company depth

- Standing organization across directives, durable role memory, audited dynamic hiring, and communication.
- Operate/support/measure/improve loops.
- Real OAuth/MCP integration catalog, connector health, credential rotation, and capability review.
- Marketing, sales-assistance, analytics, feedback, and unit-economics loops with human gates for external or
  legally consequential actions.

### Gate 5 — scale by evidence

- Fair worker pools, GKE/KEDA only if its trigger is met, regional cells, dedicated isolation tiers, and
  enterprise BYOC/self-host profiles.
- Load targets increase from observed customer demand; no architecture is called “million-user ready” without
  results at the relevant API, workflow, sandbox, database, and generated-app dimensions.

## Current-state truth

The repository has substantial tested governance, tenancy, QA, audit, agent-role, and resilience behavior. The
V2 replacement now includes the framework-neutral lifecycle, DBOS/PostgreSQL event and command stores, arbitrary
versioned workflow graphs, authenticated control API, structured PydanticAI role runtime, and a lease-renewing
worker that fairly advances lifecycle commands and graph actions without a whole-story timeout or duplicate model
spend after a crash. Agent/decision, correlated-human, and evidence-aggregating terminal graph nodes now execute;
human/operator/completion communication has an idempotent tenant inbox and authenticated API projection. It
also has an immutable, tenant-isolated small-artifact ledger, allowlisted artifact publication tool nodes, and a
grounding boundary that persists bounded model-proposed artifacts while rejecting invented/cross-tenant evidence.
One CEO directive now durably starts a bounded mission-architect graph whose identity-free JSON proposal is
authority-validated, tenant/version stamped, and launched as an immutable child graph; detailed terminal outcomes
project idempotently to the coarse CEO lifecycle, and one authenticated view links all three states. Its fail-closed
local Docker sandbox adapter serves dedicated development/CI runners. A bounded `deploy.preview` adapter can now
publish a small, same-tenant HTML artifact through an idempotent receipt and an unguessable public capability;
the serving route applies an opaque-origin CSP sandbox with network, forms, navigation, and framing disabled.
Capabilities expire on a bounded TTL and tenant owners/operators can list and idempotently revoke them.
A fast deterministic integration contract now proves the entire authenticated CEO-prompt-to-fetchable-preview
path without provider spend; the earlier live-provider mission proof separately exercises real model grounding.
CEO cancellation now propagates into both planning and execution graphs, blocks late bootstrap/launch work, and
is race-safe and idempotent rather than being only a top-level status change.
Workers can discover due tenants through a narrow scheduling-only database role while retaining tenant-RLS claims
and round-robin fairness. It still has overlapping legacy controllers, local-host assumptions, unfinished public
infrastructure, no managed-cloud sandbox/production-deploy/general-subworkflow adapters, and no complete
new-customer-browser-to-production-URL vertical.
This is a real durable execution spine, not yet the finished platform.

Therefore:

- **Do not throw away the domain behavior or test knowledge.**
- **Do not call the legacy runtime the finished scalable spine.**
- **Do not run another all-day legacy dogfood campaign as proof of the new architecture.**
- **Do build one bounded V2 prompt-to-public-URL vertical, inject failures, and then route new runs to it.**
- **Do not claim revenue, customer readiness, million-user scale, or full company autonomy before those gates
  have direct evidence.**

## Document authority and known conflict

`NORTH-STAR.md` is the constitution. `FOUNDER-INTENT.md` and the founder's explicit current direction define
the product. This document and `RESEARCH-long-term-platform-architecture.md` define the implementation
direction. Product inventory documents remain useful inputs, but dated “built” labels require current proof.

The generated `SYSTEM-REQUIREMENTS.md` previously listed “building a public SaaS product” as a non-goal. That
conflicted with the fixed North Star's commercial bar, `PRODUCT-BLUEPRINT.md`, and the founder's explicit
commercial direction. On 2026-09-08 the authoritative standing vision and refiner contract were corrected,
then the document was regenerated through `visionkeeper.py`; the contradictory non-goal is gone. Future
changes must continue through the requirements keeper rather than hand-editing its generated sections.
