"""Built-in tools that ship with AI Core.

All are safe and genuinely useful for a personal assistant: knowing the
current date/time (models have no clock), letting the model actively search
the user's long-term memory beyond what was pre-injected into the system
prompt, and searching the public web for current/external information.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import httpx

from ..config import Settings
from ..memory_client import MemoryServiceClient
from .base import Tool, ToolContext
from .websearch import WebSearchTool

logger = logging.getLogger("nevnew-ai-core")

# Repo the error-watcher (scripts/error_watcher.py) files [auto] issues
# against — kept in sync with that script's own REPO constant.
_STATUS_GH_REPO = "GRITui/nevnew"


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


def _service_healthy(info: Any) -> Optional[bool]:
    """Best-effort truthiness for one entry of the watchdog's status JSON.

    Schema isn't pinned down here (the watchdog lives outside the repo at
    `~/ops/nevnew_selfheal.py` — see BACKLOG.md "Self-heal loop" entry) so
    this stays defensive: bool, common status strings, or a nested dict with
    an ok/healthy/status field. Returns None when it can't tell.
    """
    if isinstance(info, bool):
        return info
    if isinstance(info, str):
        return info.strip().lower() in ("ok", "healthy", "up", "running", "true")
    if isinstance(info, dict):
        for key in ("healthy", "ok", "up"):
            if key in info:
                return bool(info[key])
        status = info.get("status")
        if isinstance(status, str):
            return status.strip().lower() in ("ok", "healthy", "up", "running")
    return None


class StatusTool(Tool):
    name = "status"
    description = (
        "Self-diagnostics for the NevNew stack itself: container/service "
        "health, host disk space, LiteLLM spend for the current budget "
        "period + fallback readiness, mcpo tool-discovery state, and any "
        "open auto-filed error-watcher issues. Use this when asked for a "
        "status check, health check, or 'how are you running' in an "
        "operational sense. Each check degrades independently — a down "
        "subsystem is reported as unavailable, not a tool failure. Takes "
        "no arguments."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    source = "builtin"

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> str:
        labels = [
            "Containers",
            "Disk",
            "LiteLLM budget/fallback",
            "mcpo tools",
            "Error watcher",
        ]
        results = await asyncio.gather(
            self._container_health(context),
            self._disk_free(),
            self._litellm_budget(context),
            self._mcpo_state(context),
            self._error_watcher_findings(),
            return_exceptions=True,
        )
        lines = []
        for label, result in zip(labels, results):
            if isinstance(result, BaseException):
                logger.warning("status check %s crashed: %s", label, result, exc_info=result)
                lines.append(f"- {label}: unavailable ({type(result).__name__}: {result})")
            else:
                lines.append(f"- {label}: {result}")
        return "\n".join(lines)

    async def _container_health(self, context: ToolContext) -> str:
        path = getattr(context.settings, "host_status_path", None)
        if not path:
            return (
                "unavailable (no AICORE_HOST_STATUS_PATH configured — the "
                "host watchdog's ~/ops/nevnew-status.json isn't mounted "
                "into this container)"
            )
        try:
            raw = await asyncio.to_thread(Path(path).read_text)
            data = json.loads(raw)
        except FileNotFoundError:
            return f"unavailable (host status file not found at {path})"
        except OSError as exc:
            return f"unavailable (could not read host status file: {exc})"
        except ValueError as exc:
            return f"unavailable (host status file is not valid JSON: {exc})"

        services = data.get("services") if isinstance(data, dict) else None
        if not isinstance(services, dict) or not services:
            return "unavailable (host status file has no recognizable 'services' section)"

        healthy, unhealthy, unknown = [], [], []
        for name, info in services.items():
            verdict = _service_healthy(info)
            if verdict is True:
                healthy.append(name)
            elif verdict is False:
                unhealthy.append(name)
            else:
                unknown.append(name)

        summary = f"{len(healthy)}/{len(services)} healthy"
        if unhealthy:
            summary += f" — down: {', '.join(sorted(unhealthy)[:8])}"
        if unknown:
            summary += f" — unknown: {', '.join(sorted(unknown)[:8])}"
        generated_at = data.get("generated_at") or data.get("timestamp") if isinstance(data, dict) else None
        if generated_at:
            summary += f" (as of {generated_at})"
        return summary

    async def _disk_free(self) -> str:
        # Measures this container's filesystem, which on the Docker Desktop
        # host (macOS) shares the VM disk image with every other service —
        # a reasonable proxy for the host disk that hit 94% full before
        # (see BACKLOG.md), though not a byte-exact match for `df` on macOS.
        try:
            usage = await asyncio.to_thread(shutil.disk_usage, "/")
        except OSError as exc:
            return f"unavailable ({exc})"
        if usage.total <= 0:
            return "unavailable (disk_usage reported zero total)"
        free_gb = usage.free / (1024**3)
        total_gb = usage.total / (1024**3)
        pct_used = 100.0 * usage.used / usage.total
        warning = ""
        if pct_used >= 90:
            warning = " — LOW DISK, Docker Desktop degrades badly near/above this (see BACKLOG.md)"
        return f"{free_gb:.1f}GB free of {total_gb:.1f}GB ({pct_used:.0f}% used){warning}"

    async def _litellm_budget(self, context: ToolContext) -> str:
        settings: Settings = context.settings
        base = settings.litellm_base_url
        if base.endswith("/v1"):
            base = base[: -len("/v1")]
        url = f"{base}/global/spend"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(
                    url, headers={"Authorization": f"Bearer {settings.litellm_api_key}"}
                )
        except httpx.HTTPError as exc:
            return f"unavailable (LiteLLM proxy unreachable: {exc})"
        if response.status_code != 200:
            return f"unavailable (proxy reachable, spend query returned HTTP {response.status_code})"
        try:
            data = response.json()
        except ValueError:
            return "unavailable (proxy reachable, spend response was not JSON)"

        spend: Optional[float] = None
        if isinstance(data, (int, float)):
            spend = float(data)
        elif isinstance(data, dict):
            for key in ("spend", "total_spend"):
                raw_value = data.get(key)
                if raw_value is not None:
                    try:
                        spend = float(raw_value)
                    except (TypeError, ValueError):
                        pass
                    break

        # Fallback ladder is static config (config.yaml `fallbacks:`), not a
        # live query — reported as "configured", not "active", unless a
        # dynamic failover signal exists.
        fallback_note = "fallback ladder configured (NevNew -> Mini/Haiku -> Pro)"
        if spend is None:
            return f"proxy reachable, spend total unrecognized; {fallback_note}"
        return f"${spend:.2f} spent this budget period; {fallback_note}"

    async def _mcpo_state(self, context: ToolContext) -> str:
        registry = context.registry
        if registry is None:
            return "unavailable (tool registry not wired into this request context)"
        try:
            return registry.mcpo_status()
        except Exception as exc:  # noqa: BLE001 — must never break the status check
            return f"unavailable ({exc})"

    async def _error_watcher_findings(self) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                "gh",
                "issue",
                "list",
                "--repo",
                _STATUS_GH_REPO,
                "--label",
                "bug",
                "--state",
                "open",
                "--search",
                "[auto] in:title",
                "--json",
                "number,title",
                "--limit",
                "10",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return "unavailable (gh CLI not installed in this environment)"
        except OSError as exc:
            return f"unavailable (could not launch gh CLI: {exc})"

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        except asyncio.TimeoutError:
            proc.kill()
            return "unavailable (gh CLI timed out)"

        if proc.returncode != 0:
            snippet = stderr.decode(errors="replace")[:200].strip()
            return f"unavailable (gh CLI error: {snippet})"

        try:
            issues = json.loads(stdout.decode() or "[]")
        except ValueError:
            return "unavailable (gh CLI returned non-JSON output)"

        if not isinstance(issues, list) or not issues:
            return "no open [auto] error-watcher issues"

        titles = "; ".join(
            f"#{item.get('number')} {str(item.get('title', ''))[:60]}"
            for item in issues[:5]
            if isinstance(item, dict)
        )
        more = f" (+{len(issues) - 5} more)" if len(issues) > 5 else ""
        return f"{len(issues)} open — {titles}{more}"


def builtin_tools(settings: Settings) -> List[Tool]:
    return [
        GetCurrentDatetimeTool(),
        SearchUserMemoriesTool(),
        StatusTool(),
        WebSearchTool(
            provider=settings.web_search_provider,
            timeout_seconds=settings.web_search_timeout_seconds,
            default_max_results=settings.web_search_max_results,
        ),
    ]
