#!/usr/bin/env bash
# dispatcher.sh — the activation loop: wakes idle agents that have queued work, every DISPATCH_INTERVAL
# seconds. No systemd in this WSL, so recover.sh launches it on boot. Single-instance.
set -u
ROOT="$HOME/projects/agent-os"
PIDFILE="/tmp/agentos-dispatcher.pid"
INTERVAL="${DISPATCH_INTERVAL:-300}"   # default: poll every 5 min

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null; then
  echo "dispatcher already running (pid $(cat "$PIDFILE"))"; exit 0
fi
echo $$ > "$PIDFILE"
trap 'rm -f "$PIDFILE"' EXIT

while true; do
  "$ROOT/.venv/bin/python" "$ROOT/scripts/dispatcher.py" tick >>/tmp/dispatcher.log 2>&1 || true
  "$ROOT/.venv/bin/python" "$ROOT/scripts/watchdog.py" beat dispatcher >/dev/null 2>&1 || true
  sleep "$INTERVAL"
done
