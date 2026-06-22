#!/usr/bin/env bash
# new-product.sh <name> — stand up a fully-GOVERNED, ready-to-build product in one command.
# Wires the capability manifest + enforcement hook, seeds lifecycle docs from templates, inits git,
# and registers it. This is the "minimal setup for each new product" path.
set -eu
NAME="${1:?usage: new-product.sh <product-name>}"
CP="$HOME/projects/control-plane"
REPO="$HOME/projects/products/$NAME"

[ -e "$REPO" ] && { echo "ERROR: $REPO exists"; exit 1; }

# 1) repo + builder manifest + enforce_manifest PreToolUse hook + minimal src/tests/.env
CP="$CP" bash "$HOME/projects/agent-os/scripts/wire_product_repo.sh" "$NAME" >/dev/null

# 2) seed lifecycle docs from the org templates (the Controller fills these as it advances)
mkdir -p "$REPO/docs"
for t in PROJECT-RUNBOOK SPEC ADR CHANGE-REQUEST QA-REPORT; do
  [ -f "$CP/templates/$t.md" ] && cp "$CP/templates/$t.md" "$REPO/docs/$t.md"
done
mv "$REPO/docs/ADR.md" "$REPO/docs/adr-template.md" 2>/dev/null || true

# 3) product README + git init
cat > "$REPO/README.md" <<EOF
# $NAME
A governed product on agent-os. Manifest: builder.yaml (enforce_manifest hook active).
Run it through the lifecycle: \`.venv/bin/python ~/projects/agent-os/scripts/controller.py run $NAME\`
Secrets: scope them to this product via \`vault.py put <name> $NAME <env> <roles> <value>\`.
EOF
git -C "$REPO" init -q
git -C "$REPO" -c user.email="agent-os@local" -c user.name="agent-os" add -A
git -C "$REPO" -c user.email="agent-os@local" -c user.name="agent-os" commit -q -m "scaffold $NAME (governed, manifest+hook wired, lifecycle docs seeded)"

echo "✅ product '$NAME' ready at $REPO"
echo "   • manifest + enforce_manifest hook wired (.claude/settings.json)"
echo "   • lifecycle docs seeded (docs/); git initialized"
echo "   • next: controller.py run $NAME   |   scope secrets via vault.py   |   QA via Playwright harness"
