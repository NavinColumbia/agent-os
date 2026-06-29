#!/usr/bin/env bash
# wire_product_repo.sh <product-name> — create a product repo from the control-plane template
# and wire its .claude/settings.json to enforce the Builder manifest (Step 1).
#
#   CP   = control-plane path (default ~/projects/control-plane)
#   dest = ~/projects/products/<product-name>
set -eu

NAME="${1:?usage: wire_product_repo.sh <product-name>}"
CP="${CP:-$HOME/projects/control-plane}"
TEMPLATE="$CP/templates/product-repo"
DEST="$HOME/projects/products/$NAME"

[ -d "$TEMPLATE" ] || { echo "ERROR: template not found at $TEMPLATE — is control-plane cloned?"; exit 1; }
[ -e "$DEST" ] && { echo "ERROR: $DEST already exists; remove it or pick another name."; exit 1; }

mkdir -p "$(dirname "$DEST")"
cp -r "$TEMPLATE" "$DEST"
mkdir -p "$DEST/src" "$DEST/tests" "$DEST/.claude"

cat > "$DEST/src/app.py" <<'PY'
def add(a, b):
    return a + b
PY
cat > "$DEST/tests/test_app.py" <<'PY'
from src.app import add
def test_add():
    assert add(2, 3) == 5
PY
# a .env so "deny reading .env" is a real target in the smoke test
echo "SECRET_API_KEY=replace-me" > "$DEST/.env"

# wire enforcement: CP + PreToolUse hook.
#
# Finding #42: a product repo is operated by MANY roles (builder, qa-security,
# reviewer, devops-sre, tech-lead, ...). Hard-coding CP_MANIFEST -> builder.yaml
# in settings.json "env" makes that value WIN over the per-role CP_MANIFEST that
# factory._run_once passes in the subprocess env (factory.py), so every role would
# be governed as a builder. There is no single correct role to bake into a per-repo
# file, so we DO NOT set CP_MANIFEST here — the per-run role manifest governs.
#
# Standalone use (no factory) is fail-closed by design: enforce_manifest.py DENIES
# every call when CP_MANIFEST is absent, so an operator must export the active role's
# manifest (e.g. constraint_smoke_test.sh defaults it) before the repo will allow work.
cat > "$DEST/.claude/settings.json" <<JSON
{
  "env": {
    "CP": "$CP"
  },
  "hooks": {
    "PreToolUse": [
      { "matcher": "*", "hooks": [
        { "type": "command", "command": "python3 $CP/hooks/enforce_manifest.py" } ] }
    ]
  }
}
JSON

echo "wired product repo: $DEST"
echo "  CP_MANIFEST -> set per-run by the dispatching role (factory); fail-closed if unset"
echo "prove it:  REPO=$DEST CP=$CP bash $HOME/projects/agent-os/scripts/constraint_smoke_test.sh"
