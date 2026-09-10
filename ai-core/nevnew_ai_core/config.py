"""Environment-driven configuration for the NevNew AI Core service (issue #39)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger("nevnew-ai-core")

# Hard ceiling for the tool-loop iteration cap regardless of configuration —
# a runaway model must never be able to spend unbounded round-trips.
ABSOLUTE_MAX_TOOL_ITERATIONS = 12


def _env_str(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _env_int(name: str, default: int) -> int:
    raw = _env_str(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = _env_str(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number, got {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of the AI Core configuration."""

    litellm_base_url: str
    litellm_api_key: str
    model_name: str

    memory_base_url: str
    memory_api_key: Optional[str]

    mcpo_base_url: Optional[str]
    mcpo_api_key: Optional[str]
    mcpo_refresh_seconds: int
    mcpo_timeout_seconds: float

    api_key: Optional[str]

    max_tool_iterations: int
    litellm_timeout_seconds: float
    litellm_retries: int
    memory_search_timeout_seconds: float
    memory_add_timeout_seconds: float

    memory_top_k: int
    history_max_messages: int
    max_message_chars: int
    tool_result_max_chars: int
    mcpo_max_tools: int

    timezone: str

    @classmethod
    def from_env(cls) -> "Settings":
        litellm_base_url = _env_str("LITELLM_BASE_URL", "http://litellm:4000/v1")
        litellm_api_key = _env_str("LITELLM_API_KEY") or _env_str("LITELLM_MASTER_KEY")
        if not litellm_api_key:
            raise RuntimeError(
                "LITELLM_API_KEY (or LITELLM_MASTER_KEY) is required — AI Core "
                "calls the LiteLLM proxy for all model traffic."
            )

        mcpo_base_url = _env_str("MCPO_BASE_URL", "http://mcpo:8000")
        mcpo_refresh_seconds = _env_int("MCPO_REFRESH_SECONDS", 300)
        if mcpo_refresh_seconds < 0:
            raise RuntimeError("MCPO_REFRESH_SECONDS must be >= 0 (0 disables auto-refresh)")

        max_tool_iterations = _env_int("AICORE_MAX_TOOL_ITERATIONS", 6)
        if not 1 <= max_tool_iterations <= ABSOLUTE_MAX_TOOL_ITERATIONS:
            raise RuntimeError(
                f"AICORE_MAX_TOOL_ITERATIONS must be 1-{ABSOLUTE_MAX_TOOL_ITERATIONS}, got {max_tool_iterations}"
            )

        retries = _env_int("AICORE_LITELLM_RETRIES", 2)
        if not 0 <= retries <= 5:
            raise RuntimeError("AICORE_LITELLM_RETRIES must be within 0-5")

        memory_top_k = _env_int("AICORE_MEMORY_TOP_K", 5)
        if not 1 <= memory_top_k <= 20:
            raise RuntimeError("AICORE_MEMORY_TOP_K must be within 1-20")

        history_max = _env_int("AICORE_HISTORY_MAX_MESSAGES", 40)
        if not 1 <= history_max <= 200:
            raise RuntimeError("AICORE_HISTORY_MAX_MESSAGES must be within 1-200")

        max_message_chars = _env_int("AICORE_MAX_MESSAGE_CHARS", 32000)
        if max_message_chars < 1000:
            raise RuntimeError("AICORE_MAX_MESSAGE_CHARS must be >= 1000")

        tool_result_max_chars = _env_int("AICORE_TOOL_RESULT_MAX_CHARS", 8000)
        if tool_result_max_chars < 500:
            raise RuntimeError("AICORE_TOOL_RESULT_MAX_CHARS must be >= 500")

        mcpo_max_tools = _env_int("AICORE_MCPO_MAX_TOOLS", 50)
        if not 1 <= mcpo_max_tools <= 200:
            raise RuntimeError("AICORE_MCPO_MAX_TOOLS must be within 1-200")

        timezone = _env_str("AICORE_TIMEZONE", "Asia/Bangkok")
        try:
            ZoneInfo(timezone)
        except Exception as exc:
            raise RuntimeError(
                f"AICORE_TIMEZONE is not a valid IANA timezone: {timezone!r} ({exc})"
            ) from exc

        return cls(
            litellm_base_url=litellm_base_url.rstrip("/"),
            litellm_api_key=litellm_api_key,
            model_name=_env_str("NEVNEW_MODEL_NAME", "NevNew"),
            memory_base_url=(_env_str("MEMORY_BASE_URL", "http://memory:8000") or "").rstrip("/"),
            memory_api_key=_env_str("MEMORY_API_KEY"),
            mcpo_base_url=(mcpo_base_url or "").rstrip("/") or None,
            mcpo_api_key=_env_str("MCPO_API_KEY"),
            mcpo_refresh_seconds=mcpo_refresh_seconds,
            mcpo_timeout_seconds=_env_float("AICORE_MCPO_TIMEOUT_SECONDS", 30.0),
            api_key=_env_str("AICORE_API_KEY"),
            max_tool_iterations=max_tool_iterations,
            litellm_timeout_seconds=_env_float("AICORE_LITELLM_TIMEOUT_SECONDS", 120.0),
            litellm_retries=retries,
            memory_search_timeout_seconds=_env_float("AICORE_MEMORY_SEARCH_TIMEOUT", 15.0),
            memory_add_timeout_seconds=_env_float("AICORE_MEMORY_ADD_TIMEOUT", 240.0),
            memory_top_k=memory_top_k,
            history_max_messages=history_max,
            max_message_chars=max_message_chars,
            tool_result_max_chars=tool_result_max_chars,
            mcpo_max_tools=mcpo_max_tools,
            timezone=timezone,
        )
