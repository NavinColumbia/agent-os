# V2 worker operations

## What is implemented

`agentos-v2 worker` is the long-running execution process for the V2 lifecycle command outbox. It:

- polls configured tenant shards fairly;
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
same action ID; recovery after a committed result does not execute the node twice. Tool, timer, subworkflow, and
external publication actions stay fail-closed until an idempotent adapter is registered.

Human questions, operator alerts, and lifecycle/graph completion state are idempotently delivered to the real,
tenant-isolated in-app notification ledger and exposed by `GET /v2/notifications`. External transports such as
email, Slack, SMS, and push remain separate adapters. `schedule_retry`, `cancel_active_operation`, arbitrary
external/sandbox tool execution, and subworkflows still require explicitly registered executors; until those
adapters exist, the router marks the effect failed with a durable explanation rather than reporting an operation
that did not happen.

The first allowlisted graph tools, `artifact.publish_text` and `artifact.publish_json`, turn literal or durable
prior-node output into immutable, content-addressed evidence. Their bootstrap PostgreSQL store is capped at 2 MiB
per object; large source bundles, build outputs, and release images still require the object/OCI-store adapter.
No model-controlled shell command is executed by this adapter.

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

## Configuration

Required for the worker:

- `AOS_V2_MODEL`: explicit PydanticAI `provider:model`; there is no spend-bearing default.
- `AOS_V2_WORKER_ORGANIZATIONS`: comma-separated tenant IDs in staging/production.
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

The initial tenant list is an explicit cell/shard assignment suitable for early deployments and tens of customers.
Before general self-service signup, replace static assignment with the tenant catalog/admission scheduler described
in the North Star. Do not give a cross-tenant worker an unrestricted customer request credential.

## Bounded local deployment proof

The Compose file is an evaluation/BYOC proof, not the public paying-customer environment:

```bash
cp deploy/v2.env.example deploy/v2.env
docker compose --env-file deploy/v2.env -f deploy/docker-compose.v2.yml up --build
```

It starts PostgreSQL, applies only the isolated V2 migrations (86–90), and then starts the API and worker from the exact
same non-root image. Hosted OIDC/signup, secrets management, external-effect adapters, a hosted sandbox/build
adapter, large-object storage, metering, and managed-cloud IaC are still launch blockers.
