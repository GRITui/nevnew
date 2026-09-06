#!/usr/bin/env python3
"""Tail NevNew's Docker container logs, detect real errors, and file them as
GitHub bug issues automatically — so failures don't just scroll past in
`docker compose logs` unnoticed.

Deliberately does NOT attempt any fix — see BACKLOG.md. It only detects and
files; a human (or an agent picking up the filed issue) does the fixing.

Run periodically (e.g. via cron, every 15-30 min): each run only looks at log
lines emitted since its own last run (state file), so it's safe to schedule
without dedup logic of its own.

Requires the `gh` CLI to be authenticated (`gh auth status`).
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

REPO = "GRITui/nevnew"
CONTAINERS = ["newnew-litellm", "newnew-telegram-bot", "newnew-mcpo", "newnew-open-webui"]

STATE_PATH = Path(__file__).parent / ".error_watcher_state.json"

# Known/expected failure signatures that are NOT bugs — matched against the
# raw log line, case-insensitive. Add to this list when a new "error" turns
# out to be expected/operational rather than a code defect, so the watcher
# doesn't keep re-filing it. Each entry documents *why* it's noise.
KNOWN_NOISE = [
    # OpenRouter's account-wide free-tier daily cap (50 req/day, shared
    # across ALL free models) — not per-model, so NevNew's 3-deployment
    # fallback (issue #7) can't route around it. This is a known gap
    # (tracked separately, see BACKLOG.md), not a fresh bug per occurrence.
    r"free-models-per-day",
    # The telegram-bot's own log line when LiteLLM 429s it (same root cause
    # as above, just observed from the other side of the round-trip) — the
    # bot already handles this gracefully (issue #11 requirement 4), so it's
    # not a bug in the bot either.
    r"LiteLLM round-trip failed: Client error '429",
    # uvicorn/starlette's SIGTERM shutdown path always ends in an unavoidable
    # CancelledError traceback from the lifespan's receive() — mcpo logs it
    # on every deliberate stop (deploys, watchdog revives, `docker stop`
    # tests). Expected/operational, not a bug (observed 2026-09-06).
    r"Received SIGTERM, initiating graceful shutdown",
]

# Lines that mark the start of a real error worth looking at.
ERROR_MARKERS = [
    r"\bERROR\b",
    r"Traceback \(most recent call last\)",
]


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"since": {}, "filed_signatures": {}}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True))


def is_noise(line: str) -> bool:
    return any(re.search(pattern, line, re.IGNORECASE) for pattern in KNOWN_NOISE)


def is_error_line(line: str) -> bool:
    return any(re.search(pattern, line) for pattern in ERROR_MARKERS)


def signature_for(container: str, line: str) -> str:
    # Strip timestamps, request/user IDs, and other volatile tokens so the
    # same *kind* of error hashes the same way across occurrences.
    normalized = re.sub(r"\d{2,}", "#", line)
    normalized = re.sub(r"[0-9a-fA-F-]{16,}", "#", normalized)
    return hashlib.sha256(f"{container}:{normalized}".encode()).hexdigest()[:16]


def fetch_new_logs(container: str, since_iso: str | None) -> tuple[str, str]:
    now_iso = datetime.now(timezone.utc).isoformat()
    cmd = ["docker", "logs", container, "--timestamps"]
    if since_iso:
        cmd += ["--since", since_iso]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return result.stdout + result.stderr, now_iso


def file_issue(container: str, line: str, context: str) -> int:
    title = f"[auto] {container}: {line.strip()[:100]}"
    body = (
        f"Auto-filed by `scripts/error_watcher.py` after detecting a new error "
        f"signature in `{container}`'s logs.\n\n"
        f"**Trigger line:**\n```\n{line.strip()}\n```\n\n"
        f"**Context (log excerpt):**\n```\n{context.strip()[-3000:]}\n```\n\n"
        f"Not auto-fixed by this watcher — a separate autofix pipeline picks "
        f"this up next. If this turns out to be expected/operational rather "
        f"than a bug, add its pattern to `KNOWN_NOISE` in error_watcher.py so "
        f"it stops re-filing."
    )
    result = subprocess.run(
        ["gh", "issue", "create", "--repo", REPO, "--title", title, "--body", body, "--label", "bug"],
        check=True,
        capture_output=True,
        text=True,
    )
    # gh issue create prints the new issue's URL as its only stdout line,
    # e.g. https://github.com/GRITui/nevnew/issues/17 — the trailing path
    # segment is the issue number the autofix step needs to act on.
    return int(result.stdout.strip().rsplit("/", 1)[-1])


def main() -> None:
    state = load_state()
    filed_this_run = 0
    filed_issue_numbers: list[int] = []

    for container in CONTAINERS:
        since = state["since"].get(container)
        logs, now_iso = fetch_new_logs(container, since)
        state["since"][container] = now_iso

        lines = logs.splitlines()
        for i, line in enumerate(lines):
            if not is_error_line(line):
                continue
            # Check noise against a window looking back far enough to reach
            # the actual ERROR line, not just this trigger line — a bare
            # "Traceback (most recent call last):" carries no signal itself,
            # and litellm's own exception-chaining preamble ("During
            # handling of the above exception...") can put a good 10+ lines
            # between the informative ERROR line and a later Traceback
            # header for the *same* incident.
            window = "\n".join(lines[max(0, i - 30) : i + 1])
            if is_noise(window):
                continue

            sig = signature_for(container, line)
            if sig in state["filed_signatures"]:
                continue

            context = "\n".join(lines[max(0, i - 5) : i + 15])
            issue_number = file_issue(container, line, context)
            state["filed_signatures"][sig] = {
                "first_seen": now_iso,
                "line": line.strip()[:200],
            }
            filed_this_run += 1
            filed_issue_numbers.append(issue_number)

    save_state(state)
    print(f"error_watcher: filed {filed_this_run} new issue(s)")
    # Machine-readable line for the calling n8n workflow to react to which
    # issues just got filed (so it can dispatch the autofix step per-issue)
    # — kept separate from the human-readable line above rather than
    # replacing it, since the crontab log is also read by humans.
    print(json.dumps({"filed_issue_numbers": filed_issue_numbers}))


if __name__ == "__main__":
    main()
