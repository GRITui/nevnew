#!/usr/bin/env bash
#
# scripts/offload_dispatch.sh — issue #67: async job dispatch for offload.sh.
#
# "Research X and tell me when done" / "fix that bug and ping me": this
# script is the missing trigger between a chat request and the existing
# offload machinery (scripts/offload.sh + scripts/offload_watch.py +
# n8n/workflows/offload-watcher.json). It launches offload.sh fully
# detached — the caller (an n8n MCP tool node backing ai-core's
# run_background_job tool) needs its response back in well under
# offload.sh's own worker timeout (default 300s) — and registers the job
# in the same registry file offload_watch.py already polls every minute,
# so the existing watcher/Telegram-notify path picks it up with zero
# further wiring.
#
# Usage:
#   scripts/offload_dispatch.sh [-c small|code|complex] [--via ROUTE] [--model ID] "<task>"
#   echo "task" | scripts/offload_dispatch.sh -c code
#
# Prints exactly one line on stdout: the job id (== the registry "name"
# offload_watch.py and scripts/offload_status.py key off). Nothing else is
# written to stdout, so callers can use it directly.
#
# Env overrides: OFFLOAD_REGISTRY (default /tmp/nevnew-offload-jobs.json,
# same default offload_watch.py uses).

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REGISTRY="${OFFLOAD_REGISTRY:-/tmp/nevnew-offload-jobs.json}"

usage() {
  echo "Usage: $0 [-c small|code|complex] [--via ROUTE] [--model ID] \"<task>\"" >&2
  echo "       echo \"task\" | $0 -c code" >&2
  exit 1
}

# Minimal re-parse of offload.sh's flags: we only need to (a) separate
# forwardable flags from the task text and (b) capture the task text
# ourselves, since the task has to be written to a file the backgrounded
# process can read after this script (and any piped stdin) has exited.
FORWARD=()
TASK=""
while [ $# -gt 0 ]; do
  case "$1" in
    -c|--via|--model) [ -n "${2:-}" ] || usage; FORWARD+=("$1" "$2"); shift 2 ;;
    -f) [ -n "${2:-}" ] || usage; TASK="$(cat "$2")"; shift 2 ;;
    -h|--help) usage ;;
    *) [ -z "$TASK" ] || usage; TASK="$1"; shift ;;
  esac
done
if [ -z "$TASK" ] && [ ! -t 0 ]; then TASK="$(cat)"; fi
[ -n "$TASK" ] || usage

JOB_ID="job-$(date +%Y%m%d-%H%M%S)-$$"
OUT="/tmp/nevnew-offload-out-${JOB_ID}.md"
ERR="/tmp/nevnew-offload-err-${JOB_ID}.log"
TASK_FILE="/tmp/nevnew-offload-task-${JOB_ID}.txt"
printf '%s' "$TASK" >"$TASK_FILE"

# Fully detach so the worker outlives this script (and whatever process —
# e.g. n8n's execFile — spawned it). The background job cleans up its own
# task-file scratch copy when done; out/err are left for offload_watch.py
# and scripts/offload_status.py to read.
nohup bash -c '
  trap "rm -f \"$1\"" EXIT
  "$0" "${@:2}" -f "$1"
' "$PROJECT_ROOT/scripts/offload.sh" "$TASK_FILE" "${FORWARD[@]}" >"$OUT" 2>"$ERR" &
disown 2>/dev/null || true

# Register the job so offload_watch.py (run every minute by the "NevNew
# Offload Watcher" n8n workflow) picks it up on its next poll.
python3 - "$REGISTRY" "$JOB_ID" "$OUT" "$ERR" "$TASK" <<'PYEOF'
import json
import sys
from pathlib import Path

registry_path, job_id, out, err, task_preview = sys.argv[1:6]
path = Path(registry_path)
try:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        data = []
except (OSError, json.JSONDecodeError):
    data = []
data.append(
    {
        "name": job_id,
        "out": out,
        "err": err,
        "task": task_preview[:200],
    }
)
path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
PYEOF

echo "$JOB_ID"
