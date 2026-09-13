#!/usr/bin/env bash
#
# scripts/offload.sh — tiered engineering offload dispatcher.
#
# Guideline (owner, 2026-09-06): delegate small, well-scoped tasks to
# cheaper worker models picked by task class, to save orchestrator tokens —
# the orchestrator stays the quality gate before anything reaches main.
#
# Workers run against the generic OpenAI-compatible gateway by default
# (9arm gateway, qwen3.8-27b-fp8) — direct HTTPS, no CLI needed, NOT
# cline-billing / the metered OPENROUTER_API_KEY (that key's credit is
# reserved for NevNew's chat model; it was near-dry as of 2026-09-06).
# The cline-pass / opencode-go / opencode-zen CLI routes still exist as
# explicit `--via cline|opencode-go|opencode-zen` fallbacks for the day the
# gateway quota runs out.
#
#   -c small     docs, triage, config snippets, boilerplate
#   -c code      single/multi-file codegen (connectors, workflows)
#   -c complex   architecture, cross-cutting design, hard debugging
#                (--via openai uses qwen3.8-27b-fp8 for all classes: it is
#                 the gateway's only allowed model. The cline-pass
#                 qwen3.7-plus / deepseek-v4-pro / mimo-v2.5-pro tiering
#                 remains available via --via cline.)
#
# Safety: the openai/groq/openrouter HTTP routes never offer tools to the
# model. The `cline` / `opencode-*` CLI routes run with --auto-approve false
# (or no --auto flag) in an ISOLATED scratch cwd
# (/tmp/nevnew-offload-scratch) — they cannot edit the repo. The
# orchestrator extracts the text output, reviews it, and writes files
# itself. Replies where the worker attempted a tool call are rejected.
#
# --via routes (rotate/fall back across these as quota runs out):
#   openai         generic OpenAI-compatible endpoint (DEFAULT; needs
#                  OPENAI_COMPAT_API_KEY or OPENAI_API_KEY or NINEARM_API_KEY
#                  in .env; base URL from OPENAI_COMPAT_BASE_URL or
#                  OPENAI_BASE_URL, default https://gateway.9arm.co/v1).
#                  Direct HTTP, no CLI needed.
#   cline          cline-pass account quota (fallback)
#   opencode-go    OpenCode Go account quota (`opencode-go` auth profile)
#   opencode-zen   OpenCode Zen account quota (`opencode` auth profile)
#   groq           GroqCloud free tier (needs GROQ_API_KEY in .env; console.groq.com)
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
#   ./scripts/offload.sh --via openai -c small "..."            # OpenAI-compatible gateway
#   echo "task" | ./scripts/offload.sh -c code
#
# Env overrides: OFFLOAD_VIA (default openai), OFFLOAD_PROVIDER (cline route
#                default cline-pass), OFFLOAD_THINKING,
#                OFFLOAD_TIMEOUT_SECONDS (default 300),
#                OFFLOAD_SCRATCH_DIR (default /tmp/nevnew-offload-scratch),
#                OFFLOAD_MAX_TOKENS (per-route default),
#                OPENAI_COMPAT_BASE_URL / OPENAI_BASE_URL,
#                OPENAI_COMPAT_API_KEY / OPENAI_API_KEY / NINEARM_API_KEY.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

VIA="${OFFLOAD_VIA:-openai}"
PROVIDER="${OFFLOAD_PROVIDER:-cline-pass}"
THINKING="${OFFLOAD_THINKING:-}"
TIMEOUT="${OFFLOAD_TIMEOUT_SECONDS:-300}"
SCRATCH="${OFFLOAD_SCRATCH_DIR:-/tmp/nevnew-offload-scratch}"
MODEL=""
CLASS="code"

usage() {
  echo "Usage: $0 [-c small|code|complex] [--via cline|opencode-go|opencode-zen|groq|openrouter|openai]" >&2
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

# class -> "cline-model opencode-go-model opencode-zen-model groq-model openrouter-model openai-model default-thinking"
# groq models verified live against this key 2026-09-10 via /models:
# groq/compound-mini, allam-2-7b, qwen/qwen3.8-27b, openai/gpt-oss-20b,
# groq/compound, openai/gpt-oss-120b, qwen/qwen3.6-27b
# (llama-3.1/3.3 have been retired from GroqCloud.)
# openai model is pinned to qwen3.8-27b-fp8 for every class (verified
# 2026-09-13: the gateway 403s anything else — the key's allowlist is
# ['qwen3.8-27b-fp8']). --model still overrides per call, but only to probe
# a widened allowlist; anything outside it fails closed.
# small/openrouter is cohere/north-mini-code:free (Sprint 4 light-triage
# fallback; shares OpenRouter's 50 req/day account-wide cap — expect 429s
# while the cap is exhausted, which fail closed).
class_spec() {
  case "$1" in
    small)   echo 'cline-pass/qwen3.7-plus opencode-go/qwen3.7-plus opencode/qwen3.7-plus qwen/qwen3.6-27b cohere/north-mini-code:free qwen3.8-27b-fp8 low' ;;
    code)    echo 'cline-pass/deepseek-v4-pro opencode-go/deepseek-v4-pro opencode/deepseek-v4-pro openai/gpt-oss-120b deepseek/deepseek-v4-pro qwen3.8-27b-fp8 low' ;;
    complex) echo 'cline-pass/mimo-v2.5-pro opencode-go/mimo-v2.5-pro opencode/mimo-v2.5-pro openai/gpt-oss-120b xiaomi/mimo-v2.5-pro qwen3.8-27b-fp8 medium' ;;
    *)       return 1 ;;
  esac
}

SPEC="$(class_spec "$CLASS")" || { echo "ERROR: unknown class '$CLASS'." >&2; exit 1; }
if [ -z "$MODEL" ]; then
  case "$VIA" in
    cline)        MODEL="$(echo "$SPEC" | awk '{print $1}')" ;;
    opencode-go)  MODEL="$(echo "$SPEC" | awk '{print $2}')" ;;
    opencode-zen) MODEL="$(echo "$SPEC" | awk '{print $3}')" ;;
    groq)         MODEL="$(echo "$SPEC" | awk '{print $4}')" ;;
    openrouter)   MODEL="$(echo "$SPEC" | awk '{print $5}')" ;;
    openai)       MODEL="$(echo "$SPEC" | awk '{print $6}')" ;;
    *) echo "ERROR: unknown --via '$VIA' (cline|opencode-go|opencode-zen|groq|openrouter|openai)." >&2; exit 1 ;;
  esac
fi
[ -n "$THINKING" ] || THINKING="$(echo "$SPEC" | awk '{print $7}')"

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
elif [ "$VIA" = "groq" ]; then
  # --- groq route (GroqCloud free tier, needs GROQ_API_KEY in .env) -------
  # OpenAI-compatible chat completions against api.groq.com. Fits the same
  # fail-closed contract: no tools are ever offered to the model, exit 1 on
  # any HTTP error or empty reply. Free tier is org-level ~30 RPM; a 429
  # here is quota exhaustion — fall through, don't retry.
  if [ -f "$PROJECT_ROOT/.env" ]; then
    # shellcheck disable=SC1091
    set -a
    source "$PROJECT_ROOT/.env"
    set +a
  fi
  if [ -z "${GROQ_API_KEY:-}" ]; then
    echo "ERROR: GROQ_API_KEY is not set in .env (create one at console.groq.com/keys)." >&2
    exit 1
  fi
  python3 - "$MODEL" "$SYSTEM_PROMPT" "$TASK" "${OFFLOAD_MAX_TOKENS:-8192}" "$GROQ_API_KEY" <<'PYEOF'
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
}
# reasoning_effort is only valid on Groq's gpt-oss models; other models
# (e.g. llama-*) 400 on unknown kwargs.
if "gpt-oss" in model:
    payload["reasoning_effort"] = "low"

req = urllib.request.Request(
    "https://api.groq.com/openai/v1/chat/completions",
    data=json.dumps(payload).encode("utf-8"),
    headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        # Groq's Cloudflare WAF 403s (error 1010) the default Python-urllib
        # UA — send a normal client UA.
        "User-Agent": "nevnew-offload/1.0",
    },
    method="POST",
)

try:
    with urllib.request.urlopen(req, timeout=180) as resp:
        body = json.loads(resp.read().decode("utf-8"))
except urllib.error.HTTPError as e:
    sys.stderr.write(f"Groq error {e.code}: {e.read().decode('utf-8')}\n")
    sys.exit(1)

content = body["choices"][0]["message"]["content"]
if not content or not content.strip():
    sys.stderr.write("groq worker returned no text\n")
    sys.exit(1)
print(content)
PYEOF
elif [ "$VIA" = "openrouter" ]; then
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
elif [ "$VIA" = "openai" ]; then
  # --- openai route (generic OpenAI-compatible endpoint, no CLI needed) ----
  # Base URL precedence: OPENAI_COMPAT_BASE_URL > OPENAI_BASE_URL > default
  # 9arm gateway. Key precedence: OPENAI_COMPAT_API_KEY > OPENAI_API_KEY >
  # NINEARM_API_KEY. Both may live in $PROJECT_ROOT/.env or the environment.
  # Same fail-closed contract as groq/openrouter: no tools offered, exit 1
  # on any HTTP error or empty reply.
  if [ -f "$PROJECT_ROOT/.env" ]; then
    # shellcheck disable=SC1091
    set -a
    source "$PROJECT_ROOT/.env"
    set +a
  fi
  OPENAI_BASE="${OPENAI_COMPAT_BASE_URL:-${OPENAI_BASE_URL:-https://gateway.9arm.co/v1}}"
  OPENAI_KEY="${OPENAI_COMPAT_API_KEY:-${OPENAI_API_KEY:-${NINEARM_API_KEY:-}}}"
  if [ -z "$OPENAI_KEY" ] || [ "$OPENAI_KEY" = "your-9arm-api-key-here" ]; then
    echo "ERROR: no OpenAI-compatible API key found (set OPENAI_COMPAT_API_KEY, OPENAI_API_KEY, or NINEARM_API_KEY in .env)." >&2
    exit 1
  fi
  # Normalise: strip trailing slashes so both ".../v1" and ".../v1/" work.
  OPENAI_BASE="${OPENAI_BASE%/}"
  python3 - "$MODEL" "$SYSTEM_PROMPT" "$TASK" "${OFFLOAD_MAX_TOKENS:-8192}" "$OPENAI_KEY" "$OPENAI_BASE" <<'PYEOF'
import json
import sys
import urllib.request

model, system_prompt, task, max_tokens, api_key, base_url = sys.argv[1:7]

payload = {
    "model": model,
    "messages": [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task},
    ],
    "temperature": 0.2,
    "max_tokens": int(max_tokens),
}

req = urllib.request.Request(
    f"{base_url}/chat/completions",
    data=json.dumps(payload).encode("utf-8"),
    headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "nevnew-offload/1.0",
    },
    method="POST",
)

try:
    with urllib.request.urlopen(req, timeout=180) as resp:
        body = json.loads(resp.read().decode("utf-8"))
except urllib.error.HTTPError as e:
    sys.stderr.write(f"OpenAI-compatible error {e.code}: {e.read().decode('utf-8')}\n")
    sys.exit(1)

try:
    content = body["choices"][0]["message"]["content"]
except (KeyError, IndexError, TypeError):
    sys.stderr.write(f"OpenAI-compatible error: unexpected response shape: {json.dumps(body)[:500]}\n")
    sys.exit(1)
if not content or not content.strip():
    sys.stderr.write("openai worker returned no text\n")
    sys.exit(1)
print(content)
PYEOF
else
  echo "ERROR: unknown --via '$VIA' (cline|opencode-go|opencode-zen|groq|openrouter|openai)." >&2
  exit 1
fi
