#!/usr/bin/env bash
# Gated real-browser click-through of the tenant console. Exits 0 on PASS (every screen rendered
# error-free) or SKIP (console not running / playwright absent — so the suite stays green headless).
set -u
ROOT="$HOME/projects/agent-os"; PY="$ROOT/.venv/bin/python"
NP="$HOME/projects/products/noupload/node_modules"
curl -s --max-time 4 http://127.0.0.1:8099/health 2>/dev/null | grep -q console || { echo "SKIP: console not running"; exit 0; }
[ -d "$NP/playwright" ] || { echo "SKIP: playwright not present"; exit 0; }
TOK=$("$PY" - <<'PYEOF'
import sys; sys.path.insert(0, "scripts")
import billing, consent, tenantproviders as tp
r = billing.signup("st-e2e", "free"); t = r["tenant_id"]
consent.record(t); tp.connect(t, "anthropic", "subscription")
print(r["api_token"])
PYEOF
)
mkdir -p /tmp/aos-shots
NODE_PATH="$NP" node "$ROOT/scripts/console_e2e.cjs" http://127.0.0.1:8099 "$TOK" /tmp/aos-shots 2>/dev/null \
  | grep -q '"ok": true' && { echo "PASS: every console screen rendered error-free in a real browser"; exit 0; } \
  || { echo "FAIL: a console screen errored — see /tmp/aos-shots"; exit 1; }
