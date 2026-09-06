#!/usr/bin/env bash
#
# gh_tool.sh — NevNew's GitHub tool (issue #5): a strict, allowlisted wrapper
# around the `gh` CLI, called by the n8n "NevNew macOS + GitHub Tools"
# workflow's toolCode nodes (child_process exec with a hard timeout).
#
# Safety model:
#   - Repo is pinned to GRITui/nevnew — the caller cannot aim it elsewhere.
#   - Strict subcommand allowlist; every argument is validated before exec.
#   - Destructive verbs (merge, close PR, edit, delete, label, release) are
#     deliberately NOT exposed. Read + issue triage only.
#   - 25s hard timeout via perl alarm (macOS has no GNU timeout; the alarm
#     clock survives exec, so gh is killed if it hangs on auth prompts).

set -u

REPO="GRITui/nevnew"
MAXARG=8

usage() {
  cat <<'EOF'
Usage: gh_tool.sh <subcommand> [args...]
Subcommands:
  issues_list [state=open|closed|all] [limit<=30]
  issue_view <number>
  issue_create <title> <body>
  issue_comment <number> <body>
  issue_close <number> <comment>
  pr_list [state=open|closed|merged] [limit<=20]
  pr_view <number>
  pr_diff <number>
  repos_list [limit<=30]
EOF
  exit 2
}

run_gh() {
  # 25s hard timeout: perl sets ALRM then execs gh (timer survives exec).
  perl -e 'alarm 25; exec @ARGV' -- "$@"
}

is_uint() { case "$1" in ''|*[!0-9]*) return 1 ;; *) return 0 ;; esac; }
clamp_limit() { is_uint "$1" || { echo "ERROR: limit must be a number" >&2; exit 2; }; [ "$1" -le "$2" ] || { echo "ERROR: limit too large (max $2)" >&2; exit 2; }; }

[ $# -ge 1 ] || usage
[ $# -le $MAXARG ] || { echo "ERROR: too many arguments" >&2; exit 2; }

SUB="$1"; shift

case "$SUB" in
  issues_list)
    STATE="${1:-open}"; LIMIT="${2:-10}"
    case "$STATE" in open|closed|all) ;; *) echo "ERROR: bad state" >&2; exit 2 ;; esac
    clamp_limit "$LIMIT" 30
    run_gh gh issue list --repo "$REPO" --state "$STATE" --limit "$LIMIT" \
      --json number,title,labels --template '{{range .}}#{{.number}} [{{range .labels}}{{.name}} {{end}}] {{.title}}{{"\n"}}{{end}}'
    ;;
  issue_view)
    is_uint "${1:-}" || { echo "ERROR: issue number required" >&2; exit 2; }
    run_gh gh issue view "$1" --repo "$REPO" --json number,title,state,body,labels \
      --template '#{{.number}} [{{.state}}] {{.title}}{{"\n"}}Labels: {{range .labels}}{{.name}} {{end}}{{"\n\n"}}{{.body}}'
    ;;
  issue_create)
    [ -n "${1:-}" ] && [ -n "${2:-}" ] || { echo "ERROR: issue_create needs <title> <body>" >&2; exit 2; }
    run_gh gh issue create --repo "$REPO" --title "$1" --body "$2"
    ;;
  issue_comment)
    is_uint "${1:-}" && [ -n "${2:-}" ] || { echo "ERROR: issue_comment needs <number> <body>" >&2; exit 2; }
    run_gh gh issue comment "$1" --repo "$REPO" --body "$2"
    ;;
  issue_close)
    is_uint "${1:-}" && [ -n "${2:-}" ] || { echo "ERROR: issue_close needs <number> <comment>" >&2; exit 2; }
    run_gh gh issue comment "$1" --repo "$REPO" --body "$2" && \
    run_gh gh issue close "$1" --repo "$REPO" --reason completed
    ;;
  pr_list)
    STATE="${1:-open}"; LIMIT="${2:-10}"
    case "$STATE" in open|closed|merged) ;; *) echo "ERROR: bad state" >&2; exit 2 ;; esac
    clamp_limit "$LIMIT" 20
    run_gh gh pr list --repo "$REPO" --state "$STATE" --limit "$LIMIT" \
      --json number,title,headRefName --template '{{range .}}#{{.number}} ({{.headRefName}}) {{.title}}{{"\n"}}{{end}}'
    ;;
  pr_view)
    is_uint "${1:-}" || { echo "ERROR: PR number required" >&2; exit 2; }
    run_gh gh pr view "$1" --repo "$REPO" --json number,title,state,body,headRefName,baseRefName,mergeable \
      --template '#{{.number}} [{{.state}}] {{.title}}{{"\n"}}{{.headRefName}} -> {{.baseRefName}} (mergeable: {{.mergeable}}){{"\n\n"}}{{.body}}'
    ;;
  pr_diff)
    is_uint "${1:-}" || { echo "ERROR: PR number required" >&2; exit 2; }
    run_gh gh pr diff "$1" --repo "$REPO"
    ;;
  repos_list)
    LIMIT="${1:-10}"; clamp_limit "$LIMIT" 30
    run_gh gh repo list --limit "$LIMIT" --json name,visibility,updatedAt \
      --template '{{range .}}{{.name}} ({{.visibility}}, {{.updatedAt}}){{"\n"}}{{end}}'
    ;;
  *)
    echo "ERROR: unknown or disallowed subcommand: $SUB" >&2
    usage
    ;;
esac