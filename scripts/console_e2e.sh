#!/usr/bin/env bash
# Gated real-browser click-through of the tenant console.
#
# Two modes:
#   (default)        Headless-friendly: if the console isn't running or playwright is
#                    absent, exit 0 with "SKIP" so an unattended suite stays green.
#   gate  (arg) /    UX-GATE mode (selftest, launch gate): SKIP is NOT a pass. Prerequisites
#   AOS_UX_GATE=1    are *made* present — we start the console if it's down and require
#                    playwright — and if they still can't be satisfied we FAIL (non-zero).
#                    Rationale: this is the only real UX gate; a silently-skipped gate is a
#                    gate that never runs, so in gate mode we fail CLOSED (#49).
set -u
ROOT="$HOME/projects/agent-os"; PY="$ROOT/.venv/bin/python"
NP="$HOME/projects/products/noupload/node_modules"

# Gate mode if first arg is "gate" or AOS_UX_GATE is set to a truthy value.
GATE=0
case "${1:-}" in gate|GATE) GATE=1 ;; esac
case "${AOS_UX_GATE:-0}" in 1|true|TRUE|yes) GATE=1 ;; esac

# In gate mode a missing prerequisite is a FAILURE, not a free pass.
# $1 = human reason
miss() {
  if [ "$GATE" = 1 ]; then echo "FAIL: UX gate requires $1"; exit 1; fi
  echo "SKIP: $1"; exit 0
}

console_up() { curl -s --max-time 4 http://127.0.0.1:8099/health 2>/dev/null | grep -q console; }

# Gate mode: ensure the console is actually present (start it, then wait for health).
if ! console_up; then
  if [ "$GATE" = 1 ]; then
    pgrep -f "console.py serve" >/dev/null 2>&1 || \
      ( cd "$ROOT" && setsid bash -c "exec $PY scripts/console.py serve 8099" >/tmp/console.log 2>&1 </dev/null & )
    for _ in $(seq 1 30); do console_up && break; sleep 0.5; done
  fi
  console_up || miss "the console to be running on :8099 (see /tmp/console.log)"
fi

[ -d "$NP/playwright" ] || miss "playwright (npm) to be installed"

mkdir -p /tmp/aos-shots
# 'signup' => the test creates a brand-new account through the UI (catches a broken front door),
# then clicks through every screen. No pre-seeded token.
NODE_PATH="$NP" node "$ROOT/scripts/console_e2e.cjs" http://127.0.0.1:8099 signup /tmp/aos-shots 2>/dev/null \
  | grep -q '"ok": true' && { echo "PASS: every console screen rendered error-free in a real browser"; exit 0; } \
  || { echo "FAIL: a console screen errored — see /tmp/aos-shots"; exit 1; }
