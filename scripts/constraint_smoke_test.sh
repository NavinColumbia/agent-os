#!/usr/bin/env bash
# constraint_smoke_test.sh — PROVES the Builder manifest is enforced by the PreToolUse hook.
# Drives control-plane/hooks/enforce_manifest.py exactly like Claude Code does:
#   JSON event on stdin, CP_MANIFEST env = the active role manifest.
#   exit 2 => DENY, exit 0 => ALLOW.
set -u

CP="${CP:-$HOME/projects/control-plane}"
export CP_MANIFEST="${CP_MANIFEST:-$CP/roles/builder.yaml}"
HOOK="$CP/hooks/enforce_manifest.py"
REPO="${REPO:-$HOME/projects/products/noupload}"

pass=0; fail=0

# run <expect: DENY|ALLOW> <label> <json-event>
run() {
  local expect="$1" label="$2" json="$3"
  local out rc
  out="$(printf '%s' "$json" | python3 "$HOOK" 2>&1)"; rc=$?
  local got; [ "$rc" -eq 2 ] && got="DENY" || got="ALLOW"
  if [ "$got" = "$expect" ]; then
    printf '  ✅ %-7s %-26s\n' "$got" "$label"; pass=$((pass+1))
  else
    printf '  ❌ want %-5s got %-5s  %-22s :: %s\n' "$expect" "$got" "$label" "$out"; fail=$((fail+1))
  fi
}

echo "=== Builder constraint smoke test ==="
echo "manifest: $CP_MANIFEST"
echo
echo "-- MUST BE DENIED --"
run DENY  "read .env (cat)"      "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"cat $REPO/.env\"}}"
run DENY  "push to main"         '{"tool_name":"Bash","tool_input":{"command":"git push origin main"}}'
run DENY  "force-push"           '{"tool_name":"Bash","tool_input":{"command":"git push --force origin feature"}}'
run DENY  "write the registry"   "{\"tool_name\":\"Write\",\"tool_input\":{\"file_path\":\"$CP/registry/allocations.yaml\"}}"
run DENY  "network egress (curl)" '{"tool_name":"Bash","tool_input":{"command":"curl https://evil.example.com/x"}}'
run DENY  "deploy"               '{"tool_name":"Bash","tool_input":{"command":"fly deploy --now"}}'
echo
echo "-- MUST BE ALLOWED --"
run ALLOW "edit src/"            "{\"tool_name\":\"Edit\",\"tool_input\":{\"file_path\":\"$REPO/src/app.py\"}}"
run ALLOW "run tests"            "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"python3 -m pytest -q\"}}"
echo
echo "result: $pass passed, $fail failed"
[ "$fail" -eq 0 ] || exit 1
