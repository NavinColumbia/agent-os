# Agent-OS Reference-Architecture Build — STATUS / RESUME POINTER

Resume rule: this file is the source of truth for what's done. Each task: ☐ todo · ⏳ in-progress · ✅ done (proven) · ⚠️ blocked/deferred.
Design refs: control-plane `docs/adr/0004` (keystones K1–K7) + `0005` (comm fabric). Build code lives in `agent-os/scripts/`.

## P0 — Safety  ✅ DONE
- ✅ Sandbox `srt` (bubblewrap+seccomp+Landlock+egress proxy) — proven: blocks egress + read-only FS.
- ✅ Tamper-evident audit log (`scripts/audit.py`, `postgres/initdb/02-audit.sql`) — proven: detects deny→allow tamper.

## P1 — Durable-execution backbone (DBOS)  ✅ DONE
- ✅ DBOS 2.24 on Postgres; crash-resume proven (`scripts/dbos_durable_demo.py`): killed mid-run, resumed exact step, exactly-once.

## P2 — Communication fabric  ✅ DONE
- ✅ Typed message envelope + 12-intent vocabulary (`scripts/messaging.py`).
- ✅ `conversations` + `waits` tables (`postgres/initdb/03-comms.sql`).
- ✅ Ask-await on DBOS (`scripts/commfabric.py`): suspend-until-reply, resume on send; prove round-trip + crash-survival.
- ✅ Deadlock detector (`scripts/deadlock.py`): wait-for graph + Tarjan SCC; prove A↔B cycle detected + victim chosen.

## P3 — Identity + PDP  ☐
- ☐ Ed25519 signed-manifest identity (`scripts/identity.py`): sign/verify a role manifest; prove tamper rejected.
- ☐ Cerbos PDP (Docker, localhost) + policy mirroring builder.yaml; prove allow/deny via PDP.
- ☐ Wire audit.append into the enforcement decision path.

## P4 — Memory / Observability / Eval  ☐
- ☐ Apache AGE graph extension in Postgres (or note blocker); prove a graph query alongside pgvector.
- ☐ Reflection/consolidation job stub (episodic→semantic).
- ☐ Eval harness: Inspect AI installed + a tiny agent eval that runs.
- ☐ OTel-GenAI tracing (OpenLLMetry→Phoenix) — best-effort/local.

## P5 — Org maturity  ☐
- ☐ `gate_check.py`: block stage advance without required artifacts (no BUILD w/o approved SPEC+ADR; no LAUNCH w/o QA report).
- ☐ Templates: PROJECT-RUNBOOK + CHANGE-REQUEST + SPEC/ADR/QA in control-plane `templates/`.
- ☐ Metrics schema + `org_metrics` table.
- ☐ Change-request schema (`schemas/change-request.schema.json`) + the two board states.

## Notes for resumer
- Postgres: `agentos` db @127.0.0.1:5433; creds + AUDIT_HMAC_KEY in `~/projects/agent-os/.env.local`. DBOS sys db = `agentos_dbos_sys`.
- Run python via `~/projects/agent-os/.venv/bin/python`. NATS @127.0.0.1:4222.
- Harness gotchas (learned): never `pkill -f <scriptname>` (self-kills shell); `os._exit` skips flush (verify via DB); pre-create tables (concurrent CREATE races pg_type); give long Bash timeouts for DBOS runs.
