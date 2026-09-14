#!/usr/bin/env bash
# watchdog.sh — bounded watchdog ticks with clean child ownership.
set -u

ROOT="${AOS_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PY="$ROOT/.venv/bin/python"
RUNTIME_DIR="$ROOT/.runtime"
LOG_DIR="$ROOT/logs/services"
mkdir -p "$RUNTIME_DIR" "$LOG_DIR"
PIDFILE="$RUNTIME_DIR/watchdog.pid"
LOCKFILE="$RUNTIME_DIR/watchdog.shell.lock"
INTERVAL="${WATCHDOG_INTERVAL:-60}"
TICK_TIMEOUT_S="${WATCHDOG_TICK_TIMEOUT_S:-75}"
KILL_AFTER_S="${WATCHDOG_KILL_AFTER_S:-15}"
SINGLETON_FD="${AOS_SINGLETON_WATCHDOG_FD:-8}"
CHILD_PID=""

case "$INTERVAL" in ''|*[!0-9]*) INTERVAL=60 ;; esac
case "$TICK_TIMEOUT_S" in ''|*[!0-9]*) TICK_TIMEOUT_S=75 ;; esac
case "$KILL_AFTER_S" in ''|*[!0-9]*) KILL_AFTER_S=15 ;; esac
(( INTERVAL >= 10 )) || INTERVAL=10
(( TICK_TIMEOUT_S >= 10 )) || TICK_TIMEOUT_S=10
(( TICK_TIMEOUT_S <= 90 )) || TICK_TIMEOUT_S=90
(( KILL_AFTER_S >= 1 )) || KILL_AFTER_S=1

exec 9>"$LOCKFILE"
if ! flock -n 9; then
  printf '%s\n' "watchdog already running"
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
  if ! run_child timeout --signal=TERM --kill-after="${KILL_AFTER_S}s" "${TICK_TIMEOUT_S}s" "$PY" "$ROOT/scripts/watchdog.py" tick >>"$LOG_DIR/watchdog.log" 2>&1
  then
    : # The next bounded tick retries; watchdog.py owns alerting about the failed pass.
  fi
  run_child "$PY" "$ROOT/scripts/singleton_exec.py" ready watchdog "watchdog tick completed" >/dev/null 2>&1 || true
  run_child sleep "$INTERVAL" || true
done
