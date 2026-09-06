#!/usr/bin/env bash
#
# offload_to_minimax.sh — dispatch an engineering/coding subtask to
# cline/minimax-m3 (minimax/minimax-m3:free) via OpenRouter, to offload
# work from Claude. Independent of the nevnew Open-WebUI/LiteLLM chat stack,
# which always answers users via NevNew's own configured model regardless of
# this script.
#
# Usage:
#   ./scripts/offload_to_minimax.sh "Write a Python function that ..."
#   echo "Write a Python function that ..." | ./scripts/offload_to_minimax.sh
#   ./scripts/offload_to_minimax.sh -f task.md
#
# Requires OPENROUTER_API_KEY in .env (see .env.example).

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ -f "$PROJECT_ROOT/.env" ]; then
  # shellcheck disable=SC1091
  set -a
  source "$PROJECT_ROOT/.env"
  set +a
fi

if [ -z "${OPENROUTER_API_KEY:-}" ] || [ "$OPENROUTER_API_KEY" = "your-openrouter-api-key-here" ]; then
  echo "ERROR: OPENROUTER_API_KEY is not set in .env." >&2
  exit 1
fi

# Direct OpenRouter API call (not routed through LiteLLM), so this uses
# OpenRouter's own model id format — no "openrouter/" prefix (that's a
# LiteLLM routing convention, not an OpenRouter API one). Confirmed real
# and free (pricing prompt=0/completion=0) against OpenRouter's models API
# on 2026-09-05.
MODEL="minimax/minimax-m3:free"
SYSTEM_PROMPT="You are an engineering offload worker. You receive coding and \
engineering subtasks delegated from a coordinating AI assistant (Claude). \
Solve the task directly and completely: write code, explain reasoning \
concisely, and avoid unnecessary preamble."

usage() {
  echo "Usage: $0 [-f task_file] [\"task text\"]" >&2
  echo "       echo \"task text\" | $0" >&2
  exit 1
}

TASK=""
if [ "${1:-}" = "-f" ]; then
  [ -n "${2:-}" ] || usage
  TASK="$(cat "$2")"
elif [ -n "${1:-}" ]; then
  TASK="$1"
elif [ ! -t 0 ]; then
  TASK="$(cat)"
else
  usage
fi

if [ -z "$TASK" ]; then
  echo "ERROR: empty task." >&2
  exit 1
fi

python3 - "$MODEL" "$SYSTEM_PROMPT" "$TASK" "$OPENROUTER_API_KEY" <<'PYEOF'
import json
import sys
import urllib.request

model, system_prompt, task, api_key = sys.argv[1:5]

payload = {
    "model": model,
    "messages": [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task},
    ],
}

req = urllib.request.Request(
    "https://openrouter.ai/api/v1/chat/completions",
    data=json.dumps(payload).encode("utf-8"),
    headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/GRITui/nevnew",
        "X-Title": "nevnew-offload-worker",
    },
    method="POST",
)

try:
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = json.loads(resp.read().decode("utf-8"))
except urllib.error.HTTPError as e:
    sys.stderr.write(f"OpenRouter error {e.code}: {e.read().decode('utf-8')}\n")
    sys.exit(1)

print(body["choices"][0]["message"]["content"])
PYEOF
