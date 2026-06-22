# Agent-OS Setup Log

Host: Windows + WSL2 / Ubuntu. User: **swami** (uid 1000, NOT root). Running inside tmux session `agents`.
Started: 2026-06-21.

## Golden rules in effect
- Never run as root; sudo only for system-package installs, and ask first.
- Never bind services to 0.0.0.0 — localhost or Tailscale iface only.
- No real secrets in committed files — use `.env.local`.
- One layer at a time; prove each with a test before continuing.
- Do NOT do anything that restarts WSL / the host / the Docker engine (kills this tmux session).
- Do NOT install Docker Desktop for Windows.

---

## Take-stock (2026-06-21) — DONE

| Check | Result |
|---|---|
| `whoami` / uid | `swami` / 1000 — **not root** ✅ |
| control-plane validates | `python3 scripts/validate_manifests.py` → `OK: 17 manifests valid and constitutional.` (exit 0) ✅ |
| `docker --version` | **command not found** ❌ |
| `docker ps -a` | **command not found** ❌ |
| docker pkg/daemon/service | none — no `docker`/`dockerd` binary, no dpkg package, no `/etc/init.d/docker` ❌ |
| `tmux -V` | tmux 3.4 ✅ |
| `python3 --version` | Python 3.12.3 ✅ |
| pyyaml | 6.0.1 ✅ (required by enforce_manifest hook) |
| tailscale | NOT FOUND (expected; Step 5) |
| product template | found at `control-plane/templates/product-repo` (dir skeleton: `.claude/agents`, `.github/workflows`, `docs/adr`; no files yet) ✅ |
| constraint engine | `control-plane/hooks/enforce_manifest.py` (PreToolUse hook) + `roles/builder.yaml` manifest |

### Docker — RESOLVED (2026-06-21)
- Installed Docker Engine **29.6.0** natively in WSL via the official `get.docker.com` script (no Desktop).
- swami added to `docker` group; daemon started with `sudo service docker start`.
- swami granted **scoped** passwordless sudo in `/etc/sudoers.d/swami-docker` (apt-get, apt,
  `sh /tmp/get-docker.sh`, service, usermod, systemctl only — NOT full root).
- Group change applies to new logins; in-session I use `sg docker -c "docker …"` so no relogin/restart needed.
- `docker ps` → empty list ✅.
- To start daemon after a WSL boot: `sudo service docker start`.

### ⚠️ (historical) BLOCKER: Docker was not installed
The brief states Docker is already installed in this WSL, but there is no `docker`/`dockerd`
binary, no package, and no daemon. **Steps 2 (ntfy), 3 (Postgres/pgvector), 4 (NATS) all require
Docker.** Step 1 (constraints) and most of Step 5 (whisper/tmux) do not.
→ Surfaced to swami as ⏸ NEED FROM YOU. Proceeding with Step 1 in the meantime (unblocked).

---

## Step 1 — Product template + constraint proof
Status: ✅ DONE & PROVEN (2026-06-21)

- Copied `control-plane/templates/product-repo` → `~/projects/products/noupload`.
- Added minimal real repo: `src/app.py`, `tests/test_app.py`, and a `.env` (so "read .env" is a real target).
- Wired `~/projects/products/noupload/.claude/settings.json`:
  - `env.CP` = `/home/swami/projects/control-plane`
  - `env.CP_MANIFEST` = `…/roles/builder.yaml`
  - PreToolUse hook `*` → `python3 …/hooks/enforce_manifest.py`
- Smoke test: `~/projects/agent-os/scripts/constraint_smoke_test.sh` → **8 passed, 0 failed**.
  DENIED: read .env, push main, force-push, write registry, network egress (curl), deploy.
  ALLOWED: edit src/, run tests.
- Re-run anytime: `bash ~/projects/agent-os/scripts/constraint_smoke_test.sh`

## Step 2 — Phone notifications (ntfy)
Status: ✅ DONE & PROVEN END-TO-END (2026-06-21)
- Real phone (navinashoks-s24) buzzed on urgent notify; reply captured to `bridge/inbox/deploy-fix.cmd`
  (`restart the worker`); secret reply (anthropic key) REFUSED → `bridge/rejected/`. 
- Phone subscribes via HTTPS: server `https://nyaan.tail502e3f.ts.net`, topics
  `swami-agentos-f20c58aa777c` (+ `-reply`). Tailscale Serve terminates TLS; ntfy stays localhost-only.
- notify.py uses ntfy JSON API (UTF-8; emoji-safe). Parser accepts ntfy Title, `task:` prefix,
  or `Title:/Message:` labeled body.
- Manage listener: `scripts/bridge.sh start|stop|status`. Send: `python3 scripts/notify.py "msg"`.

Built:
- `ntfy/docker-compose.yml` — `binwiederhier/ntfy`, container `agentos-ntfy`, bound **127.0.0.1:8080 only**
  (verified port map `127.0.0.1:8080->80/tcp`, health `{"healthy":true}`). Stop: `cd ~/projects/agent-os/ntfy && docker compose down`.
- `scripts/notify.py` — outbound (agent→phone); refuses to send if message looks like a secret.
- `scripts/reply_listener.py` — subscribes to `<TOPIC>-reply/json`, writes good replies to
  `bridge/inbox/<task>.cmd`, refuses secrets (redacted note → `bridge/rejected/`, notifies back).
- `scripts/secret_filter.py` — shared secret/API-key detector (openai/anthropic/aws/gh/slack/jwt/private-key/high-entropy).
- `.env.local` (gitignored) holds NTFY_TOPIC; `.env.example` is the committed template.

Local proof (no phone needed) — PASSED:
- notify.py sent a high-priority message to the topic.
- reply `restart the worker` → captured to `bridge/inbox/deploy-fix.cmd` ✅
- reply with an Anthropic key → REFUSED; not stored; redacted note in `bridge/rejected/` ✅

⚠️ Reachability gap: a localhost-bound server is NOT reachable from the phone. The real
"phone buzzed" test needs a network path (Tailscale, Step 5) or rebinding ntfy to the
Tailscale interface. Raised to swami as ⏸.

Reachability RESOLVED via Tailscale (Step 5 brought forward):
- ntfy rebound to listen on `127.0.0.1:8080` + `100.93.201.41:8080` (Tailscale IP) — never 0.0.0.0.
  Verified port map + `ss`. Health green on both. NTFY_BASE_URL=http://100.93.201.41:8080.
- Topic: `swami-agentos-f20c58aa777c`  (replies on `…-reply`).
- Reply listener runs persistently; manage with `scripts/bridge.sh start|stop|status`.
- Phone (navinashoks-s24, 100.98.116.33) must: add server http://100.93.201.41:8080 in ntfy app,
  subscribe to topic + `-reply`. Tailscale must be ON on phone.
- PENDING swami: subscribe on phone, then I send the real buzz + capture a real reply.

Update: phone ntfy app forced HTTPS → "unable to parse TLS packet header" against plain-HTTP ntfy.
Fix: front ntfy with Tailscale Serve HTTPS at `https://nyaan.tail502e3f.ts.net` (TLS terminated by
Tailscale, ntfy stays localhost-bound). Serve needs one-time tailnet enable:
`https://login.tailscale.com/f/serve?node=nw2JQkE9aP11CNTRL` → PENDING swami "enabled".
After enable: `tailscale serve --bg --https=443 http://127.0.0.1:8080`; phone server becomes
`https://nyaan.tail502e3f.ts.net` (same topics). MagicDNS name: nyaan.tail502e3f.ts.net.

## Step 2 (old marker) — BLOCKED on Docker + topic name
## Step 3 — Durable memory + crash recovery (Postgres + pgvector)
Status: ✅ DONE & PROVEN (2026-06-22)
- `postgres/docker-compose.yml` — image `pgvector/pgvector:pg16`, container `agentos-postgres`,
  bound **127.0.0.1:5433 only** (5433 to avoid clashing with any system pg on 5432). Healthcheck pg_isready.
- `postgres/initdb/01-schema.sql` — `CREATE EXTENSION vector` (pgvector 0.8.3 verified) +
  tables `task_checkpoints` (crash recovery) and `memories` (vector(384) durable memory).
- Secrets: password in `postgres/.env` (chmod 600, gitignored); `DATABASE_URL` in `.env.local` (gitignored).
- venv at `~/projects/agent-os/.venv` (psycopg 3.3.4, requests) — use `.venv/bin/python` for DB code.
- `scripts/checkpoint.py` — save/restore/list task state (upsert + monotonic seq).
- TEST `scripts/crash_recovery_test.sh`: save → `kill -9` worker → restore from fresh process →
  **PASS: exact state survived**. Re-run anytime.
- Stop DB: `cd ~/projects/agent-os/postgres && docker compose down` (data persists in `pgdata/`).
## Step 4 — Message bus (NATS + JetStream)
Status: ✅ DONE & PROVEN (2026-06-22)
- `nats/docker-compose.yml` — `nats:2.10-alpine`, container `agentos-nats`, JetStream on (`-js -sd /data`).
  Bound **127.0.0.1:4222** (client) + **127.0.0.1:8222** (monitoring) only. Cluster port 6222 exposed but
  NOT published to host. Health: `curl 127.0.0.1:8222/healthz` → `{"status":"ok"}`.
- Stream `WORK` = **WorkQueuePolicy** over subject `work.tasks` (each task to exactly one worker, removed on ack).
- `scripts/nats_roundtrip_test.py`: controller publishes task → JetStream → durable pull consumer
  `workers` fetches+acks → replies on `work.reply.<id>` → controller receives.
  **PASS: round-trip, result=42.** Run: `.venv/bin/python scripts/nats_roundtrip_test.py`.
- Stop bus: `cd ~/projects/agent-os/nats && docker compose down` (JetStream data persists in `nats/data/`).

### Multi-tenant later (NOT built now — by design)
NATS **accounts** are the isolation primitive: one account per product/tenant makes each tenant's
subjects + streams invisible to the others, enforced server-side. To enable: add a server config
(`server.conf` with an `accounts {}` block, or operator/account JWTs) and give each tenant its own
creds; mount it via the compose `command`/volume. v1 runs a single implicit account. A stub note is
in `nats/docker-compose.yml`.
## Step 5 — Reach (Tailscale) + voice (faster-whisper)
Status: ✅ DONE & PROVEN (2026-06-22)

REACH (Tailscale) — done:
- nyaan=100.93.201.41, phone navinashoks-s24=100.98.116.33, same tailnet (artmusicasia@gmail.com).
  Host↔phone ping OK. ntfy reachable from phone via `tailscale serve` HTTPS (https://nyaan.tail502e3f.ts.net).

VOICE-IN — done & proven:
- `scripts/transcribe.py` — faster-whisper `small.en`, CPU int8 (model cached in ~/.cache). Pure STT.
- `scripts/voice_guard.py` — fail-closed classifier for gated/risky intents (deploy, push main, delete,
  sudo, secrets, money, public comms, pipe-to-shell, host disruption, empty/garbled).
- `scripts/voice_to_agent.py` — SAFE transcript → delivered to agent (tmux send-keys or FIFO) + echoed;
  GATED → NOT delivered, parked in `bridge/pending/<task>.voice`, echoed back to phone via notify,
  awaits confirmation through the normal approval path. Voice = input method, never an auto-executor.
- TEST: espeak-ng samples. SAFE "edit the source file and run the tests" → transcribed → landed in
  agent pane stdin. RISKY "deploy to production right now" → transcribed → GATED(deploy) → NOT delivered
  (stdin unchanged) → parked + phone echo. **PASS.**
- Reproduce: `.venv/bin/python scripts/voice_to_agent.py samples/safe.wav --tmux <target>`.

### Original take-stock items now resolved
- swami granted **full** passwordless sudo (`/etc/sudoers.d/swami-nopasswd`, NOPASSWD:ALL).
  ⚠️ broad — consider narrowing back to the scoped docker file after setup.
- Tailscale **1.98.4** (apt). No systemd, so `tailscaled` started MANUALLY & detached:
  `sudo sh -c 'setsid tailscaled --state=/var/lib/tailscale/tailscaled.state --socket=/var/run/tailscale/tailscaled.sock >/var/log/tailscaled.log 2>&1 </dev/null &'`
  After a WSL boot, re-run that line, then `sudo tailscale up`, then `tailscale serve --bg --https=443 http://127.0.0.1:8080`.

- swami granted **full** passwordless sudo (`/etc/sudoers.d/swami-nopasswd`, NOPASSWD:ALL).
  ⚠️ broad — consider narrowing back to the scoped docker file after setup.
- Tailscale **1.98.4** installed (apt). No systemd here, so `tailscaled` started MANUALLY & detached:
  `sudo sh -c 'setsid tailscaled --state=/var/lib/tailscale/tailscaled.state --socket=/var/run/tailscale/tailscaled.sock >/var/log/tailscaled.log 2>&1 </dev/null &'`
  (kernel mode; /dev/net/tun present). To restart after WSL boot: same line + `sudo tailscale up`.
## Reboot resilience & recovery (Windows restart / WSL shutdown)
**Data is never lost on reboot** — it lives on disk: `postgres/pgdata` (47M cluster), `nats/data`
(JetStream), `ntfy/cache`, `/var/lib/tailscale/tailscaled.state` (no re-auth needed), task state in
the `task_checkpoints` table, and both git repos pushed to GitHub. A reboot only stops *running*
services; nothing is erased.

What a reboot stops + how it comes back:
- Docker daemon (no systemd) → restarted by the boot hook. Containers have `restart:unless-stopped`
  so they auto-start once the daemon is up.
- `tailscaled` (manual daemon) → restarted by the boot hook; **auto-reconnects and resumes
  `tailscale serve` from saved state** (no browser re-auth, no re-running serve).
- Reply listener (host process) → restarted by the boot hook.

Three layers of recovery:
1. **Automatic on boot** — `/etc/wsl.conf` `[boot] command = /usr/local/sbin/agentos-boot.sh`
   runs as root on every WSL start → docker + tailscaled + `recover.sh`. Tested via
   `sudo /usr/local/sbin/agentos-boot.sh` (all green). Takes effect on next WSL start.
   Reinstall/reproduce with `scripts/install_autostart.sh`.
2. **One command** — `bash ~/projects/agent-os/scripts/recover.sh` (idempotent; brings the whole
   stack back and health-checks each piece). Use this if you ever want to recover manually, or just
   tell Claude "run recover.sh".
3. **Total rebuild** — if the WSL distro is wiped: re-clone both repos and run `bash bootstrap.sh`
   (data in old volumes is gone, but code/config/secrets-template are reproducible; DB/NATS start fresh).

Scripts: `scripts/recover.sh`, `scripts/agentos-boot.sh` (installed to /usr/local/sbin),
`scripts/install_autostart.sh`.

## Remote
Private GitHub repo: **https://github.com/NavinColumbia/agent-os** (account NavinColumbia, SSH).
Push updates with `git push`. Secrets (`.env.local`, `postgres/.env`) and all data/runtime dirs are
gitignored and confirmed absent from the remote.

## Running services (all localhost / tailnet only)
| Service | Container | Bind | Start | Stop |
|---|---|---|---|---|
| ntfy | agentos-ntfy | 127.0.0.1:8080 (tailnet via `tailscale serve` 443) | `cd ~/projects/agent-os/ntfy && docker compose up -d` | `docker compose down` |
| Postgres+pgvector | agentos-postgres | 127.0.0.1:5433 | `cd ~/projects/agent-os/postgres && docker compose up -d` | `docker compose down` |
| NATS+JetStream | agentos-nats | 127.0.0.1:4222 + 8222 | `cd ~/projects/agent-os/nats && docker compose up -d` | `docker compose down` |
| reply listener | (host process) | — | `scripts/bridge.sh start` | `scripts/bridge.sh stop` |
| tailscaled | (host daemon) | tailnet | see Step 5 boot note | `sudo tailscale down` |

After a WSL reboot: `sudo service docker start`; restart tailscaled (Step 5 note) + `tailscale serve`;
`docker compose up -d` in each of ntfy/ postgres/ nats/; `scripts/bridge.sh start`.

## What I need from you (open / optional)
1. (optional, security) Narrow sudo back from NOPASSWD:ALL to the scoped `/etc/sudoers.d/swami-docker`.
2. (future) Multi-tenant NATS accounts; durable systemd-style autostart for tailscaled (no systemd in this WSL).
