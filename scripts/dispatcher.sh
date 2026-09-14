#!/usr/bin/env bash
# dispatcher.sh — activation loop with generation-bound singleton ownership.
set -u

ROOT="${AOS_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PY="$ROOT/.venv/bin/python"
RUNTIME_DIR="$ROOT/.runtime"
LOG_DIR="$ROOT/logs/services"
mkdir -p "$RUNTIME_DIR" "$LOG_DIR"
PIDFILE="$RUNTIME_DIR/dispatcher.pid"
LOCKFILE="$RUNTIME_DIR/dispatcher.shell.lock"
INTERVAL="${DISPATCH_INTERVAL:-120}"
SINGLETON_FD="${AOS_SINGLETON_DISPATCHER_FD:-8}"
CHILD_PID=""

case "$INTERVAL" in ''|*[!0-9]*) INTERVAL=120 ;; esac
(( INTERVAL >= 10 )) || INTERVAL=10

exec 9>"$LOCKFILE"
if ! flock -n 9; then
  printf '%s\n' "dispatcher already running"
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
  run_child "$PY" "$ROOT/scripts/dispatcher.py" tick >>"$LOG_DIR/dispatcher.log" 2>&1 || true
  run_child "$PY" "$ROOT/scripts/watchdog.py" beat dispatcher >/dev/null 2>&1 || true
  run_child "$PY" "$ROOT/scripts/singleton_exec.py" ready dispatcher "dispatcher tick completed" >/dev/null 2>&1 || true
  run_child sleep "$INTERVAL" || true
done
