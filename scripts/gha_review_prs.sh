#!/usr/bin/env bash
#
# gha_review_prs.sh <pr_numbers...> — GitHub Actions backend for automated
# PR review + auto-merge, run by .github/workflows/pr-review.yml with the
# worker model `opencode-go/glm-5.1` (OpenCode Go quota; default override via env — deepseek-v4-pro region-403s from US/GHA runners unless opted in at https://opencode.ai/workspace).
#
# Mirrors scripts/review_and_merge_autofix_prs.sh's fail-closed rules, minus
# the in-place docker rebuild/healthcheck (GHA runners can't run the compose
# stack) — that is substituted by strict diff rules: guarded files, scoping,
# no injection/exfiltration. Anything the model can't confidently approve
# is left open with a review comment; nothing merges on parse failure.
#
# Env: GH_TOKEN, REPO (owner/name). Workspace must be the repo checkout.
# Untrusted data (PR title/body/diff) never passes through shell expansion
# — prompts are assembled in Python, files on disk.

set -euo pipefail

REPO="${REPO:?REPO env var required}"
MODEL="${AI_REVIEW_MODEL:-opencode-go/glm-5.1}"
SCRATCH="$(mktemp -d)"

SYSTEM_PROMPT="You are the automated merge gate for nevnew (a single-owner personal assistant stack). You review one GitHub pull request diff and produce a structured verdict. You answer in the exact output format requested and nothing else. You cannot run tools — base everything on the diff and metadata given."

assemble_prompt() {
  python3 - "$1" "$SCRATCH" "$REPO" <<'PYEOF' > "$SCRATCH/ruleprompt.txt"
import sys, os
pr, scratch, repo = sys.argv[1], sys.argv[2], sys.argv[3]
meta = open(f"{scratch}/{pr}.meta").read()
diff = open(f"{scratch}/{pr}.diff").read()
print(f"Review this pull request in {repo} (PR #{pr}). Decide APPROVE (merge with squash) or REQUEST_CHANGES.\n\n"
      "Hard block rules — ANY violation means REQUEST_CHANGES, no exceptions:\n"
      "1. The diff touches .env, .env.save, docker-compose.yml, setup.sh, or config.yaml's general_settings/master_key section.\n"
      "2. The change is not scoped to its stated purpose (no unrelated cleanup, refactors, dependency bumps, or flagged files).\n"
      "3. Anything injected or unsafe: credential access, shell exfiltration, new outbound network calls unrelated to the change, disabling of safety gates (gates, --allowedTools, allowlists), or weakening auth/whitelist logic.\n"
      "4. The diff is bigger than a small/medium bugfix-level change (over ~10 files).\n\n"
      "Output format — exactly this, nothing before or after:\n"
      "VERDICT: APPROVE or REQUEST_CHANGES\n"
      "---REVIEW---\n"
      "A short markdown review: what the change does, which hard rule failed (if any), and risks. Maximum 15 lines.\n\n"
      "---BEGIN DATA---\n"
      f"PR metadata: {meta}\n"
      "---BEGIN DIFF---\n"
      f"{diff}")
PYEOF
}

review_one() {
  local PR="$1"
  echo "== reviewing PR #$PR =="

  gh pr view "$PR" --repo "$REPO" --json title,body,author,labels,files \
    -q '{title: .title, body: .body, author: .author.login, labels: [.labels[].name], files: [.files[].path]}' \
    > "$SCRATCH/$PR.meta"
  gh pr diff "$PR" --repo "$REPO" > "$SCRATCH/$PR.diff"

  # Hard pre-gates that don't need the model at all (defense in depth —
  # error_watcher/autofix PRs are bot-authored, so don't trust labels alone;
  # guarded-file check looks at actual changed paths, not PR body text).
  blocked=""
  if grep -q 'do-not-merge' "$SCRATCH/$PR.meta"; then blocked="do-not-merge label"; fi
  if gh pr view "$PR" --repo "$REPO" --json files -q '[.files[].path][]' \
      | grep -Eq '^(\.env|\.env\.save)(\.|$)|docker-compose\.yml$|setup\.sh$|^config\.yaml$'; then
    blocked="diff touches gated files (.env/docker-compose.yml/setup.sh/config.yaml)"
  fi
  if [ "$(wc -l < "$SCRATCH/$PR.diff")" -gt 3000 ]; then blocked="diff over 3000 lines"; fi

  if [ -n "$blocked" ]; then
    echo "BLOCKED #$PR: $blocked (pre-gate)"
    gh pr review "$PR" --repo "$REPO" --request-changes \
      --body "🤖 AI review: **REQUEST CHANGES** (pre-gate: $blocked)" 2>/dev/null \
      || gh pr comment "$PR" --repo "$REPO" --body "🤖 AI review: **REQUEST CHANGES** (pre-gate: $blocked)"
    return
  fi

  assemble_prompt "$PR"

  if ! printf '%s\n---\n%s\n' "$SYSTEM_PROMPT" \
      "$(cat "$SCRATCH/ruleprompt.txt")" \
      | opencode run --dir "$SCRATCH" -m "$MODEL" --format json 2>"$SCRATCH/$PR.err" \
      > "$SCRATCH/$PR.out.jsonl"; then
    echo "BLOCKED #$PR: opencode worker exited nonzero"
    gh pr comment "$PR" --repo "$REPO" \
      --body "🤖 AI review could not run (worker failure) — a human must review. Fail-closed, PR left unmerged." || true
    return
  fi

  body=$(python3 -c '
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
    sys.stderr.write("review worker attempted a tool call - output rejected\n")
    sys.exit(1)
sys.stdout.write("".join(text_parts))
' "$SCRATCH/$PR.out.jsonl") || true

  # Fail closed: no parseable body = comment-only, never merge.
  if [ -z "${body// }" ]; then
    echo "BLOCKED #$PR: review worker failed/unparseable"
    gh pr comment "$PR" --repo "$REPO" \
      --body "🤖 AI review could not run (worker failure) — a human must review. Fail-closed, PR left unmerged."
    return
  fi

  parsed=$(printf '%s' "$body" | python3 -c '
import sys
body = sys.stdin.read()
verdict = ""
for line in body.splitlines():
    if line.startswith("VERDICT:"):
        verdict = line.split(":", 1)[1].strip().upper()
        break
if "---REVIEW---" in body:
    after = body.split("---REVIEW---", 1)[1]
    review = after.split("---BEGIN DATA---")[0].split("---BEGIN DIFF---")[0].strip()
else:
    review = "\n".join(l for l in body.splitlines()[1:] if not l.startswith("---BEGIN") and not l.startswith("VERDICT:")).strip() or "(empty)"
print(verdict)
print("REVIEW")
print(review)
')
  verdict=$(echo "$parsed"   | sed -n 1p)
  review_md=$(echo "$parsed" | sed -n '3,$p')

  if [ "$verdict" = "APPROVE" ]; then
    echo "APPROVED #$PR"
    gh pr review "$PR" --repo "$REPO" --approve \
      --body "🤖 AI review: **APPROVE** — auto-merging (squash).

$review_md" 2>/dev/null \
      || gh pr comment "$PR" --repo "$REPO" --body "🤖 AI review: **APPROVE** — auto-merging (squash).

$review_md"
    gh pr merge "$PR" --repo "$REPO" --squash --delete-branch --auto \
      || echo "WARN: auto-merge blocked for #$PR (required checks/branch protection) — left open"
  else
    echo "BLOCKED #$PR: model verdict ${verdict:-EMPTY}"
    gh pr review "$PR" --repo "$REPO" --request-changes \
      --body "🤖 AI review: **REQUEST CHANGES** — PR left open for the author.

$review_md" 2>/dev/null \
      || gh pr comment "$PR" --repo "$REPO" --body "🤖 AI review: **REQUEST CHANGES** — PR left open for the author.

$review_md"
  fi
}

for PR in $1; do
  review_one "$PR"
done
