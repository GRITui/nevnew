#!/usr/bin/env bash
#
# scripts/offload.sh — tiered engineering offload dispatcher.
#
# Guideline (owner, 2026-09-06): delegate small, well-scoped tasks to
# cheaper worker models picked by task class, to save orchestrator tokens —
# the orchestrator stays the quality gate before anything reaches main.
#
# Workers run under the CLINE-PASS account quota (cline CLI provider
# "cline-pass") — NOT cline-billing / the metered OPENROUTER_API_KEY
# (that key's credit is reserved for NevNew's chat model; it was near-dry
# as of 2026-09-06). The direct-OpenRouter path still exists as an explicit
# `--via openrouter` fallback for the day credits are added.
#
#   -c small     docs, triage, config snippets, boilerplate
#                  -> cline-pass/qwen3.7-plus     (thinking low)
#   -c code      single/multi-file codegen (connectors, workflows)
#                  -> cline-pass/deepseek-v4-pro  (thinking low)
#   -c complex   architecture, cross-cutting design, hard debugging
#                  -> cline-pass/mimo-v2.5-pro    (thinking medium)
#
# Safety: workers run `cline` with --auto-approve false, or `opencode run`
# with no --auto flag, in an ISOLATED scratch cwd
# (/tmp/nevnew-offload-scratch) — they cannot edit the repo. The
# orchestrator extracts the text output, reviews it, and writes files
# itself. Replies where the worker attempted a tool call are rejected.
#
# --via routes (rotate/fall back across these as quota runs out):
#   cline          cline-pass account quota (default)
#   opencode-go    OpenCode Go account quota (`opencode-go` auth profile)
#   opencode-zen   OpenCode Zen account quota (`opencode` auth profile)
#   openrouter     metered direct OpenRouter call (needs OPENROUTER_API_KEY)
#
# Usage:
#   ./scripts/offload.sh -c small   "Write a README that ..."
#   ./scripts/offload.sh -c code    -f task.md
#   ./scripts/offload.sh -c complex "Design the retry strategy for ..."
#   ./scripts/offload.sh --model cline-pass/glm-5.3 "..."       # manual override
#   ./scripts/offload.sh --via opencode-go -c code "..."        # OpenCode Go quota
#   ./scripts/offload.sh --via opencode-zen -c small "..."      # OpenCode Zen quota
#   ./scripts/offload.sh --via openrouter -c code "..."         # metered fallback
#   echo "task" | ./scripts/offload.sh -c code
#
# Env overrides: OFFLOAD_PROVIDER (default cline-pass), OFFLOAD_THINKING,
#                OFFLOAD_TIMEOUT_SECONDS (default 300),
#                OFFLOAD_SCRATCH_DIR (default /tmp/nevnew-offload-scratch).

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

VIA="cline"
PROVIDER="${OFFLOAD_PROVIDER:-cline-pass}"
THINKING="${OFFLOAD_THINKING:-}"
TIMEOUT="${OFFLOAD_TIMEOUT_SECONDS:-300}"
SCRATCH="${OFFLOAD_SCRATCH_DIR:-/tmp/nevnew-offload-scratch}"
MODEL=""
CLASS="code"

usage() {
  echo "Usage: $0 [-c small|code|complex] [--via cline|openrouter]" >&2
  echo "          [--model <id>] [-f task_file] [\"task text\"]" >&2
  echo "       echo \"task\" | $0 -c code" >&2
  exit 1
}

TASK=""
while [ $# -gt 0 ]; do
  case "$1" in
    -c) [ -n "${2:-}" ] || usage; CLASS="$2"; shift 2 ;;
    --via) [ -n "${2:-}" ] || usage; VIA="$2"; shift 2 ;;
    --model) [ -n "${2:-}" ] || usage; MODEL="$2"; shift 2 ;;
    -f) [ -n "${2:-}" ] || usage; TASK="$(cat "$2")"; shift 2 ;;
    -h|--help) usage ;;
    *) [ -z "$TASK" ] || usage; TASK="$1"; shift ;;
  esac
done
if [ -z "$TASK" ] && [ ! -t 0 ]; then TASK="$(cat)"; fi
[ -n "$TASK" ] || usage

# class -> "cline-model opencode-go-model opencode-zen-model openrouter-model default-thinking"
class_spec() {
  case "$1" in
    small)   echo 'cline-pass/qwen3.7-plus opencode-go/qwen3.7-plus opencode/qwen3.7-plus qwen/qwen3.7-plus low' ;;
    code)    echo 'cline-pass/deepseek-v4-pro opencode-go/deepseek-v4-pro opencode/deepseek-v4-pro deepseek/deepseek-v4-pro low' ;;
    complex) echo 'cline-pass/mimo-v2.5-pro opencode-go/mimo-v2.5-pro opencode/mimo-v2.5-pro xiaomi/mimo-v2.5-pro medium' ;;
    *)       return 1 ;;
  esac
}

SPEC="$(class_spec "$CLASS")" || { echo "ERROR: unknown class '$CLASS'." >&2; exit 1; }
if [ -z "$MODEL" ]; then
  case "$VIA" in
    cline)        MODEL="$(echo "$SPEC" | awk '{print $1}')" ;;
    opencode-go)  MODEL="$(echo "$SPEC" | awk '{print $2}')" ;;
    opencode-zen) MODEL="$(echo "$SPEC" | awk '{print $3}')" ;;
    openrouter)   MODEL="$(echo "$SPEC" | awk '{print $4}')" ;;
    *) echo "ERROR: unknown --via '$VIA' (cline|opencode-go|opencode-zen|openrouter)." >&2; exit 1 ;;
  esac
fi
[ -n "$THINKING" ] || THINKING="$(echo "$SPEC" | awk '{print $5}')"

SYSTEM_PROMPT="You are an engineering offload worker. You receive coding and \
engineering subtasks delegated from a coordinating AI assistant. Solve the \
task directly and completely: write code/docs, explain reasoning concisely, \
avoid unnecessary preamble, and follow every explicit instruction in the \
task (output format, file markers, length limits) exactly. You cannot run \
tools or read files — produce your answer as text only."

if [ "$VIA" = "cline" ]; then
  # --- cline-pass route (owner's guideline default) -----------------------
  mkdir -p "$SCRATCH"
  RAW="$SCRATCH/last_run.jsonl"
  # Leading space on the prompt: cline's arg parser treats a bare single word
  # as an ambiguous subcommand (verified 2026-09-05, see telegram-bot/bot.py).
  if ! cline " $TASK" \
      --provider "$PROVIDER" \
      -m "$MODEL" \
      --thinking "$THINKING" \
      --auto-approve false \
      --json \
      -c "$SCRATCH" \
      -s "$SYSTEM_PROMPT" \
      -t "$TIMEOUT" \
      >"$RAW" 2>"$SCRATCH/last_run.err"; then
    echo "ERROR: cline worker exited nonzero — see $SCRATCH/last_run.err" >&2
    exit 1
  fi

  python3 -c '
import json
import sys

raw_path = sys.argv[1]
text = None
had_tool_calls = False
with open(raw_path, "r", errors="replace") as fh:
    for line in fh:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "run_result":
            text = event.get("text")
        elif event.get("type") == "agent_event":
            inner = event.get("event", {})
            if inner.get("type") == "iteration_end" and inner.get("hadToolCalls"):
                had_tool_calls = True

if had_tool_calls:
    sys.stderr.write(
        "offload worker attempted a tool call - output rejected "
        "(see last_run.jsonl / last_run.err in the scratch dir)\n"
    )
    sys.exit(1)
if not text or not text.strip():
    sys.stderr.write(
        "offload worker returned no text "
        "(see last_run.jsonl / last_run.err in the scratch dir)\n"
    )
    sys.exit(1)
print(text)
' "$RAW"
elif [ "$VIA" = "opencode-go" ] || [ "$VIA" = "opencode-zen" ]; then
  # --- opencode route (OpenCode Go / OpenCode Zen account quota) ----------
  # Auth profile is selected by the model's provider prefix itself
  # (opencode-go/... vs opencode/...) — see `opencode providers list`.
  mkdir -p "$SCRATCH"
  RAW="$SCRATCH/last_run.json"
  # No --auto flag: opencode does still execute read-only tools (verified
  # 2026-09-08 — a `read`/`list` call runs against --dir without a prompt),
  # but --dir is the isolated scratch dir so it can only ever touch that,
  # never the repo. The QA-gate parser below rejects the reply outright if
  # ANY tool call was attempted, sandboxed or not — same fail-closed
  # contract as cline's --auto-approve false.
  if ! opencode run \
      --dir "$SCRATCH" \
      -m "$MODEL" \
      --format json \
      >"$RAW" 2>"$SCRATCH/last_run.err" <<EOF
$SYSTEM_PROMPT

---

$TASK
EOF
  then
    echo "ERROR: opencode worker exited nonzero — see $SCRATCH/last_run.err" >&2
    exit 1
  fi

  python3 -c '
import json
import sys

raw_path = sys.argv[1]
text_parts = []
had_tool_calls = False
with open(raw_path, "r", errors="replace") as fh:
    for line in fh:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = event.get("type", "")
        part = event.get("part") or event.get("properties", {}).get("part", {})
        ptype = part.get("type") if isinstance(part, dict) else None
        if ptype == "text":
            text_parts.append(part.get("text", ""))
        elif ptype == "tool" or "tool" in etype.lower():
            had_tool_calls = True

text = "".join(text_parts).strip()
if had_tool_calls:
    sys.stderr.write(
        "offload worker attempted a tool call - output rejected "
        "(see last_run.json / last_run.err in the scratch dir)\n"
    )
    sys.exit(1)
if not text:
    sys.stderr.write(
        "offload worker returned no text "
        "(see last_run.json / last_run.err in the scratch dir)\n"
    )
    sys.exit(1)
print(text)
' "$RAW"
else
  # --- openrouter route (metered fallback, needs OPENROUTER_API_KEY) ------
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
  python3 - "$MODEL" "$SYSTEM_PROMPT" "$TASK" "${OFFLOAD_MAX_TOKENS:-16384}" "$OPENROUTER_API_KEY" <<'PYEOF'
import json
import sys
import urllib.request

model, system_prompt, task, max_tokens, api_key = sys.argv[1:6]

payload = {
    "model": model,
    "messages": [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task},
    ],
    "temperature": 0.2,
    "max_tokens": int(max_tokens),
    "include_reasoning": False,
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
    with urllib.request.urlopen(req, timeout=180) as resp:
        body = json.loads(resp.read().decode("utf-8"))
except urllib.error.HTTPError as e:
    sys.stderr.write(f"OpenRouter error {e.code}: {e.read().decode('utf-8')}\n")
    sys.exit(1)

print(body["choices"][0]["message"]["content"])
PYEOF
fi
