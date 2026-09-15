#!/usr/bin/env python3
"""offload_status.py — issue #67: point-in-time status for one offload job.

Backs ai-core's job_status(id) tool (via the n8n MCP tool node that calls
this script). Reads the same registry offload_watch.py polls (default
/tmp/nevnew-offload-jobs.json) and reports the status of one job by id
(the registry "name" — the job id scripts/offload_dispatch.sh prints), or
lists every known job. Read-only — never mutates state or the watcher's
notification bookkeeping, so it is always safe to call for a "how's it
going?" check.

Usage:
    offload_status.py <job_id>     # status + output/error tail for one job
    offload_status.py --list       # list every known job and its status
    offload_status.py              # same as --list
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_REGISTRY = "/tmp/nevnew-offload-jobs.json"
_TAIL_CHARS = 4000


def load_registry(path: str):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def job_status(job: dict) -> str:
    """Same status logic as offload_watch.py's job_status (kept in sync)."""
    out = Path(job.get("out", ""))
    err = Path(job.get("err", ""))
    if err.exists() and err.stat().st_size > 0:
        return "failed"
    if out.exists() and out.stat().st_size > 0:
        return "ok"
    return "running"


def tail(path_str: str, limit: int = _TAIL_CHARS) -> str:
    path = Path(path_str)
    if not path_str or not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    return text[-limit:]


def main() -> int:
    ap = argparse.ArgumentParser(description="status of one offload job")
    ap.add_argument("job_id", nargs="?", help="job id (registry 'name')")
    ap.add_argument("--registry", default=DEFAULT_REGISTRY)
    ap.add_argument("--list", action="store_true", help="list all known jobs")
    args = ap.parse_args()

    registry = load_registry(args.registry)

    if args.list or not args.job_id:
        jobs = [
            {"name": j.get("name", "?"), "status": job_status(j), "task": j.get("task", "")}
            for j in registry
        ]
        print(json.dumps({"jobs": jobs}, indent=2))
        return 0

    match = None
    for job in registry:
        if job.get("name") == args.job_id:
            match = job
            break
    if match is None:
        # Fall back to a substring match so a shortened/mistyped id still
        # resolves, as long as it's unambiguous.
        candidates = [j for j in registry if args.job_id in j.get("name", "")]
        if len(candidates) == 1:
            match = candidates[0]

    if match is None:
        print(
            json.dumps(
                {
                    "error": f"no job matching {args.job_id!r}",
                    "known_jobs": [j.get("name", "?") for j in registry],
                }
            )
        )
        return 1

    status = job_status(match)
    result = {"name": match.get("name"), "status": status, "task": match.get("task", "")}
    if status == "ok":
        result["output_tail"] = tail(match.get("out", ""))
    elif status == "failed":
        result["error_tail"] = tail(match.get("err", ""))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
