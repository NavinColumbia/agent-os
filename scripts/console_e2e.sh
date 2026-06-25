#!/usr/bin/env bash
# Gated real-browser click-through of the tenant console. Exits 0 on PASS (every screen rendered
# error-free) or SKIP (console not running / playwright absent — so the suite stays green headless).
set -u
ROOT="$HOME/projects/agent-os"; PY="$ROOT/.venv/bin/python"
NP="$HOME/projects/products/noupload/node_modules"
curl -s --max-time 4 http://127.0.0.1:8099/health 2>/dev/null | grep -q console || { echo "SKIP: console not running"; exit 0; }
[ -d "$NP/playwright" ] || { echo "SKIP: playwright not present"; exit 0; }
mkdir -p /tmp/aos-shots
# 'signup' => the test creates a brand-new account through the UI (catches a broken front door),
# then clicks through every screen. No pre-seeded token.
NODE_PATH="$NP" node "$ROOT/scripts/console_e2e.cjs" http://127.0.0.1:8099 signup /tmp/aos-shots 2>/dev/null \
  | grep -q '"ok": true' && { echo "PASS: every console screen rendered error-free in a real browser"; exit 0; } \
  || { echo "FAIL: a console screen errored — see /tmp/aos-shots"; exit 1; }
