"""The AI Core chat pipeline (issue #39): pre-execution assembly, the tool
loop, and post-execution hooks.

Pre-execution
-------------
1. Persona: imported from callbacks/nevnew_persona_prompt.py (mounted
   read-only into the container — same pattern as telegram-bot). We always
   send our own system message, so LiteLLM's `nevnew_persona` pre-call hook
   sees a system message already present and leaves it alone — the persona
   is applied exactly once, no duplication.
2. Memories: the last user message is used to retrieve the user's top
   memories (memory service) and they are injected into the system message.
   Retrieval failure degrades gracefully (chat proceeds without memories).
3. Tool schemas: the registry's OpenAI function schemas are passed via the
   `tools` parameter, plus a short guidance block in the system message.

Tool loop
---------
Call LiteLLM chat completions with the tools; if the model returns
tool_calls, execute each (built-ins + mcpo), append the results as
`role:"tool"` messages, and loop — bounded by the iteration cap. When the
cap is hit, tools are OMITTED from the final call so the model must produce
a text answer. An empty-content response gets one "please answer in text"
nudge (also without tools) before the pipeline gives up with an error.

Post-execution
--------------
Handled by the endpoint (main.py): a background task submits the last user
message + final reply to the memory service for fact extraction.
"""

from __future__ import annotations

import importlib.util
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .config import Settings
from .litellm_client import LiteLLMClient, LiteLLMError
from .memory_client import MemoryServiceClient, MemoryServiceError
from .schemas import ChatRequest, ToolCallRecord
from .tools.base import ToolContext, ToolRegistry

logger = logging.getLogger("nevnew-ai-core")


class ChatPipelineError(Exception):
    """The chat could not be completed (model/backend failure)."""


def load_persona_prompt() -> str:
    """Load NEVNEW_SYSTEM_PROMPT without duplicating it.

    Primary path: `nevnew_persona_prompt` importable on sys.path (the
    compose snippet mounts ../../callbacks/nevnew_persona_prompt.py into
    /app). Fallback (local dev): NEVNEW_PERSONA_FILE env var pointing at
    the file.
    """
    try:
        from nevnew_persona_prompt import NEVNEW_SYSTEM_PROMPT  # type: ignore

        return NEVNEW_SYSTEM_PROMPT
    except ImportError:
        pass

    path = os.environ.get("NEVNEW_PERSONA_FILE", "")
    if path and os.path.isfile(path):
        spec = importlib.util.spec_from_file_location("nevnew_persona_prompt", path)
        if spec is not None and spec.loader is not None:
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            prompt = getattr(module, "NEVNEW_SYSTEM_PROMPT", None)
            if isinstance(prompt, str) and prompt.strip():
                return prompt
    raise RuntimeError(
        "NEVNEW_SYSTEM_PROMPT not found. Mount callbacks/nevnew_persona_prompt.py "
        "to /app/nevnew_persona_prompt.py (see compose-snippet.yml) or set "
        "NEVNEW_PERSONA_FILE for local development."
    )


def compose_system_prompt(persona: str, memories: List[Dict[str, Any]], has_tools: bool) -> str:
    """Persona + retrieved memories + tool guidance, as one system message."""
    parts = [persona.rstrip()]

    if memories:
        lines = []
        for memory in memories:
            text = str(memory.get("memory", "")).strip()
            if not text:
                continue
            score = memory.get("score")
            if isinstance(score, (int, float)):
                lines.append(f"- {text} (relevance {float(score):.2f})")
            else:
                lines.append(f"- {text}")
        if lines:
            parts.append(
                "## Long-term memory about this user\n"
                "Facts remembered from earlier conversations. They may be "
                "incomplete or outdated — always trust what the user says "
                "right now over these notes.\n" + "\n".join(lines)
            )

    if has_tools:
        parts.append(
            "## Tools\n"
            "You can call the provided tools (function calling) when they "
            "help — e.g. to check the current date/time, look up the user's "
            "calendar/reminders, or search their long-term memory for "
            "personal context. Prefer tools over guessing facts. If a tool "
            "returns an error, tell the user plainly and continue helping "
            "without it. Always finish with a normal text answer."
        )
    return "\n\n".join(parts)


@dataclass
class ChatOutcome:
    reply: str
    iterations: int
    hit_iteration_cap: bool
    round_trips: int
    tool_calls: List[ToolCallRecord] = field(default_factory=list)
    memories: List[Dict[str, Any]] = field(default_factory=list)
    last_user_message: Optional[str] = None
class ChatPipeline:
    def __init__(
        self,
        settings: Settings,
        litellm: LiteLLMClient,
        memory_client: MemoryServiceClient,
        registry: ToolRegistry,
        persona_prompt: str,
    ):
        self._settings = settings
        self._litellm = litellm
        self._memory = memory_client
        self._registry = registry
        self._persona = persona_prompt

    # ------------------------------------------------------- pre-execution

    def _trim_content(self, content: str) -> str:
        limit = self._settings.max_message_chars
        if len(content) <= limit:
            return content
        return content[:limit] + "…[truncated]"

    async def _retrieve_memories(self, user_id: str, query: str) -> List[Dict[str, Any]]:
        try:
            return await self._memory.search(user_id, query, self._settings.memory_top_k)
        except MemoryServiceError as exc:
            logger.warning("Memory retrieval failed for user_id=%s — continuing without memories: %s", user_id, exc)
            return []

    # ------------------------------------------------------------- the loop

    async def run(self, request: ChatRequest) -> ChatOutcome:
        settings = self._settings
        max_iterations = request.max_iterations or settings.max_tool_iterations
        max_iterations = max(1, min(max_iterations, settings.max_tool_iterations))

        history = request.messages[-settings.history_max_messages :]
        last_user_message: Optional[str] = None
        for message in reversed(history):
            if message.role == "user":
                last_user_message = message.content.strip() or None
                break

        memories: List[Dict[str, Any]] = []
        if last_user_message:
            memories = await self._retrieve_memories(request.user_id, last_user_message)

        tool_schemas = self._registry.openai_schemas()
        system_content = compose_system_prompt(self._persona, memories, has_tools=bool(tool_schemas))

        messages: List[Dict[str, Any]] = [{"role": "system", "content": system_content}]
        for message in history:
            messages.append({"role": message.role, "content": self._trim_content(message.content)})

        context = ToolContext(user_id=request.user_id, memory_client=self._memory, settings=settings)

        iterations = 0
        round_trips = 0
        nudged = False
        tool_records: List[ToolCallRecord] = []

        while True:
            omit_tools = (iterations >= max_iterations) or nudged
            active_tools = None if omit_tools else tool_schemas
            try:
                data = await self._litellm.chat_completion(messages, tools=active_tools)
            except LiteLLMError as exc:
                raise ChatPipelineError(str(exc)) from exc
            round_trips += 1

            choices = data.get("choices") or []
            if not choices or not isinstance(choices[0], dict):
                raise ChatPipelineError("LiteLLM response has no choices")
            message = choices[0].get("message") or {}
            tool_calls = message.get("tool_calls") or []

            if tool_calls and not omit_tools:
                messages.append(
                    {
                        "role": "assistant",
                        "content": message.get("content") or "",
                        "tool_calls": tool_calls,
                    }
                )
                for tool_call in tool_calls:
                    function = tool_call.get("function") or {}
                    name = str(function.get("name") or "")
                    arguments_json = function.get("arguments")
                    if arguments_json is None:
                        arguments_json = "{}"
                    execution = await self._registry.execute(name, arguments_json, context)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": str(tool_call.get("id") or ""),
                            "name": name,
                            "content": execution.content,
                        }
                    )
                    tool_records.append(execution.record)
                iterations += 1
                continue

            reply = str(message.get("content") or "").strip()
            if reply:
                return ChatOutcome(
                    reply=reply,
                    iterations=iterations,
                    hit_iteration_cap=bool(iterations >= max_iterations and tool_records),
                    round_trips=round_trips,
                    tool_calls=tool_records,
                    memories=memories,
                    last_user_message=last_user_message,
                )

            # Empty content with no usable tool calls — nudge once, toolless.
            if not nudged:
                nudged = True
                logger.warning(
                    "Model returned empty content (user_id=%s, round_trips=%d) — nudging for a text answer.",
                    request.user_id,
                    round_trips,
                )
                messages.append(
                    {
                        "role": "system",
                        "content": "Please provide your final answer now as a plain text message.",
                    }
                )
                continue

            raise ChatPipelineError(
                "the model returned an empty response (no content, no tool calls) "
                f"after {round_trips} round-trip(s)"
            )

