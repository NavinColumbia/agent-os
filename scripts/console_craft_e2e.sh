#!/usr/bin/env bash
# Gated product-craft guard: responsive (no overflow @390px) + real empty-states + no raw enum/JSON.
# Same fail-closed contract as console_e2e.sh: in gate mode a missing prerequisite is a FAILURE, not a
# free pass (a silently-skipped craft gate is a craft gate that never runs).
set -u
ROOT="$HOME/projects/agent-os"; PY="$ROOT/.venv/bin/python"
NP="$HOME/projects/products/noupload/node_modules"

GATE=0
case "${1:-}" in gate|GATE) GATE=1 ;; esac
case "${AOS_UX_GATE:-0}" in 1|true|TRUE|yes) GATE=1 ;; esac
miss() { if [ "$GATE" = 1 ]; then echo "FAIL: craft gate requires $1"; exit 1; fi; echo "SKIP: $1"; exit 0; }
console_up() { curl -s --max-time 4 http://127.0.0.1:8099/health 2>/dev/null | grep -q console; }

if ! console_up; then
  if [ "$GATE" = 1 ]; then
    pgrep -f "console.py serve" >/dev/null 2>&1 || \
      ( cd "$ROOT" && setsid bash -c "exec $PY scripts/console.py serve 8099" >/tmp/console.log 2>&1 </dev/null & )
    for _ in $(seq 1 30); do console_up && break; sleep 0.5; done
  fi
  console_up || miss "the console to be running on :8099 (see /tmp/console.log)"
fi
[ -d "$NP/playwright" ] || miss "playwright (npm) to be installed"

NODE_PATH="$NP" node "$ROOT/scripts/console_craft_e2e.cjs" http://127.0.0.1:8099 2>/dev/null \
  | grep -q '^PASS' && { echo "PASS: product-craft (responsive + empty-states + microcopy)"; exit 0; } \
  || { echo "FAIL: product-craft guard found issues — run scripts/console_craft_e2e.cjs to see them"; exit 1; }
