#!/usr/bin/env bash
# bridge.sh start|stop|status — exact-identity management for the phone reply listener.
set -u

ROOT="${AOS_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PY="$ROOT/.venv/bin/python"
RECOVERY="$ROOT/scripts/service_recovery.py"
LOG="$ROOT/bridge/listener.log"

case "${1:-status}" in
  start)
    "$PY" "$RECOVERY" repair reply-listener
    rc=$?
    tail -n 1 "$LOG" 2>/dev/null || true
    exit "$rc"
    ;;
  stop)
    "$PY" "$RECOVERY" stop reply-listener
    ;;
  status)
    "$PY" "$RECOVERY" status reply-listener
    rc=$?
    printf '%s\n' "--- last log ---"
    tail -n 3 "$LOG" 2>/dev/null || true
    exit "$rc"
    ;;
  *)
    printf '%s\n' "usage: bridge.sh start|stop|status" >&2
    exit 2
    ;;
esac
