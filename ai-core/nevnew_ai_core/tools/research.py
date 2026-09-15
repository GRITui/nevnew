"""Research agent tool (issue #82) — web search -> fetch -> synthesize.

Pipeline, invoked as a single tool call from the main chat loop:
    1. Web search the topic (reuses tools/websearch.py's provider logic).
    2. Fetch and extract plain text from the top N result pages.
    3. Offload the heavy synthesis work (reading N sources, writing a fully
       sourced report + a 5-bullet summary) to the tiered offload dispatcher
       (see `dispatch_offload` below) so the main chat model doesn't have to
       burn its own context/latency budget chewing through raw source text.
    4. Return the synthesized report + summary + source list to the calling
       model, which is expected to: (a) call the `append_note` tool (exposed
       via mcpo/n8n — see n8n/macos_tools.applescript) with the report body
       to persist it as a Notes.app note, and (b) present the 5-bullet
       summary in chat. This tool does not call append_note itself: mcpo
       tools are only visible to the model's own tool-calling loop, not to
       other built-in tools (see tools/base.py's ToolRegistry).

Offload dispatcher note: ai-core runs in a slim python:3.12-slim container
that does not have scripts/ or the opencode/cline CLIs mounted/installed, so
the opencode-go / cline CLI routes documented in scripts/offload.sh cannot
actually execute here. `dispatch_offload` therefore:
    1. tries scripts/offload.sh directly (NEVNEW_OFFLOAD_SCRIPT, default
       /app/scripts/offload.sh) in case an operator later mounts the repo
       root + CLIs into this container -- same `-c <class> --via <route>`
       contract, task piped over stdin;
    2. else falls back to the same "openai" HTTP route offload.sh itself
       defaults to (OPENAI_COMPAT_BASE_URL/OPENAI_COMPAT_API_KEY gateway,
       no CLI needed) -- this *is* one of offload.sh's own routes, just
       executed in-process instead of shelling out to the script.
Both paths are fail-closed like offload.sh: no text back -> RuntimeError.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from ..config import Settings
from .base import Tool, ToolContext
from .websearch import resolve_provider, run_web_search

logger = logging.getLogger("nevnew-ai-core")

_DEFAULT_OFFLOAD_SCRIPT = "/app/scripts/offload.sh"

# Mirrors scripts/offload.sh's class_spec() openai column: the gateway key
# is allowlisted to a single model regardless of task class (verified
# 2026-09-13 in that script's comments), so the openai fallback route always
# uses this model no matter which -c class was requested.
_OPENAI_FALLBACK_MODEL = "qwen3.8-27b-fp8"

_OFFLOAD_SYSTEM_PROMPT = (
    "You are an engineering offload worker. You receive coding and "
    "engineering subtasks delegated from a coordinating AI assistant. Solve "
    "the task directly and completely: write code/docs, explain reasoning "
    "concisely, avoid unnecessary preamble, and follow every explicit "
    "instruction in the task (output format, file markers, length limits) "
    "exactly. You cannot run tools or read files -- produce your answer as "
    "text only."
)


class _TextExtractor(HTMLParser):
    """Minimal HTML-to-text extractor (stdlib only, no bs4/lxml dependency).

    Drops script/style/nav/header/footer content and collapses whitespace --
    good enough for feeding article text to a synthesis model, not meant to
    preserve layout.
    """

    _SKIP_TAGS = {"script", "style", "noscript", "nav", "header", "footer", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._parts: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Any]) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data.strip():
            self._parts.append(data.strip())

    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self._parts)).strip()


def extract_text(raw_html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(raw_html)
    except Exception:  # noqa: BLE001 — malformed HTML must never crash research
        pass
    return parser.text()


async def _fetch_source(
    client: httpx.AsyncClient, url: str, title: str, max_chars: int
) -> Dict[str, str]:
    """Fetch one URL and return {"title", "url", "text"} or {"...", "error"}."""
    try:
        response = await client.get(url, follow_redirects=True)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if "text/html" not in content_type and "text" not in content_type:
            return {"title": title, "url": url, "error": f"unsupported content-type: {content_type}"}
        text = extract_text(response.text)
        if not text:
            return {"title": title, "url": url, "error": "no extractable text"}
        return {"title": title, "url": url, "text": text[:max_chars]}
    except Exception as exc:  # noqa: BLE001 — one bad source must not sink the batch
        return {"title": title, "url": url, "error": f"{type(exc).__name__}: {exc}"}


async def _run_offload_script(
    script_path: Path, task: str, task_class: str, via: str, timeout_seconds: float
) -> Optional[str]:
    """Try scripts/offload.sh directly. Returns None if the script is
    unavailable (not an error -- the caller falls back to the openai route);
    raises RuntimeError if the script exists but the worker call failed."""
    if not script_path.is_file() or not os.access(script_path, os.X_OK):
        return None
    proc = await asyncio.create_subprocess_exec(
        "bash",
        str(script_path),
        "-c",
        task_class,
        "--via",
        via,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(task.encode("utf-8")), timeout=timeout_seconds
        )
    except asyncio.TimeoutError as exc:
        proc.kill()
        raise RuntimeError(f"offload.sh timed out after {timeout_seconds}s") from exc
    if proc.returncode != 0:
        raise RuntimeError(
            f"offload.sh ({via}, -c {task_class}) exited {proc.returncode}: "
            f"{stderr.decode('utf-8', errors='replace')[:500]}"
        )
    text = stdout.decode("utf-8", errors="replace").strip()
    if not text:
        raise RuntimeError("offload.sh returned no text")
    return text


async def _run_openai_route(
    client: httpx.AsyncClient, task: str, max_tokens: int
) -> str:
    """Same "openai" HTTP route scripts/offload.sh defaults to — the only
    offload.sh route that needs no CLI, so the only one usable in this
    container as a fallback when scripts/offload.sh itself isn't mounted."""
    base_url = (
        os.environ.get("OPENAI_COMPAT_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or "https://gateway.9arm.co/v1"
    ).rstrip("/")
    api_key = (
        os.environ.get("OPENAI_COMPAT_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("NINEARM_API_KEY")
        or ""
    ).strip()
    if not api_key or api_key == "your-9arm-api-key-here":
        raise RuntimeError(
            "no offload route available: scripts/offload.sh is not mounted in this "
            "container and no OpenAI-compatible gateway key is set "
            "(OPENAI_COMPAT_API_KEY / OPENAI_API_KEY / NINEARM_API_KEY)"
        )
    response = await client.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": _OPENAI_FALLBACK_MODEL,
            "messages": [
                {"role": "system", "content": _OFFLOAD_SYSTEM_PROMPT},
                {"role": "user", "content": task},
            ],
            "temperature": 0.2,
            "max_tokens": max_tokens,
        },
    )
    if response.status_code >= 400:
        raise RuntimeError(f"openai offload route HTTP {response.status_code}: {response.text[:300]}")
    body = response.json()
    try:
        message = body["choices"][0]["message"]
        content = message.get("content") or message.get("reasoning_content")
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"openai offload route: unexpected response shape: {str(body)[:300]}")
    if not content or not content.strip():
        raise RuntimeError("openai offload route returned no text")
    return content


async def dispatch_offload(
    client: httpx.AsyncClient,
    task: str,
    *,
    task_class: str = "complex",
    via: str = "opencode-go",
    timeout_seconds: float = 240.0,
    max_tokens: int = 4096,
    script_path: Optional[Path] = None,
) -> str:
    """Run `task` through the tiered offload dispatcher. See module docstring."""
    script_path = script_path or Path(os.environ.get("NEVNEW_OFFLOAD_SCRIPT", _DEFAULT_OFFLOAD_SCRIPT))
    try:
        result = await _run_offload_script(script_path, task, task_class, via, timeout_seconds)
        if result is not None:
            return result
    except RuntimeError as exc:
        logger.warning("scripts/offload.sh (%s) failed, falling back to openai route: %s", via, exc)
    return await _run_openai_route(client, task, max_tokens)


_SUMMARY_MARKER = "===SUMMARY==="
_REPORT_MARKER = "===REPORT==="


def _build_synthesis_prompt(topic: str, sources: List[Dict[str, str]]) -> str:
    lines = [
        f'Research topic: "{topic}"',
        "",
        "You are given raw text extracted from several web sources below. "
        "Write two things from them:",
        "",
        f"1. A section starting with the exact marker line `{_SUMMARY_MARKER}` "
        "followed by EXACTLY 5 bullet points (start each with '- '), each one "
        "concise sentence, summarizing the most important findings for a chat "
        "reply.",
        f"2. A section starting with the exact marker line `{_REPORT_MARKER}` "
        "followed by a full, well-organized briefing in Markdown: headings, "
        "short paragraphs or bullets, and inline citations like [1], [2] that "
        "reference the numbered source list. End the report with a "
        "'Sources' section listing every numbered source as '[n] Title - URL'.",
        "",
        "Only use information present in the sources below (or clearly-labeled "
        "general knowledge if a source is thin). Do not fabricate facts or URLs.",
        "",
        "Sources:",
    ]
    for index, source in enumerate(sources, start=1):
        if "error" in source:
            lines.append(f"[{index}] {source['title']} - {source['url']} (fetch failed: {source['error']})")
            continue
        lines.append(f"[{index}] {source['title']} - {source['url']}")
        lines.append(source["text"])
        lines.append("")
    return "\n".join(lines)


def _parse_synthesis(raw: str) -> Dict[str, str]:
    summary_index = raw.find(_SUMMARY_MARKER)
    report_index = raw.find(_REPORT_MARKER)
    if summary_index == -1 or report_index == -1 or report_index < summary_index:
        # Worker didn't follow the marker format -- fail soft rather than
        # crash the tool call, so the calling model still gets usable text.
        return {"summary": "", "report": raw.strip()}
    summary = raw[summary_index + len(_SUMMARY_MARKER) : report_index].strip()
    report = raw[report_index + len(_REPORT_MARKER) :].strip()
    return {"summary": summary, "report": report}


class ResearchTopicTool(Tool):
    """Research a topic: web search -> fetch -> synthesize (issue #82)."""

    name = "research_topic"
    description = (
        "Research a topic in depth: searches the web, fetches the top source "
        "pages, and synthesizes a fully sourced briefing plus a 5-bullet "
        "summary. Use this for 'research X and summarize' style requests -- "
        "not for quick lookups (use web_search for those). After calling this, "
        "you MUST: (1) call the append_note tool with the returned report body "
        "as note_body so it is saved as a Notes.app note, and (2) present the "
        "returned 5-bullet summary to the user in chat."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "topic": {
                "type": "string",
                "description": "The topic or question to research, e.g. 'the new iPhone'.",
            },
            "max_sources": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "description": "Maximum number of web sources to fetch and synthesize (default 5).",
            },
        },
        "required": ["topic"],
        "additionalProperties": False,
    }
    source = "builtin"

    def __init__(self, settings: Settings):
        self._settings = settings
        self._search_client = httpx.AsyncClient(timeout=settings.web_search_timeout_seconds)
        self._fetch_client = httpx.AsyncClient(
            timeout=settings.research_fetch_timeout_seconds,
            headers={"User-Agent": "nevnew-research-agent/1.0"},
        )
        self._offload_client = httpx.AsyncClient(timeout=settings.research_offload_timeout_seconds + 5)

    async def aclose(self) -> None:
        await self._search_client.aclose()
        await self._fetch_client.aclose()
        await self._offload_client.aclose()

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> str:
        topic = str(arguments.get("topic", "")).strip()
        if not topic:
            raise ValueError("topic must be a non-empty string")
        settings = self._settings
        try:
            max_sources = int(arguments.get("max_sources", settings.research_max_sources))
        except (TypeError, ValueError):
            max_sources = settings.research_max_sources
        max_sources = max(1, min(max_sources, 10))

        provider = resolve_provider(settings.web_search_provider)
        results = await run_web_search(provider, self._search_client, topic, max_sources)
        if not results:
            return f"No web results found for {topic!r} -- nothing to research."

        sources = await asyncio.gather(
            *(
                _fetch_source(self._fetch_client, item["url"], item["title"], settings.research_fetch_max_chars)
                for item in results
            )
        )
        usable = [source for source in sources if "text" in source]
        if not usable:
            return (
                f"Web search for {topic!r} returned {len(results)} result(s), but none of the "
                "source pages could be fetched -- try again or research a narrower topic."
            )

        prompt = _build_synthesis_prompt(topic, sources)
        try:
            raw = await dispatch_offload(
                self._offload_client,
                prompt,
                task_class=settings.research_offload_class,
                via=settings.research_offload_via,
                timeout_seconds=settings.research_offload_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 — surface as a tool error, don't crash the loop
            raise RuntimeError(f"research synthesis offload failed: {exc}") from exc

        parsed = _parse_synthesis(raw)
        summary = parsed["summary"] or "(offload worker did not return a bulleted summary -- see report below.)"
        report = parsed["report"]

        source_lines = "\n".join(
            f"[{index}] {source['title']} - {source['url']}"
            + (f" (fetch failed: {source['error']})" if "error" in source else "")
            for index, source in enumerate(sources, start=1)
        )

        return (
            f"Research on {topic!r} complete ({len(usable)}/{len(results)} sources fetched).\n\n"
            f"5-bullet chat summary (present this to the user):\n{summary}\n\n"
            "Full sourced report (pass this verbatim as note_body to the append_note tool, "
            f"e.g. noteName='Research: {topic}'):\n{report}\n\n"
            f"Sources:\n{source_lines}"
        )
