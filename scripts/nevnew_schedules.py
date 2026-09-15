#!/usr/bin/env python3
"""Shared schedules store for NevNew natural-language scheduling (issue #62).

A minimal SQLite-backed store for recurring/one-off reminders created by the
user through chat ("remind me every Monday 9am to check the dashboard").
Two callers share the SAME on-disk file (a bind mount, not a docker named
volume, so both sides see the identical path):

- ai-core's `schedule_task` / `list_schedules` / `cancel_schedule` tools
  (`ai-core/nevnew_ai_core/tools/scheduling.py`) run this logic INSIDE the
  ai-core container against `/data/schedules/schedules.db` (mounted from
  `./data/schedules` — see docker-compose.yml).
- `scripts/scheduler_runner.py` runs on the HOST via system cron (same
  pattern as `scripts/briefing.py` — n8n's ExecuteCommand hangs Python in
  its task-runner sandbox, so scheduled firing does NOT go through an n8n
  workflow) against `./data/schedules/schedules.db` directly.

ai-core's copy of this logic lives in
`ai-core/nevnew_ai_core/schedules_store.py` (Docker COPYs source at build
time, it cannot import a sibling from `scripts/` at runtime). Keep the two
IN SYNC on schema + rrule-subset semantics if you change either.

Recurrence: NOT full RFC 5545. A small, dependency-free subset (the repo has
no croniter/python-dateutil today — see requirements.txt) sufficient for
"every <day(s)> at <time>" and "every day at <time>":

    FREQ=WEEKLY;BYDAY=MO;BYHOUR=9;BYMINUTE=0
    FREQ=WEEKLY;BYDAY=MO,WE,FR;BYHOUR=18;BYMINUTE=30
    FREQ=DAILY;BYHOUR=9;BYMINUTE=0

INTERVAL is accepted only as 1 (default) — every-2-weeks etc. is out of
scope for this pass (see BACKLOG.md gap note).
"""

from __future__ import annotations

import datetime as dt
import re
import sqlite3
import uuid
from typing import Any, Dict, List, Optional

_WEEKDAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]  # index == Python weekday()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    description TEXT NOT NULL,
    rule TEXT NOT NULL,
    next_run TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    last_fired_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_schedules_user ON schedules(user_id, status);
CREATE INDEX IF NOT EXISTS idx_schedules_due ON schedules(status, next_run);
"""


class ScheduleError(ValueError):
    """Bad `when`/`rrule` input from the caller (model or user)."""


def connect(db_path: str) -> sqlite3.Connection:
    """Open (creating parent dir + schema if needed) the shared DB."""
    import os

    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def _parse_iso(value: str, tz: dt.tzinfo) -> dt.datetime:
    value = value.strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})$", value)
    if not m:
        raise ScheduleError(
            f"bad datetime {value!r} — want 'YYYY-MM-DD HH:MM' (24h, local time)"
        )
    y, mo, d, hh, mm = (int(g) for g in m.groups())
    try:
        return dt.datetime(y, mo, d, hh, mm, tzinfo=tz)
    except ValueError as exc:
        raise ScheduleError(f"bad datetime {value!r}: {exc}") from exc


def _parse_rrule(rrule: str) -> Dict[str, Any]:
    """Parse the FREQ=...;BYDAY=...;BYHOUR=...;BYMINUTE=... subset."""
    parts: Dict[str, str] = {}
    for chunk in rrule.strip().strip(";").split(";"):
        if not chunk:
            continue
        if "=" not in chunk:
            raise ScheduleError(f"bad rrule component {chunk!r} (want KEY=VALUE)")
        key, _, val = chunk.partition("=")
        parts[key.strip().upper()] = val.strip().upper()

    freq = parts.get("FREQ")
    if freq not in ("DAILY", "WEEKLY"):
        raise ScheduleError("rrule FREQ must be DAILY or WEEKLY")

    interval = parts.get("INTERVAL", "1")
    if interval != "1":
        raise ScheduleError(
            "rrule INTERVAL other than 1 is not supported yet "
            "(every-N-weeks/days) — file a follow-up if you need it"
        )

    try:
        byhour = int(parts.get("BYHOUR", "9"))
        byminute = int(parts.get("BYMINUTE", "0"))
    except ValueError as exc:
        raise ScheduleError("BYHOUR/BYMINUTE must be integers") from exc
    if not (0 <= byhour <= 23 and 0 <= byminute <= 59):
        raise ScheduleError("BYHOUR must be 0-23 and BYMINUTE 0-59")

    byday: List[str] = []
    if freq == "WEEKLY":
        raw_days = parts.get("BYDAY")
        if not raw_days:
            raise ScheduleError("rrule FREQ=WEEKLY requires BYDAY (e.g. BYDAY=MO,WE)")
        for day in raw_days.split(","):
            day = day.strip()
            if day not in _WEEKDAYS:
                raise ScheduleError(f"bad BYDAY value {day!r} (want MO/TU/WE/TH/FR/SA/SU)")
            byday.append(day)

    return {"freq": freq, "byhour": byhour, "byminute": byminute, "byday": byday}


def _next_occurrence(parsed: Dict[str, Any], after: dt.datetime) -> dt.datetime:
    """First occurrence strictly after `after` (same tzinfo as `after`)."""
    tz = after.tzinfo
    if parsed["freq"] == "DAILY":
        candidate = after.replace(
            hour=parsed["byhour"], minute=parsed["byminute"], second=0, microsecond=0
        )
        if candidate <= after:
            candidate += dt.timedelta(days=1)
        return candidate

    # WEEKLY
    target_weekdays = {_WEEKDAYS.index(d) for d in parsed["byday"]}
    for offset in range(0, 8):
        candidate_day = after + dt.timedelta(days=offset)
        if candidate_day.weekday() not in target_weekdays:
            continue
        candidate = candidate_day.replace(
            hour=parsed["byhour"], minute=parsed["byminute"], second=0, microsecond=0
        )
        if candidate > after:
            return candidate
    raise ScheduleError("could not compute next weekly occurrence (unreachable)")


def compute_first_run(
    when: Optional[str], rrule: Optional[str], tz: dt.tzinfo, now: Optional[dt.datetime] = None
) -> Dict[str, Any]:
    """Validate `when`/`rrule` and return {"rule": <str>, "next_run": <datetime>}.

    Exactly one of `when` (one-off, 'YYYY-MM-DD HH:MM') or `rrule`
    (recurring subset, see module docstring) must be given.
    """
    if bool(when) == bool(rrule):
        raise ScheduleError("give exactly one of `when` (one-off) or `rrule` (recurring)")
    now = now or dt.datetime.now(tz)

    if when:
        next_run = _parse_iso(when, tz)
        if next_run <= now:
            raise ScheduleError(f"`when` {when!r} is in the past")
        return {"rule": f"ONCE:{when.strip()}", "next_run": next_run}

    parsed = _parse_rrule(rrule)  # type: ignore[arg-type]
    next_run = _next_occurrence(parsed, now)
    return {"rule": f"RRULE:{rrule.strip().strip(';').upper()}", "next_run": next_run}


def add_schedule(
    conn: sqlite3.Connection,
    user_id: str,
    description: str,
    when: Optional[str],
    rrule: Optional[str],
    tz: dt.tzinfo,
) -> Dict[str, Any]:
    description = description.strip()
    if not description:
        raise ScheduleError("description must not be empty")
    plan = compute_first_run(when, rrule, tz)
    schedule_id = uuid.uuid4().hex[:12]
    now_iso = dt.datetime.now(tz).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO schedules (id, user_id, description, rule, next_run, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'active', ?)",
        (schedule_id, user_id, description, plan["rule"], plan["next_run"].isoformat(timespec="seconds"), now_iso),
    )
    conn.commit()
    return {
        "id": schedule_id,
        "description": description,
        "rule": plan["rule"],
        "next_run": plan["next_run"].isoformat(timespec="seconds"),
    }


def list_schedules(conn: sqlite3.Connection, user_id: str, include_cancelled: bool = False) -> List[Dict[str, Any]]:
    if include_cancelled:
        rows = conn.execute(
            "SELECT * FROM schedules WHERE user_id = ? ORDER BY next_run", (user_id,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM schedules WHERE user_id = ? AND status = 'active' ORDER BY next_run",
            (user_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def cancel_schedule(
    conn: sqlite3.Connection, user_id: str, schedule_id: Optional[str], description_contains: Optional[str]
) -> List[Dict[str, Any]]:
    """Cancel by id, or (fallback) every active schedule whose description
    contains `description_contains` (case-insensitive). Returns the
    cancelled rows (empty list if nothing matched)."""
    if bool(schedule_id) == bool(description_contains):
        raise ScheduleError("give exactly one of `schedule_id` or `description_contains`")
    if schedule_id:
        rows = conn.execute(
            "SELECT * FROM schedules WHERE id = ? AND user_id = ? AND status = 'active'",
            (schedule_id, user_id),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM schedules WHERE user_id = ? AND status = 'active' "
            "AND lower(description) LIKE ?",
            (user_id, f"%{description_contains.lower()}%"),
        ).fetchall()
    cancelled = [dict(row) for row in rows]
    if cancelled:
        ids = [row["id"] for row in cancelled]
        conn.executemany("UPDATE schedules SET status = 'cancelled' WHERE id = ?", [(i,) for i in ids])
        conn.commit()
    return cancelled


def due_schedules(conn: sqlite3.Connection, now: dt.datetime) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM schedules WHERE status = 'active' AND next_run <= ? ORDER BY next_run",
        (now.isoformat(timespec="seconds"),),
    ).fetchall()
    return [dict(row) for row in rows]


def mark_fired(conn: sqlite3.Connection, row: Dict[str, Any], tz: dt.tzinfo, now: dt.datetime) -> None:
    """Advance a recurring schedule's next_run, or retire a one-off."""
    fired_iso = now.isoformat(timespec="seconds")
    if row["rule"].startswith("RRULE:"):
        parsed = _parse_rrule(row["rule"][len("RRULE:"):])
        next_run = _next_occurrence(parsed, now)
        conn.execute(
            "UPDATE schedules SET next_run = ?, last_fired_at = ? WHERE id = ?",
            (next_run.isoformat(timespec="seconds"), fired_iso, row["id"]),
        )
    else:
        conn.execute(
            "UPDATE schedules SET status = 'done', last_fired_at = ? WHERE id = ?",
            (fired_iso, row["id"]),
        )
    conn.commit()
