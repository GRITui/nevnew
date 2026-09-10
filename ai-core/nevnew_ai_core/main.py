"""NevNew AI Core — FastAPI service (issue #39).

Run with:  uvicorn nevnew_ai_core.main:app --host 0.0.0.0 --port 8000

Endpoints:
    POST /chat                      persona + memories + tools -> model -> reply
                                    (schedules background memory extraction)
    GET  /tools                     list registered tools (built-in + mcpo)
    POST /tools/refresh             force an mcpo re-discovery
    GET  /health                    liveness (always 200)
    GET  /ready                     LiteLLM + memory-service readiness
    /memory/*                       pass-through management proxies to the
                                    memory service (single API surface for
                                    channels like the Telegram bot / n8n)

All endpoints except /health and /ready require
`Authorization: Bearer $AICORE_API_KEY` when that env var is set.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from . import __version__
from .config import Settings
from .litellm_client import LiteLLMClient
from .memory_client import MemoryServiceClient, MemoryServiceError
from .pipeline import ChatOutcome, ChatPipeline, ChatPipelineError, load_persona_prompt
from .schemas import ChatRequest, ChatResponse, ToolsResponse
from .tools.base import ToolRegistry
from .tools.builtin import builtin_tools
from .tools.mcpo import McpoTools

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("nevnew-ai-core")

SETTINGS = Settings.from_env()

if SETTINGS.api_key:
    logger.info("AI Core auth: ENABLED (AICORE_API_KEY set).")
else:
    logger.warning(
        "AICORE_API_KEY is not set — AI Core accepts UNAUTHENTICATED requests. "
        "Fine on the isolated docker network, but set it before exposing "
        "this port anywhere."
    )


async def _mcpo_refresher(registry: ToolRegistry, interval_seconds: int) -> None:
    """Keep the mcpo tool list fresh; failure-tolerant by design."""
    while True:
        try:
            await registry.refresh_mcpo()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — periodic task must survive
            await registry.record_refresh_failure(exc)
            logger.warning("mcpo refresh failed: %s", exc)
        await asyncio.sleep(interval_seconds)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    memory_client = MemoryServiceClient(
        base_url=SETTINGS.memory_base_url,
        api_key=SETTINGS.memory_api_key,
        search_timeout_seconds=SETTINGS.memory_search_timeout_seconds,
        add_timeout_seconds=SETTINGS.memory_add_timeout_seconds,
    )
    litellm = LiteLLMClient(
        base_url=SETTINGS.litellm_base_url,
        api_key=SETTINGS.litellm_api_key,
        timeout_seconds=SETTINGS.litellm_timeout_seconds,
        retries=SETTINGS.litellm_retries,
        model=SETTINGS.model_name,
    )
    mcpo: Optional[McpoTools] = None
    refresher_task: Optional[asyncio.Task] = None
    if SETTINGS.mcpo_base_url:
        mcpo = McpoTools(
            base_url=SETTINGS.mcpo_base_url,
            api_key=SETTINGS.mcpo_api_key,
            timeout_seconds=SETTINGS.mcpo_timeout_seconds,
            result_max_chars=SETTINGS.tool_result_max_chars,
            max_tools=SETTINGS.mcpo_max_tools,
        )
    registry = ToolRegistry(builtin_tools(), mcpo)
    pipeline = ChatPipeline(
        settings=SETTINGS,
        litellm=litellm,
        memory_client=memory_client,
        registry=registry,
        persona_prompt=load_persona_prompt(),
    )
    app.state.memory_client = memory_client
    app.state.litellm = litellm
    app.state.mcpo = mcpo
    app.state.registry = registry
    app.state.pipeline = pipeline

    if mcpo is not None and SETTINGS.mcpo_refresh_seconds > 0:
        try:
            await registry.refresh_mcpo()
        except Exception as exc:  # noqa: BLE001 — tools are optional
            await registry.record_refresh_failure(exc)
            logger.warning("Initial mcpo refresh failed (continuing): %s", exc)
        refresher_task = asyncio.create_task(
            _mcpo_refresher(registry, SETTINGS.mcpo_refresh_seconds)
        )

    logger.info(
        "NevNew AI Core ready (model=%s, litellm=%s, memory=%s, mcpo=%s, tz=%s).",
        SETTINGS.model_name,
        SETTINGS.litellm_base_url,
        SETTINGS.memory_base_url,
        SETTINGS.mcpo_base_url or "disabled",
        SETTINGS.timezone,
    )
    yield

    if refresher_task is not None:
        refresher_task.cancel()
        try:
            await refresher_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 — shutdown
            pass
    await litellm.aclose()
    await memory_client.aclose()
    if mcpo is not None:
        await mcpo.aclose()


app = FastAPI(
    title="NevNew AI Core",
    version=__version__,
    description=(
        "Pre-execution pipeline (persona + memories + tool schemas), bounded "
        "tool loop, and post-execution memory extraction for the NevNew stack."
    ),
    lifespan=_lifespan,
)


def _require_api_key(authorization: Optional[str] = Header(default=None)) -> None:
    if not SETTINGS.api_key:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    if not secrets.compare_digest(authorization[len("Bearer "):], SETTINGS.api_key):
        raise HTTPException(status_code=401, detail="Invalid bearer token")


def _pipeline(request: Request) -> ChatPipeline:
    return request.app.state.pipeline
async def _extract_memories(
    memory_client: MemoryServiceClient,
    user_id: str,
    messages: Dict[str, Any],
    metadata: Dict[str, Any],
) -> None:
    """Background post-execution hook (issue #37): submit the turn for
    mem0 fact extraction. Runs AFTER the response is sent; failures are
    logged, never surfaced to the caller (the reply already went out)."""
    try:
        results = await memory_client.add(user_id, messages, metadata=metadata)
        logger.info(
            "Memory extraction for user_id=%s: %d fact(s) %s",
            user_id,
            len(results),
            [item.get("event", "?") for item in results],
        )
    except Exception as exc:  # noqa: BLE001 — background task must not raise
        logger.error("Memory extraction failed for user_id=%s: %s", user_id, exc)


@app.post("/chat", response_model=ChatResponse, dependencies=[Depends(_require_api_key)])
async def chat(
    chat_request: ChatRequest,
    background_tasks: BackgroundTasks,
    request: Request,
) -> ChatResponse:
    pipeline = _pipeline(request)
    try:
        outcome: ChatOutcome = await pipeline.run(chat_request)
    except ChatPipelineError as exc:
        logger.error("Chat pipeline failed for user_id=%s: %s", chat_request.user_id, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    extraction_scheduled = False
    if (
        chat_request.store_memories
        and outcome.last_user_message
        and outcome.reply
    ):
        extraction_messages = [
            {"role": "user", "content": outcome.last_user_message},
            {"role": "assistant", "content": outcome.reply},
        ]
        metadata = {
            "source": "ai-core",
            "channel": (chat_request.channel or "api")[:32],
        }
        background_tasks.add_task(
            _extract_memories,
            request.app.state.memory_client,
            chat_request.user_id,
            extraction_messages,
            metadata,
        )
        extraction_scheduled = True

    logger.info(
        "Chat done for user_id=%s: iterations=%d round_trips=%d tools=%d memories=%d",
        chat_request.user_id,
        outcome.iterations,
        outcome.round_trips,
        len(outcome.tool_calls),
        len(outcome.memories),
    )
    return ChatResponse(
        reply=outcome.reply,
        user_id=chat_request.user_id,
        model=SETTINGS.model_name,
        iterations=outcome.iterations,
        hit_iteration_cap=outcome.hit_iteration_cap,
        round_trips=outcome.round_trips,
        tool_calls=outcome.tool_calls,
        memories_used=[str(m.get("memory", "")) for m in outcome.memories if m.get("memory")],
        memory_extraction_scheduled=extraction_scheduled,
    )


@app.get("/tools", response_model=ToolsResponse, dependencies=[Depends(_require_api_key)])
async def list_tools(request: Request) -> ToolsResponse:
    registry: ToolRegistry = request.app.state.registry
    tools = registry.describe()
    return ToolsResponse(
        tools=tools,
        count=len(tools),
        mcpo_status=registry.mcpo_status(),
    )


@app.post("/tools/refresh", dependencies=[Depends(_require_api_key)])
async def refresh_tools(request: Request) -> Dict[str, Any]:
    registry: ToolRegistry = request.app.state.registry
    try:
        count = await registry.refresh_mcpo()
    except Exception as exc:  # noqa: BLE001 — report, don't crash
        await registry.record_refresh_failure(exc)
        raise HTTPException(status_code=502, detail=f"mcpo refresh failed: {exc}") from exc
    return {"refreshed": True, "tool_count": count, "mcpo_status": registry.mcpo_status()}


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "service": "nevnew-ai-core",
        "version": __version__,
        "model": SETTINGS.model_name,
        "auth": "enabled" if SETTINGS.api_key else "disabled",
    }


@app.get("/ready")
async def ready(request: Request) -> JSONResponse:
    litellm: LiteLLMClient = request.app.state.litellm
    memory_client: MemoryServiceClient = request.app.state.memory_client
    mcpo = request.app.state.mcpo
    registry: ToolRegistry = request.app.state.registry

    litellm_ok = await litellm.ping()
    memory_ok = await memory_client.ping()
    mcpo_ok = await mcpo.ping() if mcpo is not None else None

    checks: Dict[str, Any] = {
        "litellm": "ok" if litellm_ok else "down",
        "memory": "ok" if memory_ok else "down",
        "mcpo": ("ok" if mcpo_ok else "down") if mcpo is not None else "disabled",
        "tools": registry.mcpo_status(),
    }
    ready_ok = litellm_ok and memory_ok
    return JSONResponse(
        status_code=200 if ready_ok else 503,
        content={"status": "ok" if ready_ok else "degraded", "checks": checks},
    )
# ---------------------------------------------------------------------------
# Memory management proxies — pass-through to the memory service so channels
# (Telegram bot, n8n) have ONE API surface. Status codes and error bodies
# from the memory service are relayed unchanged.
# ---------------------------------------------------------------------------


def _memory_client(request: Request) -> MemoryServiceClient:
    return request.app.state.memory_client


def _relay(response: Any) -> JSONResponse:
    try:
        content = response.json()
    except ValueError:
        content = {"detail": response.text[:500]}
    return JSONResponse(status_code=response.status_code, content=content)


@app.get("/memory/users", dependencies=[Depends(_require_api_key)])
async def memory_list_users(request: Request) -> JSONResponse:
    client = _memory_client(request)
    try:
        return _relay(await client.forward("GET", "/users"))
    except MemoryServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/memory/users/{user_id}/memories", dependencies=[Depends(_require_api_key)])
async def memory_add(user_id: str, request: Request) -> JSONResponse:
    client = _memory_client(request)
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="request body must be JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be a JSON object")
    try:
        return _relay(await client.forward("POST", f"/users/{user_id}/memories", json_body=body))
    except MemoryServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get(
    "/memory/users/{user_id}/memories/search",
    dependencies=[Depends(_require_api_key)],
)
async def memory_search(
    user_id: str,
    request: Request,
    query: str = Query(min_length=1, max_length=2000),
    limit: int = Query(default=5, ge=1, le=50),
) -> JSONResponse:
    client = _memory_client(request)
    try:
        return _relay(
            await client.forward(
                "GET",
                f"/users/{user_id}/memories/search",
                params={"query": query, "limit": limit},
            )
        )
    except MemoryServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/memory/users/{user_id}/memories", dependencies=[Depends(_require_api_key)])
async def memory_list(user_id: str, request: Request, limit: int = Query(default=100, ge=1, le=200)) -> JSONResponse:
    client = _memory_client(request)
    try:
        return _relay(
            await client.forward(
                "GET", f"/users/{user_id}/memories", params={"limit": limit}
            )
        )
    except MemoryServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/memory/users/{user_id}/memories/{memory_id}", dependencies=[Depends(_require_api_key)])
async def memory_get(user_id: str, memory_id: str, request: Request) -> JSONResponse:
    client = _memory_client(request)
    try:
        return _relay(await client.forward("GET", f"/users/{user_id}/memories/{memory_id}"))
    except MemoryServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.patch("/memory/users/{user_id}/memories/{memory_id}", dependencies=[Depends(_require_api_key)])
async def memory_update(user_id: str, memory_id: str, request: Request) -> JSONResponse:
    client = _memory_client(request)
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="request body must be JSON") from exc
    try:
        return _relay(
            await client.forward("PATCH", f"/users/{user_id}/memories/{memory_id}", json_body=body)
        )
    except MemoryServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.delete("/memory/users/{user_id}/memories/{memory_id}", dependencies=[Depends(_require_api_key)])
async def memory_delete(user_id: str, memory_id: str, request: Request) -> JSONResponse:
    client = _memory_client(request)
    try:
        return _relay(await client.forward("DELETE", f"/users/{user_id}/memories/{memory_id}"))
    except MemoryServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.delete("/memory/users/{user_id}/memories", dependencies=[Depends(_require_api_key)])
async def memory_delete_all(user_id: str, request: Request) -> JSONResponse:
    client = _memory_client(request)
    try:
        return _relay(await client.forward("DELETE", f"/users/{user_id}/memories"))
    except MemoryServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/memory/users/{user_id}/reset", dependencies=[Depends(_require_api_key)])
async def memory_reset(user_id: str, request: Request) -> JSONResponse:
    client = _memory_client(request)
    try:
        return _relay(await client.forward("POST", f"/users/{user_id}/reset"))
    except MemoryServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


