#!/usr/bin/env bash
# watchdog.sh — run the watchdog on a short loop so stalls/outages page you within ~1 minute.
# No systemd in this WSL, so recover.sh launches this on boot. Single-instance.
set -u
ROOT="$HOME/projects/agent-os"
PIDFILE="/tmp/agentos-watchdog.pid"
INTERVAL="${WATCHDOG_INTERVAL:-60}"

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null; then
  echo "watchdog already running (pid $(cat "$PIDFILE"))"; exit 0
fi
echo $$ > "$PIDFILE"
trap 'rm -f "$PIDFILE"' EXIT

while true; do
  "$ROOT/.venv/bin/python" "$ROOT/scripts/watchdog.py" tick >>/tmp/watchdog.log 2>&1 || true
  sleep "$INTERVAL"
done
