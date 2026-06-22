# Cloud / Scale Migration — agent-os

Design principle: **single-box by default, cloud-portable by construction.** Every service is a
container or a process configured by env vars — nothing is hard-wired to localhost beyond bind
addresses. Moving to cloud (for scale, GPUs, or many users) is a config + endpoint swap, not a rewrite.

| Service | Local now | Cloud target | How to migrate |
|---|---|---|---|
| **Postgres** (state, audit, comms, memory, metrics, vault, blobs) | `pgvector/pgvector:pg16` container | **RDS / Cloud SQL / Neon / Crunchy** (managed Postgres + pgvector) | change `DATABASE_URL` in `.env.local`; `pg_dump`→restore. DBOS, audit, vault, objstore all just follow the URL. |
| **Object store** (images/videos/files) | Postgres `BYTEA` (`objstore.py`) | **S3 / GCS / R2** | `objstore` is a thin interface — add an `OBJSTORE_BACKEND=s3` backend (put/get/meta/gc) keyed by the same sha256. Large media (video) belongs on S3; metadata stays in Postgres. |
| **Message bus** (NATS/JetStream) | `nats:2.10` container, localhost | **Synadia Cloud / self-host NATS cluster**, or Kafka/Redpanda for huge throughput | change `NATS_URL`; JetStream config is portable. The comm fabric is transport-agnostic. |
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

## Migration order (when you actually scale)
1. Postgres → managed (everything follows the URL).
2. Object store → S3 backend (for media/video).
3. Containerize Controller/agents → k8s; NATS → cluster.
4. Vault → cloud KMS; Cerbos → cluster.
5. Add GPU pool + CDN for ML/streaming products.

Nothing here is a rewrite — it's the same code pointed at bigger endpoints.
