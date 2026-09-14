#!/usr/bin/env bash
# rebuild.sh — stand up the ENTIRE agent-os stack from nothing, in one command.
# Target: a fresh laptop or a cloud VM. Idempotent. This is the "few steps tomorrow" path and the
# "set it up on a buyer's machine" path. It reuses the proven per-service compose files + migrations
# + seeders, then proves the box with the full self-test.
#
# Prereqs it does NOT install (prints how): docker engine, python3-venv. Everything else it does.
#
#   bash platform/rebuild.sh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${AOS_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
CP="${AOS_CONTROL_PLANE_ROOT:-$(cd "$ROOT/.." && pwd)/control-plane}"
say(){ printf '\n\033[1m== %s ==\033[0m\n' "$1"; }
ok(){ printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn(){ printf '  \033[33m!\033[0m %s\n' "$1"; }
die(){ warn "$1"; exit 1; }
have(){ command -v "$1" >/dev/null 2>&1; }
DK(){ if docker info >/dev/null 2>&1; then docker "$@"; else sg docker -c "docker $*"; fi; }

env_value(){ sed -n "s/^$1=//p" "$ROOT/.env.local" | tail -n 1; }
set_env_value(){
  local key="$1" value="$2"
  if grep -q "^${key}=" "$ROOT/.env.local"; then
    sed -i -E "s#^${key}=.*#${key}=${value}#" "$ROOT/.env.local"
  else
    printf '%s=%s\n' "$key" "$value" >> "$ROOT/.env.local"
  fi
}
ensure_secret(){
  local key="$1" generated="$2" current
  current="$(env_value "$key")"
  if [[ -z "$current" ]] || [[ "$current" == *CHANGE-ME* ]]; then
    set_env_value "$key" "$generated"
    ok "generated $key"
  fi
}

say "0. Prerequisites"
have docker || die "docker MISSING — install Docker Engine + Compose first"
DK info >/dev/null 2>&1 || { sudo service docker start >/dev/null 2>&1 || true; sleep 3; }
DK info >/dev/null 2>&1 && ok "docker reachable" || die "docker daemon not reachable"
have python3 || die "python3 MISSING"
have openssl || die "openssl MISSING"

say "1. Secrets / config (generate if absent — never overwrite)"
[ -f "$ROOT/postgres/.env" ] || { umask 077; printf 'POSTGRES_PASSWORD=%s\n' "$(openssl rand -hex 18)" > "$ROOT/postgres/.env"; ok "generated postgres/.env"; }
if [ ! -f "$ROOT/.env.local" ]; then
  cp "$ROOT/.env.example" "$ROOT/.env.local"
  ok ".env.local created"
else ok ".env.local present (kept)"; fi
chmod 600 "$ROOT/.env.local" "$ROOT/postgres/.env"
PW="$(sed -n 's/^POSTGRES_PASSWORD=//p' "$ROOT/postgres/.env" | tail -n 1)"
database_url="$(env_value DATABASE_URL)"
if [[ -z "$database_url" ]] || [[ "$database_url" == *CHANGE-ME* ]]; then
  set_env_value DATABASE_URL "postgresql://agentos:${PW}@127.0.0.1:5433/agentos"
  ok "DATABASE_URL bound to generated local Postgres credential"
fi
ensure_secret AOS_API_TOKEN "$(openssl rand -hex 32)"
ensure_secret AUDIT_HMAC_KEY "$(openssl rand -hex 32)"
ensure_secret VAULT_KEY "$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '\n')"
ensure_secret AOSNAP_PASS "$(openssl rand -hex 24)"
ensure_secret NTFY_TOPIC "aos-$(openssl rand -hex 24)"
[ -f "$ROOT/ntfy/.env" ] || cp "$ROOT/ntfy/.env.example" "$ROOT/ntfy/.env" 2>/dev/null || true

say "2. Python venv + deps"
[ -x "$ROOT/.venv/bin/python" ] || python3 -m venv "$ROOT/.venv" 2>/dev/null || die "venv failed (install python3-venv)"
"$ROOT/.venv/bin/pip" install -q --upgrade pip >/dev/null 2>&1 || true
"$ROOT/.venv/bin/pip" install -q -r "$ROOT/requirements.txt" && ok "deps installed" || die "pip install failed"

say "3. Bring up the stack (containers)"
for s in postgres nats ntfy cerbos; do
  ( cd "$ROOT/$s" && DK compose up -d ) >/dev/null 2>&1 && ok "$s up" || die "$s failed"
done

say "4. Wait for Postgres, then migrate"
for i in $(seq 1 30); do
  DK exec agentos-postgres pg_isready -U agentos -d agentos >/dev/null 2>&1 && break; sleep 2
done
DK exec agentos-postgres pg_isready -U agentos -d agentos >/dev/null 2>&1 && ok "postgres ready" || die "postgres not ready"
bash "$ROOT/platform/migrate.sh" >/tmp/agentos-migrate.log 2>&1 && ok "schema migrated (ordered + quiescence checked)" || {
  tail -n 20 /tmp/agentos-migrate.log; die "migrate failed"; }
"$ROOT/.venv/bin/python" "$ROOT/scripts/rls_readiness.py" rollout-gate \
  >/tmp/agentos-rls-rollout.log 2>&1 && ok "RLS rollout gate passed" || {
  tail -n 30 /tmp/agentos-rls-rollout.log; die "RLS rollout gate failed"; }

say "5. Seed the org (skills + roles)"
"$ROOT/.venv/bin/python" "$ROOT/scripts/skills.py" seed >/dev/null 2>&1 && ok "skills seeded" || warn "skills seed failed"
[ -d "$CP" ] && python3 "$CP/scripts/generate_org.py" >/dev/null 2>&1 && ok "roles generated" || warn "control-plane not present (clone it for roles)"
[ -d "$CP" ] && python3 "$CP/scripts/validate_manifests.py" >/dev/null 2>&1 && ok "manifests valid" || true
"$ROOT/.venv/bin/python" "$ROOT/scripts/scheduler.py" register snapshot-backup 86400 \
  '.venv/bin/python platform/snapshot.py export' >/dev/null 2>&1 && ok "daily encrypted snapshot job registered" || warn "snapshot job registration failed"
"$ROOT/.venv/bin/python" "$ROOT/scripts/scheduler.py" register trace-retention 86400 \
  '.venv/bin/python scripts/trace.py prune 30' >/dev/null 2>&1 && ok "daily trace-retention job registered" || warn "trace-retention registration failed"
"$ROOT/.venv/bin/python" "$ROOT/scripts/scheduler.py" register app-profit-guard 3600 \
  '.venv/bin/python scripts/appguard.py guard' >/dev/null 2>&1 && ok "hourly app profit-guard job registered" || warn "profit-guard registration failed"
"$ROOT/.venv/bin/python" "$ROOT/scripts/scheduler.py" register founder-digest 604800 \
  '.venv/bin/python scripts/digest.py send' >/dev/null 2>&1 && ok "weekly founder-digest job registered" || warn "digest registration failed"
"$ROOT/.venv/bin/python" "$ROOT/scripts/scheduler.py" register eval-weekly 604800 \
  '.venv/bin/python scripts/eval_factory.py run 2' >/dev/null 2>&1 && ok "weekly eval (quality-drift) job registered" || warn "eval registration failed"
"$ROOT/.venv/bin/python" "$ROOT/scripts/scheduler.py" register resume-sweep 600 \
  '.venv/bin/python scripts/factory.py resume-sweep' >/dev/null 2>&1 && ok "interrupted-build resume sweep registered" || warn "resume-sweep registration failed"
"$ROOT/.venv/bin/python" "$ROOT/scripts/scheduler.py" register resume-sweep-projects 600 \
  '.venv/bin/python scripts/project.py resume-sweep' >/dev/null 2>&1 && ok "interrupted complex-build resume sweep registered" || warn "resume-sweep-projects registration failed"
"$ROOT/.venv/bin/python" "$ROOT/scripts/scheduler.py" register devserve-keepalive 600 \
  '.venv/bin/python scripts/devserve.py up-all' >/dev/null 2>&1 && ok "dev app-server keepalive registered" || warn "devserve-keepalive registration failed"
"$ROOT/.venv/bin/python" "$ROOT/scripts/scheduler.py" register reap-orphans 600 \
  '.venv/bin/python scripts/reap.py run' >/dev/null 2>&1 && ok "orphan/stuck agent reaper registered" || warn "reap-orphans registration failed"
"$ROOT/.venv/bin/python" "$ROOT/scripts/scheduler.py" register accountability-sweep 1800 \
  '.venv/bin/python scripts/accountability.py sweep' >/dev/null 2>&1 && ok "accountability sweep registered" || warn "accountability registration failed"
"$ROOT/.venv/bin/python" "$ROOT/scripts/scheduler.py" register findings-sweep 1800 \
  '.venv/bin/python scripts/findings.py sweep' >/dev/null 2>&1 && ok "findings sweep registered" || warn "findings registration failed"
"$ROOT/.venv/bin/python" "$ROOT/scripts/scheduler.py" register tasksweep 600 \
  '.venv/bin/python scripts/tasksweep.py run' >/dev/null 2>&1 && ok "stuck-task lease reclaim registered" || warn "tasksweep registration failed"
"$ROOT/.venv/bin/python" "$ROOT/scripts/scheduler.py" register budget-forecast-sweep 3600 \
  '.venv/bin/python scripts/forecast.py sweep' >/dev/null 2>&1 && ok "budget forecast/alert sweep registered" || warn "forecast sweep registration failed"

say "6. Prove the box (full self-test)"
if bash "$ROOT/scripts/selftest.sh" >/tmp/rebuild-selftest.log 2>&1; then
  ok "$(grep -E 'SELFTEST: ' /tmp/rebuild-selftest.log | tail -1)"
else
  warn "self-test had failures — see /tmp/rebuild-selftest.log"; tail -20 /tmp/rebuild-selftest.log; exit 1
fi

printf '\n\033[1mrebuild.sh done.\033[0m Stack is up + proven. Run scripts/recover.sh for phone/Tailscale reach\n'
printf 'and to start the scheduler ticker (drives the daily encrypted snapshot in platform/snapshot.py).\n'
