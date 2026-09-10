#!/usr/bin/env bash
#
# gha_triage_issues.sh — GitHub Actions backend for automated issue triage,
# run by .github/workflows/issue-triage.yml with the worker model
# `opencode-go/glm-5.1` (OpenCode Go quota; default override via env — deepseek-v4-pro region-403s from US/GHA runners unless opted in at https://opencode.ai/workspace).
#
# Scope (fail-closed, advisory): label + priority + triage comment on open
# issues. Triage never closes, edits bodies, or merges — a human acts on
# duplicate/invalid calls. Untrusted issue data never passes shell expansion
# — prompts are assembled in Python. Repeat comments suppressed by the
# "Auto-triage" marker in existing bot comments.
#
# Env: GH_TOKEN, REPO.

set -euo pipefail

REPO="${REPO:?REPO env var required}"
MODEL="${AI_TRIAGE_MODEL:-opencode-go/glm-5.1}"
SCRATCH="$(mktemp -d)"

SYSTEM_PROMPT="You are an automated issue triager for nevnew (a personal AI assistant stack: dockerized LiteLLM/n8n/Open-WebUI/Telegram-bot). You classify one GitHub issue at a time. You answer in the exact output format requested and nothing else. You cannot run tools — base everything on the issue data given."

EXISTING_LABELS=$(gh label list --repo "$REPO" --limit 50 --json name -q 'map(.name) | join(",")')
echo "existing labels: $EXISTING_LABELS"

read_one() {
  gh issue view "$1" --repo "$REPO" \
    --json number,title,body,labels,comments \
    -q '{number: .number, title: .title, body: .body, labels: [.labels[].name], comments: [.comments[].body]}' \
    > "$SCRATCH/$1.issue.json"
}

assemble_prompt() {
  EXISTING_LABELS="$EXISTING_LABELS" python3 - "$1" "$SCRATCH" "$REPO" <<'PYEOF' > "$SCRATCH/$1.prompt.txt"
import sys, os
n, scratch, repo = sys.argv[1], sys.argv[2], sys.argv[3]
existing = os.environ["EXISTING_LABELS"]
issue = open(f"{scratch}/{n}.issue.json").read()
prompt = (
    f"Triage this issue in {repo}.\n\n"
    f"Known findings: labels already available in this repo: {existing}\n"
    "Label vocabulary (pick 1-2 from these, they all exist): bug (real defect in shipped code), "
    "enhancement (new feature), question (needs owner input), mvp (mobile/bot channel), "
    "backlog (batched idea), duplicate (clearly duplicates another open issue, name it in the comment), "
    "invalid (not a real issue for this repo), documentation, auto-triage.\n"
    "Priority: P0 (stack down or money leaking), P1 (blocks a live feature), P2 (default), P3 (nice-to-have/idea).\n"
    "IMPORTANT: you are advisory only — your comment says so; never claim you closed or fixed anything.\n"
    "comment: 1-4 short markdown sentences — what the issue is really about, missing info, "
    "and if a duplicate: 'Same as #X' plus a one-line reason. Factual, no invention.\n\n"
    "Output format — exactly this, nothing before or after:\n"
    "LABELS: <comma-separated labels>\n"
    "PRIORITY: <P0|P1|P2|P3>\n"
    "COMMENT: <markdown body, single paragraph>\n\n"
    "---BEGIN ISSUE---\n"
    f"{issue}\n"
)
print(prompt)
PYEOF
}

issue_one() {
  local N="$1"
  echo "== triaging issue #$N =="

  read_one "$N"
  assemble_prompt "$N"

  if ! printf '%s\n---\n%s\n' "$SYSTEM_PROMPT" "$(cat "$SCRATCH/$N.prompt.txt")" \
      | opencode run --dir "$SCRATCH" -m "$MODEL" --format json 2>"$SCRATCH/$N.err" \
      > "$SCRATCH/$N.out.jsonl"; then
    echo "SKIP #$N: opencode worker exited nonzero"
    return
  fi

  out=$(python3 -c '
import json, sys
text_parts, had_tool_calls = [], False
for line in open(sys.argv[1]):
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
if had_tool_calls:
    sys.stderr.write("triage worker attempted a tool call - output rejected\n")
    sys.exit(1)
sys.stdout.write("".join(text_parts))
' "$SCRATCH/$N.out.jsonl") || true

  if [ -z "${out// }" ]; then
    echo "SKIP #$N: triage worker failed/unparseable"
    return
  fi

  parsed=$(printf '%s' "$out" | EXISTING="$EXISTING_LABELS" python3 -c '
import sys, os, re
exist = set(l.strip() for l in os.environ["EXISTING"].split(",") if l.strip())
body = sys.stdin.read()
def field(name):
    m = re.search(rf"^{name}:[ \t]*(.*)$", body, re.M)
    return m.group(1).strip() if m else ""
labels = [l.strip() for l in field("LABELS").split(",") if l.strip()]
keep = [l for l in labels if l in exist][:2]
priority = field("PRIORITY") if re.fullmatch(r"P[0-3]", field("PRIORITY")) else "P2"
comment = field("COMMENT")
if not comment or "---BEGIN ISSUE---" in comment:
    comment = body.split("COMMENT:", 1)[1].split("---BEGIN ISSUE---")[0].strip() if "COMMENT:" in body else ""
# unit-separator join; one-line fields so bash `read` splits cleanly
def flat(s):
    return " ".join(s.split())[:1500]
print("\x1f".join([",".join(keep), priority, flat(comment)]))
') || true

  if [ -z "${parsed// /}" ]; then
    echo "SKIP #$N: triage output unparseable"
    return
  fi

  IFS=$'\x1f' read -r labels priority comment <<< "$parsed"
  echo "  -> labels='$labels' priority='$priority'"

  if [ -n "${labels:-}" ]; then
    gh issue edit "$N" --repo "$REPO" --add-label "$labels" || true
  fi

  if ! gh issue view "$N" --repo "$REPO" --json comments -q '[.comments[].body] | join("\n")' \
      | grep -Fq "Auto-triage"; then
    gh issue comment "$N" --repo "$REPO" \
      --body "🤖 Auto-triage (\`$MODEL\`): priority **${priority:-P2}**. Advisory — a human acts on this (triage never closes/merges).

$comment" || true
  fi
}

# Event-driven: triage just the one issue if this run was triggered by it.
TRIGGER_ISSUE="${GITHUB_TRIGGER_ISSUE:-}"
if [ -n "$TRIGGER_ISSUE" ]; then
  echo "event-driven run for issue #$TRIGGER_ISSUE"
  issue_one "$TRIGGER_ISSUE"
  exit 0
fi

# Scheduled/manual: sweep all open issues not yet auto-triaged.
NUMBERS=$(gh issue list --repo "$REPO" --state open --limit 50 --json number -q 'map(.number) | join(" ")')
echo "sweeping issues: $NUMBERS"
for N in $NUMBERS; do
  if gh issue view "$N" --repo "$REPO" --json comments -q '[.comments[].body] | join("\n")' \
      | grep -Fq "Auto-triage"; then
    echo "  -> skip #$N (already triaged)"
    continue
  fi
  issue_one "$N"
done
