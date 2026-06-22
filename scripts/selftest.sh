#!/usr/bin/env bash
# selftest.sh — re-prove the whole agent-OS stack end-to-end. The "is everything healthy + correct" command.
set -u
cd "$(dirname "$0")/.."
PY=.venv/bin/python
PW=$(grep '^DATABASE_URL=' .env.local | sed -E 's#.*//agentos:([^@]+)@.*#\1#')
pass=0; fail=0
ck(){ if eval "$2" >/tmp/st.$$ 2>&1; then printf "  ✅ %s\n" "$1"; pass=$((pass+1)); else printf "  ❌ %s\n"  "$1"; tail -2 /tmp/st.$$ | sed 's/^/       /'; fail=$((fail+1)); fi; }
dbexec(){ sg docker -c "docker exec -e PGPASSWORD=$PW agentos-postgres psql -U agentos -d agentos -tAc \"$1\""; }

echo "=== containers ==="
for c in agentos-postgres agentos-nats agentos-ntfy agentos-cerbos; do
  sg docker -c "docker ps --filter name=$c --filter status=running -q" | grep -q . && echo "  ✅ $c up" && pass=$((pass+1)) || { echo "  ❌ $c down"; fail=$((fail+1)); }
done

echo "=== enforcement & safety ==="
ck "constraint smoke (8/8 deny/allow)"      "bash scripts/constraint_smoke_test.sh | grep -q '8 passed, 0 failed'"
ck "audit chain integrity"                  "$PY scripts/audit.py verify | grep -q INTACT"
ck "Cerbos PDP (6/6 decisions)"             "$PY scripts/cerbos_check.py | grep -q '6/6 correct'"
ck "signed-identity tamper rejected"        "cp ~/projects/control-plane/roles/builder.yaml /tmp/m.yaml; $PY scripts/identity.py keygen st >/dev/null; $PY scripts/identity.py sign st /tmp/m.yaml >/dev/null; echo x>>/tmp/m.yaml; ! $PY scripts/identity.py verify st /tmp/m.yaml"

echo "=== communication fabric ==="
ck "deadlock detector (cycle found)"        "$PY scripts/deadlock.py clear >/dev/null; $PY scripts/deadlock.py edge a b>/dev/null; $PY scripts/deadlock.py edge b a>/dev/null; ! $PY scripts/deadlock.py detect; $PY scripts/deadlock.py clear>/dev/null"
ck "message envelope + deadlock-guard"      "$PY scripts/messaging.py | grep -q 'deadlock-guard works'"

echo "=== memory / eval ==="
ck "graph memory + reflection"              "$PY scripts/memory.py | grep -q PASS"
ck "eval harness (Inspect AI runs)"         "timeout 90 $PY -m inspect_ai eval scripts/demo_eval.py --model mockllm/model --log-dir /tmp/st_logs 2>&1 | grep -q 'accuracy'"

echo "=== governance ==="
ck "gate_check blocks LAUNCH w/o QA"        "GC=~/projects/control-plane/scripts/gate_check.py; ! $PY \$GC ~/projects/products/noupload LAUNCH >/dev/null 2>&1 && $PY \$GC ~/projects/products/noupload BUILD >/dev/null 2>&1"
ck "metrics ledger + KPIs"                  "$PY scripts/metrics.py | grep -q PASS"
ck "upward-feedback CR re-flow"             "$PY scripts/cr_reflow.py demo | grep -q PASS"
ck "BYO provider layer info"                "$PY scripts/providers.py info | grep -q 'active provider'"
ck "provenance signature valid"            "$PY scripts/provenance.py verify | grep -qE 'VALID|drifted'"
ck "object store (by-ref/dedup/TTL/GC)"     "$PY scripts/objstore.py test | grep -q PASS"

echo "=== integration: standing Controller ==="
ck "controller full lifecycle -> LAUNCHED"  "rm -rf ~/projects/products/st-demo; dbexec \"DELETE FROM dbos.workflow_status\" >/dev/null 2>&1 || true; sg docker -c \"docker exec -e PGPASSWORD=$PW agentos-postgres psql -U agentos -d agentos_dbos_sys -c \\\"DELETE FROM dbos.workflow_status WHERE workflow_uuid='prod-st-demo'\\\"\" >/dev/null 2>&1; $PY scripts/controller.py run st-demo | grep -q LAUNCHED"

rm -f /tmp/st.$$
echo
echo "SELFTEST: $pass passed, $fail failed"
[ "$fail" -eq 0 ] || exit 1
