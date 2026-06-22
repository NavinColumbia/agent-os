#!/usr/bin/env bash
# ticker.sh — drives the scheduler: calls `scheduler.py tick` every 15 min so recurring jobs
# (daily encrypted snapshot, retention sweeps, monitors) actually fire. No systemd in this WSL, so
# recover.sh launches this on boot. Single-instance: refuses to start a second copy.
set -u
ROOT="$HOME/projects/agent-os"
PIDFILE="/tmp/agentos-ticker.pid"
INTERVAL="${TICKER_INTERVAL:-900}"

# single-instance guard
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null; then
  echo "ticker already running (pid $(cat "$PIDFILE"))"; exit 0
fi
echo $$ > "$PIDFILE"
trap 'rm -f "$PIDFILE"' EXIT

while true; do
  "$ROOT/.venv/bin/python" "$ROOT/scripts/scheduler.py" tick >>/tmp/scheduler.log 2>&1 || true
  sleep "$INTERVAL"
done
