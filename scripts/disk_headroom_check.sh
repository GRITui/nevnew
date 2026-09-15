#!/usr/bin/env bash
#
# disk_headroom_check.sh — disk-space watchdog (issue #63).
#
# Checks free space on the filesystem backing $CHECK_PATH (defaults to the
# repo root's volume — where docker volumes, backups, and logs actually
# accumulate). Two thresholds:
#   - < 10% free: WARN — send a Telegram alert (same pattern as
#     scripts/offload_watch.py's send_telegram), matching this repo's
#     existing alerting style. Safe to fire repeatedly; the caller
#     (cron/watchdog) controls frequency.
#   - < 5% free: ESCALATE — additionally file a GitHub issue via `gh issue
#     create` (same pattern as scripts/error_watcher.py's file_issue), once
#     per day (state file dedupes so a stuck disk doesn't spam new issues
#     every run).
#
# This script does NOT run itself on a schedule — wire it into cron or an
# external watchdog (e.g. ~/ops/nevnew_watchdog.sh) to run every 10-30 min.
# It was added as a standalone script (rather than extended into an
# existing watchdog) because no disk-check logic existed in this repo's
# scripts/ directory at the time of writing, and ~/ops/nevnew_watchdog.sh
# lives outside this repo's write scope.
#
# Secrets (TELEGRAM_BOT_TOKEN, TELEGRAM_OWNER_ID) come from the repo .env,
# same as the rest of scripts/ — never hardcoded here.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_PATH="${CHECK_PATH:-$PROJECT_ROOT}"
REPO="GRITui/nevnew"
STATE_FILE="$PROJECT_ROOT/scripts/.disk_headroom_state.json"

WARN_THRESHOLD=10   # percent free, below which we warn via Telegram
ESCALATE_THRESHOLD=5  # percent free, below which we also file a GH issue

# --- Load .env for Telegram creds (never committed; matches offload_watch.py) ---
if [ -f "$PROJECT_ROOT/.env" ]; then
  # shellcheck disable=SC1091
  set -a
  source "$PROJECT_ROOT/.env"
  set +a
fi

# --- Read free space percentage on CHECK_PATH's filesystem ---
# `df -P` (POSIX output format) avoids line-wrapping on long device names;
# works on both macOS and Linux.
DF_LINE="$(df -P "$CHECK_PATH" | tail -1)"
USE_PCT="$(echo "$DF_LINE" | awk '{print $5}' | tr -d '%')"
FREE_PCT=$((100 - USE_PCT))
MOUNT="$(echo "$DF_LINE" | awk '{print $6}')"

echo "disk_headroom_check: $CHECK_PATH -> mount=$MOUNT free=${FREE_PCT}% (used=${USE_PCT}%)"

if [ "$FREE_PCT" -ge "$WARN_THRESHOLD" ]; then
  echo "disk_headroom_check: OK, above ${WARN_THRESHOLD}% free threshold"
  exit 0
fi

# --- WARN: below 10% free ---
send_telegram() {
  local text="$1"
  if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_OWNER_ID:-}" ]; then
    echo "disk_headroom_check: TELEGRAM_BOT_TOKEN/TELEGRAM_OWNER_ID not set; skipping Telegram alert" >&2
    return 0
  fi
  curl -sS -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -H "Content-Type: application/json" \
    -d "$(printf '{"chat_id":"%s","text":"%s"}' "$TELEGRAM_OWNER_ID" "$text")" \
    >/dev/null || echo "disk_headroom_check: telegram send failed" >&2
}

WARN_MSG="⚠ NevNew disk headroom warning: ${FREE_PCT}% free on ${MOUNT} (below ${WARN_THRESHOLD}%). Consider pruning docker volumes/images or rotating backups (see scripts/backup.sh)."
send_telegram "$WARN_MSG"

if [ "$FREE_PCT" -ge "$ESCALATE_THRESHOLD" ]; then
  echo "disk_headroom_check: warned via Telegram (below ${WARN_THRESHOLD}%, above escalate threshold ${ESCALATE_THRESHOLD}%)"
  exit 0
fi

# --- ESCALATE: below 5% free — file a GitHub issue, at most once/day ---
TODAY="$(date +%Y-%m-%d)"
LAST_FILED=""
if [ -f "$STATE_FILE" ]; then
  LAST_FILED="$(grep -o '"last_filed_date": *"[^"]*"' "$STATE_FILE" 2>/dev/null | sed 's/.*"\([0-9-]*\)"$/\1/' || true)"
fi

if [ "$LAST_FILED" = "$TODAY" ]; then
  echo "disk_headroom_check: already escalated today ($TODAY); not re-filing"
  exit 0
fi

if command -v gh >/dev/null 2>&1; then
  gh issue create \
    --repo "$REPO" \
    --title "[auto] Disk headroom critical: ${FREE_PCT}% free on ${MOUNT}" \
    --body "Auto-filed by \`scripts/disk_headroom_check.sh\` — free space on \`${MOUNT}\` dropped below ${ESCALATE_THRESHOLD}% (currently ${FREE_PCT}% free, used=${USE_PCT}%).

Immediate actions to consider:
- \`docker system prune\` (images/build cache no longer referenced)
- Rotate/offload old backups under ~/Backups/nevnew (see BACKLOG.md note on unrotated backups)
- Check docker volume sizes (\`docker system df -v\`)

This issue is filed at most once per day while the disk stays below ${ESCALATE_THRESHOLD}% free." \
    --label "bug" \
    >/tmp/disk_headroom_gh_issue.txt 2>&1 || echo "disk_headroom_check: gh issue create failed, see /tmp/disk_headroom_gh_issue.txt" >&2
  printf '{"last_filed_date": "%s"}\n' "$TODAY" > "$STATE_FILE"
  echo "disk_headroom_check: escalated (filed GitHub issue, see /tmp/disk_headroom_gh_issue.txt)"
else
  echo "disk_headroom_check: gh CLI not found; cannot escalate to GitHub issue" >&2
fi
