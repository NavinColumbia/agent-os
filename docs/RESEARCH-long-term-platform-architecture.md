# Agent OS long-term platform architecture

**Decision date:** 2026-09-07
**Audience:** Founder/CEO and platform engineers
**Horizon:** first 10–50 customers, then multi-region growth toward millions of users

> **2026-09-17 superseding research note:** The runtime and authorization selections below were too final.
> Broad research across durable execution, runtime assurance, delegated identity, policy standards,
> provenance, human factors, organizational reliability, privacy, formal methods, and current hyperscaler
> agent platforms changed the governing design. [ADR-002](ADR-002-intent-reconciliation-assurance.md) is now
> normative: mission intent plus bounded reconcilers sits above a replaceable workflow engine; DBOS, Temporal,
> and Restate require an empirical bakeoff; AuthZEN is the policy API; Cedar is preferred for application/effect
> policy while OPA remains infrastructure policy; the lifecycle is a milestone projection; and generic runtime,
> memory, sandbox, gateway, registry, and tracing features are treated as replaceable infrastructure rather than
> the product moat.

## Executive decision

Do not discard Agent OS, and do not continue extending its custom runtime.

Rebuild the *boundaries*, not the product, around this reference stack:

- **Modular monolith:** one versioned Python codebase with enforced domain boundaries; four deployable units rather than dozens of ad hoc services.
- **Agents:** PydanticAI behind an internal `AgentRuntime` interface, with every vendor call routed through an Agent OS-owned `ModelGateway` and capability registry.
- **Durable orchestration:** DBOS for the bootstrap profile; Temporal and Restate as growth candidates behind
  `WorkflowEngine`, selected only after replay, versioning, outage, cancellation, cost, and operational bakeoffs.
- **API and live UI protocol:** FastAPI plus AG-UI/SSE, consumed by one consolidated React/Vite console and the CLI.
- **State:** managed PostgreSQL for transactional product data, object storage for large artifacts/evidence, and an OCI registry for deployable images.
- **Identity and authorization:** managed OIDC identity with organization/tenant claims; AuthZEN at the
  application PEP/PDP boundary; Cedar for application/effect policy; OPA for infrastructure policy; local signed
  policy evaluation and PostgreSQL RLS as defense in depth. Agent identity never implies standing authority.
- **Infrastructure:** Docker/Compose for local development; Cloud Run services, worker pools and Jobs for the first production plane; GKE Autopilot only when Kubernetes-specific scheduling, sandbox density or networking is justified.
- **Infrastructure as code:** OpenTofu with encrypted remote state, not new Terraform-specific dependencies.
- **Observability:** OpenTelemetry Collector feeding Google Cloud operations backends and optional Langfuse agent traces.
- **Build/deploy:** ephemeral sandboxed Cloud Run Jobs first, Cloud Native Buildpacks/BuildKit, signed OCI artifacts, and hostname-based deployment routing; retain a GKE Job adapter for later.

This is a strangler migration. New runs move to the new runtime; existing behavior remains available until parity is proven and old runs drain. There should be no large rewrite that simultaneously changes orchestration, data, UI and QA behavior.

The concrete lifecycle replacement and its non-dual-authority migration rules are recorded in [ADR-001](ADR-001-single-authoritative-lifecycle.md).

The complete customer/product boundary, deployment profiles, agent-organization semantics, language decision,
and evidence-gated path from first customer to regional scale are normative in
[North-Star product and platform architecture](NORTH-STAR-PRODUCT-ARCHITECTURE.md). In particular, the
platform remains Python/TypeScript now: Go is reserved for a measured Kubernetes/runner control boundary and
Rust for a future privileged microVM/security boundary. A wholesale language rewrite is rejected.

The economic deployment overlay is [Pricing and cost guardrails](PRICING-AND-COST-GUARDRAILS.md): keep fixed
infrastructure at or below `$50/month` before `$500` MRR, use the existing open-source DBOS Transact adapter for
the bootstrap profile if it passes the V2 acceptance corpus, and activate Temporal Cloud only after its
revenue/SLO gate. Temporal remains the managed long-term choice; this overlay prevents buying long-term scale
before customer revenue exists.

## Open-source and GCP recheck

It is not possible to review every repository published as open source, nor would raw repository count improve the decision. This review screened the serious candidates for each required responsibility by production fit, license, durability model, operational burden, portability and migration cost. A framework was selected only where it deletes commodity Agent OS code without taking ownership of Agent OS product semantics.

| Responsibility | Adopt | Credible fallback or later option | Do not make primary |
|---|---|---|---|
| Agent/model/tool runtime | PydanticAI (MIT), behind `AgentRuntime` | Google ADK or an OpenAI SDK adapter if a customer requires it | LangChain/CrewAI/AutoGen merely to gain another abstraction |
| Durable product lifecycle | Temporal (MIT server; managed production initially) | Hatchet (MIT) if the measured spike shows materially better economics; Dapr Workflow (Apache 2.0) for a Dapr-standardized estate | Custom Postgres queues, LangGraph as a second durable engine, or Google Workflows as the core runtime |
| Experimental resumable agent substrate | No production dependency | Track Google Agent Executor/AX (Apache 2.0) and reevaluate after a stable release | AX today; its maintainers explicitly warn of breaking changes during early development |
| Agent/user and tool protocols | AG-UI and MCP; A2A only at external agent boundaries | Plain SSE/HTTP adapters | A private wire protocol for every UI and tool |
| API | FastAPI/ASGI | Another standards-compliant ASGI implementation | Standard-library HTTP servers in production |
| Infrastructure as code | OpenTofu (MPL 2.0) | Existing compatible Terraform configuration during transition | Expanding dependence on Terraform-only features |
| Identity | Managed OIDC/SCIM; Keycloak (Apache 2.0) is the self-host escape hatch | Managed ZITADEL when its B2B experience wins on operations | Building passwords, MFA, federation and recovery ourselves |
| Authorization | OPA (CNCF graduated, Apache 2.0) plus PostgreSQL RLS | Existing Cerbos PDP during policy-parity migration | Cerbos Hub as a required control plane, or UI-only/application-only tenant checks |
| Telemetry | OpenTelemetry plus cloud metrics/logs/traces; Langfuse for LLM-specific analysis when useful | Self-hosted Langfuse core | Making workflow correctness depend on a telemetry vendor |
| Supply chain | Trivy for source/image/IaC/secret scanning, CycloneDX or SPDX SBOM, Cosign signing | Syft/Grype where richer SBOM workflows are needed | Unsigned mutable deployment tags |
| Execution | Cloud Run first; GKE Autopilot/GKE Sandbox later | Firecracker for a future high-assurance tier | A permanent container per user or self-operated microVM fleet now |

PydanticAI, AG-UI, Hatchet and Temporal use permissive MIT licenses; OPA, Dapr, Cerbos, Keycloak, Trivy and Google AX use permissive Apache 2.0 licenses; OpenTofu uses MPL 2.0. ZITADEL changed its main repository to AGPL-3.0, so managed ZITADEL remains usable but should no longer be described as the permissive self-host default. Restate is source-available under BSL 1.1 rather than OSI open source and is not preferred when a permissive substitute meets the need. [PydanticAI license](https://github.com/pydantic/pydantic-ai/blob/main/LICENSE), [AG-UI license](https://github.com/ag-ui-protocol/ag-ui), [OPA project](https://www.cncf.io/projects/open-policy-agent-opa/), [Hatchet license](https://github.com/hatchet-dev/hatchet/blob/main/LICENSE), [OpenTofu license](https://github.com/opentofu/opentofu/blob/main/LICENSE), [Keycloak license](https://github.com/keycloak/keycloak/blob/main/LICENSE.txt), [ZITADEL licensing](https://github.com/zitadel/zitadel/blob/main/LICENSING.md), [Restate license](https://github.com/restatedev/restate/blob/main/LICENSE)

### GCP is not a drop-in Temporal replacement

Google Cloud Workflows is an inexpensive, multi-zone managed service for orchestrating HTTP services. At the research date it costs $0.01 per 1,000 internal steps and $0.025 per 1,000 external steps after small free allowances. It supports retries, callbacks and waits. It is excellent for bounded cloud automation such as changing a worker-pool size or invoking deployment APIs. [Google Workflows overview](https://docs.cloud.google.com/workflows/docs/overview), [pricing](https://cloud.google.com/workflows/pricing)

It is not the right source of truth for Agent OS's evolving Python lifecycle. Workflows definitions are YAML/JSON rather than application worker code, and the service has fixed limits including 128 KB workflow source, 512 KB cumulative execution data, 100,000 steps, one-year execution duration, 90-day history retention and 10,000 concurrent executions per region/project by default. Those are reasonable service-orchestration limits, but replacing Temporal with Workflows would push agent state, code version compatibility, child-run semantics and replay behavior back into Agent OS. [Google Workflows quotas and limits](https://docs.cloud.google.com/workflows/quotas)

The practical cost policy is therefore:

- Use local free Temporal during development and CI.
- Start Temporal Cloud only after the pricing policy's revenue/SLO gate; the current Essentials floor is
  $100/month.
- Record Actions per lifecycle during the vertical spike and set a cost budget.
- Keep `WorkflowEngine` independent of Temporal payload types so Hatchet can be benchmarked or adopted later without another product rewrite.
- Use Google Workflows only for small GCP control-plane automations, not CEO-prompt lifecycle truth.
- Do not self-host Temporal merely to avoid $100/month: its production Helm chart requires externally operated persistence and multiple server components, so the labor and failure risk dominate at our current team size. [Temporal Helm chart](https://github.com/temporalio/helm-charts)

Hatchet is the most credible permissive, lower-operations challenger: it is MIT licensed, Python-capable, Postgres-based for simple deployments, and provides durable tasks, event waits, fairness, rate limits, retries and a UI. Its scale evidence is primarily maintainer-reported and its operational/versioning ecosystem is younger than Temporal's. Keep it as the explicit benchmark fallback, not as another simultaneously running engine. [Hatchet overview](https://docs.hatchet.run/v1)

Dapr Workflow is also credible and Apache 2.0, but it introduces sidecars, actor placement and a state-store operational model broader than Agent OS needs. Its documentation notes state retention duties, potential workflow latency, and that workflows and activities registered in one application cannot be independently scaled. That is a poor trade for this codebase unless Agent OS adopts Dapr across the whole platform. [Dapr Workflow architecture](https://docs.dapr.io/developing-applications/building-blocks/workflow/workflow-architecture/)

Google's new Agent Executor (AX) is strategically relevant because it targets distributed isolated agent execution, event logs, snapshots and resumption on Kubernetes. It is Apache 2.0, but its repository is marked `v1alpha`, “active early development,” and warns that core protocols will have major breaking changes before stable release. Track it behind `AgentRuntime`/`SandboxRunner`; do not put paying customers on it yet. [Google AX repository](https://github.com/google/ax)

## What the repository actually contains

The repository is not an empty prototype. A static audit found:

| Measure | Finding |
|---|---:|
| Python files | 206 |
| Python lines | approximately 89,800 |
| Functions / classes | 3,015 / 76 |
| Internal dependency cycles | 4 |
| Largest cycle | 10 modules |
| Lexical SQL-operation estimate | approximately 2,038 |
| `FOR UPDATE SKIP LOCKED` call sites | 24 |
| DBOS workflow/step decorators | 7 |
| Subprocess call sites | 112 |
| SQL initialization/migration files | 77 |
| Collected pytest tests | 1,026 |
| Current singleton service locks | 17 |

The largest coupling hotspot is a ten-module cycle spanning accountability, agent requests, authority, cockpit, decision chains, the loop controller, management, orchestration, QA review and workstream views. `console` imports 45 internal modules, `loopcontroller` imports 34 and `factory` imports 25. Product artifacts are still rooted in a home-directory path in [`appregistry.py`](../scripts/appregistry.py), while the production Terraform explicitly leaves application compute unfinished in [`platform/terraform/main.tf`](../platform/terraform/main.tf).

The positive evidence matters: CI provisions PostgreSQL, applies migrations, runs a security scan and executes a broad test suite. This is therefore **a substantial tested product with an increasingly tangled architecture**, not code that should be thrown away.

## Spaghetti verdict

Agent OS has three different categories of code:

### Keep: differentiated product behavior

- Tenant, organization and authority semantics
- Budget, consent, approval and kill-switch rules
- Append-only audit/provenance intent
- QA stories, evidence, dispute and release-verdict semantics
- Product lifecycle concepts and CEO-facing progress language
- Policy intent and decision-parity tests; translate the current small Cerbos proof to OPA rather than preserving vendor-shaped policy records
- Provider-neutral goals and BYO-key support

These capabilities are above agent frameworks and represent accumulated product knowledge.

### Replace: commodity infrastructure implemented locally

- Custom workflow checkpoint/recovery logic
- Custom Postgres task claiming, leases and retry machinery
- Custom inter-agent wait graph and durable reply handling
- Custom schedulers, tickers and overlapping watchdogs
- Standard-library HTTP servers and embedded HTML dashboards
- Host-process supervision as a production architecture
- Local filesystem as the authoritative product/artifact store
- Custom trace presentation and metrics backends
- General model/tool-loop mechanics that PydanticAI supplies

### Consolidate: useful code fragmented across too many places

- `loopcontroller`, `controller`, `orchestrator`, `orchestrate` and `orchestra`
- `console`, `dashboard`, `cockpit`, `frontdoor` and status views
- `scheduler`, `dispatcher`, `jobd`, `ticker`, `watchdog` and recovery services
- Database calls scattered through domain, UI, scheduler and agent code

The problem is not simply file size. The problem is that UI, policy, persistence, workflow transitions and subprocess execution can depend directly on each other, so a change has a large and difficult-to-predict blast radius.

## Why Temporal, not LangGraph or more DBOS

LangGraph is more reliable than our custom controller for generic graph checkpointing and human interruption. Its persistence layer checkpoints state for recovery, human-in-the-loop operation and replay. The LangGraph Agent Server also separates API and worker pools and supports horizontal scaling. However, its production platform would introduce a second agent-specific server, checkpoint database, queue and Redis layer, while production standalone deployment requires a platform license key. The OSS library is MIT licensed, but the library and production server are different decisions. [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence), [Agent Server architecture](https://docs.langchain.com/langsmith/agent-server), [standalone deployment requirements](https://docs.langchain.com/langsmith/deploy-standalone-server)

DBOS is a good lightweight Postgres-backed engine, but the repository has adopted it only narrowly while continuing to build another runtime. In self-hosted distributed operation, executor recovery must be configured and coordinated deliberately. That is acceptable for smaller deployments, but we should not preserve two engines merely because DBOS is already installed. [DBOS production recovery](https://docs.dbos.dev/production/workflow-recovery)

Temporal is the strongest fit for the outer product lifecycle because it supplies durable replay, long waits, task queues, retries, timers, message passing, child workflows and worker-code versioning. Its documented worker pinning, gradual ramp and rollback matter when workflows outlive application deployments. Temporal's server and SDK ecosystem are open source; managed Temporal Cloud removes an operational cluster from our small team. Temporal documents applications with millions to billions of workflow executions, although that is a platform capability—not proof that Agent OS itself has reached that load. [Temporal workflow execution](https://docs.temporal.io/workflow-execution), [message passing](https://docs.temporal.io/encyclopedia/workflow-message-passing), [worker versioning](https://docs.temporal.io/production-deployment/worker-deployments/worker-versioning), [MIT license](https://github.com/temporalio/temporal/blob/main/LICENSE)

At the research date, Temporal Cloud Essentials starts at $100/month and includes one million Actions. That is reasonable insurance for the system's most consequential execution state, but Action counts must be budgeted because retries, timers and signals are billable operations. [Temporal pricing](https://temporal.io/pricing)

### Division of responsibility

Temporal owns *whether and when work runs*. PydanticAI owns *how an agent talks to a model and tools*. Agent OS owns *what the work means and what is allowed*.

Do not run LangGraph, DBOS and Temporal as nested durable engines for the same lifecycle. If a future customer-facing visual graph editor genuinely requires LangGraph, execute that bounded graph as an activity behind `AgentRuntime`; Temporal remains the source of lifecycle truth.

## Why PydanticAI

PydanticAI supplies provider-neutral model interfaces, tools, MCP, typed results, approvals, multi-agent patterns, observability and first-party Temporal durability. The Temporal integration moves model and tool I/O into durable activities. It also supports OpenAI, Anthropic, Gemini, Bedrock, OpenRouter, Ollama and multiple OpenAI-compatible providers, which fits Agent OS better than binding the whole product to one model vendor. [PydanticAI providers](https://pydantic.dev/docs/ai/models/overview/), [Temporal durability](https://pydantic.dev/docs/ai/capabilities/durable_execution/temporal/)

PydanticAI is a younger dependency: V1 arrived in 2025 and stable V2 in June 2026. Therefore:

- Pin an exact compatible minor series.
- Put it behind an Agent OS-owned `AgentRuntime` protocol.
- Persist our normalized messages, decisions, usage and artifacts—not opaque framework objects alone.
- Add provider contract tests and a small framework-upgrade replay corpus.

[PydanticAI's policy](https://pydantic.dev/docs/ai/project/version-policy/) promises no intentional breaking changes in minor releases and removes deprecated features only at a major release, but V2 followed V1 within a year and the next major is constrained only to be at least three months after V2. Beta APIs and some message, event and telemetry shapes can change in minor versions. The framework is useful, but it is not a five-year public contract for Agent OS.

## Five-year longevity and replacement strategy

The architecture must assume that every AI model and at least one selected framework will be replaced during the next five years. Longevity comes from owning stable contracts and data, not from predicting which vendor will win.

| Stability tier | Production policy | Examples in this decision |
|---|---|---|
| Durable foundations | Prefer mature GA services and standards; permit direct use behind infrastructure repositories/adapters | PostgreSQL, OCI images, OIDC/SCIM, HTTP/SSE, OTLP, Cloud Run, Cloud SQL and object storage |
| Replaceable engines | Adopt one engine per responsibility; keep domain transitions outside it; pin versions and rehearse upgrades | Temporal, PydanticAI, OPA and AG-UI |
| Volatile capabilities | Select per task through configuration; never make a model ID or provider response type part of the domain schema | OpenAI, Anthropic and Gemini models, embeddings, native tools and provider-hosted caches |
| Experimental systems | Evaluate in isolated spikes only; no customer-critical state or required deployment path | Google AX, preview agent sandboxes and provider-hosted agent builders |

### Vendor durability scorecard

Popularity alone is not safety, but project age, contributor diversity, neutral governance, production adoption, license, paid-service leverage and replaceability together are useful signals. GitHub stars are treated only as a weak adoption proxy, never as an SLA.

| Dependency | Durability judgment | Principal risk | Binding control |
|---|---|---|---|
| PostgreSQL | Very high: multi-decade community governance and a liberal license; the project states its intention to remain free/open source in perpetuity | Managed-database price or proprietary extensions | Portable SQL/schema, standard dumps, periodic restore outside the primary service; avoid Cloud SQL-only business logic |
| OCI, HTTP/OpenAPI, OIDC/SCIM, OTLP | Very high: standards rather than one product | Individual implementation differences | Conformance tests and at least one alternative implementation |
| OpenTelemetry | Very high: CNCF graduated in 2026 with broad multi-company governance/adoption | Collector/exporter churn | Emit standard OTLP; pin collector and exporter versions; backend is optional to execution |
| OpenTofu | High: Linux Foundation stewardship, public technical governance and MPL 2.0 | Provider compatibility lag | Keep modules conventional, archive provider binaries and preserve generated plans/state backups |
| OPA | High: created in 2015, CNCF graduated since 2021, Apache 2.0, broad production adopter list | Rego expertise and application list-filtering work | Small typed `PolicyEngine` request/result contract, policies and tests in Git, PostgreSQL RLS underneath |
| Temporal | High engine maturity; medium managed-service commercial risk | Cloud Action/storage prices, operational cost of self-hosting, workflow-history lock-in | MIT server escape, thin workflow shells, exported business/audit state, Hatchet benchmark, contractual price review at scale |
| FastAPI | High adoption; medium maintainer-concentration risk | Framework/API churn | ASGI/OpenAPI boundary, ordinary Pydantic DTOs, no business rules in route decorators |
| PydanticAI | Medium: rapidly adopted and actively maintained, but only two years old and V2 is recent | Major-version churn or future commercial/licensing direction | Existing MIT release archive, exact pin, `AgentRuntime`/`ModelGateway`, normalized data and golden evals |
| AG-UI | Medium: MIT and substantial early interest, but only about one year old | Protocol evolution or ecosystem fragmentation | Treat as an edge adapter over plain SSE/domain events; browser never owns workflow truth |
| Cerbos | Medium: sound Apache 2.0 PDP, but smaller contributor base and paid Hub pricing creates commercial leverage | Startup/control-plane pricing and policy-format dependency | Do not require Hub; migrate the current 35-line policy proof to OPA with decision-parity tests |
| Langfuse | Medium and optional | License/hosting/pricing change or outage | OTLP remains authoritative interface; execution never waits on Langfuse |
| OpenAI, Anthropic, Gemini | High vendor continuity but high model/API churn and pricing leverage | Retirement, price/capacity changes and behavioral drift | `ModelGateway`, immutable IDs, two qualified providers, eval-gated routing and customer-plan cost limits |
| Google AX and other previews | Low today | Breaking protocols, abandonment or forced migration | Research watch only; no production dependency |

[PostgreSQL license commitment](https://www.postgresql.org/about/licence/), [PostgreSQL governance](https://www.postgresql.org/about/governance/), [OpenTelemetry graduation](https://www.cncf.io/projects/opentelemetry/), [OpenTofu governance](https://github.com/opentofu/org/blob/main/GOVERNANCE.md), [OPA maturity](https://www.cncf.io/projects/open-policy-agent-opa/), [OPA adopters](https://github.com/open-policy-agent/opa/blob/main/ADOPTERS.md), [Cerbos pricing](https://www.cerbos.dev/pricing)

This scorecard makes one change to the earlier recommendation: **OPA becomes the strategic policy engine; Cerbos is retained only until the existing proof is translated and decisions match.** OPA has the stronger neutral-governance and adoption profile, while the current repository has only one 35-line Cerbos policy and does not enforce Cerbos across the full product. This is the inexpensive point to change.

Google's general Cloud terms currently provide at least 12 months' notice before discontinuing a covered GA service or making a significantly backwards-incompatible customer-facing API change, subject to replacement and legal/security/economic exceptions; the promise explicitly excludes pre-GA functionality. This makes GA GCP primitives a reasonable primary platform, not a reason to bind the product to every Vertex or preview abstraction. [Google Cloud terms](https://cloud.google.com/terms), [covered services](https://cloud.google.com/terms/deprecation)

Temporal is the most durable of the shortlisted workflow engines, but it is not maintenance-free. Temporal publishes Semantic Versioning, support across SDK/server versions, support for the last three server minor versions, at least 12 months of major-version maintenance and at least six months' EOL notice. Its 2026 releases also demonstrate normal API retirement and schema-upgrade work. Use Temporal Cloud initially, upgrade on a controlled cadence, and keep deterministic workflow shells thin. [Temporal versions and support](https://github.com/temporalio/documentation/blob/main/docs/encyclopedia/temporal-service/temporal-server.mdx), [Temporal releases](https://github.com/temporalio/temporal/releases)

### Model and provider policy

Model providers are materially less stable than the cloud primitives underneath them:

- OpenAI says its first-party SDKs follow semantic versioning and that it tries to avoid breaking changes in the versioned API, while also warning that model behavior changes and recommending pinned model versions plus evals. Its current deprecation policy provides at least six months for generally available models, three months for specialized variants, and potentially about two weeks for previews. [OpenAI API stability](https://developers.openai.com/api/reference/overview), [OpenAI deprecations](https://developers.openai.com/api/docs/deprecations)
- Anthropic identifies current model IDs as pinned snapshots, but serving infrastructure may still alter observable behavior. Publicly released models receive at least 60 days' retirement notice, and lifecycle dates can differ among the direct API, Bedrock and Google Cloud. [Anthropic model versioning](https://platform.claude.com/docs/en/about-claude/models/model-ids-and-versions), [Anthropic deprecations](https://platform.claude.com/docs/en/about-claude/model-deprecations)
- Google's current Gemini tables generally give recent stable generation models at least 12 months from release, while the older Vertex generative modules were deprecated and removed on a one-year schedule. Model retirement is routine even when the GCP foundation remains stable. [Gemini model lifecycle](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/model-versions), [Vertex SDK deprecation](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/deprecations)

Implement a small Agent OS-owned `ModelGateway` below `AgentRuntime` with these domain-neutral records:

```text
ModelRequest     capability, normalized messages, tool schemas, policy, budget
ModelResult      normalized parts, tool calls, usage, finish class, provenance
ModelRoute       provider, endpoint, immutable model ID, region, fallback policy
CapabilityTest   eval suite, required tools/modalities, latency/cost/error budgets
```

Provider-specific request builders and SDK objects end at the gateway adapter. Store prompts and eval datasets in version control or Agent OS storage, not only in hosted prompt/eval/agent-builder products. Persist immutable provider/model identifiers on every run, but choose them from configuration rather than business code.

Use direct first-party GA APIs through PydanticAI at launch: OpenAI Responses, Anthropic Messages and Google Gen AI/Vertex adapters. Do not add LiteLLM, OpenRouter or a second gateway to the hot path merely for theoretical portability; that adds another outage and semantic translation layer. Add one only when BYO-provider demand, centralized rate control or measured routing economics justify it.

Qualify at least two providers for every revenue-critical capability. This is not blind mid-run failover—models are not behavior-equivalent. Route new attempts to a pre-evaluated fallback, preserve the failed attempt's provenance, and require human review when the task's risk class demands it.

### Upgrade and exit discipline

- Use only GA cloud features and GA model families on customer-critical paths. Preview features run behind disabled-by-default flags with no authoritative state.
- Pin Python dependencies in `uv.lock`, container images by digest and model snapshots/immutable IDs where offered. Never auto-upgrade production to a `latest` alias.
- Run a monthly dependency/deprecation review and a quarterly upgrade train. Security fixes can accelerate the train; feature releases cannot bypass it.
- Before changing a model, agent framework or workflow worker, run golden task evals, Temporal replay tests, provider contract tests and shadow traffic; then canary by tenant and retain rollback.
- Keep workflow orchestration as a thin shell around application commands and events. Temporal payloads, retry types and decorators must not enter domain/application records. A future move to Hatchet or another durable engine still requires rewriting workflow shells, but not product rules, tenant data, prompts, tools, API or audit history.
- Maintain one tested exit path per material vendor: OCI images plus OpenTofu for compute, standard PostgreSQL dumps and migrations for data, S3-compatible `ArtifactStore`, OIDC/SCIM for identity, OTLP for telemetry, and provider-neutral model records for AI.
- Do not build or operate active-active multi-cloud early. Portability is an adapter-and-data property; duplicate clouds would consume the engineering and gross margin needed to find product-market fit.

### Profitability guardrail

Architecture cannot make the company valuable by itself. It can prevent scale and AI costs from destroying the business. Every paid run must record model tokens/cost, Temporal Actions, sandbox compute, storage and support-visible failures against `organization_id`, plan and build. Admission control enforces tenant concurrency and spend budgets before expensive work starts. A plan is not sellable unless its expected gross margin remains positive under retry and QA p95, not only the happy-path demo.

Start with scale-to-zero Cloud Run services/Jobs, one small worker pool and managed operations. Avoid Kubernetes, multi-cloud, self-hosted Temporal and a large observability estate until workload or paying-customer requirements cross explicit gates. This keeps the initial fixed platform bill small while preserving the path to regional cells and GKE.

### Commercial-vendor accountability

Open-source escape paths reduce leverage but do not control managed-service prices. Google's standard terms, for example, allow fee revisions even though covered GA-service deprecations receive notice. No cloud or AI vendor should be trusted on brand alone. [Google Cloud terms](https://cloud.google.com/terms)

Before any vendor becomes material, require or negotiate, proportional to spend:

- An SLA and support-response schedule tied to customer-visible severity, with service credits.
- Advance notice for breaking changes, model/service retirement and material price increases.
- Detailed metering export, billing dispute rights, quota-increase process and budget alerts.
- Full data/configuration export in documented formats, deletion certification and exit assistance.
- DPA, subprocessor-change notice, breach-notification timing, security reports and data-residency commitments.
- Rights to use independent/open adapters and to continue operating archived OSS releases.
- No multi-year minimum commitment until usage is predictable; later commitments require price protection and termination rights.

Operational triggers make this enforceable rather than aspirational:

- A new license, ownership change, deprecation notice or greater-than-20% effective unit-price increase opens an architecture review automatically.
- A non-model vendor reaching either 10% of monthly revenue or a credible two-times self-host/alternative estimate triggers an exit benchmark before renewal.
- A provider that cannot export our data/configuration or pass a twice-yearly replacement drill cannot hold authoritative state.
- Mirror approved source tarballs, wheels and OCI images by digest in our registry; generate an SBOM and license inventory for every release. Existing grants cover the archived version under its published terms even if a maintainer licenses future releases differently; counsel should confirm any unusual license before distribution.
- Review vendor health quarterly: releases, security response, maintainer diversity, ownership, roadmap, pricing, deprecations and our measured switching time.

The current repository does **not** yet satisfy this policy: only one of 12 active Python requirement lines is exactly pinned, nine are unpinned, `uv.lock` is absent, and the Cerbos and ntfy Compose services use `latest` image tags. Stage 1 must replace these with a committed lockfile and digest-pinned production images before any public deployment. Development aliases may exist only if CI resolves and records their immutable digests.

## Target logical architecture

```text
 Browser console        distributable CLI        public REST API
        \                     |                       /
         +--------- Gateway / OIDC / quotas --------+
                               |
                 Cloud Run FastAPI control API
                   (stateless, scale-to-zero)
                               |
            +------------------+------------------+
            |                  |                  |
       PostgreSQL         Temporal Cloud      Object storage
     product metadata    workflow history    source/evidence/blobs
            |                  |
            |          versioned worker pools
            |       orchestrate / agent / QA / deploy
            |                  |
            |          sandbox-job controller
            |                  |
            |          ephemeral Cloud Run Job
            |          no ambient credentials
            |                  |
            +------- signed static/OCI artifact
                               |
                   deployment and route controller
                               |
              CDN / Cloud Run service / customer URL

 All components -> OpenTelemetry Collector -> Langfuse + metrics/logs
```

There are four deployable product units, even though worker pools can use the same versioned image:

1. Static web console.
2. FastAPI control API.
3. Temporal worker image, initially one Cloud Run worker pool and later split by task-queue role.
4. Sandboxed build/QA/deployment Cloud Run Job images, with a Kubernetes `SandboxRunner` implementation available later.

This is a modular monolith: one release train and shared domain contracts, but no circular imports and no in-process access from the UI to arbitrary infrastructure code.

## Codebase boundaries

Move toward this package layout without changing behavior all at once:

```text
src/agent_os/
  domain/          # entities, decisions, invariants; imports no infrastructure
  application/     # use cases and ports/interfaces
  workflows/       # deterministic Temporal workflow definitions
  agents/          # PydanticAI agents, prompts, tools and normalized results
  qa/              # stories, evidence, verdicts
  infrastructure/  # Postgres, OIDC, OPA, storage, Kubernetes, providers
  api/             # FastAPI routes and AG-UI adapters
  cli/             # remote client and local-development commands
```

Enforce dependencies in CI:

```text
api / cli / workflows -> application -> domain
infrastructure -----------------------> application ports
domain must never import infrastructure, API, UI or workflow modules
```

Adopt `pyproject.toml`, `uv.lock`, Ruff, a type checker and import-boundary contracts. The current mostly-unpinned `requirements.txt` is not reproducible enough for production. `uv` performs exact synchronization from a committed cross-platform lockfile. [uv locking and syncing](https://docs.astral.sh/uv/concepts/projects/sync/)

## API, console and CLI

Replace the standard-library `HTTPServer` implementations with FastAPI/ASGI. Keep one versioned API consumed by all interfaces:

- `POST /v1/organizations/{org}/builds`
- `GET /v1/builds/{id}`
- `GET /v1/builds/{id}/events`
- `POST /v1/builds/{id}/messages`
- `POST /v1/approvals/{id}:decide`
- `POST /v1/deployments`
- `GET /v1/deployments/{id}/logs`

Use AG-UI for interactive agent events rather than inventing another chat-stream protocol. PydanticAI's adapter supports messages, state, tools, custom events and approval interrupts streamed over SSE. Authorization remains server-side; UI approval events are not themselves an authorization decision. [PydanticAI AG-UI integration](https://pydantic.dev/docs/ai/integrations/ui/ag-ui/)

The CLI is a client of the same API, not a second orchestrator:

```text
agent-os login
agent-os build "create ..."
agent-os watch BUILD_ID
agent-os approve APPROVAL_ID
agent-os deploy BUILD_ID
agent-os logs DEPLOYMENT_ID
agent-os local up
```

`agent-os local up` may use Docker Compose and a development Temporal server. Cloud mode must not require Docker or model credentials on the customer's laptop.

## Identity, authorization and tenant isolation

Do not promote the custom token/auth implementation into the public identity provider. Use standards-based OIDC and bind `user_id`, `organization_id`, roles and plan to a verified server-side tenant context.

Use a managed OIDC provider initially because account recovery, MFA, federation and identity abuse are security-sensitive operational responsibilities. ZITADEL Cloud remains a good B2B option because organizations are first-class tenants, but its main self-hosted repository is now AGPL-3.0. Keep application code provider-neutral. If a permissively licensed self-host deployment becomes necessary, Keycloak is Apache 2.0 and now supports organization membership, invitations, organization-specific identity providers and organization claims. [ZITADEL organizations](https://zitadel.com/docs/guides/manage/console/organizations-overview), [ZITADEL licensing](https://github.com/zitadel/zitadel/blob/main/LICENSING.md), [Keycloak organizations](https://www.keycloak.org/docs/latest/server_admin/index.html#_managing_organizations)

Use OPA for action/resource authorization behind the Agent OS `PolicyEngine` contract. OPA is a CNCF-graduated, Apache 2.0 project with documented production adopters, public governance, a third-party security audit, REST/Go/Wasm integration and over a decade of project history. Start with a small shared service for the first Cloud Run plane; move evaluation to sidecars or Wasm only when latency and availability measurements justify it. Translate the current Cerbos policy and run both engines against a shared decision corpus until parity, then remove Cerbos from the required stack. [OPA project](https://www.cncf.io/projects/open-policy-agent-opa/), [OPA repository and security evidence](https://github.com/open-policy-agent/opa), [OPA adopters](https://github.com/open-policy-agent/opa/blob/main/ADOPTERS.md)

Keep PostgreSQL RLS as defense in depth, but always connect the application through a non-owner, non-`BYPASSRLS` role and test every tenant table. PostgreSQL documents that table owners and superusers normally bypass RLS. [PostgreSQL row security](https://www.postgresql.org/docs/current/ddl-rowsecurity.html)

Every durable identifier and event must contain or resolve to an immutable `organization_id`. Tenant context must not be accepted from an arbitrary request body when it can be derived from authenticated identity. AWS's SaaS guidance likewise recommends first-class tenant context, automated onboarding, isolation across layers and tenant-specific load/isolation testing. [AWS SaaS design principles](https://docs.aws.amazon.com/wellarchitected/latest/saas-lens/general-design-principles.html)

## Data and migrations

- Use a managed, highly available PostgreSQL instance with point-in-time recovery.
- Keep transactional product state in PostgreSQL.
- Move video, screenshots, repositories, build contexts and other large blobs out of PostgreSQL into versioned object storage; retain content hashes and tenant metadata in PostgreSQL.
- Introduce Alembic revision history from a baselined schema instead of relying on fresh-volume initialization scripts as the deployment mechanism. Alembic provides versioned relational change scripts. [Alembic tutorial](https://alembic.sqlalchemy.org/en/latest/tutorial.html)
- Do not perform a wholesale ORM rewrite. Keep critical SQL visible, but route it through bounded repositories and transactions.
- Use an outbox table for domain events crossing subsystem boundaries; do not add Kafka/NATS until measured fan-out or retention requirements exceed the outbox/Temporal model.

For eventual very large scale, partition by time for append-only traces/audit/evidence and adopt regional cells. Do not prematurely shard the first production database. Decide the tenant-to-cell mapping before the first actual shard so routing remains explicit.

## Cloud Run, Kubernetes and sandbox decision

Docker defines images. Cloud Run or Kubernetes schedules and replaces them. Temporal coordinates business progress. These responsibilities must remain separate.

### First production plane: Cloud Run

Start the control API as a Cloud Run service, the continuously polling Temporal worker as a small Cloud Run worker pool, and build/QA work as Cloud Run Jobs. Cloud Run worker pools reached GA in April 2026; do not enable their separate preview-only features on the critical path. Google documents all Cloud Run resource types as sandboxed containers, and specifically publishes a reference architecture for multi-tenant platforms running customer-supplied code. Instances have a hardware-backed VM boundary plus a software sandbox; Jobs have no ingress, can run as many as 10,000 parallel tasks, and each task can run for up to seven days. [Cloud Run release notes](https://docs.cloud.google.com/run/docs/release-notes), [execution model](https://docs.cloud.google.com/run/docs/overview/what-is-cloud-run), [multi-tenant untrusted-code guidance](https://docs.cloud.google.com/run/docs/securing/multi-tenant), [security design](https://docs.cloud.google.com/run/docs/securing/security), [Job limits](https://cloud.google.com/run/docs/create-jobs)

This is both simpler and cheaper than operating Kubernetes on day one. Request-serving services can scale to zero, Jobs are pay-per-use, and Google's current worked example estimates one continuously running 1-vCPU/512-MiB worker at $11.61/month in `europe-west1` after its stated free-tier assumptions. Treat that number as an example rather than a budget quote; region, resource allocation, network, storage, databases and model calls are separate. Cloud Run's published non-GPU service SLO is 99.95% in most regions. [Cloud Run pricing](https://cloud.google.com/run/pricing), [Cloud Run SLA](https://cloud.google.com/run/sla)

Keep first-party control-plane workloads in separate GCP projects from untrusted customer workloads. For production tenant code, follow Google's recommendation of a project-level security boundary and automate project allocation from a pre-created pool. “Tenant” here means a paying customer organization or another explicit trust boundary, not every registered end user. Never interpret “container” alone as a sufficient tenant boundary. [Cloud Run multi-tenant project model](https://docs.cloud.google.com/run/docs/securing/multi-tenant)

### Kubernetes escalation path

Adopt GKE Autopilot only when measurements or a concrete feature require Kubernetes APIs: higher sandbox density, long-lived resumable workspaces, specialized node pools, richer network policy, custom schedulers, or sustained workloads whose Cloud Run economics are worse. At that point use:

- Kubernetes `Deployment` for API and worker replicas.
- Kubernetes `Job` for bounded sandbox/build/QA work.
- HPA for API CPU/request metrics.
- Queue/Temporal metrics for worker autoscaling.
- Cluster autoscaling for sandbox capacity.
- KEDA only when event-driven scale-to-zero or one-job-per-event materially reduces cost; it is not required for correctness.

Kubernetes distinguishes long-running Deployments from run-to-completion Jobs and can retry Jobs after pod/node failure. [Kubernetes workload management](https://kubernetes.io/docs/concepts/workloads/controllers/), [Jobs](https://kubernetes.io/docs/concepts/workloads/controllers/job/)

### Later Kubernetes reference

Use **GKE Autopilot** and the established **GKE Sandbox** runtime when the escalation gate is met. The managed path is decisive: GKE documents gVisor specifically for unknown or untrusted code. Google's newer Agent Sandbox feature documents additional guardrails—no service-account token, non-root execution, dropped capabilities, CPU/memory limits, and no host namespaces, privileged mode or host paths—but its current setup still uses a beta CLI. Treat Agent Sandbox as an evaluation target until it is generally available; enforce the same controls ourselves in the meantime. [GKE Sandbox](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/sandbox-pods), [GKE Agent Sandbox](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/how-install-agent-sandbox)

This recommendation changes the unfinished AWS Terraform reference, but not deployed production compute—there is none to migrate. Keep application contracts cloud-neutral through OCI, OIDC, OTLP, S3-compatible storage interfaces, `SandboxRunner`/`Deployer` ports and OpenTofu modules.

Firecracker offers a stronger microVM isolation model and should remain the later high-assurance/dedicated-runner option. Operating Firecracker safely requires host hardening, KVM, jailer setup, egress filtering and patching; adopting it ourselves before customers would create a security platform inside the product. [Firecracker design](https://github.com/firecracker-microvm/firecracker/blob/main/docs/design.md)

### Mandatory sandbox controls

- New sandbox per build or trust boundary; never execute generated code in API/Temporal worker processes.
- No ambient cloud or Kubernetes credentials.
- Read-only base image, ephemeral writable volume, explicit CPU/memory/PID/time limits.
- Default-deny ingress and egress; proxy/allowlist only required package registries and test targets.
- Tenant-scoped artifact upload credential with short expiry.
- Secrets supplied only to the exact step requiring them, never to arbitrary build code.
- Destroy environment after artifact/evidence upload.
- Capture logs, resource usage, exit status and artifact digest.

## Build, artifact and deployment pipeline

```text
source snapshot
  -> sandboxed dependency/build step
  -> tests and QA
  -> SBOM + vulnerability/secret scan
  -> static bundle or OCI image
  -> immutable digest
  -> Cosign signature/attestation
  -> deployment policy gate
  -> progressive rollout
  -> health verification
  -> stable hostname
```

Cloud Native Buildpacks can detect source and produce a runnable OCI image; retain BuildKit/Dockerfile fallback for unsupported applications. Use Trivy to scan source, dependencies, secrets, IaC and images and emit a CycloneDX or SPDX SBOM. Cosign supports signing and verification in OCI registries. [Buildpacks build lifecycle](https://buildpacks.io/docs/for-app-developers/concepts/build/), [Trivy](https://github.com/aquasecurity/trivy), [Cosign](https://github.com/sigstore/cosign)

Static applications should publish to object storage/CDN. Backend applications should initially deploy as separate versioned Cloud Run services per app/environment—not as one container per Agent OS user. Route by hostname through the external application load balancer; customer application traffic must not pass through the CEO workflow controller.

Use OpenTofu for the Cloud Run platform. If the GKE escalation gate is reached, use Argo CD for stable cluster services because it treats version-controlled declarative configuration as desired state. Do not create a Git repository and Argo application for every short-lived sandbox Job; the workflow and sandbox adapters should create those directly. [OpenTofu migration compatibility](https://opentofu.org/docs/intro/migration/), [Argo CD](https://argo-cd.readthedocs.io/en/stable/)

## Observability and operational truth

Deploy an OpenTelemetry Collector so application code exports standard OTLP rather than binding directly to a vendor. The collector can process and route data to multiple backends. Initially route conventional logs, metrics and traces to Google Cloud's managed operations backends; add Langfuse for model/tool traces and agent evaluations without making it part of the execution path. [OpenTelemetry Collector](https://opentelemetry.io/docs/collector/)

Use:

- Langfuse for model/tool traces, token/cost analysis and agent evaluations.
- Prometheus-compatible metrics and Grafana dashboards for infrastructure/SLOs.
- Loki or the cloud logging service for logs.
- Tempo or the cloud trace backend for non-LLM distributed traces if needed.
- Existing append-only Agent OS audit records for business accountability; observability data is not the legal/audit source of truth.

Langfuse's core is MIT-licensed/self-hostable and its current SDK/ingestion path is based on OpenTelemetry. Start managed or as a noncritical optional profile; do not make build execution depend on Langfuse availability. [Langfuse self-hosting](https://langfuse.com/faq/all/self-hosting-langfuse), [compatibility](https://langfuse.com/docs/compatibility)

Every trace/log/metric/event should carry `organization_id`, `workflow_id`, `build_id`, `deployment_id`, `agent_id`, model/provider, attempt and code version—excluding secrets and unredacted customer content.

## Scaling model

“Millions of users” separates into four independent dimensions:

1. Registered users and organizations.
2. Concurrent API/chat sessions.
3. Concurrent agent workflows and model requests.
4. Concurrent untrusted sandboxes plus traffic served by generated apps.

Scale and quota each independently. A million registered users might create only hundreds of concurrent builds; one customer can also create a damaging workload without fair-use limits.

### Stage A: 10–50 customers

- One region, multi-zone managed services.
- Cloud Run service for API/UI streaming and Cloud Run Jobs for build/QA. Use DBOS Transact before the managed
  workflow cost gate; after it, add one small Temporal worker pool.
- Separate first-party and untrusted-workload projects; pre-create tenant project capacity before it is needed.
- One HA PostgreSQL instance and one object/OCI storage region.
- Per-tenant concurrency, token, spend and sandbox quotas.
- Temporal Cloud Essentials or Business only after the revenue/SLO gate and according to measured Action
  volume and support needs.

### Stage B: hundreds to thousands of customers

- Separate interactive, research, build, browser/QA and deployment worker pools.
- Autoscale API and workers from request/queue metrics.
- Introduce GKE Autopilot only if Cloud Run limits, features or sustained-cost measurements justify it.
- Read replicas/caches only for measured read pressure.
- Dedicated sandbox capacity pools and optional premium-tenant isolation.
- Partition high-volume append-only tables and enforce retention.

### Stage C: very large/multi-region

- Global edge routes each organization to a home region/cell.
- Each cell has independent API, workers, PostgreSQL and object-storage boundaries.
- Central identity/billing/control catalog maps tenant to cell but is not in every workflow's hot path.
- Generated applications scale in their own data plane, independently from Agent OS orchestration.
- Premium/regulatory customers may receive siloed cells while using the same automated onboarding and management plane.

Cell boundaries limit blast radius and permit incremental capacity growth. They are preferable to betting the company on one globally shared database and one giant cluster.

## Explicit non-adoptions

Do **not** add these now:

- LangChain: PydanticAI supplies the required agent abstraction.
- LangGraph as primary orchestration: overlaps Temporal and adds a second durable state model.
- LlamaIndex/vector database: add only for a measured retrieval requirement.
- Kafka/Redpanda/NATS: use Temporal plus a transactional outbox until event throughput or independent replay consumers demand a log broker.
- Service mesh: use Cloud Run IAM/network controls or normal Kubernetes networking, TLS and policy first.
- Self-operated Firecracker fleet: use managed gVisor before building a virtualization platform.
- Per-agent/per-customer microservices: use task queues and module boundaries.
- One permanent container per customer: pool the control plane; isolate ephemeral execution and deployments according to risk/tier.

## Migration blast radius

This is a meaningful migration, but it is smaller and safer than either retaining the custom runtime indefinitely or rewriting the entire repository. Product records, policy definitions, QA semantics, audit history and most tests stay. Old workflow runs stay on their existing engine until completion; only new runs cross to Temporal after the vertical proof passes.

| Change | Relative risk | How it stays reversible |
|---|---|---|
| OpenTofu and new GCP modules | Low | Existing AWS files are unfinished and no production compute is being moved; retain provider-independent ports |
| FastAPI/AG-UI facade | Low–medium | Put it in front of existing application calls first; change the console only after API parity |
| Package boundaries and dependency locking | Medium | Compatibility imports preserve current module names while CI forbids new cycles |
| PydanticAI agent adapter | Medium | Port one agent family; normalized Agent OS messages/results remain authoritative |
| Temporal lifecycle | High | One new-workflow canary, injected-failure/replay tests, no dual-written workflow truth, drain old runs |
| Cloud Run sandbox/deploy adapter | Medium–high | Keep local Docker and later GKE implementations behind the same `SandboxRunner` and `Deployer` contracts |
| Identity provider replacement | Medium | Standard OIDC/SCIM claims and server-side tenant resolution; no provider objects in domain records |

The crucial rule is **do not translate every existing `if/else` into visual graph nodes**. Ordinary deterministic business rules remain normal typed code. Temporal models long-lived coordination, retries, waits and compensation. PydanticAI handles nondeterministic model/tool loops. This avoids creating graph spaghetti in place of Python spaghetti.

## Migration sequence and proof gates

### 0. Preserve the baseline

- Stop adding new orchestration paths.
- Record the current 1,026-test collection and identify the production-critical subset.
- Capture golden lifecycle histories and expected audit/QA outcomes.
- Document authoritative schemas and running services.

**Gate:** baseline tests reproducibly run from a clean checkout with no home-directory assumptions.

### 1. Make the code a reproducible package

- Add `pyproject.toml`, `uv.lock`, `src/agent_os`, Ruff/type checks and architecture contracts.
- Move modules behind compatibility imports; do not rewrite logic yet.
- Baseline the existing database into Alembic.

**Gate:** clean install, migrations and tests are deterministic; new dependency cycles fail CI.

### 2. Establish ports and the new API

- Define `AgentRuntime`, `WorkflowEngine`, `ArtifactStore`, `SandboxRunner`, `IdentityProvider`, `PolicyEngine` and `Deployer` protocols.
- Put existing implementations behind adapters.
- Add FastAPI endpoints and AG-UI streaming while the current controller still executes work.

**Gate:** console and CLI perform the same lifecycle through the versioned API; UI imports no database or subprocess modules.

### 3. Prove PydanticAI + Temporal vertically

- Port one bounded but real CEO prompt through research, implementation, QA, approval and delivery.
- Store large tool/media outputs in object storage, passing references through Temporal history.
- Inject failures at worker restart, model timeout, duplicate request, approval wait and deploy rollback.
- Measure latency, model cost, Temporal Actions, checkpoint/history growth and result parity.

**Gate:** recovery and audit semantics pass; no duplicate external side effects; cost/latency are within defined budgets.

### 4. Introduce the managed platform

- OpenTofu modules for Cloud Run, Cloud SQL/PostgreSQL, object storage, registry, KMS, networking, OTel Collector and secrets integration.
- Package API, worker-pool and Job images; retain cloud-neutral runtime ports.
- Add managed OIDC and connect organization claims to OPA/RLS.

**Gate:** a clean environment can be created, migrated, smoke-tested and destroyed from versioned automation.

### 5. Complete isolated build-to-URL delivery

- Run build and QA in Cloud Run Jobs with first-party and untrusted projects separated.
- Produce signed static/OCI artifacts.
- Deploy progressively to Cloud Run, verify health and attach hostname/TLS.
- Stream understandable status and evidence to the console.

**Gate:** an authenticated tenant prompt yields an isolated, traceable, rollback-capable public URL without SSH or manual port forwarding.

### 6. Strangle the old runtime

- Start all new workflows on Temporal.
- Allow old DBOS/custom runs to drain; do not dual-write workflow truth.
- Compare state/audit projections during a defined observation window.
- Delete obsolete scheduler, dispatcher, ticker, wait and host-supervisor paths only after parity.

**Gate:** no active old-engine workflows, rollback snapshot verified, critical tests/load/chaos/isolation checks green.

## Production reliability gates

Do not equate “runs in managed containers” with “ready for millions.” Before paid public access, require:

- Published API and console SLOs, initially at least 99.9% monthly availability.
- Recovery-point and recovery-time objectives with tested PostgreSQL/object-store restore.
- Idempotency keys on every consequential request and external side effect.
- Workflow replay/version-compatibility tests before worker deployment.
- Per-tenant quotas and fairness under a noisy-neighbor load test.
- Cross-tenant access tests at API, policy, database, object store and sandbox layers.
- Sandboxed malicious-code tests and egress exfiltration tests.
- Dependency/image scanning, SBOMs, signatures and admission verification.
- Canary/rollback tests for the control plane and generated deployments.
- Alerts based on user-visible symptoms: stuck workflow age, queue latency, error budget, model/provider failure, sandbox startup, QA duration and deployment health.

## Final judgment

Agent OS has built too much commodity orchestration and too many tightly connected processes, but it has also built a large body of tested governance and QA behavior that general frameworks do not provide. The right decision is neither “keep everything custom” nor “rewrite it in LangGraph.”

The durable five-year choice is:

> **Agent OS product semantics on a modular Python codebase; PydanticAI and an owned ModelGateway for agents; Temporal for workflows; Cloud Run first and GKE only at a measured escalation point; OpenTofu, OIDC, PostgreSQL, OCI and OpenTelemetry at every replaceable boundary.**

This concentrates custom engineering on the product customers might pay for and delegates failure recovery, scheduling, sandboxing, identity, telemetry and container reconciliation to systems built specifically for those jobs. It is designed to reach the first customers economically and to scale by cells later; it does not claim that an architecture diagram alone creates product-market fit, revenue or a billion-dollar company.
