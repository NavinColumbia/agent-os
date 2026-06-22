#!/usr/bin/env bash
# bootstrap.sh — set up agent-os from scratch on a Windows+WSL2 / Ubuntu host.
# Idempotent: re-running skips what's already done. It AUTOMATES the safe parts and
# PRINTS the manual one-time steps (installs/logins/phone) it must not do for you.
#
#   CONTROL_PLANE_REPO  git URL to clone control-plane from (default below)
#
# It never runs anything destructive and never needs sudo itself.
set -u

CONTROL_PLANE_REPO="${CONTROL_PLANE_REPO:-git@github.com:NavinColumbia/control-plane.git}"
ROOT="$HOME/projects/agent-os"
CP="$HOME/projects/control-plane"
PRODUCT="noupload"

say()  { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }

say "1. Prerequisites (host tools)"
for t in git python3 tmux; do have "$t" && ok "$t: $(command -v $t)" || warn "$t MISSING — install it (sudo apt-get install $t)"; done
have docker && ok "docker: $(docker --version 2>/dev/null)" || warn "docker MISSING — install Docker ENGINE in WSL (curl -fsSL https://get.docker.com | sudo sh). NOT Docker Desktop."
docker ps >/dev/null 2>&1 || warn "docker daemon not reachable — 'sudo service docker start' (no systemd in this WSL)."
have ffmpeg && ok "ffmpeg (voice)" || warn "ffmpeg MISSING (Step 5 voice) — sudo apt-get install ffmpeg"
have tailscale && ok "tailscale present" || warn "tailscale MISSING (Step 5 reach) — curl -fsSL https://tailscale.com/install.sh | sudo sh"
have gh && ok "gh present" || warn "gh MISSING (optional, for pushing) — apt-get install gh"

say "2. control-plane"
if [ -d "$CP/.git" ]; then ok "already at $CP"
else
  git clone "$CONTROL_PLANE_REPO" "$CP" && ok "cloned control-plane" || warn "clone failed — set CONTROL_PLANE_REPO or check SSH auth"
fi
if [ -f "$CP/scripts/validate_manifests.py" ]; then
  python3 "$CP/scripts/validate_manifests.py" && ok "control-plane manifests valid" || warn "manifest validation FAILED"
fi

say "3. Python venv + deps"
if [ ! -x "$ROOT/.venv/bin/python" ]; then
  python3 -m venv "$ROOT/.venv" 2>/dev/null || warn "venv failed — sudo apt-get install python3-venv, then re-run"
fi
if [ -x "$ROOT/.venv/bin/pip" ]; then
  "$ROOT/.venv/bin/pip" install -q --upgrade pip >/dev/null 2>&1
  "$ROOT/.venv/bin/pip" install -q -r "$ROOT/requirements.txt" && ok "venv deps installed" || warn "pip install failed"
fi

say "4. Local config"
[ -f "$ROOT/.env.local" ] && ok ".env.local exists" || { cp "$ROOT/.env.example" "$ROOT/.env.local"; warn ".env.local created from template — EDIT IT (set NTFY_TOPIC, DATABASE_URL password)"; }
[ -f "$ROOT/ntfy/.env" ] || cp "$ROOT/ntfy/.env.example" "$ROOT/ntfy/.env"
[ -f "$ROOT/postgres/.env" ] || { warn "postgres/.env missing — create it with: echo POSTGRES_PASSWORD=\$(openssl rand -hex 18) > $ROOT/postgres/.env"; }

say "5. Wire the product repo (Step 1)"
if [ -d "$HOME/projects/products/$PRODUCT" ]; then ok "product repo '$PRODUCT' already wired"
else CP="$CP" bash "$ROOT/scripts/wire_product_repo.sh" "$PRODUCT" && ok "wired '$PRODUCT'"; fi

say "6. PROVE Step 1 (constraints)"
REPO="$HOME/projects/products/$PRODUCT" CP="$CP" bash "$ROOT/scripts/constraint_smoke_test.sh" \
  && ok "constraints enforced (8/8)" || warn "smoke test FAILED"

say "7. Manual one-time steps you must do (cannot be automated)"
cat <<EOF
  a) Docker (if missing):   curl -fsSL https://get.docker.com | sudo sh
                            sudo usermod -aG docker \$USER ; sudo service docker start
  b) Start services:        cd $ROOT/ntfy && docker compose up -d
                            cd $ROOT/postgres && docker compose up -d
                            cd $ROOT/nats && docker compose up -d
  c) Phone bridge (Step 2): edit $ROOT/.env.local NTFY_TOPIC; bash $ROOT/scripts/bridge.sh start
  d) Tailscale (Step 5):    sudo tailscale up --operator=\$USER ; then on phone install Tailscale+ntfy
                            tailscale serve --bg --https=443 http://127.0.0.1:8080
  e) Prove each step:       bash $ROOT/scripts/crash_recovery_test.sh        # Step 3
                            $ROOT/.venv/bin/python $ROOT/scripts/nats_roundtrip_test.py  # Step 4
  See QUICKSTART.md and SETUP_LOG.md for full detail.
EOF
echo
ok "bootstrap finished (manual steps above)"
