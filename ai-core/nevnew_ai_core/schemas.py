"""Pydantic request/response models for the AI Core API (issue #39)."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

# user_id must be path-safe (it appears in URL paths of the memory service)
# and whitespace-free (mem0 rejects whitespace ids). This charset covers
# Telegram numeric ids, Open-WebUI ids, UUIDs, emails and n8n channel ids.
USER_ID_PATTERN = r"^[A-Za-z0-9._@:-]{1,255}$"


class ChatMessage(BaseModel):
    """One OpenAI-style conversation message from the caller."""

    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=128000)

    @field_validator("content")
    @classmethod
    def _content_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must not be blank")
        return value


class ChatRequest(BaseModel):
    """POST /chat request body."""

    user_id: str = Field(
        description="Stable per-user id (used for memory isolation). "
        "Allowed: letters, digits, . _ @ : - (max 255).",
        pattern=USER_ID_PATTERN,
    )
    messages: List[ChatMessage] = Field(
        min_length=1,
        max_length=100,
        description="Conversation history (at least one user message expected).",
    )
    channel: Optional[str] = Field(
        default=None,
        max_length=32,
        description="Origin channel tag (e.g. 'telegram', 'n8n') — stored with extracted memories.",
    )
    store_memories: bool = Field(
        default=True,
        description="Whether to schedule post-call memory extraction for this turn.",
    )
    max_iterations: Optional[int] = Field(
        default=None,
        ge=1,
        le=12,
        description="Optional per-request tool-loop cap. Cannot exceed the server cap "
        "(AICORE_MAX_TOOL_ITERATIONS); higher values are clamped.",
    )


class ToolCallRecord(BaseModel):
    """One executed tool call, returned for observability."""

    name: str
    arguments: Dict[str, Any]
    result: str
    ok: bool
    duration_ms: int


class ChatResponse(BaseModel):
    """POST /chat response."""

    reply: str
    user_id: str
    model: str
    iterations: int = Field(description="Tool-execution rounds used (0 = direct answer).")
    hit_iteration_cap: bool
    round_trips: int = Field(description="Total LiteLLM round-trips made for this chat.")
    tool_calls: List[ToolCallRecord]
    memories_used: List[str] = Field(description="Memory texts injected into the system prompt.")
    memory_extraction_scheduled: bool


class ToolInfo(BaseModel):
    name: str
    source: str
    description: str
    parameters: Dict[str, Any]


class ToolsResponse(BaseModel):
    tools: List[ToolInfo]
    count: int
    mcpo_status: str
