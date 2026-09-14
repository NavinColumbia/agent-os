# QUICKSTART — agent-os from scratch

Target host: **Windows + WSL2 / Ubuntu**, a non-root user (here: `swami`). Two private repos:
`agent-os` (this one) and `control-plane`. `bootstrap.sh` clones control-plane and wires everything
it safely can; a few steps need you (installs, logins, phone) and are printed at the end.

## 0. One-time host prep (you, manually)
```bash
# Docker ENGINE inside WSL — NOT Docker Desktop (Desktop restarts WSL).
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"
sudo service docker start          # no systemd in WSL → start the daemon like this
# (open a new shell so the docker group applies, or prefix with: sg docker -c "...")

sudo apt-get install -y python3-venv ffmpeg     # venv (Steps 1-5) + audio decode (Step 5)
```

## 1. Clone + bootstrap
```bash
mkdir -p ~/projects && cd ~/projects
git clone git@github.com:NavinColumbia/agent-os.git
cd agent-os
# point at your control-plane fork if different:
CONTROL_PLANE_REPO=git@github.com:NavinColumbia/control-plane.git bash bootstrap.sh
```
Bootstrap will: clone+validate control-plane, build the venv, create `.env.local`, wire the
`noupload` product repo, and **prove Step 1** (constraint smoke test, 8/8). Then it prints the
manual steps below.

## 2. Fill secrets (gitignored)
```bash
# DB password:
echo "POSTGRES_PASSWORD=$(openssl rand -hex 18)" > postgres/.env
# then put the matching DATABASE_URL + a hard-to-guess NTFY_TOPIC in .env.local:
nano .env.local
```

## 3. Start the services (all localhost-only)
```bash
cd ntfy     && docker compose up -d && cd ..
cd postgres && docker compose up -d && cd ..
cd cerbos   && docker compose up -d && cd ..
bash scripts/bridge.sh start          # ntfy reply listener
```

## 4. Prove each layer
```bash
bash scripts/constraint_smoke_test.sh                 # Step 1  → 8/8
bash scripts/crash_recovery_test.sh                   # Step 3  → state survives kill -9
.venv/bin/python scripts/orchestra/bus.py             # Step 4  → durable communication round-trip
```

## 5. Reach + phone + voice (need your action)
```bash
# Tailscale (reach):
curl -fsSL https://tailscale.com/install.sh | sudo sh
sudo sh -c 'setsid tailscaled --state=/var/lib/tailscale/tailscaled.state \
  --socket=/var/run/tailscale/tailscaled.sock >/var/log/tailscaled.log 2>&1 </dev/null &'
sudo tailscale up --operator="$USER"                  # approve in browser
tailscale serve --bg --https=443 http://127.0.0.1:8080  # HTTPS in front of ntfy
# put the resulting https name in ntfy/.env (NTFY_BASE_URL) and: cd ntfy && docker compose up -d

# Phone: install Tailscale (same account) + ntfy apps. In ntfy add server = your https://<host>.ts.net,
# subscribe to <NTFY_TOPIC> and <NTFY_TOPIC>-reply.

# Voice (Step 5): transcribe + safe-route
.venv/bin/python scripts/voice_to_agent.py <audio.wav> --tmux <session:win.pane>
```

## Notes
- **Nothing binds to 0.0.0.0** — only `127.0.0.1` and the Tailscale interface.
- Secrets live ONLY in gitignored `.env.local`, `postgres/.env`, `ntfy/.env`.
- After a WSL reboot: `sudo service docker start`; restart tailscaled + `tailscale serve`;
  `docker compose up -d` in each stack; `scripts/bridge.sh start`. (See SETUP_LOG.md.)
- This WSL has no systemd, so `tailscaled` and the daemon are started manually (above).
