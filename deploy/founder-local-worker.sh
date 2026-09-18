#!/usr/bin/env bash
# Persistent host-side worker for the founder-local tmux session.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RUNTIME="$ROOT/.runtime/founder-local"
ENV_FILE="$RUNTIME/founder.env"
WORKER_LOG="$RUNTIME/worker.log"

[ -f "$ENV_FILE" ] || { printf 'missing %s\n' "$ENV_FILE" >&2; exit 1; }
umask 077
exec >>"$WORKER_LOG" 2>&1
set -a
# shellcheck disable=SC1090 -- generated owner-only environment file
source "$ENV_FILE"
set +a
export AOS_ENVIRONMENT=staging
export AOS_V2_CREATE_SCHEMA=0
export AOS_V2_EXECUTION_CELL_ID="${AOS_V2_EXECUTION_CELL_ID:-local}"
export AOS_V2_SYSTEM_DATABASE_URL="postgresql://agentos_runtime:${AOS_V2_DATABASE_RUNTIME_PASSWORD}@127.0.0.1:${AOS_V2_POSTGRES_HOST_PORT}/agentos"
export AOS_V2_APPLICATION_DATABASE_URL="$AOS_V2_SYSTEM_DATABASE_URL"

exec "$ROOT/.venv/bin/agentos-v2" worker
