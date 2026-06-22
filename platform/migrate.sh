#!/usr/bin/env bash
# migrate.sh — apply every Postgres migration idempotently against the running DB.
# All initdb SQL uses CREATE ... IF NOT EXISTS, so this is safe to run any number of times.
# On a FRESH Postgres volume the container runs these automatically; this exists for existing
# volumes, restores, and "make sure the schema is current" — it is the explicit, ordered truth.
#
#   bash platform/migrate.sh
set -eu
ROOT="$HOME/projects/agent-os"
DK(){ if docker info >/dev/null 2>&1; then docker "$@"; else sg docker -c "docker $*"; fi; }

ok(){ printf '  \033[32m✓\033[0m %s\n' "$1"; }
n=0
for f in "$ROOT"/postgres/initdb/*.sql; do
  DK exec -i agentos-postgres psql -v ON_ERROR_STOP=1 -U agentos -d agentos -q < "$f"
  ok "applied $(basename "$f")"; n=$((n+1))
done
printf '\n\033[1mmigrate.sh: %d migrations applied (idempotent).\033[0m\n' "$n"
