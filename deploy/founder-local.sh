#!/usr/bin/env bash
# Operate a loopback-only first-user rehearsal using the existing Codex login.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RUNTIME="$ROOT/.runtime/founder-local"
ENV_FILE="$RUNTIME/founder.env"
WORKSPACE="$RUNTIME/sandbox"
WORKER_LOG="$RUNTIME/worker.log"
WORKER_SESSION=agent-os-founder-local-worker
PROJECT=agent-os-founder-local
BASE="$ROOT/deploy/docker-compose.v2.yml"
OVERLAY="$ROOT/deploy/docker-compose.founder-local.yml"
LEGACY_SANDBOX_IMAGE='python:3.12-slim-trixie@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea'
BROWSER_SANDBOX_TAG='agent-os-founder-sandbox-browser:playwright-1.62.0'

die(){ printf 'error: %s\n' "$1" >&2; exit 1; }
need(){ command -v "$1" >/dev/null 2>&1 || die "$1 is required"; }
compose(){ docker compose --project-name "$PROJECT" --env-file "$ENV_FILE" -f "$BASE" -f "$OVERLAY" "$@"; }
value(){ sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1; }

replace_value(){
  local key="$1" replacement="$2" temporary
  temporary="$(mktemp "$RUNTIME/.founder-env.XXXXXX")"
  awk -v key="$key" -v replacement="$key=$replacement" '
    index($0, key "=") == 1 { print replacement; found=1; next }
    { print }
    END { if (!found) print replacement }
  ' "$ENV_FILE" > "$temporary"
  chmod 600 "$temporary"
  mv -f "$temporary" "$ENV_FILE"
}

ensure_browser_sandbox(){
  local configured image_id
  configured="$(value AOS_V2_SANDBOX_IMAGE)"
  if [ "$configured" = "$LEGACY_SANDBOX_IMAGE" ]; then
    printf 'building pinned browser-capable local sandbox...\n'
    docker build --tag "$BROWSER_SANDBOX_TAG" \
      --file "$ROOT/deploy/Dockerfile.sandbox-browser-local" "$ROOT" >/dev/null
    image_id="$(docker image inspect --format '{{.Id}}' "$BROWSER_SANDBOX_TAG")"
    [[ "$image_id" =~ ^sha256:[0-9a-f]{64}$ ]] \
      || die "browser sandbox build did not produce an immutable image ID"
    replace_value AOS_V2_SANDBOX_IMAGE "$image_id"
    printf 'browser-capable sandbox pinned by local image ID\n'
  elif [[ "$configured" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    docker image inspect "$configured" >/dev/null 2>&1 \
      || die "configured local sandbox image is missing; restore the founder browser image"
  fi
  [ -n "$(value AOS_V2_ARTIFACT_MAX_CONTENT_BYTES)" ] \
    || replace_value AOS_V2_ARTIFACT_MAX_CONTENT_BYTES 16777216
  [ -n "$(value AOS_V2_SANDBOX_MAX_OUTPUT_BYTES)" ] \
    || replace_value AOS_V2_SANDBOX_MAX_OUTPUT_BYTES 8388608
  [ -n "$(value AOS_V2_SANDBOX_WORKSPACE_LIMIT_BYTES)" ] \
    || replace_value AOS_V2_SANDBOX_WORKSPACE_LIMIT_BYTES 33554432
  if [ "$(value AOS_V2_MODEL_REQUEST_TIMEOUT_SECONDS)" = "300" ]; then
    replace_value AOS_V2_MODEL_REQUEST_TIMEOUT_SECONDS 900
  fi
}

worker_alive(){
  tmux has-session -t "$WORKER_SESSION" >/dev/null 2>&1
}

check_codex_login(){
  local status
  status="$(codex login status 2>&1)" || die "Codex CLI is not authenticated"
  printf '%s' "$status" | grep -q 'Logged in using ChatGPT' \
    || die "run 'codex login' and choose ChatGPT subscription access"
}

init(){
  need docker
  need openssl
  need codex
  need tmux
  docker info >/dev/null 2>&1 || die "Docker daemon is unavailable"
  [ -S /var/run/docker.sock ] || die "/var/run/docker.sock is unavailable"
  check_codex_login
  mkdir -p "$WORKSPACE" "$RUNTIME"
  chmod 700 "$RUNTIME" "$WORKSPACE"
  if [ -f "$ENV_FILE" ]; then
    [ ! -L "$ENV_FILE" ] || die "$ENV_FILE must not be a symlink"
    chmod 600 "$ENV_FILE"
    printf 'kept existing owner-only configuration: %s\n' "$ENV_FILE"
    return
  fi
  umask 077
  {
    printf 'AOS_V2_POSTGRES_PASSWORD=%s\n' "$(openssl rand -hex 24)"
    printf 'AOS_V2_DATABASE_RUNTIME_PASSWORD=%s\n' "$(openssl rand -hex 24)"
    printf 'AOS_V2_AUTH_SECRET=%s\n' "$(openssl rand -hex 32)"
    printf 'AOS_V2_CAPABILITY_SECRET=%s\n' "$(openssl rand -hex 32)"
    printf 'AOS_V2_PUBLIC_PORT=8088\n'
    printf 'AOS_V2_POSTGRES_HOST_PORT=55432\n'
    printf 'AOS_V2_PUBLIC_BASE_URL=http://127.0.0.1:8088\n'
    printf 'AOS_V2_APPLICATION_VERSION=founder-local\n'
    printf 'AOS_V2_EXECUTION_CELL_ID=local\n'
    printf 'AOS_V2_IDENTITY_MODE=hmac\n'
    printf 'AOS_V2_BILLING_MODE=disabled\n'
    printf 'AOS_V2_TENANT_MONTHLY_MODEL_BUDGET_CENTS=10000\n'
    printf 'AOS_V2_PREVIEW_TTL_SECONDS=604800\n'
    printf 'AOS_V2_ARTIFACT_MAX_CONTENT_BYTES=16777216\n'
    printf 'AOS_V2_MODEL=codex-cli:default\n'
    printf 'AOS_V2_MODEL_REQUEST_TIMEOUT_SECONDS=900\n'
    printf 'AOS_V2_MODEL_REQUEST_LIMIT=12\n'
    printf 'AOS_V2_MODEL_OUTPUT_TOKENS_LIMIT=30000\n'
    printf 'AOS_V2_SANDBOX_BACKEND=docker\n'
    printf 'AOS_V2_SANDBOX_IMAGE=%s\n' "$LEGACY_SANDBOX_IMAGE"
    printf 'AOS_V2_SANDBOX_TIMEOUT_SECONDS=300\n'
    printf 'AOS_V2_SANDBOX_MAX_OUTPUT_BYTES=8388608\n'
    printf 'AOS_V2_SANDBOX_WORKSPACE_LIMIT_BYTES=33554432\n'
    printf 'AOS_V2_SANDBOX_WORKSPACE_ROOT=%s\n' "$WORKSPACE"
    printf 'AOS_V2_CONNECTOR_SECRET_DIR=%s\n' "$ROOT/deploy/connector-secrets"
  } > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  printf 'created %s\n' "$ENV_FILE"
}

preflight(){
  init
  ensure_browser_sandbox
  [ "$(value AOS_V2_PUBLIC_BASE_URL)" = "http://127.0.0.1:8088" ] \
    || die "founder rehearsal must remain on the loopback URL"
  [ "$(value AOS_V2_BILLING_MODE)" = "disabled" ] \
    || die "billing must remain disabled in the founder rehearsal"
  [ "$(value AOS_V2_IDENTITY_MODE)" = "hmac" ] \
    || die "founder rehearsal requires signed local invitations"
  [ "$(value AOS_V2_MODEL)" = "codex-cli:default" ] \
    || die "founder rehearsal requires the subscription-backed Codex model"
  [ -n "$(value AOS_V2_EXECUTION_CELL_ID)" ] \
    || replace_value AOS_V2_EXECUTION_CELL_ID local
  [ "$(value AOS_V2_EXECUTION_CELL_ID)" = "local" ] \
    || die "founder rehearsal API, activation job, and worker must share execution cell local"
  compose config >/dev/null
  printf 'founder-local preflight: ok\n'
}

wait_api(){
  local port deadline
  port="$(value AOS_V2_PUBLIC_PORT)"
  deadline=$((SECONDS + 180))
  until curl -fsS --max-time 3 "http://127.0.0.1:${port}/ready" >/dev/null 2>&1; do
    [ "$SECONDS" -lt "$deadline" ] || {
      compose ps
      compose logs --tail 80 api migrate postgres
      die "local API did not become ready"
    }
    sleep 2
  done
}

start_worker(){
  worker_alive && return
  : > "$WORKER_LOG"
  chmod 600 "$WORKER_LOG"
  tmux new-session -d -s "$WORKER_SESSION" "$ROOT/deploy/founder-local-worker.sh"
  local deadline
  deadline=$((SECONDS + 30))
  until grep -q '"event":"worker_started"' "$WORKER_LOG" 2>/dev/null; do
    worker_alive || {
      tail -n 80 "$WORKER_LOG" >&2
      die "local subscription worker failed to start"
    }
    [ "$SECONDS" -lt "$deadline" ] || {
      tail -n 80 "$WORKER_LOG" >&2
      die "local subscription worker did not report ready"
    }
    sleep 1
  done
}

stop_worker(){
  if worker_alive; then
    local pid deadline
    pid="$(tmux list-panes -t "$WORKER_SESSION" -F '#{pane_pid}' | head -n 1)"
    kill "$pid" 2>/dev/null || true
    deadline=$((SECONDS + 20))
    while worker_alive && [ "$SECONDS" -lt "$deadline" ]; do sleep 1; done
    tmux kill-session -t "$WORKER_SESSION" 2>/dev/null || true
  fi
}

restart_worker(){
  preflight
  stop_worker
  start_worker
  printf 'subscription worker restarted\n'
  worker_alive || {
    tail -n 80 "$WORKER_LOG" >&2
    die "local subscription worker failed to start"
  }
}

issue_invite(){
  local output temporary
  output="$RUNTIME/invite-local-company-founder.json"
  umask 077
  temporary="$(mktemp "$RUNTIME/.invite.XXXXXX")"
  if ! compose run --rm --no-deps -T api agentos-v2 issue-local-token \
    --subject founder --organization local-company --role owner --ttl-seconds 86400 > "$temporary"; then
    mv "$temporary" "$temporary.failed"
    die "founder invitation issuance failed"
  fi
  mv -f "$temporary" "$output"
  chmod 600 "$output"
  printf '%s\n' "$output"
}

up(){
  preflight
  local sandbox_image
  sandbox_image="$(value AOS_V2_SANDBOX_IMAGE)"
  if [[ "$sandbox_image" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    docker image inspect "$sandbox_image" >/dev/null
  else
    docker pull "$sandbox_image" >/dev/null
  fi
  compose up --build --detach postgres migrate api
  compose run --rm --no-deps -T api agentos-v2 activate-release
  stop_worker
  start_worker
  wait_api
  local invite
  invite="$(issue_invite)"
  printf 'founder rehearsal ready: http://127.0.0.1:%s/app\n' "$(value AOS_V2_PUBLIC_PORT)"
  printf 'paste the token from this owner-only file: %s\n' "$invite"
}

status(){
  [ -f "$ENV_FILE" ] || die "run deploy/founder-local.sh up first"
  compose ps
  if worker_alive; then
    printf 'subscription worker: running (pid %s)\n' \
      "$(tmux list-panes -t "$WORKER_SESSION" -F '#{pane_pid}' | head -n 1)"
  else
    printf 'subscription worker: stopped\n'
  fi
  if curl -fsS --max-time 5 "http://127.0.0.1:$(value AOS_V2_PUBLIC_PORT)/ready" >/dev/null; then
    printf 'local readiness: ok\n'
  else
    printf 'local readiness: failed\n'
    return 1
  fi
}

logs(){
  [ -f "$ENV_FILE" ] || die "run deploy/founder-local.sh up first"
  compose logs --tail 100 api postgres
  printf '\nsubscription worker:\n'
  tail -n 100 "$WORKER_LOG" 2>/dev/null || true
}

down(){
  [ -f "$ENV_FILE" ] || die "run deploy/founder-local.sh up first"
  stop_worker
  compose down
  printf 'founder rehearsal stopped; database volume preserved\n'
}

case "${1:-}" in
  init) init ;;
  preflight) preflight ;;
  up) up ;;
  status) status ;;
  logs) logs ;;
  restart-worker) restart_worker ;;
  down) down ;;
  *) die "usage: deploy/founder-local.sh {init|preflight|up|status|logs|restart-worker|down}" ;;
esac
