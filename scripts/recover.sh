#!/usr/bin/env bash
# recover.sh — WSL boot auto-recovery for the Agent OS control plane.
#
# Recovery is intentionally conservative. Host services are reconciled through
# service_recovery.py, which binds ownership to an exact process generation and
# exact argv. A healthy legacy/unowned process is never killed or duplicated.
set -u

ROOT="${AOS_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PY="$ROOT/.venv/bin/python"
RECOVERY="$ROOT/scripts/service_recovery.py"

ok(){ printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn(){ printf '  \033[33m!\033[0m %s\n' "$1"; }
hdr(){ printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

docker_run() {
  if docker info >/dev/null 2>&1; then
    docker "$@"
    return
  fi
  local command
  printf -v command '%q ' docker "$@"
  sg docker -c "$command"
}

recover_service() {
  local name="$1" label="$2" output rc
  output="$($PY "$RECOVERY" repair "$name" 2>&1)"
  rc=$?
  if printf '%s' "$output" | grep -q '"state": "healthy"'; then
    ok "$label"
  elif printf '%s' "$output" | grep -Eq '"state": "(legacy_adoption_deferred|unowned_readiness_deferred)"'; then
    warn "$label is legacy/unowned but was left untouched (safe adoption deferred)"
  else
    warn "$label failed recovery (rc=$rc): $output"
    return 1
  fi
}

hdr "1. Docker daemon"
if docker_run info >/dev/null 2>&1; then
  ok "already running"
elif sudo service docker start >/dev/null 2>&1; then
  sleep 3
  docker_run info >/dev/null 2>&1 && ok "started" || warn "daemon started but is not ready"
else
  warn "failed to start Docker"
fi

hdr "2. Optional private Tailscale edge"
if command -v tailscale >/dev/null 2>&1; then
  if pgrep -x tailscaled >/dev/null 2>&1; then
    ok "tailscaled running"
  else
    sudo mkdir -p /var/run/tailscale /var/lib/tailscale
    sudo sh -c 'setsid tailscaled --state=/var/lib/tailscale/tailscaled.state --socket=/var/run/tailscale/tailscaled.sock >/var/log/tailscaled.log 2>&1 </dev/null &' || true
    sleep 3
    pgrep -x tailscaled >/dev/null 2>&1 && ok "tailscaled started" || warn "tailscaled failed"
  fi
  sudo tailscale up --operator="$USER" --hostname=nyaan >/dev/null 2>&1 || true
  tailscale status >/dev/null 2>&1 && ok "tailnet connected" || warn "tailnet not connected"
else
  warn "Tailscale not installed; public edge may still be provided by Caddy"
fi

hdr "3. Container dependencies"
for stack in ntfy postgres cerbos; do
  if docker_run compose --project-directory "$ROOT/$stack" up -d >/dev/null 2>&1; then
    ok "$stack up"
  else
    warn "$stack failed"
  fi
done

hdr "4. Non-database host services"
recover_service noupload-static "NoUpload static site" || true

# Do not launch database clients while Postgres is merely 'starting'. This
# barrier is bounded so boot cannot hang forever, but failure is fail-closed:
# no DB-dependent host process is started against an unavailable database.
POSTGRES_READY=0
for _attempt in $(seq 1 12); do
  if docker_run exec agentos-postgres pg_isready -U agentos -d agentos >/dev/null 2>&1; then
    POSTGRES_READY=1
    break
  fi
  sleep 5
done

if [ "$POSTGRES_READY" -ne 1 ]; then
  warn "Postgres was not ready after 60 seconds; DB-dependent host services were not started"
  exit 1
fi
ok "Postgres ready"

hdr "5. Database-dependent host services"
if ! grep -qE '^AOS_API_TOKEN=.+' "$ROOT/.env.local" 2>/dev/null; then
  warn "AOS_API_TOKEN is unset; authenticated API recovery will fail closed"
fi
recover_service api "API on 127.0.0.1:8090" || true
recover_service dashboard "mission-control dashboard" || true
recover_service jobd "durable controller worker" || true
recover_service evidence-publisher "deferred QA evidence publisher" || true
recover_service frontdoor "self-serve front door" || true
recover_service console "tenant CEO console" || true
recover_service assurance "public Release Assurance intake" || true
recover_service statuspage "public status page" || true
recover_service metrics "Prometheus metrics exporter" || true
recover_service ticker "scheduler ticker" || true
recover_service watchdog "health watchdog" || true
recover_service dispatcher "agent dispatcher" || true
recover_service reply-listener "phone reply listener" || true
recover_service replybridge "reply bridge" || true
recover_service cockpit-api "CEO cockpit API" || true
recover_service cockpit-web "CEO cockpit web app" || true

hdr "6. Optional private routes"
if command -v tailscale >/dev/null 2>&1 && tailscale status >/dev/null 2>&1; then
  tailscale serve --bg --https=9443 http://127.0.0.1:8092 >/dev/null 2>&1 || true
  tailscale serve --bg --https=8095 http://127.0.0.1:8093 >/dev/null 2>&1 || true
  tailscale serve --bg --https=8096 http://127.0.0.1:8099 >/dev/null 2>&1 || true
  ok "private dashboard, front door, and console routes reconciled"
fi

hdr "7. Health summary"
curl -fsS --max-time 5 http://127.0.0.1:8080/v1/health >/dev/null 2>&1 \
  && ok "ntfy healthy" || warn "ntfy unhealthy"
curl -fsS --max-time 5 http://127.0.0.1:8099/health >/dev/null 2>&1 \
  && ok "CEO console healthy" || warn "CEO console unhealthy"

printf '\n\033[1mrecover.sh complete.\033[0m Review any ! lines above.\n'
