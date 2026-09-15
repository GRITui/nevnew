"""Natural-language scheduling tools (issue #62).

`schedule_task` / `list_schedules` / `cancel_schedule` write/read a shared
SQLite table (see `nevnew_ai_core/schedules_store.py`) that lives on a bind
mount (`/data/schedules/schedules.db`, see docker-compose.yml). These tools
only manage schedule ROWS — they never fire a reminder themselves. Firing
(and the Telegram push) is done by `scripts/scheduler_runner.py`, invoked by
system cron on the host, following the same pattern already established by
the proactive-briefing engine (issue #65): n8n's ExecuteCommand hangs Python
in its task-runner sandbox, so recurring background work goes through system
cron talking to ai-core's /chat + the Telegram Bot API directly, not through
an n8n Schedule Trigger workflow.
"""

from __future__ import annotations

from typing import Any, Dict, List

from .. import schedules_store as store
from ..config import Settings
from .base import Tool, ToolContext


def _connect(context: ToolContext):
    return store.connect(context.settings.schedules_db_path)


class ScheduleTaskTool(Tool):
    name = "schedule_task"
    description = (
        "Create a reminder that fires later and pushes a Telegram message — "
        "either once (`when`) or recurring (`rrule`). Give exactly one of "
        "`when`/`rrule`. Recurrence is a small subset: "
        "'FREQ=WEEKLY;BYDAY=MO;BYHOUR=9;BYMINUTE=0' (every Monday 9:00), "
        "'FREQ=WEEKLY;BYDAY=MO,WE,FR;BYHOUR=18;BYMINUTE=30', or "
        "'FREQ=DAILY;BYHOUR=9;BYMINUTE=0' (every day at 9:00). BYDAY uses "
        "MO/TU/WE/TH/FR/SA/SU. Use get_current_datetime first if you need to "
        "reason about 'today'/'tomorrow' relative to now."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "What to remind the user about, e.g. 'check the dashboard'.",
            },
            "when": {
                "type": "string",
                "description": "One-off due date/time, 'YYYY-MM-DD HH:MM' (24h, user's local timezone). Omit if using rrule.",
            },
            "rrule": {
                "type": "string",
                "description": "Recurring rule subset, e.g. 'FREQ=WEEKLY;BYDAY=MO;BYHOUR=9;BYMINUTE=0'. Omit if using when.",
            },
        },
        "required": ["description"],
        "additionalProperties": False,
    }
    source = "builtin"

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> str:
        import asyncio
        from zoneinfo import ZoneInfo

        description = str(arguments.get("description", "")).strip()
        when = arguments.get("when")
        rrule = arguments.get("rrule")
        when = str(when).strip() if when else None
        rrule = str(rrule).strip() if rrule else None

        settings: Settings = context.settings
        tz = ZoneInfo(settings.timezone)

        def _do() -> Dict[str, Any]:
            conn = _connect(context)
            try:
                return store.add_schedule(conn, context.user_id, description, when, rrule, tz)
            finally:
                conn.close()

        try:
            result = await asyncio.to_thread(_do)
        except store.ScheduleError as exc:
            raise ValueError(str(exc)) from exc

        return (
            f"Scheduled: {result['description']} "
            f"(id {result['id']}, rule {result['rule']}, next run {result['next_run']})"
        )


class ListSchedulesTool(Tool):
    name = "list_schedules"
    description = (
        "List this user's schedules (active by default). Use before "
        "cancelling if you need the schedule id."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "include_cancelled": {
                "type": "boolean",
                "description": "Also include cancelled/done schedules (default false).",
            },
        },
        "additionalProperties": False,
    }
    source = "builtin"

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> str:
        import asyncio

        include_cancelled = bool(arguments.get("include_cancelled", False))

        def _do() -> List[Dict[str, Any]]:
            conn = _connect(context)
            try:
                return store.list_schedules(conn, context.user_id, include_cancelled)
            finally:
                conn.close()

        rows = await asyncio.to_thread(_do)
        if not rows:
            return "No schedules found."
        lines = []
        for row in rows:
            lines.append(
                f"- [{row['id']}] {row['description']} — {row['rule']} "
                f"(next {row['next_run']}, status {row['status']})"
            )
        return "\n".join(lines)


class CancelScheduleTool(Tool):
    name = "cancel_schedule"
    description = (
        "Cancel one or more of this user's active schedules. Give exactly "
        "one of `schedule_id` (from list_schedules) or `description_contains` "
        "(case-insensitive substring match against the description — cancels "
        "ALL matches, so prefer schedule_id when precision matters)."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "schedule_id": {
                "type": "string",
                "description": "Exact schedule id from list_schedules.",
            },
            "description_contains": {
                "type": "string",
                "description": "Cancel all active schedules whose description contains this text.",
            },
        },
        "additionalProperties": False,
    }
    source = "builtin"

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> str:
        import asyncio

        schedule_id = arguments.get("schedule_id")
        description_contains = arguments.get("description_contains")
        schedule_id = str(schedule_id).strip() if schedule_id else None
        description_contains = (
            str(description_contains).strip() if description_contains else None
        )

        def _do() -> List[Dict[str, Any]]:
            conn = _connect(context)
            try:
                return store.cancel_schedule(
                    conn, context.user_id, schedule_id, description_contains
                )
            finally:
                conn.close()

        try:
            cancelled = await asyncio.to_thread(_do)
        except store.ScheduleError as exc:
            raise ValueError(str(exc)) from exc

        if not cancelled:
            return "No matching active schedule found."
        names = ", ".join(f"{row['description']} ({row['id']})" for row in cancelled)
        return f"Cancelled {len(cancelled)} schedule(s): {names}"
