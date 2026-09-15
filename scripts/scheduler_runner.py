#!/usr/bin/env python3
"""Scheduled-reminder firing driver (issue #62).

Invoked by system cron every minute (see docs note below), NOT by an n8n
Schedule Trigger workflow — same reasoning as scripts/briefing.py (issue
#65): n8n's ExecuteCommand hangs Python in its task-runner sandbox, so
anything that must fire on a wall-clock schedule runs on the host instead.

What it does each run:
  1. Open ./data/schedules/schedules.db (the SAME file ai-core's
     schedule_task/list_schedules/cancel_schedule tools write to, via the
     ./data/schedules bind mount — see docker-compose.yml).
  2. Find schedules whose next_run <= now.
  3. For each, push a Telegram message directly (Bot API, no n8n) and mark
     it fired (one-off -> status=done; recurring -> next_run advanced).

user_id -> chat_id resolution: schedules are stored with whatever
`user_id` the chat request carried (see ai-core's ToolContext). The
Telegram bot's convention is `telegram:<chat_id>` (see briefing.py's
aicore_chat). If a schedule's user_id doesn't match that pattern, this
falls back to TELEGRAM_OWNER_ID (single-owner deployment default) so a
schedule created from, say, the Open-WebUI chat still fires somewhere.

Usage:
    scheduler_runner.py [--dry-run]

Install (crontab dedup pattern — see docs/SPRINT-2026-09-15.md):
    (crontab -l 2>/dev/null | grep -v nevnew_schedule_runner; \\
     echo "* * * * * cd '<repo>' && /usr/bin/python3 scripts/scheduler_runner.py >> /tmp/nevnew_scheduler.log 2>&1 # nevnew_schedule_runner") | crontab -
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from zoneinfo import ZoneInfo

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import datetime as dt  # noqa: E402

import nevnew_schedules as store  # noqa: E402

TZ_NAME = os.environ.get("AICORE_TIMEZONE", "Asia/Bangkok")
TZ = ZoneInfo(TZ_NAME)
DB_PATH = os.path.join(REPO, "data", "schedules", "schedules.db")


def load_env() -> dict:
    vals: dict = {}
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_OWNER_ID"):
        vals[name] = os.environ.get(name, "")
    env_path = os.path.join(REPO, ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k in vals and not vals[k]:
                    vals[k] = v.strip()
    return vals


def tg_send(token: str, chat_id: str, text: str, dry_run: bool) -> None:
    if dry_run:
        print(f"[dry-run] would send to {chat_id}:\n{text}")
        return
    body = json.dumps({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=30).read()


def resolve_chat_id(user_id: str, owner_id: str) -> str:
    prefix = "telegram:"
    if user_id.startswith(prefix):
        chat_id = user_id[len(prefix):].strip()
        if chat_id:
            return chat_id
    return owner_id


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    env = load_env()
    if not env["TELEGRAM_BOT_TOKEN"] or not env["TELEGRAM_OWNER_ID"]:
        sys.exit("missing in env/.env: TELEGRAM_BOT_TOKEN, TELEGRAM_OWNER_ID")

    conn = store.connect(DB_PATH)
    try:
        now = dt.datetime.now(TZ)
        due = store.due_schedules(conn, now)
        if not due:
            print("no due schedules")
            return
        fired = 0
        for row in due:
            chat_id = resolve_chat_id(row["user_id"], env["TELEGRAM_OWNER_ID"])
            text = f"Reminder: {row['description']}"
            try:
                tg_send(env["TELEGRAM_BOT_TOKEN"], chat_id, text, dry_run)
            except Exception as exc:  # noqa: BLE001 — keep going; log and retry next run
                print(f"FAILED to send schedule {row['id']}: {exc}")
                continue
            if not dry_run:
                store.mark_fired(conn, row, TZ, now)
            fired += 1
        print(f"fired {fired}/{len(due)} due schedule(s)")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
