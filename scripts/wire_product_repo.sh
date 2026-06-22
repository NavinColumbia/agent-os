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

# wire enforcement: CP + builder manifest + PreToolUse hook
cat > "$DEST/.claude/settings.json" <<JSON
{
  "env": {
    "CP": "$CP",
    "CP_MANIFEST": "$CP/roles/builder.yaml"
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
echo "  CP_MANIFEST -> $CP/roles/builder.yaml"
echo "prove it:  REPO=$DEST CP=$CP bash $HOME/projects/agent-os/scripts/constraint_smoke_test.sh"
