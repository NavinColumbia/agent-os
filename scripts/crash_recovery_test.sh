#!/usr/bin/env bash
# crash_recovery_test.sh — PROVES task state survives a process crash via Postgres checkpoints.
set -u
ROOT="$HOME/projects/agent-os"
PY="$ROOT/.venv/bin/python"
TASK="crash-demo-$$"

echo "=== 1) start a 'worker' that saves a checkpoint mid-task, then blocks ==="
"$PY" - "$TASK" <<'PYWORKER' &
import sys, time
sys.path.insert(0, __import__('os').path.expanduser('~/projects/agent-os/scripts'))
import checkpoint
task = sys.argv[1]
state = {"step": 3, "total_steps": 5, "done": ["fetch", "parse", "transform"],
         "pending": ["load", "verify"], "cursor": 4096}
seq = checkpoint.save(task, state)
print(f"[worker] saved checkpoint seq={seq}: {state}", flush=True)
time.sleep(600)  # simulate long in-progress work; we will hard-kill this
PYWORKER
WPID=$!
sleep 4

echo "=== 2) HARD-KILL the worker (kill -9) to simulate a crash ==="
kill -9 "$WPID" 2>/dev/null
wait "$WPID" 2>/dev/null
echo "[test] worker pid $WPID killed; is it gone?"
kill -0 "$WPID" 2>/dev/null && echo "  STILL ALIVE (bad)" || echo "  confirmed dead"

echo "=== 3) restore from a FRESH process — state must survive ==="
RESTORED="$("$PY" "$ROOT/scripts/checkpoint.py" restore "$TASK")"
echo "[restore] $RESTORED"

echo "=== 4) assert it matches what the (dead) worker saved ==="
"$PY" -c '
import sys, json
state = json.loads(sys.argv[1])
assert state["step"] == 3 and state["pending"] == ["load", "verify"] and state["cursor"] == 4096, state
print("PASS: checkpoint survived the crash with exact state intact")
' "$RESTORED"
rc=$?

echo "=== cleanup demo row ==="
"$PY" - "$TASK" <<'PYCLEAN'
import sys, os, psycopg
sys.path.insert(0, os.path.expanduser('~/projects/agent-os/scripts'))
from checkpoint import _db_url
with psycopg.connect(_db_url()) as c, c.cursor() as cur:
    cur.execute("DELETE FROM task_checkpoints WHERE task_id=%s", (sys.argv[1],)); c.commit()
print("cleaned demo checkpoint")
PYCLEAN
exit $rc
