# agent-os

Infrastructure for a self-hosted "agent OS" on a Windows + WSL2 / Ubuntu host (user `swami`),
built one layer at a time, each proven by a test before moving on. Everything binds to
**localhost or the Tailscale interface only** — never `0.0.0.0`. No secrets are committed.

## From scratch
```bash
git clone git@github.com:NavinColumbia/agent-os.git ~/projects/agent-os
cd ~/projects/agent-os && bash bootstrap.sh
```
`bootstrap.sh` clones the companion **control-plane** repo
(`git@github.com:NavinColumbia/control-plane.git`), builds the venv, wires the product repo, and
proves Step 1, then prints the manual one-time steps (Docker/Tailscale/phone/gh). Full walkthrough:
**[QUICKSTART.md](QUICKSTART.md)**. Build history + restart-after-reboot: **[SETUP_LOG.md](SETUP_LOG.md)**.

## Layers (all proven)

| Step | What | Test |
|---|---|---|
| 1 | **Constraints** — product repo wired to the control-plane's `enforce_manifest.py` PreToolUse hook | `scripts/constraint_smoke_test.sh` → 8/8 (denies .env-read, push-main, force-push, registry-write, egress, deploy; allows src-edit + tests) |
| 2 | **Phone bridge** — self-hosted ntfy over Tailscale HTTPS; notify + reply listener with secret-refusal | real phone buzz; reply → `bridge/inbox/<task>.cmd`; secret reply refused |
| 3 | **Durable memory** — Postgres + pgvector; checkpoint save/restore | `scripts/crash_recovery_test.sh` → state survives `kill -9` |
| 4 | **Message bus** — NATS + JetStream work-queue, controller↔worker | `scripts/nats_roundtrip_test.py` → round-trip |
| 5 | **Reach + voice** — Tailscale; faster-whisper `small.en` STT with gated-action approval | safe transcript → agent stdin; risky → gated + echoed, never auto-run |

## Services (localhost / tailnet only)

| Service | Container | Bind | Up | Down |
|---|---|---|---|---|
| ntfy | `agentos-ntfy` | `127.0.0.1:8080` (+ tailnet via `tailscale serve`) | `cd ntfy && docker compose up -d` | `docker compose down` |
| Postgres+pgvector | `agentos-postgres` | `127.0.0.1:5433` | `cd postgres && docker compose up -d` | `docker compose down` |
| NATS+JetStream | `agentos-nats` | `127.0.0.1:4222` + `8222` | `cd nats && docker compose up -d` | `docker compose down` |
| reply listener | host process | — | `scripts/bridge.sh start` | `scripts/bridge.sh stop` |

See [`SETUP_LOG.md`](SETUP_LOG.md) for the full build record, restart-after-reboot steps, and open items.

## Secrets
`.env.local`, `postgres/.env`, `.venv/`, `*/pgdata`, `nats/data`, and all `bridge/` runtime are
**gitignored**. Copy `.env.example` → `.env.local` and fill it in on a fresh host.
