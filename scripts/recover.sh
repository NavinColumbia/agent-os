#!/usr/bin/env bash
# recover.sh — bring the whole agent-os stack back after a WSL/Windows restart.
# Idempotent: safe to run anytime; skips what's already healthy. Run as swami.
#
#   bash ~/projects/agent-os/scripts/recover.sh
#
# DATA is never lost on reboot (Postgres pgdata, NATS data, git, checkpoints are on disk).
# This only restarts the RUNNING pieces: docker daemon, tailscaled, containers, reply listener.
set -u
ROOT="$HOME/projects/agent-os"
ok(){ printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn(){ printf '  \033[33m!\033[0m %s\n' "$1"; }
hdr(){ printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

hdr "1. Docker daemon"
if docker info >/dev/null 2>&1 || sg docker -c "docker info" >/dev/null 2>&1; then ok "already running"
else sudo service docker start >/dev/null 2>&1 && sleep 3 && ok "started" || warn "failed to start docker"; fi
DK(){ sg docker -c "$*"; }   # run docker in the docker group

hdr "2. Tailscale"
if pgrep -x tailscaled >/dev/null; then ok "tailscaled running"
else
  sudo mkdir -p /var/run/tailscale /var/lib/tailscale
  sudo sh -c 'setsid tailscaled --state=/var/lib/tailscale/tailscaled.state --socket=/var/run/tailscale/tailscaled.sock >/var/log/tailscaled.log 2>&1 </dev/null &'
  sleep 3; pgrep -x tailscaled >/dev/null && ok "tailscaled started (auto-reconnects + resumes serve from saved state)" || warn "tailscaled failed"
fi
# state has wantRunning=true + serve config, so it reconnects on its own; nudge up just in case.
sudo tailscale up --operator="$USER" --hostname=nyaan >/dev/null 2>&1 || true
tailscale status >/dev/null 2>&1 && ok "tailnet up ($(tailscale ip -4 2>/dev/null | head -1))" || warn "tailnet not up yet"
tailscale serve status 2>/dev/null | grep -q ts.net && ok "serve (HTTPS) active" || warn "serve not active — run: tailscale serve --bg --https=443 http://127.0.0.1:8080"

hdr "3. Containers (restart:unless-stopped should auto-start; ensure anyway)"
for stack in ntfy postgres nats; do
  DK "cd $ROOT/$stack && docker compose up -d" >/dev/null 2>&1 && ok "$stack up" || warn "$stack failed"
done

hdr "4. Reply listener (host process)"
bash "$ROOT/scripts/bridge.sh" start >/dev/null 2>&1
pgrep -f reply_listener.py >/dev/null && ok "listener running" || warn "listener not running"

hdr "4b. NoUpload static site (private over Tailscale :8443)"
NU_DIST="$HOME/projects/products/noupload/dist"
if [ -d "$NU_DIST" ]; then
  if pgrep -f "http.server 5000" >/dev/null; then ok "static server running"
  else
    ( cd "$NU_DIST" && setsid bash -c "exec python3 -m http.server 5000 --bind 127.0.0.1" >/tmp/noupload_serve.log 2>&1 </dev/null & )
    sleep 1; pgrep -f "http.server 5000" >/dev/null && ok "static server started" || warn "static server failed"
  fi
  tailscale serve status 2>/dev/null | grep -q 8443 || tailscale serve --bg --https=8443 http://127.0.0.1:5000 >/dev/null 2>&1
  ok "exposed at https://nyaan.tail502e3f.ts.net:8443"
else
  warn "noupload dist/ not built (run: cd ~/projects/products/noupload && npm run build)"
fi

hdr "5. Health checks"
curl -s --max-time 5 http://127.0.0.1:8080/v1/health 2>/dev/null | grep -q healthy && ok "ntfy healthy (local)" || warn "ntfy not healthy"
DK "docker exec agentos-postgres pg_isready -U agentos -d agentos" >/dev/null 2>&1 && ok "postgres ready" || warn "postgres not ready"
curl -s --max-time 5 http://127.0.0.1:8222/healthz 2>/dev/null | grep -q ok && ok "nats healthy" || warn "nats not healthy"
curl -s --max-time 8 https://nyaan.tail502e3f.ts.net/v1/health 2>/dev/null | grep -q healthy && ok "ntfy reachable over Tailscale HTTPS" || warn "tailnet ntfy not reachable (phone won't get pushes until fixed)"

printf '\n\033[1mrecover.sh done.\033[0m If anything shows ! above, see SETUP_LOG.md.\n'
