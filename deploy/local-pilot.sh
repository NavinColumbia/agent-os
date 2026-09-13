#!/usr/bin/env bash
# Operate the zero-fixed-cost, invite-only Agent OS laptop pilot.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RUNTIME="$ROOT/.runtime/local-pilot"
ENV_FILE="$RUNTIME/pilot.env"
WORKSPACE="$RUNTIME/sandbox"
PROJECT=agent-os-pilot
BASE="$ROOT/deploy/docker-compose.v2.yml"
OVERLAY="$ROOT/deploy/docker-compose.pilot.yml"
FUNNEL_PORT=10000

die(){ printf 'error: %s\n' "$1" >&2; exit 1; }
need(){ command -v "$1" >/dev/null 2>&1 || die "$1 is required"; }
compose(){ docker compose --project-name "$PROJECT" --env-file "$ENV_FILE" -f "$BASE" -f "$OVERLAY" "$@"; }
value(){ sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1; }

init(){
  need docker
  need openssl
  need tailscale
  docker info >/dev/null 2>&1 || die "Docker daemon is unavailable"
  [ -S /var/run/docker.sock ] || die "/var/run/docker.sock is unavailable"
  mkdir -p "$WORKSPACE"
  chmod 700 "$RUNTIME" "$WORKSPACE"
  if [ -f "$ENV_FILE" ]; then
    [ ! -L "$ENV_FILE" ] || die "$ENV_FILE must not be a symlink"
    chmod 600 "$ENV_FILE"
    printf 'kept existing owner-only configuration: %s\n' "$ENV_FILE"
    return
  fi
  local dns_name public_url docker_gid
  dns_name="$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
  [ -n "$dns_name" ] || die "Tailscale has no MagicDNS name"
  public_url="https://${dns_name}:${FUNNEL_PORT}"
  docker_gid="$(stat -c %g /var/run/docker.sock)"
  umask 077
  {
    printf 'AOS_V2_POSTGRES_PASSWORD=%s\n' "$(openssl rand -hex 24)"
    printf 'AOS_V2_DATABASE_RUNTIME_PASSWORD=%s\n' "$(openssl rand -hex 24)"
    printf 'AOS_V2_AUTH_SECRET=%s\n' "$(openssl rand -hex 32)"
    printf 'AOS_V2_CAPABILITY_SECRET=%s\n' "$(openssl rand -hex 32)"
    printf 'AOS_V2_PUBLIC_PORT=8088\n'
    printf 'AOS_V2_PUBLIC_BASE_URL=%s\n' "$public_url"
    printf 'AOS_V2_APPLICATION_VERSION=local-pilot\n'
    printf 'AOS_V2_IDENTITY_MODE=hmac\n'
    printf 'AOS_V2_BILLING_MODE=disabled\n'
    printf 'AOS_V2_TENANT_MONTHLY_MODEL_BUDGET_CENTS=10000\n'
    printf 'AOS_V2_PREVIEW_TTL_SECONDS=604800\n'
    printf 'AOS_V2_MODEL=google:gemini-3.8-flash\n'
    printf 'GEMINI_API_KEY=CHANGE_ME_FREE_AUTH_KEY\n'
    printf 'GOOGLE_API_KEY=\n'
    printf 'AOS_V2_SANDBOX_BACKEND=docker\n'
    printf 'AOS_V2_SANDBOX_IMAGE=%s\n' 'python:3.12-slim-trixie@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea'
    printf 'AOS_V2_SANDBOX_TIMEOUT_SECONDS=300\n'
    printf 'AOS_V2_SANDBOX_WORKSPACE_ROOT=%s\n' "$WORKSPACE"
    printf 'AOS_PILOT_UID=%s\n' "$(id -u)"
    printf 'AOS_PILOT_GID=%s\n' "$(id -g)"
    printf 'AOS_PILOT_DOCKER_GID=%s\n' "$docker_gid"
  } > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  printf 'created %s\n' "$ENV_FILE"
  printf 'add a restricted Gemini auth key, then run: deploy/local-pilot.sh up\n'
}

preflight(){
  "$ROOT/.venv/bin/python" "$ROOT/deploy/local_pilot_preflight.py" --env-file "$ENV_FILE"
  compose config >/dev/null
}

public_url(){ value AOS_V2_PUBLIC_BASE_URL; }

wait_ready(){
  local port deadline
  port="$(value AOS_V2_PUBLIC_PORT)"
  deadline=$((SECONDS + 180))
  until curl -fsS --max-time 3 "http://127.0.0.1:${port}/ready" >/dev/null 2>&1; do
    [ "$SECONDS" -lt "$deadline" ] || {
      compose ps
      compose logs --tail 80 api worker
      die "local API did not become ready"
    }
    sleep 2
  done
}

issue_invite(){
  local subject="${1:-founder}" organization="${2:-local-company}" ttl="${3:-86400}" output temporary
  [[ "$subject" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] \
    || die "invite subject must be a safe 1-64 character identifier"
  [[ "$organization" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] \
    || die "invite organization must be a safe 1-64 character identifier"
  [[ "$ttl" =~ ^[0-9]+$ ]] && [ "$ttl" -ge 60 ] && [ "$ttl" -le 2592000 ] \
    || die "invite ttl must be between 60 and 2592000 seconds"
  output="$RUNTIME/invite-${organization}-${subject}.json"
  umask 077
  temporary="$(mktemp "$RUNTIME/.invite.XXXXXX")"
  if ! compose run --rm --no-deps -T api agentos-v2 issue-local-token \
    --subject "$subject" --organization "$organization" --role owner --ttl-seconds "$ttl" > "$temporary"; then
    rm -f "$temporary"
    die "invitation issuance failed"
  fi
  mv -f "$temporary" "$output"
  chmod 600 "$output"
  printf '%s\n' "$output"
}

up(){
  init
  preflight
  docker pull "$(value AOS_V2_SANDBOX_IMAGE)" >/dev/null
  compose up --build --detach
  wait_ready
  if ! tailscale funnel --bg --yes --https="$FUNNEL_PORT" \
    "http://127.0.0.1:$(value AOS_V2_PUBLIC_PORT)"; then
    die "Tailscale Funnel activation failed"
  fi
  if ! curl -fsS --max-time 20 "$(public_url)/ready" >/dev/null; then
    tailscale funnel --yes --https="$FUNNEL_PORT" off >/dev/null 2>&1 || true
    die "public Funnel readiness failed; Funnel was disabled"
  fi
  local invite
  if ! invite="$(issue_invite founder local-company 86400)"; then
    tailscale funnel --yes --https="$FUNNEL_PORT" off >/dev/null 2>&1 || true
    die "founder invitation failed; Funnel was disabled"
  fi
  printf 'pilot ready: %s/app\n' "$(public_url)"
  printf 'founder invitation (owner-only file, valid 24h): %s\n' "$invite"
}

status(){
  [ -f "$ENV_FILE" ] || die "run deploy/local-pilot.sh init first"
  compose ps
  tailscale funnel status
  if curl -fsS --max-time 5 "http://127.0.0.1:$(value AOS_V2_PUBLIC_PORT)/ready" >/dev/null; then
    printf 'local readiness: ok\n'
  else
    printf 'local readiness: failed\n'
    return 1
  fi
}

down(){
  [ -f "$ENV_FILE" ] || die "run deploy/local-pilot.sh init first"
  tailscale funnel --yes --https="$FUNNEL_PORT" off >/dev/null 2>&1 || true
  compose down
  printf 'public pilot stopped; database volume preserved\n'
}

case "${1:-}" in
  init) init ;;
  preflight) [ -f "$ENV_FILE" ] || init; preflight ;;
  up) up ;;
  status) status ;;
  invite)
    [ -f "$ENV_FILE" ] || die "run deploy/local-pilot.sh init first"
    issue_invite "${2:-guest}" "${3:-local-company}" "${4:-86400}"
    ;;
  logs) [ -f "$ENV_FILE" ] || die "run deploy/local-pilot.sh init first"; compose logs --follow --tail 100 api worker ;;
  down) down ;;
  *) die "usage: deploy/local-pilot.sh {init|preflight|up|status|invite [subject organization ttl]|logs|down}" ;;
esac
