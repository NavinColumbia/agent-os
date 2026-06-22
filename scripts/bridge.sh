#!/usr/bin/env bash
# bridge.sh start|stop|status — manage the ntfy reply listener (phone -> agent).
set -u
ROOT="$HOME/projects/agent-os"
LOG="$ROOT/bridge/listener.log"
case "${1:-status}" in
  start)
    pkill -f reply_listener.py 2>/dev/null; sleep 1
    cd "$ROOT" && setsid bash -c "python3 -u scripts/reply_listener.py > '$LOG' 2>&1" < /dev/null & disown
    sleep 2; pgrep -f reply_listener.py >/dev/null && echo "listener started" || echo "FAILED"; tail -1 "$LOG" 2>/dev/null ;;
  stop)
    pkill -f reply_listener.py && echo "listener stopped" || echo "not running" ;;
  status)
    pgrep -af reply_listener.py | grep -v pgrep || echo "not running"; echo "--- last log ---"; tail -3 "$LOG" 2>/dev/null ;;
  *) echo "usage: bridge.sh start|stop|status" ;;
esac
