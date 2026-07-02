#!/usr/bin/env bash
# Gated EXHAUSTIVE action-coverage crawl: seeds a real tenant + company + consent + provider, then drives
# console_actions_e2e.cjs to click/fill EVERY interactive control on EVERY screen and assert none are dead
# and none throw a JS error. Same fail-closed contract as console_e2e.sh (a skipped gate is a gate that
# never ran). This is the "did QA actually try every action" gate — a measured coverage number.
set -u
ROOT="$HOME/projects/agent-os"; PY="$ROOT/.venv/bin/python"
NP="$HOME/projects/products/noupload/node_modules"
GATE=0; case "${1:-}" in gate|GATE) GATE=1 ;; esac
case "${AOS_UX_GATE:-0}" in 1|true|TRUE|yes) GATE=1 ;; esac
miss() { if [ "$GATE" = 1 ]; then echo "FAIL: action gate requires $1"; exit 1; fi; echo "SKIP: $1"; exit 0; }
console_up() { curl -s --max-time 4 http://127.0.0.1:8099/health 2>/dev/null | grep -q console; }

if ! console_up; then
  if [ "$GATE" = 1 ]; then
    pgrep -f "console.py serve" >/dev/null 2>&1 || \
      ( cd "$ROOT" && setsid bash -c "exec $PY scripts/console.py serve 8099" >/tmp/console.log 2>&1 </dev/null & )
    for _ in $(seq 1 30); do console_up && break; sleep 0.5; done
  fi
  console_up || miss "the console to be running on :8099"
fi
[ -d "$NP/playwright" ] || miss "playwright (npm) to be installed"

# seed a real tenant + company + consent + provider (all local; no external API calls)
read TOK TID < <("$PY" -c "
import sys;sys.path.insert(0,'$ROOT/scripts');import billing,tenantproviders
r=billing.signup('ActionCrawl','free')
try: tenantproviders.connect(r['tenant_id'],'anthropic','subscription')
except Exception: pass
print(r['api_token'], r['tenant_id'])" 2>/dev/null)
[ -n "${TOK:-}" ] || miss "a seeded tenant token"
ORG=$(curl -s -X POST http://127.0.0.1:8099/api/orgs/new -H "X-Tenant-Token: $TOK" -H 'Content-Type: application/json' -d '{"name":"ActionCrawl Co"}' | grep -oE '[0-9]+' | head -1)
curl -s -X POST http://127.0.0.1:8099/api/settings/consent -H "X-Tenant-Token: $TOK" -H 'Content-Type: application/json' -d '{"accept":true}' >/dev/null 2>&1

# crawl; retry once on failure (a transient — console warming, a slow first paint — must not red the suite;
# a REAL dead/erroring control fails deterministically both times). Fresh seed on retry.
crawl() { NODE_PATH="$NP" node "$ROOT/scripts/console_actions_e2e.cjs" http://127.0.0.1:8099 "$1" "${2:-0}" 2>/dev/null; }
OUT="$(crawl "$TOK" "$ORG")"
if ! echo "$OUT" | grep -q '^PASS'; then
  sleep 2
  read TOK2 _ < <("$PY" -c "import sys;sys.path.insert(0,'$ROOT/scripts');import billing,tenantproviders;r=billing.signup('ActionCrawlR','free')
try: tenantproviders.connect(r['tenant_id'],'anthropic','subscription')
except Exception: pass
print(r['api_token'])" 2>/dev/null)
  ORG2=$(curl -s -X POST http://127.0.0.1:8099/api/orgs/new -H "X-Tenant-Token: $TOK2" -H 'Content-Type: application/json' -d '{"name":"ActionCrawlR Co"}' | grep -oE '[0-9]+' | head -1)
  curl -s -X POST http://127.0.0.1:8099/api/settings/consent -H "X-Tenant-Token: $TOK2" -H 'Content-Type: application/json' -d '{"accept":true}' >/dev/null 2>&1
  OUT="$(crawl "$TOK2" "$ORG2")"
fi
if echo "$OUT" | grep -q '^PASS'; then echo "PASS: exhaustive action coverage (every control fires, no JS errors)"; exit 0
else echo "FAIL: action crawl found dead/erroring controls (twice) — run scripts/console_actions_e2e.cjs to see them"; echo "$OUT" | tail -4; exit 1; fi
