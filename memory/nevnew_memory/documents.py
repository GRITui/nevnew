"""NevNew document RAG store (issue #79): per-user document chunks in Qdrant.

Isolation model
----------------
Reuses the exact per-user isolation pattern from `store.py`
(`sanitize_user_id_for_collection`): every user_id gets its own Qdrant
collection (`<doc_collection_prefix><sanitized_user_id>`), separate from
the mem0 memory collections (different prefix, same Qdrant instance).

Resource sharing
-----------------
This module does NOT construct its own embedder or QdrantClient — it is
handed the ones already built by `MemoryStore` (see service.py's lifespan),
so documents never load a second ~470 MB embedding model copy and never
open a second connection pool.

Storage
-------
One Qdrant point per chunk: vector = embedding of the chunk text, payload
= {user_id, text, source, chunk_index, chunk_count, ingested_at}. Point
ids are uuid5-derived from (collection, source, chunk_index), so
re-ingesting the same `source` name overwrites its old chunks in place
instead of accumulating duplicates.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from .store import sanitize_user_id_for_collection

logger = logging.getLogger("nevnew-memory")


class DocumentValidationError(Exception):
    """Invalid input rejected before it ever reaches Qdrant/the embedder."""


class DocumentBackendError(Exception):
    """The document backend (Qdrant / embedder) is unreachable or failed."""


# Character-window chunking: no tokenizer dependency (this service already
# ships torch/sentence-transformers; adding tiktoken just for chunk sizing
# is not worth another dependency). Sized generously under the embedder's
# ~256-token effective window for paraphrase-multilingual-MiniLM-L12-v2.
DEFAULT_CHUNK_CHARS = 1200
DEFAULT_CHUNK_OVERLAP = 150


def chunk_text(
    text: str,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> List[str]:
    """Split `text` into overlapping chunks, preferring paragraph/sentence
    boundaries near the window edge so a chunk rarely cuts mid-sentence."""
    normalized = re.sub(r"\r\n?", "\n", text).strip()
    if not normalized:
        return []
    if len(normalized) <= chunk_chars:
        return [normalized]

    chunks: List[str] = []
    start = 0
    length = len(normalized)
    while start < length:
        end = min(start + chunk_chars, length)
        if end < length:
            boundary = normalized.rfind("\n\n", start, end)
            if boundary == -1 or boundary <= start:
                boundary = normalized.rfind(". ", start, end)
            if boundary != -1 and boundary > start:
                end = boundary + 1
        chunk = normalized[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= length:
            break
        start = max(end - overlap, start + 1)
    return chunks


@dataclass
class DocumentChunkHit:
    text: str
    source: str
    chunk_index: int
    chunk_count: int
    score: float
    ingested_at: Optional[str]


class DocumentStore:
    """Per-user document chunk store over Qdrant, sharing MemoryStore's
    QdrantClient and (when local) embedding model.

    All methods are coroutines; blocking Qdrant/embedder calls are
    dispatched via `asyncio.to_thread`.
    """

    def __init__(
        self,
        qdrant: QdrantClient,
        embedder: Any,
        embedder_dims: int,
        collection_prefix: str,
    ):
        self._qdrant = qdrant
        self._embedder = embedder
        self._dims = embedder_dims
        self._prefix = collection_prefix
        self._ensured: Set[str] = set()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- naming

    def collection_name(self, user_id: str) -> str:
        return sanitize_user_id_for_collection(user_id, self._prefix)

    # ------------------------------------------------------------- setup

    async def _ensure_collection(self, collection: str) -> None:
        if collection in self._ensured:
            return
        async with self._lock:
            if collection in self._ensured:
                return

            def _ensure() -> None:
                if not self._qdrant.collection_exists(collection):
                    self._qdrant.create_collection(
                        collection_name=collection,
                        vectors_config=qmodels.VectorParams(
                            size=self._dims, distance=qmodels.Distance.COSINE
                        ),
                    )

            try:
                await asyncio.to_thread(_ensure)
            except Exception as exc:
                raise DocumentBackendError(
                    f"failed to ensure document collection {collection!r}: {exc}"
                ) from exc
            self._ensured.add(collection)

    def _embed(self, text: str, action: str) -> List[float]:
        # mem0's embedder classes (mem0.embeddings.*) implement
        # embed(text, memory_action=...) — same shared instance MemoryStore
        # swaps into every per-user AsyncMemory (see store.py).
        return self._embedder.embed(text, memory_action=action)

    # -------------------------------------------------- document operations

    async def ingest(
        self,
        user_id: str,
        source: str,
        text: str,
        extra_metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Chunk + embed + upsert one document's text for a user."""
        if self._embedder is None:
            raise DocumentBackendError(
                "no local embedder is configured — document ingest requires "
                "MEM0_EMBEDDER_PROVIDER=huggingface"
            )
        source = (source or "").strip()
        if not source:
            raise DocumentValidationError("source must be a non-empty string")
        cleaned = (text or "").strip()
        if not cleaned:
            raise DocumentValidationError("document text is empty")

        chunks = chunk_text(cleaned)
        if not chunks:
            raise DocumentValidationError("document produced no chunks")

        collection = self.collection_name(user_id)
        await self._ensure_collection(collection)
        ingested_at = datetime.now(timezone.utc).isoformat()

        def _build_and_upsert() -> None:
            points: List[qmodels.PointStruct] = []
            for index, chunk in enumerate(chunks):
                vector = self._embed(chunk, "add")
                point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{collection}:{source}:{index}"))
                payload: Dict[str, Any] = {
                    "user_id": user_id,
                    "text": chunk,
                    "source": source,
                    "chunk_index": index,
                    "chunk_count": len(chunks),
                    "ingested_at": ingested_at,
                }
                if extra_metadata:
                    payload.update(extra_metadata)
                points.append(qmodels.PointStruct(id=point_id, vector=vector, payload=payload))
            self._qdrant.upsert(collection_name=collection, points=points)

        try:
            await asyncio.to_thread(_build_and_upsert)
        except Exception as exc:
            raise DocumentBackendError(
                f"document ingest failed for user {user_id!r} source {source!r}: {exc}"
            ) from exc

        logger.info(
            "Ingested document for user_id=%s source=%s (%d chunk(s))",
            user_id, source, len(chunks),
        )
        return {"source": source, "chunks": len(chunks), "collection": collection}

    async def search(self, user_id: str, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Semantic search over one user's document chunks (score-ranked)."""
        if self._embedder is None:
            raise DocumentBackendError(
                "no local embedder is configured — document search requires "
                "MEM0_EMBEDDER_PROVIDER=huggingface"
            )
        cleaned = (query or "").strip()
        if not cleaned:
            raise DocumentValidationError("query must be a non-empty string")

        collection = self.collection_name(user_id)
        top_k = max(1, min(limit, 50))

        try:
            exists = await asyncio.to_thread(self._qdrant.collection_exists, collection)
        except Exception as exc:
            raise DocumentBackendError(f"document search failed for user {user_id!r}: {exc}") from exc
        if not exists:
            return []

        def _query() -> Any:
            vector = self._embed(cleaned, "search")
            return self._qdrant.query_points(
                collection_name=collection,
                query=vector,
                limit=top_k,
                query_filter=qmodels.Filter(
                    must=[qmodels.FieldCondition(key="user_id", match=qmodels.MatchValue(value=user_id))]
                ),
                with_payload=True,
            )

        try:
            result = await asyncio.to_thread(_query)
        except Exception as exc:
            raise DocumentBackendError(f"document search failed for user {user_id!r}: {exc}") from exc

        points = getattr(result, "points", result) or []
        hits: List[Dict[str, Any]] = []
        for point in points:
            payload = point.payload or {}
            text = str(payload.get("text", "")).strip()
            if not text:
                continue
            hits.append(
                {
                    "text": text,
                    "source": payload.get("source", "unknown"),
                    "chunk_index": payload.get("chunk_index", 0),
                    "chunk_count": payload.get("chunk_count", 1),
                    "ingested_at": payload.get("ingested_at"),
                    "score": float(point.score) if point.score is not None else 0.0,
                }
            )
        return hits

    async def list_sources(self, user_id: str) -> List[Dict[str, Any]]:
        """Distinct sources currently stored for a user (best-effort scan)."""
        collection = self.collection_name(user_id)
        try:
            exists = await asyncio.to_thread(self._qdrant.collection_exists, collection)
        except Exception as exc:
            raise DocumentBackendError(f"list_sources failed for user {user_id!r}: {exc}") from exc
        if not exists:
            return []

        def _scroll() -> List[Dict[str, Any]]:
            seen: Dict[str, Dict[str, Any]] = {}
            offset = None
            while True:
                points, offset = self._qdrant.scroll(
                    collection_name=collection,
                    limit=200,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
                for point in points:
                    payload = point.payload or {}
                    source = payload.get("source", "unknown")
                    entry = seen.setdefault(
                        source,
                        {"source": source, "chunks": 0, "ingested_at": payload.get("ingested_at")},
                    )
                    entry["chunks"] += 1
                if offset is None:
                    break
            return sorted(seen.values(), key=lambda item: item["source"])

        try:
            return await asyncio.to_thread(_scroll)
        except Exception as exc:
            raise DocumentBackendError(f"list_sources failed for user {user_id!r}: {exc}") from exc

    async def delete_source(self, user_id: str, source: str) -> int:
        """Delete all chunks of one source for a user. Returns chunks removed."""
        collection = self.collection_name(user_id)
        try:
            exists = await asyncio.to_thread(self._qdrant.collection_exists, collection)
        except Exception as exc:
            raise DocumentBackendError(f"delete_source failed for user {user_id!r}: {exc}") from exc
        if not exists:
            return 0

        existing = await self.search(user_id, source, limit=50)
        matched = [hit for hit in existing if hit.get("source") == source]

        def _delete() -> None:
            self._qdrant.delete(
                collection_name=collection,
                points_selector=qmodels.FilterSelector(
                    filter=qmodels.Filter(
                        must=[
                            qmodels.FieldCondition(key="user_id", match=qmodels.MatchValue(value=user_id)),
                            qmodels.FieldCondition(key="source", match=qmodels.MatchValue(value=source)),
                        ]
                    )
                ),
            )

        try:
            await asyncio.to_thread(_delete)
        except Exception as exc:
            raise DocumentBackendError(
                f"delete_source failed for user {user_id!r} source {source!r}: {exc}"
            ) from exc
        return len(matched)
