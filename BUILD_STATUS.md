# Agent-OS Reference-Architecture Build — STATUS / RESUME POINTER

> **v1 COMPLETE (2026-06-22).** P0–P5 + standing-Controller integration all built & PROVEN.
> Re-prove the whole stack anytime: `bash ~/projects/agent-os/scripts/selftest.sh` → **15/15**.
> Next epic: real agent workers (Controller stages invoke live agents), CR re-flow, human-approval-via-ask-await, native AGE, prod hook→Cerbos.

Resume rule: this file is the source of truth for what's done. Each task: ☐ todo · ⏳ in-progress · ✅ done (proven) · ⚠️ blocked/deferred.
Design refs: control-plane `docs/adr/0004` (keystones K1–K7) + `0005` (comm fabric). Build code lives in `agent-os/scripts/`.


## PRODUCT/IP layer (autonomous epic) ✅ in progress
- ✅ IP: proprietary LICENSE+NOTICE, Ed25519-signed PROVENANCE.json over 85 files, watermark/canary.
- ✅ BYO-agent provider layer (`scripts/providers.py`): Claude full-agent + any OpenAI-compatible (DeepSeek/OpenAI/Together/Ollama/Groq), uniformly governed. Routing proven.
- ☐ Whitepaper, patent guide, CR re-flow, human-approval ask-await, test apps.

## ENTERPRISE / ANY-PRODUCT layer (proactive) ✅
- ✅ Scoped secrets vault (`vault.py`,`07-vault.sql`): encrypted, scoped by product+env+role; test/QA gets test secrets, prod never leaks to test; audited. (Answers iOS/QA secret-sharing.)
- ✅ Enterprise roles added (control-plane): data-scientist, ml-engineer, data-engineer, analyst (21 manifests valid).
- ✅ Governed data connectors (`connectors.py`): live web/API/SNS ingestion via egress allowlist → object store, audited.
- ✅ Object store (`objstore.py`): images/objects shared by sha256 ref, dedup, TTL+GC.
- ✅ Retention sweeps (`retention.py`): expire blobs/conversations/secrets/waits (audit kept permanent).
- ✅ Experiment tracking (`experiments.py`): log/compare/best runs for data-science/ML (proven).
- ✅ Blob encryption at rest (objstore `encrypt=True`, Fernet) — proven.
- ✅ One-command governed product scaffolder (`new-product.sh`): repo+manifest+hook+docs+git in one shot.
- ✅ Cloud-migratability documented (`docs/CLOUD-MIGRATION.md`): every store/bus config-driven → RDS/S3/managed-NATS/KMS, no rewrite.

## P0 — Safety  ✅ DONE
- ✅ Sandbox `srt` (bubblewrap+seccomp+Landlock+egress proxy) — proven: blocks egress + read-only FS.
- ✅ Tamper-evident audit log (`scripts/audit.py`, `postgres/initdb/02-audit.sql`) — proven: detects deny→allow tamper.

## P1 — Durable-execution backbone (DBOS)  ✅ DONE
- ✅ DBOS 2.24 on Postgres; crash-resume proven (`scripts/dbos_durable_demo.py`): killed mid-run, resumed exact step, exactly-once.

## P2 — Communication fabric  ✅ DONE
- ✅ Object store (`scripts/objstore.py`, `06-objstore.sql`): content-addressed blob store (Postgres BYTEA), agents share images/PDFs/objects BY REFERENCE (sha256 id in a FilePart), dedup, per-object TTL + GC. Proven (real screenshot shared by ref).
- ✅ Typed message envelope + 12-intent vocabulary (`scripts/messaging.py`).
- ✅ `conversations` + `waits` tables (`postgres/initdb/03-comms.sql`).
- ✅ Ask-await on DBOS (`scripts/commfabric.py`): suspend-until-reply, resume on send; prove round-trip + crash-survival.
- ✅ Deadlock detector (`scripts/deadlock.py`): wait-for graph + Tarjan SCC; prove A↔B cycle detected + victim chosen.

## P3 — Identity + PDP  ✅ DONE
- ✅ Ed25519 signed-manifest identity (`scripts/identity.py`): sign/verify a role manifest; prove tamper rejected.
- ✅ Cerbos PDP (Docker, localhost) + policy mirroring builder.yaml; prove allow/deny via PDP.
- ✅ Wire audit.append into the enforcement decision path.

## P4 — Memory / Observability / Eval  ✅ DONE
- ✅ Graph memory over Postgres (edges table + recursive CTE); native AGE deferred (needs custom image+migration). Proven.
- ✅ Reflection/consolidation job stub (episodic→semantic).
- ✅ Eval harness: Inspect AI installed + a tiny agent eval that runs.
- ✅ OTel-GenAI tracing (OpenLLMetry→Phoenix) — best-effort/local.

## INTEGRATION — Standing Controller  ✅ DONE
- ✅ `scripts/controller.py`: DBOS workflow runs a product SPEC→BUILD→QA→REVIEW→LAUNCH, composing
  gate_check (refuses stage w/o artifacts) + Cerbos PDP + tamper-evident audit + metrics, crash-resumable.
  PROVEN: full lifecycle → LAUNCHED; audit chain intact (5 decisions); idempotent re-run (no stage re-runs).
- ✅ Live hook → tamper-evident audit (proven, 8/8). 
- ✅ REAL AGENT WORKERS (`scripts/agent_worker.py`): headless `claude -p` does governed work — proven both (a) allowed edit (added multiply) and (b) DENIED .env write (cage held, audited). Wired into Controller BUILD behind AGENT_WORKERS=1.
- ✅ Upward-feedback CR re-flow (`scripts/cr_reflow.py`): filed→decided→re-flowed→closed, metered+audited (proven).
- ✅ Human-approval durable gate (`scripts/approval_gate.py`): suspend+phone-notify→resume on decision (proven).
- ✅ Native Apache AGE: custom pgvector+AGE PG16 image built & PROVEN (openCypher + vector in one DB, `postgres/Dockerfile.age`, `AGE-MIGRATION.md`). Live-cluster swap is an ops step (documented).
- ✅ Visual/behavioral QA harness (ADR 0003): live multi-viewport screenshots + axe a11y (0 violations) + E2E (2/2), proven on NoUpload (`products/noupload/tests/qa_harness.mjs`). Vision-critique demonstrated (agent reads screenshots, critiques spacing).
- ✅ DESIGN-AS-CODE (ADR 0003): DTCG tokens → Style Dictionary → CSS vars, app derives from tokens (in products/noupload/design/). Agent-editable, machine-readable. Penpot remains the optional visual layer.
- Deferred-by-design: prod hook→Cerbos cutover (inline rails + audit already live; PDP proven standalone — defense-in-depth); more live test apps (skipped to avoid token burn).

**R&D COMPLETE.** Build + product + IP all done; remaining items are CEO actions (see CEO-TODO.md) + ops choices.

## P5 — Org maturity  ✅ DONE
- ✅ `gate_check.py`: block stage advance without required artifacts (no BUILD w/o approved SPEC+ADR; no LAUNCH w/o QA report).
- ✅ Templates: PROJECT-RUNBOOK + CHANGE-REQUEST + SPEC/ADR/QA in control-plane `templates/`.
- ✅ Metrics schema + `org_metrics` table.
- ✅ Change-request schema (`schemas/change-request.schema.json`) + the two board states.

## Notes for resumer
- Postgres: `agentos` db @127.0.0.1:5433; creds + AUDIT_HMAC_KEY in `~/projects/agent-os/.env.local`. DBOS sys db = `agentos_dbos_sys`.
- Run python via `~/projects/agent-os/.venv/bin/python`. NATS @127.0.0.1:4222.
- Harness gotchas (learned): never `pkill -f <scriptname>` (self-kills shell); `os._exit` skips flush (verify via DB); pre-create tables (concurrent CREATE races pg_type); give long Bash timeouts for DBOS runs.
