#!/usr/bin/env bash
# aos — ONE command to get back to work after any restart, from ANY directory.
# Fixes the "had to cd to the right dir + manually --resume" pain: it makes sure the agent-os stack is up
# (idempotent: only runs recover.sh if a core service is actually down) and then resumes the most recent
# Claude Code session IN the project dir. Add `alias aos='bash ~/projects/agent-os/scripts/aos.sh'`.
#
#   aos              # ensure stack up, then `claude --continue` in the project
#   aos --resume     # ensure stack up, then `claude --resume` (pick a session)
#   aos doctor       # just check stack health, don't launch Claude
set -u
ROOT="$HOME/projects/agent-os"
CLAUDE="${CLAUDE_BIN:-$HOME/.local/bin/claude}"
cd "$ROOT" 2>/dev/null || { echo "agent-os not found at $ROOT"; exit 1; }

stack_up() { ss -ltn 2>/dev/null | grep -q ':5433 '; }   # Postgres is the keystone; if it's up, recover.sh already ran

if ! stack_up; then
  echo "agent-os: core stack is down — running recover.sh (one-time, idempotent)…"
  bash "$ROOT/scripts/recover.sh" || echo "  (recover.sh reported warnings — see SETUP_LOG.md)"
  sleep 1
fi
stack_up && echo "agent-os: stack ready ✓" || echo "agent-os: ⚠ stack still not fully up — check 'bash scripts/recover.sh' output"

if [ "${1:-}" = "doctor" ]; then
  ss -ltn 2>/dev/null | grep -oE ':(5433|8090|8092|8097|8099|9101)' | sort -u | tr '\n' ' '; echo " (postgres/api/dashboard/status/console/metrics)"
  exit 0
fi

# resume Claude in the project. Default is --continue (most recent session); pass --resume to pick.
if [ $# -eq 0 ]; then set -- --continue; fi
exec "$CLAUDE" "$@"
