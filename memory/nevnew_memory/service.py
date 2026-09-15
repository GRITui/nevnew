"""FastAPI HTTP service wrapping the MemoryStore (issue #37).

Run with:  uvicorn nevnew_memory.service:app --host 0.0.0.0 --port 8000

Endpoints (bearer auth via MEMORY_API_KEY when set — see env-snippet.txt):

    GET    /health                                liveness (always 200)
    GET    /ready                                 qdrant/embedder readiness
    POST   /users/{user_id}/memories              add/extract memories
    GET    /users/{user_id}/memories/search       semantic search (query=...)
    GET    /users/{user_id}/memories              list all memories
    GET    /users/{user_id}/memories/{memory_id}  fetch one memory
    PATCH  /users/{user_id}/memories/{memory_id}  rewrite one memory
    DELETE /users/{user_id}/memories/{memory_id}  delete one memory
    DELETE /users/{user_id}/memories              delete all of a user's memories
    POST   /users/{user_id}/reset                 drop the user's collections
    GET    /users                                 list known users

    Document RAG (issue #79) — separate Qdrant collections, same isolation
    model, sharing the memory service's Qdrant connection + local embedder:

    POST   /users/{user_id}/documents             upload a file (multipart:
                                                    `file`, optional `source`)
                                                    -> chunk, embed, store
    GET    /users/{user_id}/documents/search      semantic search (query=...)
    GET    /users/{user_id}/documents             list ingested sources
    DELETE /users/{user_id}/documents/{source}    delete one source's chunks

user_id rules: 1-255 chars, no whitespace (mem0 requirement), and it is
URL-path-safe — ai-core enforces `^[A-Za-z0-9._@:-]{1,255}$` upstream.
"""

from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from .config import MemorySettings
from .documents import DocumentBackendError, DocumentStore, DocumentValidationError
from .extract import ExtractionError, extract_text
from .store import MemoryBackendError, MemoryNotFound, MemoryStore, MemoryValidationError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("nevnew-memory")

# Module-level on purpose: a bad environment must crash the process at boot
# with a clear log line (docker restart + visible error), not 500 at runtime.
SETTINGS = MemorySettings.from_env()

if SETTINGS.api_key:
    logger.info("Memory service auth: ENABLED (MEMORY_API_KEY set).")
else:
    logger.warning(
        "MEMORY_API_KEY is not set — the memory service accepts UNAUTHENTICATED "
        "requests. Fine on the isolated docker network, but set it before ever "
        "exposing this port."
    )


@asynccontextmanager
async def _lifespan(app: FastAPI):
    store = MemoryStore(SETTINGS)
    app.state.store = store
    try:
        await store.initialize()
    except Exception:  # noqa: BLE001 — start anyway, /ready reports the failure
        logger.exception("MemoryStore warm-up failed — starting degraded; check /ready.")

    # Document RAG (issue #79): shares the QdrantClient + local embedder
    # MemoryStore just warmed up above — never a second model load.
    doc_store = DocumentStore(
        qdrant=store.qdrant_client,
        embedder=store.shared_embedder,
        embedder_dims=SETTINGS.embedder_dims,
        collection_prefix=SETTINGS.doc_collection_prefix,
    )
    app.state.doc_store = doc_store

    yield
    await store.close()


app = FastAPI(
    title="NevNew Memory Service",
    version="1.0.0",
    description="Per-user long-term memory (mem0 + Qdrant) for the NevNew stack.",
    lifespan=_lifespan,
)


def _store(request: Request) -> MemoryStore:
    store = request.app.state.store
    if store is None:  # defensive; lifespan always sets it before serving
        raise HTTPException(status_code=503, detail="memory store not initialized")
    return store


def _doc_store(request: Request) -> DocumentStore:
    doc_store = getattr(request.app.state, "doc_store", None)
    if doc_store is None:  # defensive; lifespan always sets it before serving
        raise HTTPException(status_code=503, detail="document store not initialized")
    return doc_store


def _require_api_key(authorization: Optional[str] = Header(default=None)) -> None:
    if not SETTINGS.api_key:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization[len("Bearer "):]
    if not secrets.compare_digest(token, SETTINGS.api_key):
        raise HTTPException(status_code=401, detail="Invalid bearer token")


def _user_id(user_id: str) -> str:
    cleaned = user_id.strip()
    if not cleaned or len(cleaned) > 255 or any(ch.isspace() for ch in cleaned):
        raise HTTPException(
            status_code=400,
            detail="user_id must be 1-255 characters with no whitespace",
        )
    return cleaned


def _error_status(exc: Exception) -> int:
    if isinstance(exc, MemoryNotFound):
        return 404
    if isinstance(exc, MemoryValidationError):
        return 400
    if isinstance(exc, (DocumentValidationError, ExtractionError)):
        return 400
    return 503  # MemoryBackendError, DocumentBackendError and anything unexpected
class AddMemoriesRequest(BaseModel):
    """Provide exactly one of `text` (plain string) or `messages`."""

    text: Optional[str] = Field(default=None, description="Raw text to extract memories from.")
    messages: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description='OpenAI-style messages: [{"role": "user", "content": "..."}, ...].',
    )
    metadata: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Arbitrary JSON metadata stored alongside extracted memories.",
    )

    @model_validator(mode="after")
    def _exactly_one_payload(self) -> "AddMemoriesRequest":
        if self.text is None and not self.messages:
            raise ValueError("provide either 'text' or 'messages'")
        if self.text is not None and self.messages:
            raise ValueError("provide only one of 'text' or 'messages'")
        for message in self.messages or []:
            if (
                not isinstance(message, dict)
                or not isinstance(message.get("role"), str)
                or not isinstance(message.get("content"), str)
            ):
                raise ValueError("each message must be an object with string 'role' and 'content'")
        return self

    def payload(self) -> Any:
        if self.text is not None:
            return self.text
        return self.messages


class UpdateMemoryRequest(BaseModel):
    text: str = Field(min_length=1, description="New memory text.")


@app.get("/health", tags=["ops"])
async def health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "service": "nevnew-memory",
        "qdrant": f"{SETTINGS.qdrant_host}:{SETTINGS.qdrant_port}",
        "collection_prefix": SETTINGS.collection_prefix,
        "auth": "enabled" if SETTINGS.api_key else "disabled",
    }


@app.get("/ready", tags=["ops"])
async def ready(request: Request) -> JSONResponse:
    store: Optional[MemoryStore] = getattr(app.state, "store", None)
    checks = await store.health_checks() if store else {"qdrant": "unknown", "embedder": "unknown"}
    ready_ok = all(value == "ok" for value in checks.values())
    return JSONResponse(
        status_code=200 if ready_ok else 503,
        content={"status": "ok" if ready_ok else "degraded", "checks": checks},
    )


@app.post(
    "/users/{user_id}/memories",
    status_code=200,
    tags=["memories"],
    dependencies=[Depends(_require_api_key)],
)
async def add_memories(user_id: str, body: AddMemoriesRequest, request: Request) -> Dict[str, Any]:
    uid = _user_id(user_id)
    store = _store(request)
    try:
        results = await store.add(uid, body.payload(), metadata=body.metadata)
    except (MemoryNotFound, MemoryValidationError, MemoryBackendError) as exc:
        raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc
    return {
        "user_id": uid,
        "collection": store.collection_name(uid),
        "results": results,
        "count": len(results),
    }


@app.get(
    "/users/{user_id}/memories/search",
    tags=["memories"],
    dependencies=[Depends(_require_api_key)],
)
async def search_memories(
    request: Request,
    user_id: str,
    query: str = Query(min_length=1, max_length=2000),
    limit: int = Query(default=SETTINGS.default_search_top_k, ge=1, le=50),
) -> Dict[str, Any]:
    uid = _user_id(user_id)
    store = _store(request)
    try:
        results = await store.search(uid, query, limit=limit)
    except (MemoryNotFound, MemoryValidationError, MemoryBackendError) as exc:
        raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc
    return {"user_id": uid, "query": query, "results": results, "count": len(results)}
@app.get(
    "/users/{user_id}/memories",
    tags=["memories"],
    dependencies=[Depends(_require_api_key)],
)
async def list_memories(
    request: Request,
    user_id: str,
    limit: int = Query(default=100, ge=1, le=200),
) -> Dict[str, Any]:
    uid = _user_id(user_id)
    store = _store(request)
    try:
        results = await store.list_memories(uid, limit=limit)
    except (MemoryNotFound, MemoryValidationError, MemoryBackendError) as exc:
        raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc
    return {
        "user_id": uid,
        "collection": store.collection_name(uid),
        "results": results,
        "count": len(results),
    }


@app.get(
    "/users/{user_id}/memories/{memory_id}",
    tags=["memories"],
    dependencies=[Depends(_require_api_key)],
)
async def get_memory(user_id: str, memory_id: str, request: Request) -> Dict[str, Any]:
    uid = _user_id(user_id)
    store = _store(request)
    try:
        item = await store.get(uid, memory_id)
    except (MemoryNotFound, MemoryValidationError, MemoryBackendError) as exc:
        raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc
    if item is None:
        raise HTTPException(status_code=404, detail=f"memory {memory_id!r} not found for user {uid!r}")
    return item


@app.patch(
    "/users/{user_id}/memories/{memory_id}",
    tags=["memories"],
    dependencies=[Depends(_require_api_key)],
)
async def update_memory(user_id: str, memory_id: str, body: UpdateMemoryRequest, request: Request) -> Dict[str, Any]:
    uid = _user_id(user_id)
    store = _store(request)
    try:
        await store.update(uid, memory_id, body.text)
        item = await store.get(uid, memory_id)
    except (MemoryNotFound, MemoryValidationError, MemoryBackendError) as exc:
        raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc
    return item if item is not None else {"status": "updated"}


@app.delete(
    "/users/{user_id}/memories/{memory_id}",
    tags=["memories"],
    dependencies=[Depends(_require_api_key)],
)
async def delete_memory(user_id: str, memory_id: str, request: Request) -> Dict[str, str]:
    uid = _user_id(user_id)
    store = _store(request)
    try:
        await store.delete(uid, memory_id)
    except (MemoryNotFound, MemoryValidationError, MemoryBackendError) as exc:
        raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc
    return {"status": "deleted", "memory_id": memory_id, "user_id": uid}


@app.delete(
    "/users/{user_id}/memories",
    tags=["memories"],
    dependencies=[Depends(_require_api_key)],
)
async def delete_all_memories(user_id: str, request: Request) -> Dict[str, Any]:
    uid = _user_id(user_id)
    store = _store(request)
    try:
        deleted = await store.delete_all(uid)
    except (MemoryNotFound, MemoryValidationError, MemoryBackendError) as exc:
        raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc
    return {"status": "deleted_all", "user_id": uid, "deleted": deleted}


@app.post(
    "/users/{user_id}/reset",
    tags=["memories"],
    dependencies=[Depends(_require_api_key)],
)
async def reset_user(user_id: str, request: Request) -> Dict[str, Any]:
    uid = _user_id(user_id)
    store = _store(request)
    try:
        result = await store.reset(uid)
    except (MemoryNotFound, MemoryValidationError, MemoryBackendError) as exc:
        raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc
    return {"status": "reset", **result}


@app.get("/users", tags=["memories"], dependencies=[Depends(_require_api_key)])
async def list_users(request: Request) -> Dict[str, Any]:
    store = _store(request)
    try:
        users = await store.list_users()
    except (MemoryNotFound, MemoryValidationError, MemoryBackendError) as exc:
        raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc
    return {"users": users, "count": len(users)}


# ---------------------------------------------------------------------------
# Document RAG (issue #79): per-user document chunks, isolated the same way
# as memories but stored in separate Qdrant collections (see documents.py).
# ---------------------------------------------------------------------------

_DOC_ERRORS = (DocumentValidationError, DocumentBackendError, ExtractionError)


@app.post(
    "/users/{user_id}/documents",
    status_code=200,
    tags=["documents"],
    dependencies=[Depends(_require_api_key)],
)
async def ingest_document(
    user_id: str,
    request: Request,
    file: UploadFile = File(...),
    source: Optional[str] = Form(default=None),
) -> Dict[str, Any]:
    """Upload a file (PDF/TXT/MD) — extracted, chunked, embedded, stored.

    `source` defaults to the uploaded filename; re-uploading the same
    `source` overwrites that source's chunks (see documents.py).
    """
    uid = _user_id(user_id)
    doc_store = _doc_store(request)

    raw = await file.read()
    if len(raw) > SETTINGS.doc_max_upload_bytes:
        raise HTTPException(
            status_code=400,
            detail=f"file exceeds the {SETTINGS.doc_max_upload_bytes}-byte upload limit",
        )
    if not raw:
        raise HTTPException(status_code=400, detail="uploaded file is empty")

    effective_source = (source or file.filename or "upload").strip() or "upload"
    try:
        text = extract_text(raw, filename=file.filename, content_type=file.content_type)
    except ExtractionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        result = await doc_store.ingest(uid, effective_source, text)
    except _DOC_ERRORS as exc:
        raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc
    return {"user_id": uid, **result}


@app.get(
    "/users/{user_id}/documents/search",
    tags=["documents"],
    dependencies=[Depends(_require_api_key)],
)
async def search_documents(
    request: Request,
    user_id: str,
    query: str = Query(min_length=1, max_length=2000),
    limit: int = Query(default=SETTINGS.doc_search_top_k, ge=1, le=50),
) -> Dict[str, Any]:
    uid = _user_id(user_id)
    doc_store = _doc_store(request)
    try:
        results = await doc_store.search(uid, query, limit=limit)
    except _DOC_ERRORS as exc:
        raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc
    return {"user_id": uid, "query": query, "results": results, "count": len(results)}


@app.get(
    "/users/{user_id}/documents",
    tags=["documents"],
    dependencies=[Depends(_require_api_key)],
)
async def list_documents(user_id: str, request: Request) -> Dict[str, Any]:
    uid = _user_id(user_id)
    doc_store = _doc_store(request)
    try:
        sources = await doc_store.list_sources(uid)
    except _DOC_ERRORS as exc:
        raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc
    return {"user_id": uid, "sources": sources, "count": len(sources)}


@app.delete(
    "/users/{user_id}/documents/{source}",
    tags=["documents"],
    dependencies=[Depends(_require_api_key)],
)
async def delete_document(user_id: str, source: str, request: Request) -> Dict[str, Any]:
    uid = _user_id(user_id)
    doc_store = _doc_store(request)
    try:
        deleted = await doc_store.delete_source(uid, source)
    except _DOC_ERRORS as exc:
        raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc
    return {"status": "deleted", "user_id": uid, "source": source, "deleted_chunks": deleted}


