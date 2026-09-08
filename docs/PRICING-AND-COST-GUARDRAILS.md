# Agent OS pricing and infrastructure cost guardrails

**Decision date:** 2026-09-08
**Scope:** what Agent OS pays to operate, what customers pay Agent OS, and when fixed infrastructure may be activated

## First: do not mix up the two bills

There are two unrelated money flows:

1. **Platform operating cost** — money Agent OS pays cloud, model, database, workflow, storage, email, and
   monitoring providers.
2. **Customer price** — money customers pay Agent OS for the product and any metered work.

The older `$1,000/month` Release Assurance plan is an optional, human-assisted B2B service price paid **to
Agent OS by a customer**. It is not a required vendor subscription, not the default self-serve Agent OS price,
and not an operating cost Agent OS pays.

No `$933/month` fixed operating line exists in the repository's selected architecture. A calculator or vendor
package near that amount is not approved for the initial deployment.

## Non-negotiable cost rule

Agent OS starts close to zero and earns the right to add fixed infrastructure.

| Commercial stage | Recurring revenue | Fixed infrastructure target | Hard rule |
|---|---:|---:|---|
| local/private proof | $0 | $0–25/month | use local/free/scale-to-zero services; no paid HA claim |
| public beta | under $500 MRR | at most $50/month | no vendor package with a three-digit monthly floor |
| early paid | $500–2,500 MRR | at most 20% of MRR, capped at $175/month | activate managed durability only when revenue or a customer contract covers it |
| growth | above $2,500 MRR | below 15% of MRR | scale from measured load and SLOs, not customer count |

Model calls, untrusted build/QA sandboxes, third-party APIs, generated-app runtime, and customer data egress are
variable COGS. They are excluded from the fixed-cost percentage only because the customer pricing system must
reserve and recover them separately. They still count in gross margin.

Any exception must identify the signed revenue or contractual requirement that pays for it. “We may have
millions of users later” is not a cost exception.

## Cost-first deployment profiles

### Bootstrap: private proof and public beta

Target fixed cost: **$0–30/month**, excluding domain registration and actual model/build usage.

| Component | Initial choice | Idle/fixed posture |
|---|---|---|
| web console | static hosting/CDN | free tier |
| control API | Cloud Run request-based, minimum instances `0` | scale to zero; usually free-tier at low traffic |
| workflow | DBOS Transact using the application PostgreSQL, or local Temporal for non-public development | no workflow SaaS subscription |
| PostgreSQL | Neon Free for development; Neon Launch for public beta | free or usage-based; typical Launch example is about $15/month |
| identity | WorkOS AuthKit without paid enterprise connections | $0 below its published first-million-MAU allowance |
| object/artifact storage | small regional object bucket/OCI registry | usage-based; lifecycle-delete temporary artifacts |
| policy | OPA library/Wasm or scale-to-zero service | no paid policy control plane |
| telemetry | OpenTelemetry plus cloud free allowances/local inspection | no paid Datadog/Langfuse commitment |
| payments | Stripe standard/pay-as-you-go | transaction/volume fees, no required fixed subscription |
| model usage | BYOK or prepaid, hard-capped hosted-model wallet | variable and reserved before execution |

Official current pricing supports this shape: Cloud Run charges for resources used and has monthly free
allowances; Neon offers free and usage-based scale-to-zero PostgreSQL; WorkOS AuthKit advertises the first one
million users free when paid enterprise connections are not used; Stripe standard pricing has no setup or
monthly platform fee. [Cloud Run pricing](https://cloud.google.com/run/pricing),
[Neon pricing](https://neon.com/pricing), [WorkOS AuthKit](https://workos.com/user-management),
[Stripe pricing](https://stripe.com/pricing)

The bootstrap profile is suitable for alpha/beta customers with an honest beta reliability statement. It must
still have backups, recovery checks, tenant isolation, idempotency, and monitoring. It must not advertise an
enterprise HA/SLA that the low-cost deployment cannot support.

### Managed durability: once revenue justifies it

Temporal Cloud Essentials currently charges the greater of `$100/month` or its consumption percentage. It is
the selected long-term managed workflow service, but it is activated only when:

- MRR is at least `$750`; or
- a signed customer requires the stronger managed-durability/SLA posture and that deal's contribution margin
  covers the fixed cost.

[Temporal Cloud pricing](https://github.com/temporalio/documentation/blob/main/docs/evaluate/temporal-cloud/pricing.mdx)

With low-traffic Cloud Run, usage-based PostgreSQL, free basic identity, small storage, and Temporal, the first
managed production target is approximately **$115–175/month fixed**, not `$933` or `$1,000`. Actual deployment
must emit its own calculator output and budget alert before provisioning.

### Explicitly not purchased at launch

- Hatchet Team (`$500/month`) or Scale (`$1,000/month`) managed packages.
- DBOS Pro/Teams management tooling (`$99/$499 per month`).
- GKE clusters, dedicated nodes, paid service meshes, or a self-operated Firecracker fleet.
- Cloud SQL HA, multi-region databases, enterprise SSO connections, paid SIEM/APM, or premium support before a
  customer/revenue requirement triggers them.
- Reserved GPU or permanent per-customer/agent containers.

Hatchet's free developer/usage tier may be benchmarked, but its paid package is not part of the launch bill.
[Hatchet pricing](https://hatchet.run/pricing), [DBOS pricing](https://www.dbos.dev/dbos-pricing)

## Workflow-engine cost policy

The domain lifecycle and command contracts remain framework-neutral.

- A run is born on exactly one workflow engine and never dual-writes authoritative state.
- DBOS Transact is allowed for the bootstrap profile because it is already present, open source, uses
  PostgreSQL, and needs no separate workflow server or paid console.
- Temporal remains the managed growth engine once its revenue/SLO activation gate passes.
- Migration is for **new runs**; existing runs drain on their birth engine.
- The same replay, idempotency, cancellation, wait, failure-injection, and recovery acceptance corpus must pass
  before either adapter accepts customer work.

This is not permission to rebuild custom queues, leases, sweepers, or another controller around DBOS.

## Customer pricing: small base plus transparent usage

Do not sell unlimited agent work for a fixed low subscription, and do not require a `$1,000/month` self-serve
commitment. The default pricing shape is a modest platform fee plus prepaid/metered execution.

### Launch hypothesis

| Plan | Platform price | Usage | Intended customer |
|---|---:|---|---|
| Evaluate | `$0` | BYOK; tightly limited sandbox allowance | experience the product without surprise cost |
| Solo | `$19/month` | prepaid metered work | one founder, one active company |
| Company | `$49/month` | prepaid metered work; pooled across included seats | small operating company |
| Agency | `$99/month + seats` | prepaid pooled work; multiple client companies | agencies/resellers |
| Managed launch | `$299` one-time, optional | usage charged separately | hands-on onboarding and first public release |
| Enterprise | negotiated only when required | committed usage or BYOK/BYOC | SSO, residency, SLA, dedicated isolation/support |

These are testable starting hypotheses, not promises to preserve an unprofitable price. Conversion, activation,
support time, usage, and margin determine later changes. There is no arbitrary enterprise floor before the
customer asks for enterprise features.

### What is metered

Customers see dollars and work outcomes, not a confusing token ledger. Internally every run attributes:

- model input, cached input, output, and tool-call cost by provider/model;
- sandbox CPU, memory, GPU, browser minutes, storage, artifact retention, and egress;
- paid external APIs and generated-app hosting;
- durable-workflow operations and retries;
- refunds/credits caused by Agent OS failures;
- gross margin by customer, company, run, and plan.

### Charging contract

Before expensive work:

1. Show an honest estimated range and what can make it larger.
2. Reserve the upper bound from a prepaid wallet or obtain one bounded approval.
3. Charge measured successful work; do not charge repeated work caused solely by an Agent OS defect.
4. Pause before exceeding the approved ceiling.
5. Release unused reservation immediately at terminal completion/cancellation.
6. Show an itemized receipt and the next expected cost.

Default monthly hard cap is on. Auto-recharge is off until the customer explicitly enables an amount and
ceiling. No silent overage, negative balance, surprise invoice, or “unlimited” plan whose economics rely on
most users not using it.

### Usage price and margin

Until the V2 vertical measures real p50/p95 COGS, retail execution price is formula-based rather than invented
per “credit”:

```text
hosted-model usage price = measured variable COGS / (1 - target gross margin)
BYOK usage price         = measured non-model COGS / (1 - target gross margin)
```

Initial target variable gross margin is 65%; therefore the hosted variable charge is roughly `2.86 ×` measured
variable COGS. The platform subscription pays for the persistent company, governance, storage allowance,
console, product development, and ordinary support. High-touch human work is a separately stated service,
never hidden inside usage.

The formula must be recalculated from real V2 runs before public pricing launches. If the result is not
competitive, optimize model routing, prompts, caching, QA scope, and sandbox utilization; do not hide the cost
or sell below cost indefinitely.

## Automatic financial controls

- Fixed-infrastructure budget checked before every IaC apply.
- Variable-cost reservation before every model/sandbox/external-API activity.
- Alerts at 50%, 80%, and 100% of customer and platform caps.
- Automatic pause at 100%; cleanup and evidence upload remain allowed.
- Per-vendor monthly forecast and concentration ratio.
- A paid vendor reaching 10% of MRR triggers an alternative/self-host benchmark.
- A fixed vendor upgrade cannot occur from a free/usage tier unless signed MRR preserves the stage limit.
- Weekly unit-economics report: MRR, fixed cost, variable COGS, contribution margin, refunds, free-user cost,
  and the exact workloads responsible.

## Immediate decision

Do not provision the `$900+` architecture. Build and test the V2 vertical on the bootstrap profile. Activate
Temporal Cloud and stronger managed infrastructure only when revenue or a signed reliability requirement pays
for them. Customer pricing is a small base plus bounded usage; the `$1,000/month` managed assurance offer is a
separate optional service, not the product's entry price.
