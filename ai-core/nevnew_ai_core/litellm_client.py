"""Async client for the LiteLLM proxy's chat-completions API.

Retry policy: transport errors and 429/5xx are retried up to
`litellm_retries` times with exponential backoff + jitter. The proxy itself
already retries upstream (config.yaml: num_retries: 3, request_timeout: 60)
so this layer only covers proxy-side failures — hence a modest default of 2.
429s back off longer (they usually mean OpenRouter's shared daily cap is
hot, and hammering it makes things worse).
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("nevnew-ai-core")

_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


class LiteLLMError(Exception):
    """A chat-completions call failed after retries."""


class LiteLLMClient:
    def __init__(self, base_url: str, api_key: str, timeout_seconds: float, retries: int, model: str):
        self._chat_url = f"{base_url.rstrip('/')}/chat/completions"
        self._model = model
        self._retries = retries
        self._client = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=timeout_seconds,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def ping(self, timeout: float = 3.0) -> bool:
        """Cheap liveness check against the proxy (health/liveliness)."""
        try:
            base = self._chat_url.rsplit("/chat/completions", 1)[0]
            response = await self._client.get(f"{base}/health/liveliness", timeout=timeout)
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def chat_completion(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """One chat-completions round-trip. Returns the parsed JSON body.

        Raises LiteLLMError on persistent failure or malformed response.
        """
        payload: Dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        attempts = self._retries + 1
        last_error = "no attempt made"
        for attempt in range(attempts):
            try:
                response = await self._client.post(self._chat_url, json=payload)
            except httpx.HTTPError as exc:
                last_error = f"transport error: {exc}"
                if attempt < attempts - 1:
                    await self._backoff(attempt, rate_limited=False)
                    continue
                break

            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    raise LiteLLMError("LiteLLM returned non-JSON for a 200 response") from exc

            snippet = response.text[:300].replace("\n", " ")
            last_error = f"HTTP {response.status_code}: {snippet}"
            if response.status_code not in _RETRYABLE_STATUSES:
                break
            if attempt < attempts - 1:
                await self._backoff(attempt, rate_limited=(response.status_code == 429))

        raise LiteLLMError(f"LiteLLM chat completion failed after {attempts} attempt(s): {last_error}")

    async def _backoff(self, attempt: int, rate_limited: bool) -> None:
        base_delay = 4.0 if rate_limited else 1.5
        delay = min(base_delay * (2**attempt), 15.0)
        delay *= 0.5 + random.random()  # full jitter
        logger.warning("LiteLLM call failed (attempt %d) — retrying in %.1fs", attempt + 1, delay)
        await asyncio.sleep(delay)
