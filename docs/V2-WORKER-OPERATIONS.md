# V2 worker operations

## What is implemented

`agentos-v2 worker` is the long-running execution process for the V2 lifecycle command outbox. It:

- discovers ready tenant shards from PostgreSQL, or polls an explicit BYOC/cell allowlist;
- atomically claims one due command under a renewable, owner-fenced lease;
- keeps the lease alive during long agent turns;
- caps each model turn by requests, output tokens, provider-call timeout, and configured cost;
- supplies the model with an authoritative bootstrap organization directory;
- commits the complete structured agent turn to the organization ledger in one transaction;
- submits a deterministic lifecycle follow-up and waits for its durable result before acknowledging the command;
- reconstructs committed turns after a crash without paying for or executing the model twice;
- retries transient provider/database errors from durable state with backoff and no whole-story timeout;
- handles SIGTERM/SIGINT between commands and emits structured JSON operational events.

The same process fairly alternates the lifecycle-command and arbitrary graph-action queues per tenant. Graph
actions use independent renewable leases and deterministic begin/result events. Agent and decision nodes use
structured model output, human nodes create and resume correlated waits without model spend, and terminal nodes
accept upstream evidence rather than inventing new evidence. Provider retries keep a token running and reuse the
same action ID; recovery after a committed result does not execute the node twice. A deterministic node rejection
is committed as a graph failure instead of leaving a permanently running token. Timer and unregistered production
release actions stay fail-closed until an idempotent adapter is registered.

Before any lifecycle or graph agent calls a model provider, the worker creates an idempotent tenant usage
reservation keyed by the durable command/action ID. A per-tenant monthly ceiling serializes concurrent reservations,
so replicas cannot race past it. Successful calls settle request/tool/token counts and provider cost in integer USD
micros; when a provider/model does not report a trustworthy price, the reserved maximum remains charged rather than
pretending the call was free. A crash/retry with the same source ID cannot double-count usage. Authenticated summary
and owner-level event APIs are available at `GET /v2/usage/summary` and `GET /v2/usage/events`; the CEO workspace
shows current committed model budget. Those meter records are cost-control/billing evidence, not payment collection;
the platform uses a separate Stripe subscription boundary rather than treating provider token counts as money.
When billing is enabled, authenticated owners can open Stripe-hosted Checkout and Customer Portal sessions. Raw-body
signed `checkout.session.completed` and `customer.subscription.*` webhooks project idempotent, order-safe tenant
subscription truth under PostgreSQL RLS and reconcile the plan's model-spend ceiling before later provider calls.
Checkout redirects never grant access by themselves, a late Checkout event cannot downgrade or overwrite a
subscription event, inactive/cancelled subscriptions fall back to the free ceiling, and production refuses test keys
or disabled billing. Metered overage invoicing, prepaid wallet/credits, tax-policy configuration, refunds, and
invoice history remain later commercial milestones; the current paid plans are subscription entitlements plus a
hard provider-spend ceiling, not permission for silent overage.
Stripe secrets are API-only configuration and are deliberately absent from the worker container.

New CEO directives now emit `start_mission`, not a second hard-coded research/build agent chain. That durable
command starts a built-in planner graph. The mission architect must persist a complete, identity-free mission
program—not merely a task graph. The deterministic admission contract requires an honest feasibility verdict,
ordered time/cost ranges and assumptions; scoped material clarifications; accountable human/agent/service roles;
resource and capability inventories with acquisition/expansion nodes; mapped workstreams; evidence claims and
repair routes; and a recurring replan decision. A claimed available capability cannot cite a runtime tool that is
not actually registered. Each directive carries an explicit CEO-authorized external-spend ceiling (zero by
default); admission rejects a larger program budget, and a later revision cannot expand either budget or revision
authority. A forecast above current authority must remain visible as an owned budget-acquisition gap rather than
an invented permission to spend. `workflow.launch` supplies tenant/creator/version identities, permits only registered
node/tool kinds, caps the plan at 64 nodes and 256 edges, limits per-node and total iterations, rejects ungoverned
nodes or any node without a terminal path, and launches its correlated child run. `GET /v2/runs/{run_id}/mission`
projects the admitted program alongside the lifecycle, planning graph, detailed execution graph, and releases.
A terminal child outcome is idempotently projected back to the coarse CEO lifecycle, while the six phases remain
a UI/status projection rather than the orchestration engine.

A material replan cannot mutate token output and pretend the plan changed. The admitted graph must route that
condition through the internal `workflow.revise` authority and provide a new complete mission-program artifact.
The authority validates the whole replacement, registers exactly version N+1 with an explicit supersedes fence,
atomically archives obsolete live/waiting tokens, advances program and workflow revisions, and schedules the new
entry token on the same run ID. History and evidence remain intact, raw API revision events are rejected, revision
authority is bounded by the prior program, and lifecycle cancellation still targets the same mission run.

`GET /v2/runs/{run_id}/management` is the CEO/manager read model over that execution truth. It projects
mission-scoped roles, accountable owners and managers, materialized work, attempts, evidence, human waits,
risks, decisions, delegation/staffing proposals, and recommended management actions. It reads bounded action
timing and lease records under tenant RLS. In particular, it distinguishes a slow action whose renewable lease
is healthy from an expired recoverable lease, scheduled retry, delayed dispatch, contradictory state, and
terminal business failure. A review interval therefore creates a diagnostic signal; it never abandons healthy
work or pretends the whole adaptive graph is a fixed-size percentage plan. It also projects the admitted
feasibility, program revision, unresolved questions, specifically blocked workstreams, outstanding resources and
capabilities, and whether independent work is continuing. The CEO drawer renders those facts and upfront ranges.
Agent communications, risks, decisions, delegations, and staffing requests first commit in workflow state, then a
separate crash-recoverable outbox effect records them in the lifecycle-scoped organization ledger without repeating
the model call. The same ledger receives the complete `program_admitted` record, so management communications and
the charter they refer to share one immutable mission history. Consequential actions remain proposals until their
governed approval/effect executor applies them.

Every graph run also creates a durable management watch. The fair tenant worker claims due watches under an
owner-fenced lease, runs the same authority-derived diagnosis, and schedules the next check without keeping a
tenant permanently hot. Sustained `slow_but_owned`, expired/recovering lease, delayed dispatch, or contradictory
state produces one idempotent manager notification; critical infrastructure symptoms also reach the operator.
Only a condition that survives the configured number of consecutive checks escalates to the CEO, and a later
healthy check publishes recovery to the same audience. Rechecking or crashing between notification publication
and watch acknowledgement cannot duplicate the inbox item. The monitor never cancels work. A future manager-agent
turn may add contextual repair/reassignment proposals, but deterministic health classification and delivery do
not depend on a model being available.

The first standing organization write model is also live. `aos_v2_companies` and its append-only tenant event
stream survive individual directives. `GET /v2/company/organization` projects the bootstrap teams plus approved
changes; `GET /v2/company/activity` exposes the immutable history. An owner/operator may idempotently add a
bounded AI role with `POST /v2/company/agents` or retire a non-core role with
`POST /v2/company/agents/{agent_id}/retire`. The authority prevents retirement of the accountable mission
manager, current team managers, and managers that still own direct reports. Active standing roles, reporting
lines, capabilities, tool grants, hiring authority, and per-turn spending authority are supplied to graph agents
as authoritative context and therefore carry into later directives. Mission-created graph roles remain scoped
to that mission unless an authorized company change promotes them. Team mutation, automatic application of model
staffing proposals, and access provisioning remain approval/effect milestones; this directory does not pretend an
event record granted an unconfigured tool.

Mission staffing proposals now receive content-derived stable IDs and pending/approved/rejected status in the
management projection. An owner/operator may decide an AI-agent proposal at
`POST /v2/runs/{run_id}/management/proposals/{proposal_id}/hiring-decision`. Approval atomically records one
immutable decision and promotes up to 32 deterministic agent identities into a selected standing team under one
active manager, with explicit tool and spend limits; retries cannot create duplicate identities. Rejection records
the durable decision without creating capacity. Approved human/vendor proposals instead create deterministic
`awaiting_external_onboarding` cases. They remain absent from the routable organization until an owner confirms the
required identity, terms, access, and (for a vendor) contract attestations at
`POST /v2/company/external-onboarding/{onboarding_id}/confirm`. Confirmed humans use their authenticated identity
subject and an explicit response SLA; confirmed vendors become service participants under an accountable agent.
`GET /v2/company/external-onboarding` exposes every case and its state. This completes the software workflow while
truthfully leaving employment, contracting, identity verification, and account provisioning as external actions.
Fully autonomous proposal application remains policy work: the model cannot approve its own staffing request merely
because the requested spend is zero.

Human questions, operator alerts, and lifecycle/graph completion state are idempotently delivered to the real,
tenant-isolated in-app notification ledger and exposed by `GET /v2/notifications`. External transports such as
email, Slack, SMS, and push remain separate adapters. Retry waits use an RFC 3339 due time written directly into the
command outbox's `available_at`; no worker sleeps while waiting. When claimed, the explicit `schedule_retry`
executor re-checks the exact correlation against current lifecycle state, treats cancelled/superseded timers as
harmless, and idempotently emits the original resume command. Protocol-specific effects outside the registered
HTTP/deployment/sandbox adapters still fail closed until an explicit executor exists.

`connector.invoke` is the governed general HTTP capability adapter. An owner registers an immutable tenant-scoped
HTTPS origin, path prefixes, methods, response/time bounds, authentication mode, opaque credential reference, and
(for writes) upstream idempotency header at `POST /v2/connectors`; definitions and disable operations are isolated
by PostgreSQL RLS. The planner receives only the non-secret active catalog. Runtime joins the fixed origin to a
bounded path/query, refuses redirects and non-public DNS answers, pins TLS to an address it already validated,
caps request/response bytes and call time, and persists the response plus a secret-free receipt as immutable
evidence. POST/PUT/PATCH/DELETE additionally require a completed Human node and send the durable graph action ID as
the upstream idempotency key. Provider 408/425/429/5xx and network failures use the existing durable lease/backoff
loop; a whole mission is never killed by a wall-clock story timeout. Local secrets are tenant-digest-namespaced,
read-only files with symlink refusal. Hosted secrets use a deterministic tenant/ref-derived Secret Manager name and
workload identity, permitting per-secret IAM without storing raw keys in connector definitions, SQL, logs, prompts,
or receipts. This adapter covers conventional JSON/HTTP APIs; protocol-specific OAuth consent and webhook ingestion
remain explicit adapters because treating those security ceremonies as a generic HTTP call would be unsafe.

General mission decomposition is recursive rather than a fixed six-step chain. An admitted workflow may use a
`subworkflow` node whose source is another complete, immutable mission-program artifact. The runtime revalidates
that child charter, enforces the parent's delegated budget and an eight-level recursion bound, starts it under a
deterministic tenant-owned identity, and parks only the parent token while independent siblings continue. Terminal
child evidence resumes the exact correlated token; child failure follows an explicit failure route, and revision or
cancellation cascades to every still-waiting child without generating a fake human notification. Both mission and
management APIs expose a bounded recursive hierarchy with parent/token identities, objective, state, and token
counts. A child program has the same clarification, organization, resource, capability, verification, and replan
contract as its parent, so adding teams does not bypass governance.

`cancel_active_operation` now has a mission-aware executor. A terminal CEO cancellation is propagated to both the
planner and any launched child graph, turns all live graph tokens into cancelled tokens, and produces the graph's
normal durable cancellation action/notification. A cancelled lifecycle is checked before late planning and both
before and after child launch. The cancellation handler can derive the deterministic child run ID from committed
plan evidence, so a launch/cancel race cannot quietly orphan continuing work. Replaying the same cancellation is
idempotent.

The first allowlisted graph tools, `artifact.publish_text` and `artifact.publish_json`, turn literal or durable
prior-node output into immutable, content-addressed evidence. Their bootstrap PostgreSQL store is capped at 2 MiB
per object; large source bundles, build outputs, and release images still require the object/OCI-store adapter.
No model-controlled shell command is executed by this adapter.

Lifecycle and graph agents may also propose up to 16 bounded text, JSON, HTML, or source-bundle artifacts in a
structured turn. The authority layer validates and content-addresses those objects, replaces the proposal bodies
with durable artifact records, and only then permits their IDs to support completion. A model-supplied evidence
ID must already exist in the same tenant or come from authoritative upstream evidence; invented, cross-tenant,
and nested decision citations fail before workflow progress is committed. JSON can be proposed as a structured
`json_value`, avoiding fragile nested JSON string escaping.

`sandbox.run` is available only when a dedicated development/CI worker sets
`AOS_V2_SANDBOX_BACKEND=docker`. The local adapter accepts an immutable Agent OS source bundle, uses direct argv
inside a digest-pinned image, denies networking, runs as a non-root UID, mounts only an ephemeral workspace,
drops every Linux capability, enables `no-new-privileges`, and caps time, memory, CPU, PIDs, file count, artifact
bytes, and captured logs. It uses `--pull never` and never falls back to host execution, so the pinned image must
be pre-pulled. Do not mount the Docker socket into the API or general worker container; the hosted adapter remains
a separate Cloud Run Job/GKE sandbox boundary.

Hosted production uses `AOS_V2_SANDBOX_BACKEND=cloud-run-job`. Its adapter stages a deterministic input object,
creates short-lived generation-bound GCS read/write capabilities, executes one overridden Cloud Run Job, resumes a
previously recorded long-running operation after a worker crash, and persists the same immutable output/result
evidence contract as the local adapter. The Job runs in a separate GCP project on a dedicated Direct VPC egress
network: private DNS maps Google APIs to `restricted.googleapis.com`, HTTPS to those documented ranges is allowed,
and all other IPv4 egress is denied. Its service account has no project roles and receives no application, model,
database, billing, or tenant secret. A root controller exists only to own transfer capabilities and demote the
direct-argv child to UID/GID 65532; the child gets a scrubbed environment and bounded in-memory workspace, file,
process, time, log, and output limits. The Cloud Run execution is pay-per-use and scales to zero.

For a dedicated local runner host:

```bash
docker pull python:3.12-slim-trixie@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
export AOS_V2_SANDBOX_BACKEND=docker
agentos-v2 worker
```

Docker exit 125 is treated as retryable infrastructure failure and is not cached as customer output. Customer
command failures and hard timeouts retain their bounded result/output evidence and may follow an explicitly
declared repair edge.

`deploy.preview` is the first bounded deployment adapter. It accepts only a same-tenant immutable `text/html`
artifact of at most 256 KiB, creates an idempotent deployment receipt, and publishes it at a tenant-fenced URL
containing a 256-bit HMAC capability. The public route requires no account session because possession of that
unguessable URL is the read capability. It returns `no-store`, `nosniff`, no-referrer, and deny-framing headers,
and forces the generated document into an opaque-origin CSP sandbox. Inline scripts and styles may render an
interactive demo, but network connections, forms, top-level navigation, parent-origin access, and external
resources remain blocked. This is a disposable static evaluation surface, not production promotion, a custom
domain, or an arbitrary backend deployment. Every capability expires (seven days by default). An authenticated
owner/operator can list tenant previews with `GET /v2/deployments/previews` and idempotently revoke one with
`DELETE /v2/deployments/previews/{deployment_id}` plus `Idempotency-Key`. Preview expiry revokes the public
capability; it does not delete a source artifact that may also be mission evidence. Hosted GCS artifact payloads
use the configured 365-day retention window (30–3650 days), and noncurrent object generations are removed after
seven more days so bucket versioning cannot silently defeat that policy. SQL retains the secret-free artifact
identity as an audit tombstone. Local SQL storage is an explicitly capped evaluation backend, not production.

`deploy.static` is the first production promotion adapter. It consumes a bounded same-tenant
`application/vnd.agent-os.source-bundle+json` artifact with `index.html`, stores each release below an immutable
content digest in a private GCS bucket, and advances an opaque stable route with generation compare-and-swap.
The workflow validator requires both a preceding source node and a preceding Human node; the runtime re-resolves
the durable human output and accepts only the exact boolean `approved: true`. A deterministic receipt makes crash
replay idempotent. The stable URL redirects without caching to a year-cacheable immutable revision.

Published content is served by `agentos-v2 static-router`, a separate public Cloud Run service and origin. Its
identity can only read exact objects from the publication bucket and receives no database, OIDC, model, billing, or
control-plane secret. The publishing worker has prefix-conditioned object permissions and no delete permission.
Credential-bearing paths and high-signal credential patterns are rejected before upload. HTML receives a
restrictive CSP, generated apps cannot connect to the network, and the service never handles CEO
sessions. Static publication is one option; `deploy.service` also builds bounded source bundles in an isolated
project, promotes digest-pinned images to scale-to-zero Cloud Run services, checks the declared health path, and
automatically restores the prior ready revision on a failed promotion. `GET /v2/deployments` projects the
tenant-fenced immutable receipts for previews, static sites, successful services, and failed/rolled-back releases.
An owner can redeploy any prior source-artifact ID through the same human-approved workflow, which is the audited
rollback path. Custom per-app domains, abuse response operations, and a real cloud smoke test remain separate gates.

The CEO inbox renders live graph waits with Approve, Decline, and free-response controls. The API recomputes
actionability from the current durable token rather than trusting a stale notification. Only owners/operators or
the Human node's named recipient may resume it; response objects are bounded, owner retries are idempotent, and
viewers cannot decide or cancel work. A Human node may declare a distinct `rejection_condition`, so a negative
decision reaches a repair/stop branch and cannot accidentally satisfy its affirmative path.

`tests/test_prompt_to_preview_vertical.py` joins the production-shaped seams in one bounded contract: authenticated
CEO prompt, DBOS lifecycle command, mission-planner graph, authority-validated child graph, model-proposed HTML
artifact, idempotent deployment, terminal projection, authenticated mission status, and an unauthenticated fetch of
the generated app. It uses deterministic PydanticAI test models so CI proves orchestration in seconds without
provider spend. It does not substitute for a hosted-provider, browser-onboarding, or managed-cloud deployment test.

## Configuration

Required for the worker:

- `AOS_V2_MODEL`: explicit PydanticAI `provider:model`; there is no spend-bearing default.
- the same database variables and application version used by the API.
- the credential for the selected provider, such as `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`.
- in hosted production, `AOS_V2_PUBLISHED_APP_BUCKET` and a separate HTTPS `AOS_V2_APPS_BASE_URL`.

Important controls:

- `AOS_V2_LEASE_SECONDS` (default `60`)
- `AOS_V2_IDLE_POLL_SECONDS` (default `1`)
- `AOS_V2_ERROR_BACKOFF_SECONDS` (default `5`)
- `AOS_V2_MODEL_REQUEST_LIMIT` (default `12`)
- `AOS_V2_MODEL_OUTPUT_TOKENS_LIMIT` (default `8000`)
- `AOS_V2_MODEL_REQUEST_TIMEOUT_SECONDS` (default `120` per provider call)
- `AOS_V2_MAX_TURN_COST_CENTS` (default `100`)
- `AOS_V2_RETRY_MAX_ATTEMPTS` (unset means transient infrastructure failures keep retrying)
- `AOS_V2_TENANT_DISCOVERY_LIMIT` (default `128`, maximum `1000` ready tenants per fair cycle)
- `AOS_V2_WORKER_ORGANIZATIONS` (optional comma-separated static BYOC/cell allowlist)
- `AOS_V2_PUBLIC_BASE_URL` (the externally reachable API origin; production requires HTTPS)
- `AOS_V2_PREVIEW_TTL_SECONDS` (default `604800`; bounded from 60 seconds through 30 days)
- `AOS_V2_MANAGEMENT_CHECK_SECONDS` (default `30`; durable review cadence)
- `AOS_V2_SLOW_WORK_SECONDS` (default `300`; diagnostic threshold, never a kill timeout)
- `AOS_V2_MANAGEMENT_ESCALATION_CHECKS` (default `3`; consecutive checks before CEO escalation)
- `AOS_V2_CONNECTOR_SECRET_BACKEND` (`file` locally, `gcp` in hosted production)
- `AOS_V2_CONNECTOR_SECRET_DIR` (local/BYOC read-only credential root)
- `AOS_V2_CONNECTOR_SECRET_PROJECT_ID` (Secret Manager project when the backend is `gcp`)
- `AOS_V2_TENANT_MONTHLY_MODEL_BUDGET_CENTS` (default `10000`; reserved before provider calls)
- `AOS_V2_BILLING_MODE` (`disabled` locally; public production requires `stripe`)
- `AOS_V2_STRIPE_SECRET_KEY` (production requires an `sk_live_` key)
- `AOS_V2_STRIPE_WEBHOOK_SECRET` (signing secret for `/v2/billing/webhooks/stripe`)
- `AOS_V2_STRIPE_STARTER_PRICE_ID` / `AOS_V2_STRIPE_GROWTH_PRICE_ID`
- `AOS_V2_STRIPE_STARTER_MODEL_BUDGET_CENTS` / `AOS_V2_STRIPE_GROWTH_MODEL_BUDGET_CENTS`
- `AOS_V2_STRIPE_API_VERSION` (explicitly pinned; default `2025-06-30.basil`)

The control API supports two identity modes behind the same tenant-binding boundary. `hmac` is only the local/BYOC
evaluation token flow; public production rejects it. Production requires `AOS_V2_IDENTITY_MODE=oidc` plus an HTTPS
issuer, API audience, and JWKS URL. The verifier accepts only an explicit asymmetric algorithm allowlist, caches a
bounded rotating key set, validates `iss`, `aud`, `sub`, `iat`, `exp`, and multi-audience `azp`, caps token lifetime,
and maps configurable organization/role claims only after signature validation. It never accepts a request tenant
header or an access token from a browser cookie. `AOS_V2_CAPABILITY_SECRET` independently signs public preview
capabilities and must not be reused as a local-token secret in production.

`/app` now serves the packaged CEO workspace from the same origin as the control API. In OIDC mode its public
configuration uses authorization code + PKCE and keeps the resulting bearer credential only in page memory; in
local/BYOC mode it accepts the local evaluation token without persisting it in browser storage. The workspace can
start and list tenant missions, inspect the lifecycle/mission/management projections, observe manager signals and
materialized work, cancel a mission, select the team and manager for an approved AI staffing proposal, and view the
standing company, notification inbox, and published previews. Its CSP permits API traffic to self and the single
validated token origin, with no inline script/style execution or framing. `GET /v2/runs` is tenant-scoped and returns
bounded summaries; the full directive is available only from the authenticated single-run view.

With no static allowlist, staging/production workers use the narrow `agentos_worker` database role to discover
only tenant IDs with due or abandoned queue work. That role receives column-level access to scheduling metadata,
not customer command/action payloads. Actual claims and mutations still enter the existing `agentos_app` tenant
role with RLS, and renewable leases coordinate replicas. Discovery rotates from a cursor so a tenant with a deep
backlog cannot permanently hide later tenant IDs. A production database runtime principal must be allowed to
`SET ROLE` to both `agentos_worker` and `agentos_app`; it must not own tables or receive `BYPASSRLS`.

## Bounded local deployment proof

The Compose file is an evaluation/BYOC proof, not the public paying-customer environment:

```bash
cp deploy/v2.env.example deploy/v2.env
docker compose --env-file deploy/v2.env -f deploy/docker-compose.v2.yml up --build
```

It starts PostgreSQL, applies only the isolated V2 migrations (86–99zzzz), and then starts the API and worker from the
exact same non-root image. Hosted OIDC access-token verification and the PKCE browser client are implemented, but an
actual provider tenant, secrets management, production configuration, artifact garbage collection,
usage-invoice export/prepaid credits, and a real managed-cloud apply/smoke are still launch
blockers.

For a stable, zero-fixed-cost external pilot from the existing WSL laptop, use
[`deploy/LOCAL-PILOT.md`](../deploy/LOCAL-PILOT.md). That profile keeps the API loopback-only behind Tailscale
Funnel, uses short-lived signed invitations, disables billing, supports Gemini's free tier, and executes generated
tests in networkless resource-capped Docker containers. It is a customer-demonstration bridge, not a substitute for
the isolated managed production cell.

## Managed GCP production-cell automation

`deploy/gcp` now contains the first OpenTofu production cell and a credential-late deployment script. It creates a
scale-to-zero Cloud Run API and secretless generated-app router, manually sized Cloud Run worker pool,
digest-pinned one-shot migration Job, private
Artifact Registry/bucket, separate runtime/build/migration identities, Secret Manager containers, remote versioned
state, and optional GitHub workload identity bound to the repository's immutable numeric ID. Secret values are added
out-of-band and never enter OpenTofu state. The migration credential is readable only by the Job; the Job verifies
that the named application login is non-superuser/non-`BYPASSRLS` before granting the two narrow runtime roles. The
API cannot read the model key and the worker cannot read Stripe keys. A release executes migrations before it creates
or updates serving revisions, then checks both deployed health endpoints.

The HCL is pinned to OpenTofu 1.12.6 and Google provider 7.22.x and validates against the real provider schema. The
migration image is base-digest-pinned, non-root, and locally proven against a clean PostgreSQL instance. No GCP
resources have been applied yet, and this cell intentionally leaves Cloud SQL/GKE out of the bootstrap bill. See
`deploy/gcp/README.md` for the exact credential checklist and one-command deploy flow.

The same production cell now selects the GCS `ArtifactStore` adapter for both API and worker. Payload bytes are
uploaded with a create-only generation precondition and checksum verification to an opaque tenant-digest prefix;
PostgreSQL retains the RLS-scoped identity, digest, media type, idempotency key, and immutable object generation.
Downloads re-check length and SHA-256 before returning bytes. The API and worker identities have object
creator/viewer so authenticated uploads and autonomous outputs both work, but neither can overwrite or delete
objects. Existing inline SQL artifacts remain readable during
migration. Production fails startup if it is configured back to the capped SQL payload adapter.

Hosted OIDC can enable `AOS_V2_OIDC_PERSONAL_TENANTS=1` for zero-touch first-user onboarding. A token without the
configured organization claim is assigned a stable tenant derived by HMAC from the already verified issuer and
subject. The derivation key never reaches the worker or browser, provider roles are ignored on this fallback path,
and the subject receives owner authority only inside that isolated personal company. Tokens that do carry an
organization still require the explicit roles claim. The managed GCP cell generates and deletion-protects this key
and excludes it from bulk rotation because rotating it would change personal tenant identities.

Multi-user access is provider-neutral. Owners create bounded, single-use signed invitations; PostgreSQL retains only
the token digest, expiry, roles, claim identity, and revocation history. An authenticated subject can select an
invited company with `X-Agent-OS-Organization`; every request re-checks the active membership before applying that
tenant's RLS scope. The CEO workspace exposes organization selection, invitation claim/creation, member inventory,
and revocation. Subject-scoped RLS permits a user to discover only their own memberships, while mutations remain
tenant-scoped.

Each company can choose its model policy through `GET/PUT /v2/settings/model` or the CEO workspace. The setting is
versioned, idempotent, tenant-RLS fenced, and limited to a provider/model plus an optional opaque credential
reference. No provider key enters the API request, browser, SQL, prompt, or audit record. With no company setting,
the worker uses its explicit `AOS_V2_MODEL` platform default. With a credential reference, the worker resolves the
same tenant-digest-namespaced file or GCP Secret Manager locator used by connectors and constructs an isolated
OpenAI, Anthropic, or Google provider client just for that turn. The usage ledger records the model actually chosen.
This makes provider choice and BYOK fully implemented; provisioning the referenced secret remains an external
credential action.
