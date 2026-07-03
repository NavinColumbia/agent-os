#!/usr/bin/env bash
# selftest.sh — re-prove the whole agent-OS stack end-to-end. The "is everything healthy + correct" command.
#
# PERF: the independent module selftests run CONCURRENTLY (bounded pool, default 12) instead of strictly
# serially — each is row-isolated (random tenant/product ids), audit appends are serialized by a
# pg_advisory_xact_lock, and audit `verify` reads a consistent MVCC snapshot, so concurrency is safe.
# Checks that share mutable state OR are wall-clock-sensitive are kept SERIAL (see "serial" block):
#   * deadlock + retention   -> both touch the `waits` table (deadlock does TRUNCATE waits)
#   * research + loopcontroller -> poll a daemon-thread fleet on a tight (10s/14s) deadline that
#                                  starves under pool CPU load; run them contention-free instead
#   * controller lifecycle   -> wipes global dbos.workflow_status + product filesystem
# The pass/fail TALLY is deterministic regardless of scheduling; only the interleaving of printed
# lines can vary. Tune concurrency with SELFTEST_JOBS=N.
set -u
cd "$(dirname "$0")/.."
PY=.venv/bin/python
PW=$(grep '^DATABASE_URL=' .env.local | sed -E 's#.*//agentos:([^@]+)@.*#\1#')
pass=0; fail=0
MAXJ=${SELFTEST_JOBS:-12}
ck(){ if eval "$2" >/tmp/st.$$ 2>&1; then printf "  ✅ %s\n" "$1"; pass=$((pass+1)); else printf "  ❌ %s\n"  "$1"; tail -2 /tmp/st.$$ | sed 's/^/       /'; fail=$((fail+1)); fi; }
dbexec(){ sg docker -c "docker exec -e PGPASSWORD=$PW agentos-postgres psql -U agentos -d agentos -tAc \"$1\""; }

# ---- parallel check queue -------------------------------------------------
# ckp enqueues a parallel-safe check; the pool runs them concurrently and the tally is aggregated
# afterwards in enqueue order (so the count is deterministic and section headers stay grouped).
PAR_NAMES=(); PAR_CMDS=(); PAR_SECT=()
SECTION=""
sect(){ SECTION="$1"; }
ckp(){ PAR_NAMES+=("$1"); PAR_CMDS+=("$2"); PAR_SECT+=("$SECTION"); }

run_one(){ # $1=index $2=results-dir  — runs in a background subshell
  local i="$1" RD="$2" name="${PAR_NAMES[$i]}" cmd="${PAR_CMDS[$i]}"
  local t0=$SECONDS
  if [ -n "${SELFTEST_TIMING:-}" ]; then trap 'printf "%6ss  %s\n" "$((SECONDS-t0))" "$name" >>"$SELFTEST_TIMING"' RETURN; fi
  # RESILIENCE: every check is hard-capped so ONE hung command can never stall the whole suite (a wedged DB
  # lock / a slow model call / an infinite loop fails THAT check, not the run). SIGTERM, then SIGKILL 10s
  # later. A timed-out check is a FAIL with a clear reason — never a silent 75-minute hang.
  if timeout -k 10 "${CKP_TIMEOUT:-300}" bash -c "$cmd" >"$RD/$i.log" 2>&1; then
    { echo PASS; printf "  ✅ %s\n" "$name"; } >"$RD/$i.res"
  else
    local rc=$?
    if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then echo "CHECK TIMED OUT after ${CKP_TIMEOUT:-300}s (hang guard)" >>"$RD/$i.log"; fi
    { echo FAIL; printf "  ❌ %s\n" "$name"; tail -2 "$RD/$i.log" | sed 's/^/       /'; } >"$RD/$i.res"
  fi
}

run_pool(){
  local RD; RD=$(mktemp -d "${TMPDIR:-/tmp}/selftest.XXXXXX")
  local N=${#PAR_NAMES[@]} i running=0
  for ((i=0; i<N; i++)); do
    run_one "$i" "$RD" &
    running=$((running+1))
    if (( running >= MAXJ )); then wait -n; running=$((running-1)); fi
  done
  wait
  # aggregate in enqueue order: deterministic tally + grouped section headers
  local last="" status
  for ((i=0; i<N; i++)); do
    if [ "${PAR_SECT[$i]}" != "$last" ]; then echo "=== ${PAR_SECT[$i]} ==="; last="${PAR_SECT[$i]}"; fi
    status=$(head -1 "$RD/$i.res")
    tail -n +2 "$RD/$i.res"
    if [ "$status" = PASS ]; then pass=$((pass+1)); else fail=$((fail+1)); fi
  done
  rm -rf "$RD"
}

echo "=== containers ==="
for c in agentos-postgres agentos-ntfy agentos-cerbos; do
  sg docker -c "docker ps --filter name=$c --filter status=running -q" | grep -q . && echo "  ✅ $c up" && pass=$((pass+1)) || { echo "  ❌ $c down"; fail=$((fail+1)); }
done

# ===========================================================================
# PARALLEL-SAFE CHECKS (enqueued here, executed concurrently by run_pool below)
# ===========================================================================
sect "enforcement & safety"
ckp "constraint smoke (8/8 deny/allow)"      "bash scripts/constraint_smoke_test.sh | grep -q '8 passed, 0 failed'"
ckp "audit chain integrity"                  "$PY scripts/audit.py verify | grep -q INTACT"
ckp "Cerbos PDP (6/6 decisions)"             "$PY scripts/cerbos_check.py | grep -q '6/6 correct'"
ckp "signed-identity tamper rejected"        "cp ~/projects/control-plane/roles/builder.yaml /tmp/m.yaml; $PY scripts/identity.py keygen st >/dev/null; $PY scripts/identity.py sign st /tmp/m.yaml >/dev/null; echo x>>/tmp/m.yaml; ! $PY scripts/identity.py verify st /tmp/m.yaml"

sect "communication fabric"
ckp "message envelope + deadlock-guard"      "$PY scripts/messaging.py | grep -q 'deadlock-guard works'"

sect "eval"
ckp "eval harness (Inspect AI runs)"         "timeout 90 $PY -m inspect_ai eval scripts/demo_eval.py --model mockllm/model --log-dir /tmp/st_logs 2>&1 | grep -q 'accuracy'"

sect "governance"
ckp "gate_check blocks LAUNCH w/o QA"        "GC=~/projects/control-plane/scripts/gate_check.py; ! $PY \$GC ~/projects/products/noupload LAUNCH >/dev/null 2>&1 && $PY \$GC ~/projects/products/noupload BUILD >/dev/null 2>&1"
ckp "metrics ledger + KPIs"                  "$PY scripts/metrics.py | grep -q PASS"
ckp "upward-feedback CR re-flow"             "$PY scripts/cr_reflow.py demo | grep -q PASS"
ckp "BYO provider layer info"                "$PY scripts/providers.py info | grep -q 'active provider'"
ckp "provenance signature valid"            "$PY scripts/provenance.py verify | grep -qE 'VALID|drifted'"
ckp "object store (by-ref/dedup/TTL/GC)"     "$PY scripts/objstore.py test | grep -q PASS"
ckp "scoped secrets vault"                    "$PY scripts/vault.py test | grep -q PASS"
ckp "governed data connector (egress allowlist)" "$PY scripts/connectors.py test | grep -q PASS"
ckp "experiment tracking (log/compare/best)" "$PY scripts/experiments.py demo | grep -q PASS"
ckp "scheduler (recurring jobs)"             "$PY scripts/scheduler.py test | grep -q PASS"
ckp "budget governor (token caps)"          "$PY scripts/budget.py test | grep -q PASS"
ckp "feature flags + rollout"               "$PY scripts/flags.py test | grep -q PASS"
ckp "health monitor + alerting"             "$PY scripts/monitor.py test | grep -q PASS"
ckp "HTTP API (health+auth)"                 "curl -s http://127.0.0.1:8090/health | grep -q agent-os && [ \$(curl -s -o /dev/null -w %{http_code} http://127.0.0.1:8090/status) = 401 ]"
ckp "multi-tenant isolation"                 "$PY scripts/tenancy.py test | grep -q PASS"
ckp "skills/capability registry"            "$PY scripts/skills.py test | grep -q PASS"
ckp "portable encrypted snapshot"           "$PY platform/snapshot.py selftest | grep -q PASS"
ckp "platform inventory covers stack"       "for s in postgres ntfy cerbos; do grep -q \"name: \$s\" platform/inventory.yaml || exit 1; done"
ckp "autonomous factory (governed line)"     "$PY scripts/factory.py selftest | grep -q PASS"
ckp "fleet visibility view"                  "$PY scripts/fleet.py status | grep -q 'agent-os fleet'"
ckp "dashboard state (real data)"            "$PY scripts/dashboard.py state | grep -q '\"overall\"'"
ckp "watchdog detect + heartbeat"            "$PY scripts/watchdog.py selftest | grep -q PASS"
ckp "auto-remediation routing"               "$PY scripts/responder.py selftest | grep -q PASS"
ckp "incident-commander context"             "$PY scripts/incident.py selftest | grep -q PASS"
ckp "SaaS billing (meter/plan/invoice)"      "$PY scripts/billing.py test | grep -q PASS"
ckp "agent directory + conflict detection"   "$PY scripts/directory.py selftest | grep -q PASS"
ckp "orchestration (route/spawn/priority)"   "$PY scripts/orchestrate.py selftest | grep -q PASS"
ckp "debug trace store + replay"             "$PY scripts/trace.py selftest | grep -q PASS"
ckp "trace secret redaction"                 "$PY scripts/redact.py selftest | grep -q PASS"
ckp "launch-kit (marketing-as-code)"         "$PY scripts/launch_kit.py selftest | grep -q PASS"
ckp "portfolio business view"                "$PY scripts/portfolio.py json | grep -q totals"
ckp "founder digest + next-steps"            "$PY scripts/digest.py selftest | grep -q PASS"
ckp "market-intel wiring"                    "$PY scripts/intel.py selftest | grep -q PASS"
ckp "app circuit-breaker (auto-pause)"       "$PY scripts/appguard.py selftest | grep -q PASS"
ckp "OS query plane (agent-queryable)"       "$PY scripts/osq.py selftest | grep -q PASS"
ckp "self-serve front door (signup loop)"    "curl -s http://127.0.0.1:8093/health | grep -q frontdoor && [ \$(curl -s -o /dev/null -w %{http_code} http://127.0.0.1:8093/api/builds) = 401 ]"
ckp "risk register (real risks)"             "$PY scripts/risk.py selftest | grep -q PASS"
ckp "dispatcher (wakes idle agents)"         "$PY scripts/dispatcher.py selftest | grep -q PASS"
ckp "complex-project graph engine"           "$PY scripts/project.py selftest | grep -q PASS"
ckp "research fleet (decompose/parallel/synth)" "$PY scripts/research_fleet.py selftest | grep -q PASS"
ckp "scalable verification (tiered)"        "$PY scripts/verify.py selftest | grep -q PASS"
ckp "budget->capability scheduler"           "$PY scripts/scale.py selftest | grep -q PASS"
ckp "continuous improvement (safe-deploy)" "$PY scripts/improve.py selftest | grep -q PASS"
ckp "task board (asked->status->done)"     "$PY scripts/taskboard.py selftest | grep -q PASS"
ckp "orphan/stuck agent reaper"          "$PY scripts/reap.py selftest | grep -q PASS"
ckp "accountability: dropped-handoff detection" "$PY scripts/accountability.py selftest | grep -q PASS"
ckp "findings -> routed owner -> tracked" "$PY scripts/findings.py selftest | grep -q PASS"
ckp "multi-org (first-class orgs per tenant)" "$PY scripts/orgs.py selftest | grep -q PASS"
ckp "quality loop (climb-to-bar)" "$PY scripts/qualityloop.py selftest | grep -q PASS"
ckp "per-tenant push transport" "$PY scripts/push.py selftest | grep -q PASS"
ckp "agent->human request (resumable)" "$PY scripts/agent_request.py selftest | grep -q PASS"
ckp "ask-user (pause/resume mid-loop)" "$PY scripts/askuser.py selftest | grep -q PASS"
ckp "agent->agent alerts (routed)" "$PY scripts/alerts.py selftest | grep -q PASS"
ckp "design fleet (prototype 3 surfaces)" "$PY scripts/design_fleet.py selftest | grep -q PASS"
ckp "design gallery view" "$PY scripts/designview.py selftest | grep -q PASS"
ckp "data-driven recommendations" "$PY scripts/recommend.py selftest | grep -q PASS"
ckp "cross-org ops (merge/steal-feature)" "$PY scripts/crossorg.py selftest | grep -q PASS"
ckp "cross-org portfolio view" "$PY scripts/crossorgview.py selftest | grep -q PASS"
ckp "agentic features (embed agents in product)" "$PY scripts/agentfeatures.py selftest | grep -q PASS"
ckp "queue: stuck-task lease reclaim"    "$PY scripts/tasksweep.py selftest | grep -q PASS"
ckp "runtime kill-switch (halt/resume)"  "$PY scripts/killswitch.py selftest | grep -q PASS"
ckp "AI-consent gate (block/allow/revoke)" "$PY scripts/consent.py selftest | grep -q PASS"
ckp "notification taxonomy (silent/std)" "$PY scripts/notifications.py selftest | grep -q PASS"
ckp "public status page (live verdict)"  "$PY scripts/statuspage.py selftest | grep -q PASS"
ckp "prometheus metrics exposition"      "$PY scripts/metricsexport.py selftest | grep -q PASS"
ckp "CEO cockpit (workers/comms/budget)" "$PY scripts/cockpit.py selftest | grep -q PASS"
ckp "projects workspace (list/detail)"   "$PY scripts/projectsview.py selftest | grep -q PASS"
ckp "observability + trace explorer"     "$PY scripts/traceview.py selftest | grep -q PASS"
ckp "approvals/decisions inbox"          "$PY scripts/approvals.py selftest | grep -q PASS"
ckp "integrations marketplace"           "$PY scripts/integrationsview.py selftest | grep -q PASS"
ckp "billing & plans view"               "$PY scripts/billingview.py selftest | grep -q PASS"
ckp "settings (profile/consent/prefs)"   "$PY scripts/settingsview.py selftest | grep -q PASS"
ckp "templates/blueprints gallery"       "$PY scripts/templatesview.py selftest | grep -q PASS"
ckp "budget forecast + pre-emptive alerts" "$PY scripts/forecast.py selftest | grep -q PASS"
ckp "orchestrator chat (clarify->build)"  "$PY scripts/orchestrator.py selftest | grep -q PASS"
ckp "multi-provider BYO keys (claude/codex)" "$PY scripts/tenantproviders.py selftest | grep -q PASS"
ckp "plain-language quality verdict"      "$PY scripts/qualityview.py selftest | grep -q PASS"
ckp "pre-commit cost estimate"            "$PY scripts/estimate.py selftest | grep -q PASS"
ckp "per-project budget caps"             "$PY scripts/projbudget.py selftest | grep -q PASS"
ckp "guided onboarding wizard"            "$PY scripts/onboarding.py selftest | grep -q PASS"
ckp "product versioning + rollback"       "$PY scripts/versions.py selftest | grep -q PASS"
ckp "account export + GDPR delete"        "$PY scripts/account.py selftest | grep -q PASS"
ckp "in-app help assistant"               "$PY scripts/helpagent.py selftest | grep -q PASS"
ckp "custom standing agents (CEO factory)" "$PY scripts/customagents.py selftest | grep -q PASS"
ckp "tenant org chart (live agents)"      "$PY scripts/orgview.py selftest | grep -q PASS"
ckp "live product status + URL"           "$PY scripts/livestatus.py selftest | grep -q PASS"
ckp "tenant CONSOLE (all area routes)"    "$PY scripts/console.py selftest | grep -q PASS"
ckp "console click-through (real browser)" "bash scripts/console_e2e.sh gate | grep -q PASS"
ckp "product craft (responsive/empty-states/microcopy)" "bash scripts/console_craft_e2e.sh gate | grep -q PASS"
ckp "exhaustive action coverage (every control)" "bash scripts/console_actions_e2e.sh gate | grep -q PASS"
ckp "prompt-injection sanitize"              "$PY scripts/sanitize.py selftest | grep -q PASS"
ckp "callsite signature wiring"              "$PY scripts/test_callsites_wired.py | grep -q PASS"
ckp "enforcement-layer consistency (gov==hook)" "$PY scripts/test_enforcement_consistency.py | grep -q PASS"
ckp "governance controls wired"              "$PY scripts/test_governance_wired.py | grep -q PASS"
ckp "quality-lens triad wired"               "$PY scripts/test_quality_lenses_wired.py | grep -q PASS"
ckp "daemon supervision wired"               "$PY scripts/test_supervision_wired.py | grep -q PASS"
ckp "sentinel silent-failure observer"       "$PY scripts/sentinel.py selftest | grep -q PASS"
ckp "memory spine (company mem + role lessons)"  "$PY scripts/companymemory.py selftest | grep -q PASS"
ckp "acceptance dogfood wired"               "$PY scripts/dogfood.py selftest | grep -q PASS"
ckp "security scan (invariants)"             "$PY scripts/security_scan.py | grep -q PASS"
ckp "unit test suite (pytest)"               "$PY -m pytest tests/ -q | tail -1 | grep -q passed"

sect "quality engine (agentic QA — REBUILD-PLAN C1)"
# The five scripts/qa/* offline selftests (deterministic: AI calls + browser stubbed, no network/spend)
# + the real-browser bridge protocol selftest. Tight grep patterns on purpose: qa output legitimately
# contains the word "PASSED" in verdict strings, so a bare `grep -q PASS` could green a FAIL run.
ckp "qa story generation (offline)"          "$PY scripts/qa/story_gen.py selftest | grep -q 'PASS: story_gen'"
ckp "qa explorer (blame/settle contracts)"   "$PY scripts/qa/qa_explorer.py 2>&1 | grep -q 'qa_explorer selftest: PASS'"
ckp "qa dev-fix loop (plan/spawn/judge)"     "$PY scripts/qa/dev_loop.py selftest | grep -q 'PASS: dev-fix loop wired'"
ckp "qa grounded report (md+json verdict)"   "$PY scripts/qa/qa_report.py selftest | grep -q 'PASS: qa_report'"
ckp "qa autonomous loop (rounds/fix/reset)"  "$PY scripts/qa/qa_run.py selftest | grep -q 'qa_run selftest: PASS'"
ckp "qa browser bridge (real browser proto)" "NODE_PATH=\$HOME/projects/products/noupload/node_modules node scripts/qa/browser_bridge.js selftest | grep -q '\"selftest\":\"PASS\"'"
# Wiring guard (STANDARDS-verification: probe the mechanism, never the claim): FAILS if factory's QA
# stage / gate_check LAUNCH / loopcontroller TESTQA stop consuming the qa verdict JSON (passed,
# blocking_open==0, stories>0), or if a self-grading `qa_run` shadow reappears in factory.py.
ckp "QA ship-gate wired (verdict JSON consumed)" "$PY scripts/test_qa_gate_wired.py | grep -q '^PASS'"

# Fire the bounded concurrent pool for everything enqueued above.
run_pool

# ===========================================================================
# SERIAL CHECKS — share mutable, non-row-scoped state; must NOT run concurrently.
# ===========================================================================
echo "=== serial (shared mutable state / wall-clock sensitive) ==="
# deadlock + retention both touch the `waits` table (deadlock TRUNCATEs it) -> run one at a time.
ck "deadlock detector (cycle found)"        "$PY scripts/deadlock.py clear >/dev/null; $PY scripts/deadlock.py edge a b>/dev/null; $PY scripts/deadlock.py edge b a>/dev/null; ! $PY scripts/deadlock.py detect; $PY scripts/deadlock.py clear>/dev/null"
ck "retention sweep (record expiry)"        "$PY scripts/retention.py test | grep -q PASS"
# research + loopcontroller run a fleet in a DAEMON THREAD and poll for completion on a tight
# wall-clock deadline (research: 10s; loopcontroller: ~14s). Under the pool's CPU saturation those
# threads get starved past the deadline and the check flakes — so they run here, contention-free.
ck "research state+options"                 "$PY scripts/research.py selftest | grep -q PASS"
ck "CLOSED-LOOP controller (discover->deliver)" "$PY scripts/loopcontroller.py selftest | grep -q PASS"

echo "=== integration: standing Controller ==="
# C1: the Controller NEVER seeds the QA verdict for itself. Without docs/QA-VERDICT.json the lifecycle
# must BLOCK honestly at the REVIEW gate (naming the missing verdict); with a real machine verdict
# present (here: a green fixture shaped exactly like qa_run.write_verdict output) it reaches LAUNCHED.
wipe_wf(){ dbexec "DELETE FROM dbos.workflow_status" >/dev/null 2>&1 || true; sg docker -c "docker exec -e PGPASSWORD=$PW agentos-postgres psql -U agentos -d agentos_dbos_sys -c \"DELETE FROM dbos.workflow_status WHERE workflow_uuid='prod-st-demo'\"" >/dev/null 2>&1; }
ck "controller: no QA verdict -> honest REVIEW block" "rm -rf ~/projects/products/st-demo; wipe_wf; $PY scripts/controller.py run st-demo 2>&1 | grep -q 'QA-VERDICT'"
ck "controller lifecycle -> LAUNCHED (verdict present)" "$PY -c \"import json,time,pathlib; p=pathlib.Path.home()/'projects/products/st-demo/docs/QA-VERDICT.json'; p.parent.mkdir(parents=True,exist_ok=True); p.write_text(json.dumps({'schema':'aos.qa.verdict/1','product':'st-demo','passed':True,'stories':3,'blocking_open':0,'open_bugs':0,'verdict':'ALL 3 STORIES PASSED','report_md':'/tmp/aos-qa/report-st-demo.md','report_json':'/tmp/aos-qa/report-st-demo.json','producer':'qa_run','generated_at':time.time()}))\"; wipe_wf; $PY scripts/controller.py run st-demo | grep -q LAUNCHED"

rm -f /tmp/st.$$
echo
echo "SELFTEST: $pass passed, $fail failed"
[ "$fail" -eq 0 ] || exit 1
