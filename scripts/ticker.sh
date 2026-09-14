#!/usr/bin/env bash
# ticker.sh — fixed-cadence scheduler driver with clean child ownership.
set -u

ROOT="${AOS_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PY="$ROOT/.venv/bin/python"
RUNTIME_DIR="$ROOT/.runtime"
LOG_DIR="$ROOT/logs/services"
mkdir -p "$RUNTIME_DIR" "$LOG_DIR"
PIDFILE="$RUNTIME_DIR/ticker.pid"
LOCKFILE="$RUNTIME_DIR/ticker.shell.lock"
INTERVAL="${TICKER_INTERVAL:-60}"
SINGLETON_FD="${AOS_SINGLETON_TICKER_FD:-8}"
CHILD_PID=""

case "$INTERVAL" in ''|*[!0-9]*) INTERVAL=60 ;; esac
(( INTERVAL >= 10 )) || INTERVAL=10

# This direct-invocation lock complements service_recovery's generation-bound
# singleton. Both are inherited by this shell and explicitly dropped by children.
exec 9>"$LOCKFILE"
if ! flock -n 9; then
  printf '%s\n' "ticker already running"
  exit 0
fi
echo $$ > "$PIDFILE"

run_child() {
  "$@" 9>&- {SINGLETON_FD}>&- &
  CHILD_PID=$!
  wait "$CHILD_PID"
  local rc=$?
  CHILD_PID=""
  return "$rc"
}

cleanup() {
  rm -f "$PIDFILE"
}

stop() {
  local pid="${CHILD_PID:-}"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    kill -TERM "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  fi
  exit 0
}

trap cleanup EXIT
trap stop INT TERM

while true; do
  STARTED=$SECONDS
  run_child "$PY" "$ROOT/scripts/scheduler.py" tick >>"$LOG_DIR/scheduler.log" 2>&1 || true
  run_child "$PY" "$ROOT/scripts/watchdog.py" beat ticker >/dev/null 2>&1 || true
  run_child "$PY" "$ROOT/scripts/singleton_exec.py" ready ticker "scheduler loop completed" >/dev/null 2>&1 || true
  # The ticker is also responsible for restoring its health monitor if that
  # monitor disappears between supervisor passes.
  run_child "$PY" "$ROOT/scripts/service_recovery.py" repair watchdog >>"$LOG_DIR/ticker.log" 2>&1 || true
  ELAPSED=$(( SECONDS - STARTED ))
  REMAINING=$(( INTERVAL - ELAPSED ))
  (( REMAINING >= 1 )) || REMAINING=1
  run_child sleep "$REMAINING" || true
done
