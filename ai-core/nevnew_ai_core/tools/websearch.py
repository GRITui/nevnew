"""Web search tool (built-in) — gives NevNew access to current/external
information it cannot know from training data or the user's memories.

Providers (selected by the WEB_SEARCH_PROVIDER env var, default "auto"):
  auto        tavily if TAVILY_API_KEY is set, else brave if BRAVE_API_KEY
              is set, else duckduckgo (keyless)
  duckduckgo  keyless, via the `ddgs` package (sync API -> run in a thread)
  tavily      POST https://api.tavily.com/search (Bearer TAVILY_API_KEY)
  brave       GET  https://api.search.brave.com/res/v1/web/search
              (X-Subscription-Token: BRAVE_API_KEY)

All providers return the same normalized shape:
    [{"title": str, "url": str, "snippet": str}, ...]

Failures raise RuntimeError — the registry turns that into an
"ERROR: ..." tool result so the model can recover mid-loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List

import httpx

from .base import Tool, ToolContext

logger = logging.getLogger("nevnew-ai-core")

PROVIDERS = ("auto", "duckduckgo", "tavily", "brave")

_TAVILY_URL = "https://api.tavily.com/search"
_BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"

# Per-snippet cap keeps tool results compact for the model's context.
_SNIPPET_MAX_CHARS = 400


def resolve_provider(requested: str) -> str:
    """Map a (possibly "auto") provider request to a concrete provider."""
    requested = (requested or "auto").strip().lower()
    if requested == "auto":
        if os.environ.get("TAVILY_API_KEY", "").strip():
            return "tavily"
        if os.environ.get("BRAVE_API_KEY", "").strip():
            return "brave"
        return "duckduckgo"
    if requested not in ("duckduckgo", "tavily", "brave"):
        raise RuntimeError(
            f"WEB_SEARCH_PROVIDER must be one of {', '.join(PROVIDERS)} (got {requested!r})"
        )
    return requested


def _clean(value: Any, limit: int = _SNIPPET_MAX_CHARS) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def _duckduckgo_search(query: str, max_results: int) -> List[Dict[str, str]]:
    """Keyless DuckDuckGo web results via the `ddgs` package (sync)."""
    try:
        from ddgs import DDGS
    except ImportError as exc:
        raise RuntimeError(
            "duckduckgo provider needs the 'ddgs' package (pip install ddgs)"
        ) from exc
    raw = DDGS().text(query, max_results=max_results)
    results: List[Dict[str, str]] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("href") or item.get("url") or "").strip()
        if not url:
            continue
        results.append(
            {
                "title": _clean(item.get("title"), 200),
                "url": url,
                "snippet": _clean(item.get("body") or item.get("snippet")),
            }
        )
    return results


async def _tavily_search(
    client: httpx.AsyncClient, api_key: str, query: str, max_results: int
) -> List[Dict[str, str]]:
    response = await client.post(
        _TAVILY_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json={"query": query, "max_results": max_results, "search_depth": "basic"},
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"tavily returned HTTP {response.status_code}: {response.text[:200]}"
        )
    data = response.json()
    results: List[Dict[str, str]] = []
    for item in data.get("results") or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        results.append(
            {
                "title": _clean(item.get("title"), 200),
                "url": url,
                "snippet": _clean(item.get("content") or item.get("snippet")),
            }
        )
    return results


async def _brave_search(
    client: httpx.AsyncClient, api_key: str, query: str, max_results: int
) -> List[Dict[str, str]]:
    response = await client.get(
        _BRAVE_URL,
        params={"q": query, "count": max_results},
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": api_key,
        },
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"brave returned HTTP {response.status_code}: {response.text[:200]}"
        )
    data = response.json()
    results: List[Dict[str, str]] = []
    for item in (data.get("web") or {}).get("results") or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        results.append(
            {
                "title": _clean(item.get("title"), 200),
                "url": url,
                "snippet": _clean(item.get("description")),
            }
        )
    return results


def format_results(query: str, provider: str, results: List[Dict[str, str]]) -> str:
    """Render normalized results as a compact numbered list for the model."""
    if not results:
        return f"No web results found for {query!r}."
    lines = [f"Web search results for {query!r} (provider: {provider}):"]
    for index, item in enumerate(results, start=1):
        lines.append(f"{index}. {item['title']}")
        lines.append(f"   URL: {item['url']}")
        if item["snippet"]:
            lines.append(f"   {item['snippet']}")
    return "\n".join(lines)


async def run_web_search(
    provider: str,
    client: httpx.AsyncClient,
    query: str,
    max_results: int,
) -> List[Dict[str, str]]:
    """Dispatch to the concrete provider. Shared by the tool and the
    POST /web_search endpoint (which needs the structured results)."""
    if provider == "duckduckgo":
        return await asyncio.to_thread(_duckduckgo_search, query, max_results)
    if provider == "tavily":
        api_key = os.environ.get("TAVILY_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("TAVILY_API_KEY is not set")
        return await _tavily_search(client, api_key, query, max_results)
    if provider == "brave":
        api_key = os.environ.get("BRAVE_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("BRAVE_API_KEY is not set")
        return await _brave_search(client, api_key, query, max_results)
    raise RuntimeError(f"unknown web search provider {provider!r}")


class WebSearchTool(Tool):
    """Search the public web for current/external information."""

    name = "web_search"
    description = (
        "Search the web for up-to-date or external information: news, current "
        "events, prices, weather, sports scores, or any fact that may be newer "
        "than your knowledge. Returns top results with titles, URLs and "
        "snippets. Use it whenever the answer depends on current information "
        "rather than on the user's personal context (for that use "
        "search_user_memories instead)."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query — a few keywords or a short question.",
            },
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "description": "Maximum number of results to return (default 5).",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }
    source = "builtin"

    def __init__(
        self,
        provider: str = "auto",
        timeout_seconds: float = 20.0,
        default_max_results: int = 5,
    ):
        self._provider = provider
        self._timeout_seconds = timeout_seconds
        self._default_max_results = default_max_results
        self._client = httpx.AsyncClient(timeout=timeout_seconds)

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def provider(self) -> str:
        """Concrete provider after 'auto' resolution (raises if misconfigured)."""
        return resolve_provider(self._provider)

    @property
    def client(self) -> httpx.AsyncClient:
        return self._client

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> str:
        query = str(arguments.get("query", "")).strip()
        if not query:
            raise ValueError("query must be a non-empty string")
        try:
            max_results = int(arguments.get("max_results", self._default_max_results))
        except (TypeError, ValueError):
            max_results = self._default_max_results
        max_results = max(1, min(max_results, 10))

        provider = self.provider
        results = await asyncio.wait_for(
            run_web_search(provider, self._client, query, max_results),
            timeout=self._timeout_seconds + 5,
        )
        return format_results(query, provider, results)

