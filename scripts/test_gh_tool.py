#!/usr/bin/env python3
"""Regression test for issue #126: gh_tool.sh must work under a stripped
environment (n8n task-runner sandbox: no PATH, no HOME).

Before the fix, `gh` resolved fine in an interactive shell but silently
failed inside n8n's sandboxed task runner. The fix pins HOME/PATH and the
absolute gh binary path inside gh_tool.sh; this test reproduces the
sandbox with `env -i` so a regression fails loudly instead of silently.

Usage: test_gh_tool.py [--json]

Exit 0 if all checks pass, 1 otherwise. Stdlib-only, same conventions as
scripts/e2e_nevnew.py (PASS/FAIL lines, --json option).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "gh_tool.sh"
TIMEOUT = 60  # gh_tool.sh has its own 25s perl alarm per gh call


def run(args: list[str], stripped: bool = True) -> tuple[int, str, str]:
    """Run gh_tool.sh. stripped=True simulates the n8n sandbox (env -i)."""
    if stripped:
        cmd = ["env", "-i", "/bin/bash", str(SCRIPT), *args]
    else:
        cmd = ["/bin/bash", str(SCRIPT), *args]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


def check_pr_list_sandbox() -> tuple[str, bool, str]:
    code, out, err = run(["pr_list", "open", "3"])
    if code == 0 and "#" in out:
        first = out.strip().splitlines()[0][:80] if out.strip() else ""
        return "pr_list_sandbox", True, f"exit=0 first={first!r}"
    return "pr_list_sandbox", False, f"exit={code} err={err[:120]!r}"


def check_issues_list_sandbox() -> tuple[str, bool, str]:
    code, out, err = run(["issues_list", "open", "2"])
    if code == 0 and "#" in out:
        return "issues_list_sandbox", True, f"exit=0 lines={len(out.strip().splitlines())}"
    return "issues_list_sandbox", False, f"exit={code} err={err[:120]!r}"


def check_allowlist_sandbox() -> tuple[str, bool, str]:
    """Safety model must survive the sandbox too: bad subcommand -> exit 2."""
    code, out, err = run(["definitely_not_a_subcommand"])
    if code == 2 and "disallowed" in err:
        return "allowlist_sandbox", True, "exit=2 disallowed rejected"
    return "allowlist_sandbox", False, f"exit={code} err={err[:120]!r}"


def check_pr_list_normal() -> tuple[str, bool, str]:
    """Sanity: also works in a normal environment (guards against the fix
    only passing because env -i happens to inherit something)."""
    code, out, err = run(["pr_list", "open", "1"], stripped=False)
    if code == 0:
        return "pr_list_normal", True, f"exit=0"
    return "pr_list_normal", False, f"exit={code} err={err[:120]!r}"


def main() -> int:
    want_json = "--json" in sys.argv[1:]
    results = [
        check_pr_list_sandbox(),
        check_issues_list_sandbox(),
        check_allowlist_sandbox(),
        check_pr_list_normal(),
    ]
    if want_json:
        print(json.dumps(
            [{"name": n, "ok": o, "detail": d} for n, o, d in results],
            indent=2))
    else:
        for name, ok, detail in results:
            tag = "PASS" if ok else "FAIL"
            print(f"{tag}({name}): {detail}")
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
