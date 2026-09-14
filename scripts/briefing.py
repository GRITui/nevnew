#!/usr/bin/env python3
"""Proactive briefing engine driver (issue #65).

Two modes, invoked by the n8n "NevNew Briefings" workflow via ExecuteCommand
(same pattern as scripts/offload_watch.py):
    briefing.py brief [--dry-run]   daily 07:00 morning brief
    briefing.py nudge [--dry-run]   every-15-min event nudge check

Design notes:
- All Calendar/Reminder/Memory context is collected by ai-core's own tool
  loop (mcpo tools, proven path). This script NEVER shells osascript: raw
  osascript hangs in headless sessions on macOS TCC permission prompts.
- Secrets come from the repo .env (AICORE_API_KEY, TELEGRAM_BOT_TOKEN,
  TELEGRAM_OWNER_ID). Nothing secret is baked into the n8n workflow JSON.
- Weather is Open-Meteo (keyless). Email is skipped until the Google
  bundle (#4/#44) lands — the brief says so instead of failing.
- Nudge uses a deterministic template (no model call): exactly-once via
  the state file. Quiet hours 23:00-07:00 Asia/Bangkok.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import urllib.request
from zoneinfo import ZoneInfo

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BKK = ZoneInfo("Asia/Bangkok")
# Bangkok coordinates (owner home). Open-Meteo is keyless.
LAT, LON = 13.7563, 100.5018
AICORE_URL = "http://localhost:8010"
STATE_PATH = os.path.join(REPO, "scripts", ".briefing_state.json")
QUIET_START, QUIET_END = 23, 7


def load_env() -> dict:
    vals: dict = {}
    for name in ("AICORE_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_OWNER_ID"):
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


def load_state() -> dict:
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"last_brief": "", "nudged": []}


def save_state(state: dict) -> None:
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


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


def aicore_chat(api_key: str, prompt: str, timeout: int) -> str:
    body = json.dumps(
        {
            "user_id": f"telegram:{ENV['TELEGRAM_OWNER_ID']}",
            "messages": [{"role": "user", "content": prompt}],
            "channel": "n8n-briefing",
            "store_memories": True,
        }
    ).encode()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(f"{AICORE_URL}/chat", data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)["reply"]


def fetch_weather() -> str:
    url = (
        "https://api.open-meteo.com/v1/forecast?latitude=13.7563&longitude=100.5018"
        "&current=temperature_2m,weather_code&daily=temperature_2m_max,"
        "temperature_2m_min,precipitation_probability_max&timezone=Asia%2FBangkok"
    )
    with urllib.request.urlopen(url, timeout=20) as resp:
        d = json.load(resp)
    cur, daily = d["current"], d["daily"]
    return (
        f"Bangkok now {cur['temperature_2m']}C (code {cur['weather_code']}), "
        f"today {daily['temperature_2m_min'][0]}-{daily['temperature_2m_max'][0]}C, "
        f"rain chance {daily['precipitation_probability_max'][0]}%"
    )


def do_brief(dry_run: bool) -> None:
    today = dt.datetime.now(BKK).strftime("%A %Y-%m-%d")
    weather = fetch_weather()
    prompt = (
        f"Compose my morning brief for {today} (Asia/Bangkok). "
        "Use your List_Calendar_Events and List_Reminders tools for today, "
        "and search my memories for anything relevant. "
        f"Current weather: {weather}. "
        "Email is not connected yet (Google bundle pending) — say so in one "
        "line instead of skipping silently. Keep it tight: weather, schedule, "
        "reminders, one memory nudge if relevant. Plain text, no markdown "
        "tables."
    )
    reply = aicore_chat(ENV["AICORE_API_KEY"], prompt, timeout=180)
    tg_send(ENV["TELEGRAM_BOT_TOKEN"], ENV["TELEGRAM_OWNER_ID"], reply, dry_run)
    if not dry_run:
        state = load_state()
        state["last_brief"] = dt.datetime.now(BKK).strftime("%Y-%m-%d")
        save_state(state)
    print("brief sent" if not dry_run else "brief dry-run done")


def do_nudge(dry_run: bool) -> None:
    now = dt.datetime.now(BKK)
    if now.hour >= QUIET_START or now.hour < QUIET_END:
        print(f"quiet hours ({now:%H:%M}) — skipping")
        return
    prompt = (
        "List my remaining calendar events for today as a bare JSON array, "
        'no prose: [{"title": "...", "start": "YYYY-MM-DD HH:MM"}]. '
        "Use your List_Calendar_Events tool. Empty array if none."
    )
    reply = aicore_chat(ENV["AICORE_API_KEY"], prompt, timeout=120)
    m = re.search(r"\[.*\]", reply, re.DOTALL)
    if not m:
        print("no event list parsed — skipping")
        return
    try:
        events = json.loads(m.group(0))
    except ValueError:
        print("event JSON unparseable — skipping")
        return
    state = load_state()
    nudged = set(state.get("nudged", []))
    window = now + dt.timedelta(minutes=30)
    sent = 0
    for ev in events:
        try:
            start = dt.datetime.strptime(ev["start"], "%Y-%m-%d %H:%M").replace(tzinfo=BKK)
        except (KeyError, ValueError):
            continue
        key = f"{ev.get('title', '?')}@{ev.get('start', '?')}"
        if now < start <= window and key not in nudged:
            mins = int((start - now).total_seconds() // 60)
            tg_send(
                ENV["TELEGRAM_BOT_TOKEN"],
                ENV["TELEGRAM_OWNER_ID"],
                f"⏰ In ~{mins} min: {ev.get('title', '(untitled)')}",
                dry_run,
            )
            sent += 1
            if not dry_run:
                nudged.add(key)
    if not dry_run:
        # Prune keys from previous days so the list stays small.
        today = now.strftime("%Y-%m-%d")
        state["nudged"] = [k for k in nudged if today in k]
        save_state(state)
    print(f"nudge check done, sent={sent}")


ENV = load_env()

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    dry = "--dry-run" in sys.argv
    missing = [k for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_OWNER_ID") if not ENV[k]]
    if missing:
        sys.exit(f"missing in env/.env: {', '.join(missing)}")
    if mode == "brief":
        do_brief(dry)
    elif mode == "nudge":
        do_nudge(dry)
    else:
        sys.exit("usage: briefing.py [brief|nudge] [--dry-run]")
