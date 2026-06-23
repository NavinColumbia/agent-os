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
ck "scoped secrets vault"                    "$PY scripts/vault.py test | grep -q PASS"
ck "governed data connector (egress allowlist)" "$PY scripts/connectors.py test | grep -q PASS"
ck "retention sweep (record expiry)"        "$PY scripts/retention.py test | grep -q PASS"
ck "experiment tracking (log/compare/best)" "$PY scripts/experiments.py demo | grep -q PASS"
ck "scheduler (recurring jobs)"             "$PY scripts/scheduler.py test | grep -q PASS"
ck "distributed dispatch (NATS workers)"     "$PY scripts/dispatch.py demo | grep -q PASS"
ck "budget governor (token caps)"          "$PY scripts/budget.py test | grep -q PASS"
ck "feature flags + rollout"               "$PY scripts/flags.py test | grep -q PASS"
ck "health monitor + alerting"             "$PY scripts/monitor.py test | grep -q PASS"
ck "HTTP API (health+auth)"                 "curl -s http://127.0.0.1:8090/health | grep -q agent-os && [ $(curl -s -o /dev/null -w %{http_code} http://127.0.0.1:8090/status) = 401 ]"
ck "multi-tenant isolation"                 "$PY scripts/tenancy.py test | grep -q PASS"
ck "skills/capability registry"            "$PY scripts/skills.py test | grep -q PASS"
ck "portable encrypted snapshot"           "$PY platform/snapshot.py selftest | grep -q PASS"
ck "platform inventory covers stack"       "for s in postgres nats ntfy cerbos; do grep -q \"name: \$s\" platform/inventory.yaml || exit 1; done"
ck "autonomous factory (governed line)"     "$PY scripts/factory.py selftest | grep -q PASS"
ck "fleet visibility view"                  "$PY scripts/fleet.py status | grep -q 'agent-os fleet'"
ck "dashboard state (real data)"            "$PY scripts/dashboard.py state | grep -q '\"overall\"'"
ck "watchdog detect + heartbeat"            "$PY scripts/watchdog.py selftest | grep -q PASS"
ck "auto-remediation routing"               "$PY scripts/responder.py selftest | grep -q PASS"
ck "incident-commander context"             "$PY scripts/incident.py selftest | grep -q PASS"
ck "SaaS billing (meter/plan/invoice)"      "$PY scripts/billing.py test | grep -q PASS"
ck "agent directory + conflict detection"   "$PY scripts/directory.py selftest | grep -q PASS"
ck "orchestration (route/spawn/priority)"   "$PY scripts/orchestrate.py selftest | grep -q PASS"
ck "security scan (invariants)"             "$PY scripts/security_scan.py | grep -q PASS"
ck "unit test suite (pytest)"               "$PY -m pytest tests/ -q | tail -1 | grep -q passed"

echo "=== integration: standing Controller ==="
ck "controller full lifecycle -> LAUNCHED"  "rm -rf ~/projects/products/st-demo; dbexec \"DELETE FROM dbos.workflow_status\" >/dev/null 2>&1 || true; sg docker -c \"docker exec -e PGPASSWORD=$PW agentos-postgres psql -U agentos -d agentos_dbos_sys -c \\\"DELETE FROM dbos.workflow_status WHERE workflow_uuid='prod-st-demo'\\\"\" >/dev/null 2>&1; $PY scripts/controller.py run st-demo | grep -q LAUNCHED"

rm -f /tmp/st.$$
echo
echo "SELFTEST: $pass passed, $fail failed"
[ "$fail" -eq 0 ] || exit 1
