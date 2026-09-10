"""NevNew long-term memory store (issue #37): a mem0 (mem0ai 2.0.0) wrapper
with strict per-user_id isolation.

Isolation model
---------------
Every user_id gets its own Qdrant collection on the shared `newnew-qdrant`
service (`<prefix><sanitized_user_id>`), and every operation additionally
carries a `user_id` metadata filter (mem0's own scoping), so a leak would
require both layers to fail. Deleting/resetting a user only ever touches
that user's collections.

Resource sharing
----------------
Each user gets a lazily-built, cached `mem0.AsyncMemory` instance bound to
their collection (mem0 binds the collection at construction time, so
per-user collections imply per-user instances). Three heavyweight
components are shared across all of them instead of being rebuilt per user:

* one `QdrantClient` (passed via the vector-store config's `client` key,
  which mem0's Qdrant store supports explicitly);
* one local embedding model (sentence-transformers) — constructed ONCE and
  swapped into each per-user instance's `embedding_model` attribute. Without
  this every user would load their own ~470 MB model copy. The per-user
  instances are built with a cheap throwaway OpenAI-style embedder config
  (never called, swapped out immediately) so construction stays fast;
* one SQLite history database (mem0's own history tracking).

Pinning
-------
This module is written and verified against mem0ai==2.0.0 (see
requirements.txt). mem0's `AsyncMemory` API is keyword-based
(`search(query, filters={"user_id": ...})`, `add(messages, user_id=...)`);
2.x removed the top-level `user_id=` argument from `search`/`get_all`, so
don't "simplify" those calls back when bumping the pin.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from typing import Any, Dict, List, Optional

from mem0 import AsyncMemory
from qdrant_client import QdrantClient

from .config import MemorySettings

logger = logging.getLogger("nevnew-memory")

# mem0 raises bare ValueError for "memory not found" (delete/get) — the
# service layer translates these into typed exceptions so HTTP status codes
# stay accurate.
class MemoryNotFound(Exception):
    """The requested memory (or user collection) does not exist."""


class MemoryValidationError(Exception):
    """Invalid input rejected by the memory backend."""


class MemoryBackendError(Exception):
    """The memory backend (Qdrant / LLM / embedder) is unreachable or failed."""


# The throwaway embedder config used while constructing per-user AsyncMemory
# instances when a shared local embedder will be swapped in. It must be cheap
# to construct (an OpenAI client object only — no network, no model load) and
# is never actually used for embedding. The bogus values make any accidental
# use loud and harmless.
_THROWAWAY_EMBEDDER_CONFIG: Dict[str, Any] = {
    "provider": "openai",
    "config": {
        "model": "text-embedding-3-small",
        "api_key": "unused-local-embedder",
        "embedding_dims": 0,  # replaced per-store with the real dims
        "openai_base_url": "http://127.0.0.1:9/v1",  # port 9 (discard); never called
    },
}


def sanitize_user_id_for_collection(user_id: str, prefix: str) -> str:
    """Map a user_id to a deterministic, valid Qdrant collection name.

    Qdrant collection names must be non-empty, alphanumeric/underscore/
    hyphen, and reasonably short. Characters outside that set become "_".
    When sanitization actually changes the id (or truncates it), a short
    hash suffix keeps the mapping collision-free: "a b" and "a_b" sanitize
    to the same string but hash differently.
    """
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", user_id)
    if len(cleaned) > 80:
        cleaned = cleaned[:80]
    if not cleaned:
        cleaned = "unknown"
    suffix = ""
    if cleaned != user_id:
        suffix = "_" + hashlib.sha1(user_id.encode("utf-8")).hexdigest()[:8]
    return f"{prefix}{cleaned}{suffix}"


def _normalize_memory(item: Any) -> Dict[str, Any]:
    """Coerce a mem0 result item (dict or object) into a plain JSON dict."""
    if isinstance(item, dict):
        raw: Dict[str, Any] = item
    else:
        raw = {
            key: getattr(item, key, None)
            for key in ("id", "memory", "hash", "metadata", "score", "created_at", "updated_at", "event")
        }
    normalized: Dict[str, Any] = {}
    for key in ("id", "memory", "hash", "metadata", "score", "created_at", "updated_at", "event"):
        value = raw.get(key)
        if value is not None:
            normalized[key] = value
    return normalized


def _result_items(result: Any) -> List[Any]:
    """Extract the result list from a mem0 response ({"results": [...]})."""
    if isinstance(result, dict):
        items = result.get("results", [])
    else:
        items = getattr(result, "results", None) or []
    if not isinstance(items, list):
        return []
    return items
class MemoryStore:
    """Per-user long-term memory over mem0 + Qdrant.

    All methods are coroutines; blocking work (model loads, Qdrant admin
    calls, AsyncMemory construction) is dispatched via `asyncio.to_thread`
    so a first-user hit never stalls the event loop.
    """

    def __init__(self, settings: MemorySettings):
        self._settings = settings
        self._qdrant = QdrantClient(
            host=settings.qdrant_host,
            port=settings.qdrant_port,
            api_key=settings.qdrant_api_key,  # None is fine for the local unauthenticated instance
            prefer_grpc=False,
        )
        self._shared_embedder: Any = None
        self._memories: Dict[str, AsyncMemory] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ setup

    async def initialize(self) -> None:
        """Warm up shared resources. Called once at service startup.

        Failures here are logged but do NOT abort startup — the service comes
        up and reports itself not-ready via /ready, which lets it recover
        when Qdrant comes up later without a container restart.
        """
        history_dir = os.path.dirname(self._settings.history_db_path)
        if history_dir:
            await asyncio.to_thread(os.makedirs, history_dir, exist_ok=True)

        if self._settings.embedder_provider == "huggingface":
            logger.info(
                "Loading shared local embedding model %r (%d dims) — the first "
                "start downloads it (~470 MB) into the /data/hf volume and can "
                "take a few minutes; later starts load from disk.",
                self._settings.embedder_model,
                self._settings.embedder_dims,
            )
            self._shared_embedder = await asyncio.to_thread(self._load_shared_embedder)
            logger.info("Shared embedding model ready.")

        await self.ping()
        logger.info(
            "MemoryStore initialized (qdrant=%s:%d, prefix=%r, llm=%s @ %s).",
            self._settings.qdrant_host,
            self._settings.qdrant_port,
            self._settings.collection_prefix,
            self._settings.llm_model,
            self._settings.llm_base_url,
        )

    def _load_shared_embedder(self) -> Any:
        # Imported lazily: pulls in sentence-transformers (heavy) and is only
        # needed when the local huggingface embedder is selected.
        from mem0.configs.embeddings.base import BaseEmbedderConfig
        from mem0.embeddings.huggingface import HuggingFaceEmbedding

        config = BaseEmbedderConfig(
            model=self._settings.embedder_model,
            embedding_dims=self._settings.embedder_dims,
            model_kwargs=dict(self._settings.embedder_model_kwargs),
        )
        return HuggingFaceEmbedding(config)

    async def ping(self) -> None:
        """Liveness ping against Qdrant. Raises on failure."""

        def _ping() -> None:
            self._qdrant.get_collections()

        await asyncio.to_thread(_ping)

    async def health_checks(self) -> Dict[str, str]:
        """Non-raising variant used by /ready."""
        checks: Dict[str, str] = {
            "qdrant": "down",
            "embedder": "not-loaded" if self._shared_embedder is None else "ok",
        }
        try:
            await self.ping()
            checks["qdrant"] = "ok"
        except Exception as exc:  # noqa: BLE001 — readiness reporting
            checks["qdrant"] = f"error: {type(exc).__name__}"
        return checks
    # -------------------------------------------------- per-user instances

    def collection_name(self, user_id: str) -> str:
        return sanitize_user_id_for_collection(user_id, self._settings.collection_prefix)

    async def _memory_for(self, user_id: str) -> AsyncMemory:
        existing = self._memories.get(user_id)
        if existing is not None:
            return existing
        async with self._lock:
            # Re-check under the lock: another request may have built it.
            existing = self._memories.get(user_id)
            if existing is not None:
                return existing
            collection = self.collection_name(user_id)
            logger.info("Building memory instance for user_id=%s (collection=%s)", user_id, collection)
            try:
                memory = await asyncio.to_thread(self._construct_memory, collection)
            except Exception as exc:
                raise MemoryBackendError(
                    f"failed to initialize memory for user {user_id!r} "
                    f"(collection {collection!r}): {exc}"
                ) from exc
            self._memories[user_id] = memory
            return memory

    def _construct_memory(self, collection: str) -> AsyncMemory:
        """Build one per-user AsyncMemory (blocking; run via to_thread)."""
        settings = self._settings

        vector_config: Dict[str, Any] = {
            "collection_name": collection,
            "embedding_model_dims": settings.embedder_dims,
            # Share one QdrantClient across all per-user instances — mem0's
            # Qdrant store takes precedence over host/port when `client` is
            # set. host/port are still required by QdrantConfig's validator.
            "client": self._qdrant,
            "host": settings.qdrant_host,
            "port": settings.qdrant_port,
            "on_disk": settings.qdrant_on_disk,
        }
        if settings.qdrant_api_key:
            vector_config["api_key"] = settings.qdrant_api_key

        llm_config: Dict[str, Any] = {
            "provider": "openai",
            "config": {
                "model": settings.llm_model,
                "api_key": settings.llm_api_key,
                "openai_base_url": settings.llm_base_url,
                "temperature": settings.llm_temperature,
                "max_tokens": settings.llm_max_tokens,
            },
        }

        if self._shared_embedder is not None:
            # Cheap throwaway; swapped for the shared model right after
            # construction (see module docstring).
            embedder_config = {
                "provider": _THROWAWAY_EMBEDDER_CONFIG["provider"],
                "config": dict(_THROWAWAY_EMBEDDER_CONFIG["config"], embedding_dims=settings.embedder_dims),
            }
        else:
            # No local model to share (HTTP-based embedder) — build the real
            # one per instance; it's just a lightweight HTTP client.
            embedder_config = {
                "provider": settings.embedder_provider,
                "config": {
                    "model": settings.embedder_model,
                    "embedding_dims": settings.embedder_dims,
                    "model_kwargs": dict(settings.embedder_model_kwargs),
                    **(
                        {
                            "api_key": settings.embedder_api_key,
                            "openai_base_url": settings.embedder_base_url,
                        }
                        if settings.embedder_provider == "openai"
                        else {}
                    ),
                },
            }

        config: Dict[str, Any] = {
            "vector_store": {"provider": "qdrant", "config": vector_config},
            "llm": llm_config,
            "embedder": embedder_config,
            "history_db_path": settings.history_db_path,
        }
        if settings.custom_instructions:
            config["custom_instructions"] = settings.custom_instructions

        memory = AsyncMemory.from_config(config)
        if self._shared_embedder is not None:
            memory.embedding_model = self._shared_embedder
        return memory
    # ------------------------------------------------------ memory operations

    async def add(
        self,
        user_id: str,
        messages: Any,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Extract + persist memories for a user.

        `messages` may be a plain string or an OpenAI-style message list
        ([{"role": ..., "content": ...}, ...]) — mem0 runs its LLM fact
        extraction over the content and dedupes/updates against what is
        already stored. Returns the normalized per-fact results
        ({"id", "memory", "event": ADD|UPDATE|NONE}).
        """
        memory = await self._memory_for(user_id)
        try:
            result = await memory.add(messages, user_id=user_id, metadata=metadata, infer=True)
        except ValueError as exc:
            raise MemoryValidationError(f"mem0 rejected the add payload: {exc}") from exc
        except Exception as exc:
            raise MemoryBackendError(f"memory add failed for user {user_id!r}: {exc}") from exc
        items = [_normalize_memory(item) for item in _result_items(result)]
        logger.info(
            "Memory add for user_id=%s: %d fact(s) %s",
            user_id,
            len(items),
            [item.get("event", "?") for item in items],
        )
        return items

    async def search(
        self,
        user_id: str,
        query: str,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Semantic search over one user's memories (score-ranked)."""
        top_k = limit if limit is not None else self._settings.default_search_top_k
        top_k = max(1, min(top_k, 50))
        memory = await self._memory_for(user_id)
        try:
            result = await memory.search(
                query,
                top_k=top_k,
                filters={"user_id": user_id},
                threshold=self._settings.search_threshold,
            )
        except ValueError as exc:
            raise MemoryValidationError(f"mem0 rejected the search: {exc}") from exc
        except Exception as exc:
            raise MemoryBackendError(f"memory search failed for user {user_id!r}: {exc}") from exc
        return [_normalize_memory(item) for item in _result_items(result)]

    async def get(self, user_id: str, memory_id: str) -> Optional[Dict[str, Any]]:
        memory = await self._memory_for(user_id)
        try:
            item = await memory.get(memory_id)
        except Exception as exc:
            raise MemoryBackendError(f"memory get failed for user {user_id!r}: {exc}") from exc
        if item is None:
            return None
        return _normalize_memory(item)

    async def list_memories(self, user_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        top_k = max(1, min(limit, 200))
        memory = await self._memory_for(user_id)
        try:
            result = await memory.get_all(filters={"user_id": user_id}, top_k=top_k)
        except ValueError as exc:
            raise MemoryValidationError(f"mem0 rejected the list: {exc}") from exc
        except Exception as exc:
            raise MemoryBackendError(f"memory list failed for user {user_id!r}: {exc}") from exc
        return [_normalize_memory(item) for item in _result_items(result)]
    async def update(self, user_id: str, memory_id: str, text: str) -> Dict[str, Any]:
        memory = await self._memory_for(user_id)
        try:
            await memory.get(memory_id)  # ValueError if missing -> 404
        except ValueError as exc:
            raise MemoryNotFound(f"memory {memory_id!r} not found for user {user_id!r}") from exc
        except Exception as exc:
            raise MemoryBackendError(f"memory update failed for user {user_id!r}: {exc}") from exc
        try:
            return await memory.update(memory_id, text)
        except ValueError as exc:
            raise MemoryNotFound(f"memory {memory_id!r} not found for user {user_id!r}") from exc
        except Exception as exc:
            raise MemoryBackendError(f"memory update failed for user {user_id!r}: {exc}") from exc

    async def delete(self, user_id: str, memory_id: str) -> None:
        memory = await self._memory_for(user_id)
        try:
            await memory.delete(memory_id)
        except ValueError as exc:
            raise MemoryNotFound(f"memory {memory_id!r} not found for user {user_id!r}") from exc
        except Exception as exc:
            raise MemoryBackendError(f"memory delete failed for user {user_id!r}: {exc}") from exc

    async def delete_all(self, user_id: str) -> int:
        """Delete every memory of a user (points removed; collection kept)."""
        before = await self.list_memories(user_id, limit=200)
        memory = await self._memory_for(user_id)
        try:
            await memory.delete_all(user_id=user_id)
        except ValueError as exc:
            raise MemoryValidationError(f"mem0 rejected delete_all: {exc}") from exc
        except Exception as exc:
            raise MemoryBackendError(f"delete_all failed for user {user_id!r}: {exc}") from exc
        logger.info("Deleted all memories for user_id=%s (%d point(s))", user_id, len(before))
        return len(before)

    async def reset(self, user_id: str) -> Dict[str, Any]:
        """Hard reset: drop the user's Qdrant collections entirely.

        Deliberately NOT mem0's `AsyncMemory.reset()` — that closes the
        shared QdrantClient (breaking every other user) and rebuilds from
        the config. Instead we drop the per-user collection (+ its entities
        collection) through our own client and evict the cached instance;
        the next request rebuilds a fresh collection lazily.
        """
        collection = self.collection_name(user_id)
        dropped: List[str] = []
        try:
            for name in (collection, f"{collection}_entities"):
                exists = await asyncio.to_thread(self._qdrant.collection_exists, name)
                if exists:
                    await asyncio.to_thread(self._qdrant.delete_collection, collection_name=name)
                    dropped.append(name)
        except Exception as exc:
            raise MemoryBackendError(f"reset failed for user {user_id!r}: {exc}") from exc

        evicted = self._memories.pop(user_id, None)
        if evicted is not None and hasattr(evicted, "close"):
            try:
                evicted.close()  # releases that instance's SQLite handle only
            except Exception:  # noqa: BLE001 — best-effort cleanup
                logger.warning("Failed to close evicted memory instance for user_id=%s", user_id, exc_info=True)
        logger.info("Reset memories for user_id=%s (dropped collections: %s)", user_id, dropped)
        return {"user_id": user_id, "dropped_collections": dropped}
    async def list_users(self) -> List[Dict[str, str]]:
        """List users known to the store (from existing Qdrant collections).

        The reported id is the sanitized collection suffix — for exotic
        user_ids it is not the original string (the mapping is one-way by
        design), which is why the collection name is included too.
        """
        prefix = self._settings.collection_prefix
        try:
            response = await asyncio.to_thread(self._qdrant.get_collections)
        except Exception as exc:
            raise MemoryBackendError(f"could not list collections: {exc}") from exc
        users: List[Dict[str, str]] = []
        for collection in response.collections:
            name = collection.name
            if not name.startswith(prefix) or name.endswith("_entities"):
                continue
            users.append({"user_id": name[len(prefix):], "collection": name})
        users.sort(key=lambda entry: entry["user_id"])
        return users

    async def close(self) -> None:
        """Release resources on shutdown. Safe to call multiple times."""
        for user_id, memory in list(self._memories.items()):
            try:
                if hasattr(memory, "close"):
                    memory.close()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                logger.warning("Failed to close memory instance for user_id=%s", user_id, exc_info=True)
        self._memories.clear()
        try:
            self._qdrant.close()
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass




