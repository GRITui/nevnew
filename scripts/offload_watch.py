#!/usr/bin/env python3
"""offload_watch.py — watch offload worker jobs so the orchestrator doesn't poll.

Delegation-watch guideline (owner, 2026-09-06): worker watching is delegated
to n8n (the "Offload Watcher" workflow runs this script on a 1-minute
schedule + a manual webhook trigger). When a job transitions state, or when
all jobs finish, a Telegram notification is sent exactly once — otherwise
the script stays quiet. The orchestrator reads /tmp/out_*.md outputs when
notified, and remains the quality gate.

Job registry: --registry (default /tmp/nevnew-offload-jobs.json), a JSON
array of {"name": str, "out": path, "err": path} — written by whoever
dispatches jobs (see scripts/offload.sh usage patterns).

Status per job:
    ok       — out file exists and is non-empty (worker text output ready)
    failed   — err file exists and is non-empty (worker/gate failure)
    running  — neither yet (cline timeout guarantees eventual resolution)

State file (--state, default /tmp/nevnew-offload-state.json) records the
last notified state per job plus the "final" flag, so repeated runs only
notify on changes. When every job is ok/failed, one summary is sent and no
further messages go out until the registry changes.

Usage:
    offload_watch.py            # print JSON status; notify on transitions
    offload_watch.py --json     # print JSON status only (no Telegram)
    offload_watch.py --reset    # forget state (e.g. after registry edit)

Telegram delivery uses TELEGRAM_BOT_TOKEN and TELEGRAM_OWNER_ID from the
repo .env (read here, never committed). If unset, notifications fall back
to stderr — the watcher never crashes on missing config.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_REGISTRY = "/tmp/nevnew-offload-jobs.json"
DEFAULT_STATE = "/tmp/nevnew-offload-state.json"


def load_json(path, fallback):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return fallback


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)


def job_status(job):
    out = Path(job["out"])
    err = Path(job["err"])
    if err.exists() and err.stat().st_size > 0:
        return "failed"
    if out.exists() and out.stat().st_size > 0:
        return "ok"
    return "running"


def send_telegram(token, chat_id, text):
    """Send one message; return an error string on failure, else None."""
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        "https://api.telegram.org/bot%s/sendMessage" % token,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
        return None
    except Exception as exc:  # noqa: BLE001 — notify must never crash the watcher
        return "telegram send failed: %s" % exc


def main():
    ap = argparse.ArgumentParser(description="watch offload worker jobs")
    ap.add_argument("--registry", default=DEFAULT_REGISTRY)
    ap.add_argument("--state", default=DEFAULT_STATE)
    ap.add_argument("--json", action="store_true", help="status JSON only, no Telegram")
    ap.add_argument("--reset", action="store_true", help="forget saved state")
    args = ap.parse_args()

    registry = load_json(args.registry, [])
    if not isinstance(registry, list) or not registry:
        print(json.dumps({"jobs": [], "all_done": True, "summary": "registry empty"}))
        return 0

    state = {} if args.reset else load_json(args.state, {})
    last = state.get("jobs", {})
    final_sent = state.get("final_sent", False)

    jobs = []
    lines = []
    for job in registry:
        name = job.get("name", "?")
        status = job_status(job)
        jobs.append({"name": name, "status": status})
        if last.get(name) != status:
            if status == "ok":
                lines.append("✅ %s: done — output at %s" % (name, job["out"]))
            elif status == "failed":
                err_detail = ""
                try:
                    err_detail = Path(job["err"]).read_text(errors="replace")[:120].strip()
                except OSError:
                    pass
                lines.append("❌ %s: FAILED — %s" % (name, err_detail))

    all_done = all(j["status"] in ("ok", "failed") for j in jobs)
    n_ok = sum(1 for j in jobs if j["status"] == "ok")
    n_failed = sum(1 for j in jobs if j["status"] == "failed")
    n_running = len(jobs) - n_ok - n_failed

    message = None
    if lines and not all_done:
        message = "🤖 offload workers update:\n" + "\n".join(lines) + (
            "\n⏳ %d still running." % n_running
        )
    if all_done and not final_sent:
        message = (
            "🏁 ALL offload jobs finished: %d ok, %d failed.\n" % (n_ok, n_failed)
            + "\n".join(
                "✅ %s" % j["name"] if j["status"] == "ok" else "❌ %s" % j["name"]
                for j in jobs
            )
            + "\nOutputs: /tmp/out_<name>.md — orchestrator review pending."
        )
        final_sent = True

    # Persist state AFTER computing the message so each transition fires once.
    state["jobs"] = {j["name"]: j["status"] for j in jobs}
    state["final_sent"] = final_sent
    try:
        save_json(args.state, state)
    except OSError as exc:
        print("warning: could not save state: %s" % exc, file=sys.stderr)

    if message and not args.json:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("TELEGRAM_OWNER_ID", "")
        env_file = PROJECT_ROOT / ".env"
        if (not token or not chat_id) and env_file.exists():
            for raw in env_file.read_text().splitlines():
                if raw.startswith("TELEGRAM_BOT_TOKEN="):
                    token = raw.split("=", 1)[1].strip()
                elif raw.startswith("TELEGRAM_OWNER_ID="):
                    chat_id = raw.split("=", 1)[1].strip()
        if token and chat_id and token != "your-telegram-bot-token-here":
            err = send_telegram(token, chat_id, message)
            if err:
                print(err, file=sys.stderr)
        else:
            print("-- telegram not configured; message follows --", file=sys.stderr)
            print(message, file=sys.stderr)

    print(json.dumps({"jobs": jobs, "all_done": all_done,
                      "ok": n_ok, "failed": n_failed, "running": n_running}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
