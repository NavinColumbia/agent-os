#!/usr/bin/env bash
# Fail-closed, one-command single-host public installation.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${AOS_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
ENV_FILE="$ROOT/.env.local"
LOCK_FILE="${AOS_INSTALL_LOCK:-/tmp/agentos-public-install.lock}"

say(){ printf '\n\033[1m== %s ==\033[0m\n' "$1"; }
die(){ printf '  \033[31m! %s\033[0m\n' "$1" >&2; exit 1; }
need(){ command -v "$1" >/dev/null 2>&1 || die "$1 is required"; }

need bash
need python3
need sudo
need flock
[ -f "$ENV_FILE" ] || die "copy deploy/public.env.example to .env.local and configure public inputs first"
chmod 600 "$ENV_FILE"

exec 8>"$LOCK_FILE"
flock -n 8 || die "another public installation is already running"

say "1. Build and prove the private stack"
AOS_ROOT="$ROOT" bash "$ROOT/platform/rebuild.sh"

say "2. Fail-closed public configuration and runtime preflight"
"$ROOT/.venv/bin/python" "$ROOT/deploy/preflight.py" --env-file "$ENV_FILE"

say "3. Install exact-identity service supervision"
unit_tmp="$(mktemp)"
trap 'rm -f "$unit_tmp"' EXIT
sed -e "s|@@ROOT@@|$ROOT|g" \
    -e "s|@@USER@@|$(id -un)|g" \
    -e "s|@@GROUP@@|$(id -gn)|g" \
    "$ROOT/deploy/agentos-supervisor.service.template" > "$unit_tmp"
sudo install -m 0644 "$unit_tmp" /etc/systemd/system/agentos-supervisor.service
sudo systemctl daemon-reload
sudo systemctl enable agentos-supervisor.service
sudo systemctl restart agentos-supervisor.service
sudo systemctl is-active --quiet agentos-supervisor.service || die "agentos-supervisor did not become active"

say "4. Validate and start the TLS edge"
PUBLIC_HOST="$(sed -n 's/^AOS_PUBLIC_HOST=//p' "$ENV_FILE" | tail -n 1)"
if command -v caddy >/dev/null 2>&1 && sudo systemctl cat caddy.service >/dev/null 2>&1; then
  EDGE_MODE=host
  sudo install -d -m 0755 /etc/caddy /etc/systemd/system/caddy.service.d
  if id caddy >/dev/null 2>&1; then
    sudo install -d -o caddy -g caddy -m 0750 /var/log/caddy
  else
    sudo install -d -m 0750 /var/log/caddy
  fi
  sudo install -m 0644 "$ROOT/deploy/Caddyfile" /etc/caddy/Caddyfile
  edge_tmp="$(mktemp)"
  sed "s|@@ROOT@@|$ROOT|g" "$ROOT/deploy/caddy-agentos.conf.template" > "$edge_tmp"
  sudo install -m 0644 "$edge_tmp" /etc/systemd/system/caddy.service.d/agentos.conf
  rm -f "$edge_tmp"
  sudo systemctl daemon-reload
  # caddy validate does not inherit the service drop-in. Supply only the non-secret hostname required by
  # the Caddyfile placeholder; the running service loads the full EnvironmentFile through systemd.
  sudo env "AOS_PUBLIC_HOST=$PUBLIC_HOST" caddy validate --config /etc/caddy/Caddyfile
  sudo systemctl enable caddy.service
  sudo systemctl restart caddy.service
  sudo systemctl is-active --quiet caddy.service || die "host Caddy did not become active"
else
  EDGE_MODE=container
  need docker
  docker compose --env-file "$ROOT/.env.local" \
    -f "$ROOT/deploy/docker-compose.public.yml" config >/dev/null
  docker compose --env-file "$ROOT/.env.local" \
    -f "$ROOT/deploy/docker-compose.public.yml" up -d
  docker compose --env-file "$ROOT/.env.local" \
    -f "$ROOT/deploy/docker-compose.public.yml" ps --status running --services | \
    grep -qx caddy || die "Docker Caddy did not become active"
fi

say "5. Verify the private upstream and selected edge"
curl -fsS --max-time 10 http://127.0.0.1:8099/health >/dev/null || \
  die "console health check failed"
printf '  \033[32m✓\033[0m public installation ready (edge=%s)\n' "$EDGE_MODE"
printf '  DNS must point %s at this host before Caddy can issue its public certificate.\n' \
  "$PUBLIC_HOST"
