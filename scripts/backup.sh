#!/usr/bin/env bash
# backup.sh — full agent-os backup: ALL Postgres data (state, audit, comms, memory, metrics, vault,
# blobs, experiments, budgets, flags) + signing keys + local config, into one encryptable archive.
# restore.sh documents the inverse. Schedule via scheduler.py for periodic backups.
set -eu
ROOT="$HOME/projects/agent-os"
OUT="${1:-$ROOT/backups}"
mkdir -p "$OUT"
TS=$(date +%Y%m%d-%H%M%S)
STAGE=$(mktemp -d)
PW=$(grep '^DATABASE_URL=' "$ROOT/.env.local" | sed -E 's#.*//agentos:([^@]+)@.*#\1#')

# 1) full database dump (custom format, compressed)
sg docker -c "docker exec -e PGPASSWORD=$PW agentos-postgres pg_dump -U agentos -Fc agentos" > "$STAGE/agentos.dump"
# 2) signing keys + local secrets/config (the irreplaceable bits)
cp -r "$ROOT/keys" "$STAGE/keys" 2>/dev/null || true
cp "$ROOT/.env.local" "$STAGE/env.local.bak" 2>/dev/null || true
cp "$ROOT/PROVENANCE.json" "$STAGE/" 2>/dev/null || true
echo "agent-os backup $TS" > "$STAGE/MANIFEST.txt"
du -h "$STAGE/agentos.dump" | awk '{print "db dump: "$1}' >> "$STAGE/MANIFEST.txt"

ARCHIVE="$OUT/agent-os-backup-$TS.tgz"
tar czf "$ARCHIVE" -C "$STAGE" .
rm -rf "$STAGE"
echo "✅ backup -> $ARCHIVE ($(du -h "$ARCHIVE" | cut -f1))"
echo "   contains: full DB dump + signing keys + .env.local + provenance"
echo "   restore: tar xzf <archive>; createdb + pg_restore agentos.dump; restore keys/ + .env.local"
echo "   ⚠ this archive contains secrets — store it encrypted/offline."
