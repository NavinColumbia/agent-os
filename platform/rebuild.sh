#!/usr/bin/env bash
# rebuild.sh — stand up the ENTIRE agent-os stack from nothing, in one command.
# Target: a fresh laptop or a cloud VM. Idempotent. This is the "few steps tomorrow" path and the
# "set it up on a buyer's machine" path. It reuses the proven per-service compose files + migrations
# + seeders, then proves the box with the full self-test.
#
# Prereqs it does NOT install (prints how): docker engine, python3-venv. Everything else it does.
#
#   bash platform/rebuild.sh
set -u
ROOT="$HOME/projects/agent-os"
CP="$HOME/projects/control-plane"
say(){ printf '\n\033[1m== %s ==\033[0m\n' "$1"; }
ok(){ printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn(){ printf '  \033[33m!\033[0m %s\n' "$1"; }
have(){ command -v "$1" >/dev/null 2>&1; }
DK(){ if docker info >/dev/null 2>&1; then docker "$@"; else sg docker -c "docker $*"; fi; }

say "0. Prerequisites"
have docker || { warn "docker MISSING — curl -fsSL https://get.docker.com | sudo sh ; sudo usermod -aG docker \$USER ; sudo service docker start"; exit 1; }
DK info >/dev/null 2>&1 || { sudo service docker start >/dev/null 2>&1 || true; sleep 3; }
DK info >/dev/null 2>&1 && ok "docker reachable" || { warn "docker daemon not reachable"; exit 1; }
have python3 || { warn "python3 MISSING"; exit 1; }

say "1. Secrets / config (generate if absent — never overwrite)"
[ -f "$ROOT/postgres/.env" ] || { echo "POSTGRES_PASSWORD=$(openssl rand -hex 18)" > "$ROOT/postgres/.env"; ok "generated postgres/.env"; }
if [ ! -f "$ROOT/.env.local" ]; then
  PW=$(grep -oE 'POSTGRES_PASSWORD=.*' "$ROOT/postgres/.env" | cut -d= -f2)
  cp "$ROOT/.env.example" "$ROOT/.env.local"
  # point DATABASE_URL at the freshly generated password + local port
  if grep -q '^DATABASE_URL=' "$ROOT/.env.local"; then
    sed -i -E "s#^DATABASE_URL=.*#DATABASE_URL=postgresql://agentos:${PW}@127.0.0.1:5433/agentos#" "$ROOT/.env.local"
  else
    echo "DATABASE_URL=postgresql://agentos:${PW}@127.0.0.1:5433/agentos" >> "$ROOT/.env.local"
  fi
  warn ".env.local created — review NTFY_TOPIC / AOS_API_TOKEN before going live"
else ok ".env.local present (kept)"; fi
[ -f "$ROOT/ntfy/.env" ] || cp "$ROOT/ntfy/.env.example" "$ROOT/ntfy/.env" 2>/dev/null || true
if ! grep -q '^AOSNAP_PASS=' "$ROOT/.env.local" 2>/dev/null; then
  printf '\n# Encrypted-snapshot passphrase (STORE A COPY OFFLINE for disaster recovery)\nAOSNAP_PASS=%s\n' "$(openssl rand -hex 24)" >> "$ROOT/.env.local"
  warn "generated AOSNAP_PASS in .env.local — save a copy offline (without it, snapshots are unrecoverable)"
fi

say "2. Python venv + deps"
[ -x "$ROOT/.venv/bin/python" ] || python3 -m venv "$ROOT/.venv" 2>/dev/null || warn "venv failed (sudo apt-get install python3-venv)"
"$ROOT/.venv/bin/pip" install -q --upgrade pip >/dev/null 2>&1 || true
"$ROOT/.venv/bin/pip" install -q -r "$ROOT/requirements.txt" && ok "deps installed" || warn "pip install failed"

say "3. Bring up the stack (containers)"
for s in postgres nats ntfy cerbos; do
  ( cd "$ROOT/$s" && DK compose up -d ) >/dev/null 2>&1 && ok "$s up" || warn "$s failed"
done

say "4. Wait for Postgres, then migrate"
for i in $(seq 1 30); do
  DK exec agentos-postgres pg_isready -U agentos -d agentos >/dev/null 2>&1 && break; sleep 2
done
DK exec agentos-postgres pg_isready -U agentos -d agentos >/dev/null 2>&1 && ok "postgres ready" || warn "postgres not ready"
bash "$ROOT/platform/migrate.sh" >/dev/null 2>&1 && ok "schema migrated (all initdb SQL)" || warn "migrate failed"

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
  warn "self-test had failures — see /tmp/rebuild-selftest.log"; tail -3 /tmp/rebuild-selftest.log
fi

printf '\n\033[1mrebuild.sh done.\033[0m Stack is up + proven. Run scripts/recover.sh for phone/Tailscale reach\n'
printf 'and to start the scheduler ticker (drives the daily encrypted snapshot in platform/snapshot.py).\n'
