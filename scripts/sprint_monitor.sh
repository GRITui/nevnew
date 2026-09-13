#!/usr/bin/env bash
#
# scripts/sprint_monitor.sh — Sprint 4 watch items (hourly via cron).
#
# Watches the two things flagged in sprint planning (2026-09-13):
#   1. error_watcher re-filing the LiteLLM<->Postgres DB signature after #117
#      was fixed. Recurrence means the DB fault is back (or the watcher's
#      dedupe window needs widening) — either way the orchestrator must look.
#   2. 9arm gateway /models allowlist. offload.sh pins qwen3.8-27b-fp8 because
#      the allowlist is exactly that one model; if it widens, the pin can be
#      relaxed (and tiering returns to the primary route). If the gateway is
#      unreachable, the primary offload route is down.
#
# State: /tmp/nevnew-sprint-monitor.json (last seen counts + model list).
# Log:   /Users/grit/ops/nevnew-sprint-monitor.log (appended).
# Output: one STATUS line normally; ALERT lines on change. Exit 0 always
# (cron must not spam on transient curl/gh hiccups — failures print WARN).

set -uo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_FILE="/tmp/nevnew-sprint-monitor.json"
LOG_FILE="/Users/grit/ops/nevnew-sprint-monitor.log"
NOW="$(date -u +%FT%TZ)"

log() { echo "$NOW $*" >>"$LOG_FILE"; }

if [ -f "$PROJECT_ROOT/.env" ]; then
  # shellcheck disable=SC1091
  set -a
  # shellcheck disable=SC1090
  source "$PROJECT_ROOT/.env"
  set +a
fi

# --- check 1: open DB-signature auto issues --------------------------------
DB_COUNT="unknown"
if command -v gh >/dev/null 2>&1; then
  DB_COUNT="$(gh issue list --state open --json title --limit 200 2>/dev/null \
    | python3 -c "
import json,sys
try:
    issues = json.load(sys.stdin)
except Exception:
    print('unknown'); raise SystemExit
sigs = ('Prisma DB reconnect', 'P1001', 'All connection attempts failed',
        'reset_budget_job', 'reload_mcp_servers_from_db', '_init_search_tools_in_db')
print(sum(1 for i in issues if any(s in i.get('title','') for s in sigs)))
" 2>/dev/null)" || DB_COUNT="unknown"
fi

# --- check 2: gateway allowlist --------------------------------------------
GATEWAY_BASE="${OPENAI_COMPAT_BASE_URL:-${OPENAI_BASE_URL:-https://gateway.9arm.co/v1}}"
GATEWAY_BASE="${GATEWAY_BASE%/}"
GATEWAY_KEY="${OPENAI_COMPAT_API_KEY:-${OPENAI_API_KEY:-${NINEARM_API_KEY:-}}}"
MODELS="unknown"
if [ -n "$GATEWAY_KEY" ]; then
  MODELS="$(curl -s --max-time 20 -H "Authorization: Bearer $GATEWAY_KEY" \
    "$GATEWAY_BASE/models" 2>/dev/null \
    | python3 -c "
import json,sys
try:
    body = json.load(sys.stdin)
    print(','.join(sorted(m.get('id','?') for m in body.get('data',[]))) or 'empty')
except Exception:
    print('unreachable')
" 2>/dev/null)" || MODELS="unknown"
fi

# --- compare with last state ------------------------------------------------
PREV_DB="none"; PREV_MODELS="none"
if [ -f "$STATE_FILE" ]; then
  PREV_DB="$(python3 -c "import json; print(json.load(open('$STATE_FILE')).get('db_issues','none'))" 2>/dev/null || echo none)"
  PREV_MODELS="$(python3 -c "import json; print(json.load(open('$STATE_FILE')).get('models','none'))" 2>/dev/null || echo none)"
fi
python3 -c "import json; json.dump({'db_issues':'$DB_COUNT','models':'$MODELS','checked_at':'$NOW'}, open('$STATE_FILE','w'))"

STATUS="STATUS db_issues=$DB_COUNT gateway_models=[$MODELS]"
log "$STATUS"

# Alert on recurrence (count grew since last run) — the #117-is-back signal.
if [ "$PREV_DB" != "none" ] && [ "$DB_COUNT" != "unknown" ] && [ "$PREV_DB" != "unknown" ] \
   && [ "$DB_COUNT" -gt "$PREV_DB" ] 2>/dev/null; then
  log "ALERT db-signature issues grew $PREV_DB -> $DB_COUNT since last run — possible #117 recurrence, or error_watcher dedupe window needs widening."
fi
# Alert on allowlist change in either direction.
if [ "$PREV_MODELS" != "none" ] && [ "$MODELS" != "unknown" ] && [ "$MODELS" != "$PREV_MODELS" ]; then
  log "ALERT gateway allowlist changed [$PREV_MODELS] -> [$MODELS] — revisit the qwen3.8 pin in scripts/offload.sh."
fi

echo "$STATUS"
