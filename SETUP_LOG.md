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
Status: 🟡 mechanism PROVEN locally; real phone buzz pending (needs your topic + reachability)

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

Pending from swami: hard-to-guess topic name; phone app install + subscription; decision on
reachability (bring Tailscale forward vs. accept local proof for now).

## Step 2 (old marker) — BLOCKED on Docker + topic name
## Step 3 — Durable memory (Postgres+pgvector) — BLOCKED on Docker
## Step 4 — Message bus (NATS+JetStream) — BLOCKED on Docker
## Step 5 — Reach (Tailscale) + voice (faster-whisper) — pending

---

## What I need from you (open)
1. **Docker**: it is not installed in this WSL. See ⏸ block in chat. Needed before Steps 2–4.
2. (Step 2) a hard-to-guess ntfy topic name + confirm phone app installed/subscribed.
