"""Tool abstractions: Tool, ToolContext, and the ToolRegistry.

A Tool is anything with an OpenAI function schema plus an async executor.
The registry merges built-in tools with tools discovered from the mcpo
service (see mcpo.py) and exposes them as the `tools` array for LiteLLM
chat-completions calls.

Execution contract: `Tool.execute` returns the string content for the
`role:"tool"` message on success and RAISES on failure — the registry
catches every exception and turns it into an "ERROR: ..." tool result so
the model can recover mid-loop instead of the whole chat failing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..schemas import ToolCallRecord

logger = logging.getLogger("nevnew-ai-core")

# OpenAI function-name constraint: ^[a-zA-Z0-9_-]{1,64}$
_MAX_NAME_LENGTH = 64


@dataclass
class ToolContext:
    """Per-request context handed to every tool execution."""

    user_id: str
    memory_client: Any  # MemoryServiceClient (duck-typed to avoid a cycle)
    settings: Any  # Settings


class Tool(ABC):
    """Base class for all tools registered with the AI Core."""

    name: str = ""
    description: str = ""
    parameters: Dict[str, Any] = field(default_factory=dict)
    source: str = "builtin"

    def openai_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    @abstractmethod
    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> str:
        """Return the tool-result string. Raise on failure."""


@dataclass
class ToolExecution:
    record: ToolCallRecord
    content: str
class ToolRegistry:
    """Merges built-in tools with mcpo-discovered tools."""

    def __init__(self, builtins: List[Tool], mcpo: Optional[Any] = None):
        self._builtin: Dict[str, Tool] = {}
        for tool in builtins:
            if tool.name in self._builtin:
                raise ValueError(f"duplicate built-in tool name: {tool.name!r}")
            self._builtin[tool.name] = tool
        self._mcpo = mcpo
        self._mcpo_tools: Dict[str, Tool] = {}
        self._lock = asyncio.Lock()
        self._last_refresh_error: Optional[str] = None
        self._last_refresh_at: Optional[float] = None

    @property
    def mcpo_enabled(self) -> bool:
        return self._mcpo is not None

    def mcpo_status(self) -> str:
        if self._mcpo is None:
            return "disabled"
        if self._last_refresh_error:
            return f"error: {self._last_refresh_error}"
        if self._mcpo_tools:
            return f"ok ({len(self._mcpo_tools)} tools)"
        return "no tools discovered yet"

    async def refresh_mcpo(self) -> int:
        """(Re)discover tools from mcpo. Returns the tool count.

        Raises on failure — the periodic refresher catches and records it.
        """
        if self._mcpo is None:
            return 0
        tools = await self._mcpo.fetch_tools()
        table: Dict[str, Tool] = {}
        for tool in tools:
            name = tool.name
            if name in self._builtin or name in table:
                # Namespace collision with a built-in/other tool — prefix
                # rather than silently shadow or drop the tool.
                name = f"mcpo_{name}"[:_MAX_NAME_LENGTH]
                if name in self._builtin or name in table:
                    logger.warning("Skipping mcpo tool %r (name collision)", tool.name)
                    continue
                tool.name = name
            table[name] = tool
        async with self._lock:
            self._mcpo_tools = table
            self._last_refresh_error = None
            self._last_refresh_at = time.time()
        logger.info("mcpo refresh: %d tool(s) available", len(table))
        return len(table)

    async def record_refresh_failure(self, exc: Exception) -> None:
        async with self._lock:
            self._last_refresh_error = f"{type(exc).__name__}: {exc}"[:300]
            self._last_refresh_at = time.time()

    def openai_schemas(self) -> List[Dict[str, Any]]:
        return [tool.openai_schema() for tool in self.all_tools()]

    def all_tools(self) -> List[Tool]:
        tools = list(self._builtin.values())
        tools.extend(self._mcpo_tools.values())
        return tools

    def describe(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": tool.name,
                "source": tool.source,
                "description": tool.description,
                "parameters": tool.parameters,
            }
            for tool in self.all_tools()
        ]

    def _snapshot(self) -> Dict[str, Tool]:
        merged: Dict[str, Tool] = dict(self._mcpo_tools)
        merged.update(self._builtin)
        return merged
    async def execute(self, name: str, arguments_json: str, context: ToolContext) -> ToolExecution:
        """Execute a tool by name, never raising.

        Bad JSON arguments, unknown tools and tool exceptions all become
        error tool-results so the model can self-correct on the next turn.
        """
        started = time.monotonic()
        raw_arguments: Dict[str, Any] = {}
        parse_error: Optional[str] = None
        if arguments_json is not None and arguments_json.strip():
            try:
                parsed = json.loads(arguments_json)
                if isinstance(parsed, dict):
                    raw_arguments = parsed
                else:
                    parse_error = "arguments must be a JSON object"
            except ValueError as exc:
                parse_error = f"invalid JSON arguments: {exc}"
        # A missing/empty arguments string is fine: no-arg tools (and some
        # backends that send "" for them) land here.

        tool = self._snapshot().get(name) if parse_error is None else None
        if parse_error is not None or tool is None:
            message = (
                f"ERROR: {parse_error}"
                if parse_error is not None
                else f"ERROR: unknown tool {name!r} — available: {', '.join(sorted(self._snapshot()))}"
            )
            duration = int((time.monotonic() - started) * 1000)
            return ToolExecution(
                record=ToolCallRecord(
                    name=name, arguments={}, result=message, ok=False, duration_ms=duration
                ),
                content=message,
            )

        try:
            content = await tool.execute(raw_arguments, context)
            ok = True
        except Exception as exc:  # noqa: BLE001 — tools must never break the loop
            logger.warning("Tool %s failed: %s", name, exc, exc_info=True)
            content = f"ERROR: tool {name!r} failed: {exc}"[: context.settings.tool_result_max_chars]
            ok = False
        duration = int((time.monotonic() - started) * 1000)
        return ToolExecution(
            record=ToolCallRecord(
                name=name,
                arguments=raw_arguments,
                result=content[: context.settings.tool_result_max_chars],
                ok=ok,
                duration_ms=duration,
            ),
            content=content,
        )


