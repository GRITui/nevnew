"""HTTP client for the NevNew memory service (memory/ module, issue #37).

AI Core talks to the memory layer over its REST API rather than importing
it: the memory container carries mem0 + torch + sentence-transformers
(~2 GB) and AI Core stays a slim FastAPI/httpx service. Timeouts differ
per operation — search is interactive (seconds), add runs an LLM
fact-extraction round-trip (can take a minute or more).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("nevnew-ai-core")


class MemoryServiceError(Exception):
    """The memory service call failed (network or non-2xx)."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class MemoryServiceClient:
    """Thin async wrapper over the memory service REST API."""

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str],
        search_timeout_seconds: float = 15.0,
        add_timeout_seconds: float = 240.0,
        general_timeout_seconds: float = 30.0,
    ):
        self._base_url = base_url.rstrip("/")
        self._search_url = f"{self._base_url}/users/{{user_id}}/memories/search"
        self._add_url = f"{self._base_url}/users/{{user_id}}/memories"
        headers: Dict[str, str] = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.AsyncClient(
            headers=headers,
            timeout=general_timeout_seconds,
        )
        self._search_timeout = search_timeout_seconds
        self._add_timeout = add_timeout_seconds

    async def aclose(self) -> None:
        await self._client.aclose()

    # ----------------------------------------------------------------- ops

    async def ping(self, timeout: float = 3.0) -> bool:
        """Cheap liveness check used by /ready."""
        try:
            response = await self._client.get(f"{self._base_url}/health", timeout=timeout)
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def search(self, user_id: str, query: str, limit: int) -> List[Dict[str, Any]]:
        """Return ranked memories for a user (raises MemoryServiceError)."""
        url = self._search_url.format(user_id=user_id)
        try:
            response = await self._client.get(
                url,
                params={"query": query, "limit": limit},
                timeout=self._search_timeout,
            )
        except httpx.HTTPError as exc:
            raise MemoryServiceError(f"memory service unreachable: {exc}") from exc
        if response.status_code != 200:
            raise MemoryServiceError(
                f"memory search failed (HTTP {response.status_code}): {response.text[:300]}",
                status=response.status_code,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise MemoryServiceError("memory service returned invalid JSON") from exc
        results = payload.get("results")
        return results if isinstance(results, list) else []

    async def add(
        self,
        user_id: str,
        messages: List[Dict[str, str]],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Submit a conversation turn for background memory extraction."""
        url = self._add_url.format(user_id=user_id)
        body: Dict[str, Any] = {"messages": messages}
        if metadata is not None:
            body["metadata"] = metadata
        try:
            response = await self._client.post(url, json=body, timeout=self._add_timeout)
        except httpx.HTTPError as exc:
            raise MemoryServiceError(f"memory service unreachable: {exc}") from exc
        if response.status_code != 200:
            raise MemoryServiceError(
                f"memory add failed (HTTP {response.status_code}): {response.text[:300]}",
                status=response.status_code,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise MemoryServiceError("memory service returned invalid JSON") from exc
        results = payload.get("results")
        return results if isinstance(results, list) else []

    async def forward(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> httpx.Response:
        """Raw pass-through used by the /memory/* proxy endpoints."""
        url = f"{self._base_url}{path}"
        try:
            return await self._client.request(method, url, params=params, json=json_body)
        except httpx.HTTPError as exc:
            raise MemoryServiceError(f"memory service unreachable: {exc}") from exc
