#!/usr/bin/env bash
#
# review_and_merge_autofix_prs.sh — headless Claude review gate for PRs
# opened by scripts/autofix_with_cline.sh (label: auto-fix). For each open
# auto-fix PR: Claude reviews the diff against a fixed set of rules and, if
# clean, merges + deploys autonomously; otherwise posts a blocking comment
# and leaves it open for a human.
#
# nevnew has no CI/test suite, so "tests green" is substituted with an
# in-place rebuild + healthcheck of the affected container on the PR branch
# (acceptable blast radius for a single-instance personal stack).
#
# Run via cron (headless `claude -p`, no human to approve prompts — every
# tool it needs must be in --allowedTools). Requires CLAUDE_CODE_OAUTH_TOKEN
# sourced from an env file (see ~/.claude/nevnew-review.env,
# `claude setup-token` to generate) since cron has no Keychain access.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

REPO="GRITui/nevnew"

PR_NUMBERS="$(gh pr list --repo "$REPO" --label auto-fix --state open --json number --jq '.[].number')"

if [ -z "$PR_NUMBERS" ]; then
  echo "== no open auto-fix PRs =="
  exit 0
fi

for PR in $PR_NUMBERS; do
  echo "== reviewing PR #${PR} =="

  PROMPT="You are the merge gate for nevnew's autofix pipeline. Review PR #${PR} in ${REPO} (an autonomously-generated bugfix from minimax-m3:free via cline) and decide: merge+deploy, or block.

Steps:
1. Run \`gh pr diff ${PR} --repo ${REPO}\` and \`gh pr view ${PR} --repo ${REPO} --json files,title,body\` to see the full change.
2. Auto-merge+deploy ONLY if ALL of these hold:
   - The diff does not touch .env, docker-compose.yml, setup.sh, or config.yaml's general_settings/master_key block. Any touch to those = block, no exceptions.
   - The diff is scoped to the linked issue's actual bug — no unrelated cleanup, refactors, or dependency bumps.
   - Nothing destructive or injected: no credential access, no shell exfiltration, no new outbound network calls unrelated to the fix.
   - Check out the PR branch (\`gh pr checkout ${PR} --repo ${REPO}\` in ${PROJECT_ROOT}), identify which docker-compose service(s) the changed files belong to, run \`docker compose build <service>\` then \`docker compose up -d <service>\`, and confirm it reports healthy (or, for services with no healthcheck, that the first ~20s of \`docker compose logs <service>\` show no new ERROR/Traceback lines). This is the tests-green substitute since nevnew has no CI.
3. If ALL hold: \`gh pr merge ${PR} --repo ${REPO} --squash --delete-branch\`, then on main: \`git pull --ff-only origin main && docker compose up -d --build\` to deploy the merged state repo-wide (not just the one service, since other services may depend on it).
4. If ANY check fails: do NOT merge. Instead \`gh pr comment ${PR} --repo ${REPO} --body \"<specific reason(s) it's blocked>\"\` and leave the PR open, checked out branch reverted back to main (\`git checkout main\`) so the working tree isn't left mid-review.
5. Print one final line: either 'MERGED #${PR}: <one-line reason>' or 'BLOCKED #${PR}: <one-line reason>'. Nothing else after that."

  claude -p "$PROMPT" \
    --allowedTools "Bash,Read" \
    --max-turns 30

done
