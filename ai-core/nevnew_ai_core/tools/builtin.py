"""Built-in tools that ship with AI Core.

Both are safe, dependency-free and genuinely useful for a personal
assistant: knowing the current date/time (models have no clock) and letting
the model actively search the user's long-term memory beyond what was
pre-injected into the system prompt.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

from ..memory_client import MemoryServiceClient
from .base import Tool, ToolContext

logger = logging.getLogger("nevnew-ai-core")


class GetCurrentDatetimeTool(Tool):
    name = "get_current_datetime"
    description = (
        "Get the current date and time in the user's timezone (ISO-8601 plus a "
        "human-readable form). Use this whenever the current date/time matters "
        "— you have no internal clock. Takes no arguments."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    source = "builtin"

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> str:
        tz = ZoneInfo(context.settings.timezone)
        now = datetime.now(tz)
        return (
            f"{now.isoformat(timespec='seconds')}\n"
            f"Human-readable: {now.strftime('%A, %d %B %Y, %H:%M (%Z)')}"
        )


class SearchUserMemoriesTool(Tool):
    name = "search_user_memories"
    description = (
        "Search this user's long-term memory (facts learned from earlier "
        "conversations). Use it when the automatically provided memory notes "
        "are not enough to answer something personal about the user."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to look up, phrased like a fact (e.g. 'favorite food', 'work project').",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "description": "Maximum number of memories to return (default 5).",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }
    source = "builtin"

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> str:
        query = str(arguments.get("query", "")).strip()
        if not query:
            raise ValueError("query must be a non-empty string")
        try:
            limit = int(arguments.get("limit", 5))
        except (TypeError, ValueError):
            limit = 5
        limit = max(1, min(limit, 20))

        client: MemoryServiceClient = context.memory_client
        hits = await client.search(context.user_id, query, limit)
        if not hits:
            return "No matching memories found."
        lines = []
        for hit in hits:
            text = str(hit.get("memory", "")).strip()
            score = hit.get("score")
            if not text:
                continue
            if isinstance(score, (int, float)):
                lines.append(f"- {text} (relevance {float(score):.2f})")
            else:
                lines.append(f"- {text}")
        return "\n".join(lines) if lines else "No matching memories found."


def builtin_tools() -> List[Tool]:
    return [GetCurrentDatetimeTool(), SearchUserMemoriesTool()]
