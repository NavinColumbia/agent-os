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
is committed as a graph failure instead of leaving a permanently running token. Timer, arbitrary subworkflow, and
production release actions stay fail-closed until an idempotent adapter is registered.

New CEO directives now emit `start_mission`, not a second hard-coded research/build agent chain. That durable
command starts a built-in planner graph. The mission architect must persist an identity-free JSON graph proposal;
`workflow.launch` supplies tenant/creator/version identities, permits only registered node/tool kinds, caps the
plan at 32 nodes and 128 edges, limits per-node and total iterations, rejects any node without a terminal path,
registers the immutable definition, and launches its correlated child run. `GET /v2/runs/{run_id}/mission`
projects the lifecycle, planning graph, and detailed execution graph together. A terminal child outcome is
idempotently projected back to the coarse CEO lifecycle, while the six phases remain a UI/status projection rather
than the orchestration engine.

`GET /v2/runs/{run_id}/management` is the first CEO/manager read model over that execution truth. It projects
mission-scoped roles, accountable owners and managers, materialized work, attempts, evidence, human waits,
risks, decisions, delegation/staffing proposals, and recommended management actions. It reads bounded action
timing and lease records under tenant RLS. In particular, it distinguishes a slow action whose renewable lease
is healthy from an expired recoverable lease, scheduled retry, delayed dispatch, contradictory state, and
terminal business failure. A review interval therefore creates a diagnostic signal; it never abandons healthy
work or pretends the whole adaptive graph is a fixed-size percentage plan. Agent nodes persist their bounded
management proposals in workflow state, but external messages and staffing requests remain labelled proposals
until a governed effect/approval executor applies them.

Every graph run also creates a durable management watch. The fair tenant worker claims due watches under an
owner-fenced lease, runs the same authority-derived diagnosis, and schedules the next check without keeping a
tenant permanently hot. Sustained `slow_but_owned`, expired/recovering lease, delayed dispatch, or contradictory
state produces one idempotent manager notification; critical infrastructure symptoms also reach the operator.
Only a condition that survives the configured number of consecutive checks escalates to the CEO, and a later
healthy check publishes recovery to the same audience. Rechecking or crashing between notification publication
and watch acknowledgement cannot duplicate the inbox item. The monitor never cancels work. A future manager-agent
turn may add contextual repair/reassignment proposals, but deterministic health classification and delivery do
not depend on a model being available.

Human questions, operator alerts, and lifecycle/graph completion state are idempotently delivered to the real,
tenant-isolated in-app notification ledger and exposed by `GET /v2/notifications`. External transports such as
email, Slack, SMS, and push remain separate adapters. `schedule_retry`, arbitrary external tool execution, and
general subworkflows still require explicitly registered executors; until those adapters exist, the router marks
the effect failed with a durable explanation rather than reporting an operation that did not happen.

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
`DELETE /v2/deployments/previews/{deployment_id}` plus `Idempotency-Key`. A managed object-storage adapter and
cleanup of expired bytes remain to be implemented before customer launch.

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

It starts PostgreSQL, applies only the isolated V2 migrations (86–94), and then starts the API and worker from the
exact same non-root image. Hosted OIDC/signup, secrets management, production deploy/rollback, a hosted
sandbox/build adapter, large-object storage/garbage collection, metering, and managed-cloud IaC are still launch
blockers.
