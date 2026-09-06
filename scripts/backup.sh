#!/usr/bin/env bash
#
# backup.sh — local backup of everything a dead Mac Mini or a bad
# `docker compose down -v` would otherwise lose:
#   - the nevnew repo itself (config, callbacks, scripts, .env)
#   - the newnew_open_webui_data volume (Open-WebUI chat history/db)
#   - ~/.n8n (n8n workflows/database)
#
# Writes a timestamped set of archives to ~/Backups/nevnew/<timestamp>/.
# Cloud upload is NOT included here — that needs a separate credentialed
# service and is tracked as a follow-up, not done by this script.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="$HOME/Backups/nevnew/$TIMESTAMP"

mkdir -p "$BACKUP_DIR"
echo "==> Backing up to: $BACKUP_DIR"

echo "==> [1/3] nevnew repo (config, callbacks, scripts, .env)..."
tar czf "$BACKUP_DIR/nevnew-repo.tar.gz" \
  -C "$(dirname "$PROJECT_ROOT")" \
  --exclude=".git" \
  "$(basename "$PROJECT_ROOT")"

echo "==> [2/3] Open-WebUI chat history/database (docker volume)..."
docker run --rm \
  -v newnew_open_webui_data:/data:ro \
  -v "$BACKUP_DIR":/backup \
  alpine \
  tar czf /backup/open-webui-data.tar.gz -C /data .

echo "==> [3/3] n8n workflows/database (~/.n8n)..."
if [ -d "$HOME/.n8n" ]; then
  tar czf "$BACKUP_DIR/n8n-data.tar.gz" -C "$HOME" ".n8n"
else
  echo "    (skipped — ~/.n8n not found)"
fi

echo ""
echo "==> Done. Contents:"
ls -lh "$BACKUP_DIR"
echo ""
echo "==> NOTE: this is local-only. Copy $BACKUP_DIR somewhere off this Mac"
echo "    (external drive, cloud bucket) for real disaster-recovery coverage."
