#!/usr/bin/env bash
#
# backup.sh — local backup of everything a dead Mac Mini or a bad
# `docker compose down -v` would otherwise lose:
#   - the nevnew repo itself (config, callbacks, scripts, .env)
#   - the newnew_open_webui_data volume (Open-WebUI chat history/db)
#   - postgres (pg_dumpall — LiteLLM spend/budget tracking data)
#   - the qdrant_storage volume (mem0 long-term memory vectors)
#   - the nevnew_memory_data volume (mem0 history db + embedder cache)
#   - the n8n_data volume (n8n workflows/database — n8n runs in Docker,
#     not on the host, so this is NOT ~/.n8n)
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

echo "==> [1/6] nevnew repo (config, callbacks, scripts, .env)..."
tar czf "$BACKUP_DIR/nevnew-repo.tar.gz" \
  -C "$(dirname "$PROJECT_ROOT")" \
  --exclude=".git" \
  "$(basename "$PROJECT_ROOT")"

echo "==> [2/6] Open-WebUI chat history/database (docker volume)..."
docker run --rm \
  -v newnew_open_webui_data:/data:ro \
  -v "$BACKUP_DIR":/backup \
  alpine \
  tar czf /backup/open-webui-data.tar.gz -C /data .

echo "==> [3/6] Postgres (LiteLLM spend/budget tracking — pg_dumpall)..."
if [ "$(docker inspect -f '{{.State.Running}}' newnew-postgres 2>/dev/null || true)" = "true" ]; then
  # Read credentials from the running container's own environment rather
  # than hardcoding them here — docker-compose.yml sets POSTGRES_USER /
  # POSTGRES_PASSWORD / POSTGRES_DB for the postgres service.
  PG_USER="$(docker exec newnew-postgres printenv POSTGRES_USER)"
  PG_PASSWORD="$(docker exec newnew-postgres printenv POSTGRES_PASSWORD)"
  docker exec -e PGPASSWORD="$PG_PASSWORD" newnew-postgres \
    pg_dumpall -U "$PG_USER" > "$BACKUP_DIR/postgres-dump.sql"
else
  echo "    (skipped — newnew-postgres container not running)"
fi

echo "==> [4/6] Qdrant storage (mem0 long-term memory vectors, docker volume)..."
# Qdrant now requires auth (#73, QDRANT_API_KEY), which the snapshot HTTP
# API (POST /snapshots) would need to send on every call, plus a follow-up
# step to copy the resulting snapshot file back out of the container. A
# direct tar of the qdrant_storage volume is simpler and just as reliable
# for a stopped-in-time local backup, and matches the pattern already used
# for the Open-WebUI volume above, so that's what this step does.
docker run --rm \
  -v qdrant_storage:/data:ro \
  -v "$BACKUP_DIR":/backup \
  alpine \
  tar czf /backup/qdrant-storage.tar.gz -C /data .

echo "==> [5/6] Memory service data (mem0 history db + embedder cache, docker volume)..."
docker run --rm \
  -v nevnew_memory_data:/data:ro \
  -v "$BACKUP_DIR":/backup \
  alpine \
  tar czf /backup/memory-data.tar.gz -C /data .

echo "==> [6/6] n8n workflows/database (docker volume)..."
# n8n runs in Docker with a named volume (n8n_data mounted at
# /home/node/.n8n in the container) — it does NOT write to ~/.n8n on the
# host, so backing up the host path was stale and silently backed up
# nothing useful.
docker run --rm \
  -v n8n_data:/data:ro \
  -v "$BACKUP_DIR":/backup \
  alpine \
  tar czf /backup/n8n-data.tar.gz -C /data .

echo ""
echo "==> Done. Contents:"
ls -lh "$BACKUP_DIR"
echo ""
echo "==> NOTE: this is local-only. Copy $BACKUP_DIR somewhere off this Mac"
echo "    (external drive, cloud bucket) for real disaster-recovery coverage."
