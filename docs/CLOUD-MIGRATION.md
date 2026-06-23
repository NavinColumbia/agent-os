# Cloud / Scale Migration — agent-os

> **The executable counterpart of this doc is `platform/`** — `inventory.yaml` (component registry),
> `rebuild.sh` (stand up anywhere), `snapshot.py` (encrypted portable backup), `terraform/` (cloud IaC).
> This doc is the *what maps to what*; `platform/README.md` is the *how*.

Design principle: **single-box by default, cloud-portable by construction.** Every service is a
container or a process configured by env vars — nothing is hard-wired to localhost beyond bind
addresses. Moving to cloud (for scale, GPUs, or many users) is a config + endpoint swap, not a rewrite.

| Service | Local now | Cloud target | How to migrate |
|---|---|---|---|
| **Postgres** (state, audit, comms, memory, metrics, vault, blobs) | `pgvector/pgvector:pg16` container | **RDS / Cloud SQL / Neon / Crunchy** (managed Postgres + pgvector) | change `DATABASE_URL` in `.env.local`; `pg_dump`→restore. DBOS, audit, vault, objstore all just follow the URL. |
| **Object store** (images/videos/files) | Postgres `BYTEA` (`objstore.py`) | **S3 / GCS / R2** | `objstore` is a thin interface — add an `OBJSTORE_BACKEND=s3` backend (put/get/meta/gc) keyed by the same sha256. Large media (video) belongs on S3; metadata stays in Postgres. |
| **Work queue / dispatch** (`tasks` table + `dispatcher`/`orchestrate`) | Postgres `tasks` table claimed with `FOR UPDATE SKIP LOCKED`; in-process ThreadPool for fleet builds | **same managed Postgres — no separate broker** | Horizontal scale = run N worker processes (the dispatcher) on N machines, ALL pointed at the same `DATABASE_URL`. `SKIP LOCKED` guarantees no double-processing. Config-only. (Swap in SQS/NATS/Kafka *only* if you outgrow Postgres throughput — a backend swap, not a rewrite.) |
| **Durable execution** (DBOS) | library on local Postgres | same library on managed Postgres (or DBOS Cloud) | no code change — follows `DATABASE_URL`. Temporal is the swap-in if you outgrow one Postgres. |
| **Policy engine** (Cerbos) | container, localhost | Cerbos container in k8s / Cerbos Cloud | change PDP URL; policies are git-versioned, portable. |
| **Secrets vault** | `vault.py` on Postgres (Fernet) | **AWS Secrets Manager / Vault by HashiCorp / GCP Secret Manager** | `vault.py` is an interface — back it with the cloud KMS; scoping/audit logic stays. |
| **Notifications** (ntfy) | self-hosted container | hosted ntfy / push service | change `NTFY_BASE_URL`. |
| **Reach** (Tailscale) | tailnet | Tailscale works identically in cloud; or a load balancer + auth | unchanged. |
| **Agents / Controller** | host processes | **containers on k8s / ECS**, autoscaled | they're stateless reducers; state is in Postgres. Containerize + scale horizontally. |
| **GPU / ML training & video serving** | n/a (single box) | **cloud GPUs (A100/H100), GPU k8s, CDN for video** | the OS orchestrates; the user provisions the compute. ml-engineer role requests `spend`/`gpu_budget` via approval. |

## What stays the same after migration
Governance (manifests + sandbox + PDP), tamper-evident audit, durable execution semantics, the comm
fabric, provenance, and the lifecycle — all are storage/transport-agnostic and follow config. The
single-box advantages (transactional co-location) relax gracefully: at cloud scale you accept
eventual consistency between separated stores and add the outbox/inbox patterns already designed in ADR 0005.

## What actually changes — config, not code (the ~1:1 promise)
For the realistic first cloud move (one bigger VM, then a worker pool), it is **env-config only**:

| Change | From | To |
|---|---|---|
| `DATABASE_URL` | local pgvector container | managed Postgres (RDS/Neon/Cloud SQL) — **everything follows this one URL**: state, audit, comms, vault, blobs, durable exec, AND the work queue |
| `OBJSTORE_BACKEND` (+ bucket) | `postgres` | `s3` *(drop in the S3 backend module — only when media gets large)* |
| `NTFY_BASE_URL` / `CERBOS_URL` | localhost | hosted/cluster endpoints |
| (scale out) | one box | run the dispatcher on **N machines, same `DATABASE_URL`** — no new service |

The thing that makes this true: **workers are stateless; all state is in Postgres; the work queue IS
Postgres (`tasks` + `SKIP LOCKED`).** So "more capacity" = "more workers on the same DB" = config.

## Migration order (when you actually scale)
1. Postgres → managed (everything, incl. the work queue, follows the URL).
2. Containerize dashboard/api/frontdoor/dispatcher; run more dispatcher workers for throughput.
3. Object store → S3 backend (only when media gets large).
4. Vault → cloud KMS; Cerbos → cluster.
5. Add GPU pool + CDN for ML/streaming products.

The only genuinely additive (non-config) steps are *implementing* the S3 object-store backend and a
cloud-KMS vault backend — both are drop-in modules behind existing interfaces, not rewrites. Everything
else is the same code pointed at bigger endpoints.
