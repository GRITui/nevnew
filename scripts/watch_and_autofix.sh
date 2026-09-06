#!/usr/bin/env bash
#
# watch_and_autofix.sh — runs error_watcher.py, then dispatches
# autofix_with_cline.sh for every issue it just filed. Single entry point for
# the n8n "NevNew Error Watch & Autofix" workflow's Execute Command node
# (mirrors the single-command pattern already used by
# slowlife-game/scripts/ci/update_project_status.sh's n8n workflow).

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

WATCHER_OUTPUT="$(python3 scripts/error_watcher.py)"
echo "$WATCHER_OUTPUT"

LAST_LINE="$(echo "$WATCHER_OUTPUT" | tail -n 1)"
ISSUE_NUMBERS="$(echo "$LAST_LINE" | python3 -c 'import json,sys; print(" ".join(str(n) for n in json.load(sys.stdin)["filed_issue_numbers"]))' 2>/dev/null || echo "")"

if [ -z "$ISSUE_NUMBERS" ]; then
  echo "== no new issues, nothing to autofix =="
  exit 0
fi

for ISSUE in $ISSUE_NUMBERS; do
  echo "== dispatching autofix for issue #${ISSUE} =="
  bash scripts/autofix_with_cline.sh "$ISSUE" || echo "== autofix failed for issue #${ISSUE}, left for a human =="
done
