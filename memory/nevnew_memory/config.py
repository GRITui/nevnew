"""Environment-driven configuration for the NevNew memory service (issue #37).

Everything is configured via environment variables (12-factor style); the
values used inside docker-compose are documented in ../compose-snippet.yml
and ../env-snippet.txt. Invalid or missing required values fail fast at
startup with an actionable error so a misconfigured container dies loudly
instead of 500-ing at request time.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger("nevnew-memory")

# Default local embedding model. Must be multilingual (NevNew chats in Thai
# and English): paraphrase-multilingual-MiniLM-L12-v2 supports 50+ languages
# including Thai, is small (~470 MB) and runs fine on CPU. If you swap the
# model, set MEM0_EMBEDDER_DIMS to match its output dimensionality — the
# Qdrant collections are created with exactly this many vector dims.
DEFAULT_EMBEDDER_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"
DEFAULT_EMBEDDER_DIMS = 384


def _env_str(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _env_int(name: str, default: int) -> int:
    raw = _env_str(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = _env_str(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number, got {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = _env_str(name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise RuntimeError(f"{name} must be a boolean (true/false), got {raw!r}")


@dataclass(frozen=True)
class MemorySettings:
    """Immutable snapshot of the memory service configuration."""

    qdrant_host: str
    qdrant_port: int
    qdrant_api_key: Optional[str]
    qdrant_on_disk: bool

    collection_prefix: str

    llm_model: str
    llm_base_url: str
    llm_api_key: str
    llm_temperature: float
    llm_max_tokens: int

    embedder_provider: str
    embedder_model: str
    embedder_dims: int
    embedder_model_kwargs: Dict[str, Any]
    embedder_api_key: Optional[str]
    embedder_base_url: Optional[str]

    history_db_path: str
    custom_instructions: Optional[str]

    default_search_top_k: int
    search_threshold: float

    api_key: Optional[str]

    @classmethod
    def from_env(cls) -> "MemorySettings":
        # mem0's OpenAI LLM provider (mem0/llms/openai.py) hard-routes to
        # OpenRouter whenever OPENROUTER_API_KEY is present in the process
        # environment, ignoring the api_key / openai_base_url we configure
        # below — extraction calls would go to openrouter.ai with the raw
        # OpenRouter key (no "NevNew" alias there, and outside the proxy's
        # budget cap). This service always configures an explicit
        # OpenAI-compatible backend, so strip those vars from THIS process
        # only. compose-snippet.yml deliberately avoids `env_file: .env` for
        # the same reason.
        removed = [
            name
            for name in ("OPENROUTER_API_KEY", "OPENROUTER_API_BASE")
            if os.environ.pop(name, None) is not None
        ]
        if removed:
            logger.warning(
                "Removed %s from this process's environment: mem0's OpenAI-LLM "
                "provider auto-routes to OpenRouter when they are set, which "
                "would bypass the configured LiteLLM backend.",
                removed,
            )

        # mem0 phones home usage telemetry by default (posthog). This is a
        # private, local stack — default it off (still overridable).
        os.environ.setdefault("MEM0_TELEMETRY", "false")

    @classmethod
    def _load_validated(cls) -> "MemorySettings":
        qdrant_host = _env_str("QDRANT_HOST", "qdrant")
        if not qdrant_host:
            raise RuntimeError("QDRANT_HOST must not be empty")
        qdrant_port = _env_int("QDRANT_PORT", 6333)
        if not 1 <= qdrant_port <= 65535:
            raise RuntimeError(f"QDRANT_PORT must be 1-65535, got {qdrant_port}")

        collection_prefix = _env_str("MEM0_COLLECTION_PREFIX", "nevnew_mem_u_")
        if not collection_prefix or len(collection_prefix) > 32:
            raise RuntimeError("MEM0_COLLECTION_PREFIX must be 1-32 characters")
        if not all(ch.isalnum() or ch in "_-" for ch in collection_prefix):
            raise RuntimeError("MEM0_COLLECTION_PREFIX may only contain [A-Za-z0-9_-]")
        if not collection_prefix[0].isalpha():
            raise RuntimeError("MEM0_COLLECTION_PREFIX must start with a letter")

        llm_model = _env_str("MEM0_LLM_MODEL", "NevNew")
        llm_base_url = _env_str("MEM0_LLM_BASE_URL", "http://litellm:4000/v1")
        llm_api_key = _env_str("MEM0_LLM_API_KEY") or _env_str("LITELLM_MASTER_KEY")
        if not llm_api_key:
            raise RuntimeError(
                "MEM0_LLM_API_KEY (or LITELLM_MASTER_KEY) is required — mem0 "
                "uses the LiteLLM proxy for memory extraction and this "
                "authenticates against it."
            )
        llm_temperature = _env_float("MEM0_LLM_TEMPERATURE", 0.1)
        if not 0.0 <= llm_temperature <= 2.0:
            raise RuntimeError("MEM0_LLM_TEMPERATURE must be within 0.0-2.0")
        llm_max_tokens = _env_int("MEM0_LLM_MAX_TOKENS", 2000)
        if llm_max_tokens < 100:
            raise RuntimeError("MEM0_LLM_MAX_TOKENS must be >= 100")
        return cls._load_embedder(
            qdrant_host=qdrant_host,
            qdrant_port=qdrant_port,
            collection_prefix=collection_prefix,
            llm_model=llm_model,
            llm_base_url=llm_base_url,
            llm_api_key=llm_api_key,
            llm_temperature=llm_temperature,
            llm_max_tokens=llm_max_tokens,
        )

    @classmethod
    def _load_embedder(
        cls,
        qdrant_host: str,
        qdrant_port: int,
        collection_prefix: str,
        llm_model: str,
        llm_base_url: str,
        llm_api_key: str,
        llm_temperature: float,
        llm_max_tokens: int,
    ) -> "MemorySettings":
        embedder_provider = _env_str("MEM0_EMBEDDER_PROVIDER", "huggingface")
        if embedder_provider not in ("huggingface", "openai"):
            raise RuntimeError(
                "MEM0_EMBEDDER_PROVIDER must be 'huggingface' (local, default) "
                "or 'openai' (OpenAI-compatible HTTP endpoint)"
            )
        embedder_model = _env_str("MEM0_EMBEDDER_MODEL", DEFAULT_EMBEDDER_MODEL)
        dims_explicit = _env_str("MEM0_EMBEDDER_DIMS") is not None
        embedder_dims = _env_int("MEM0_EMBEDDER_DIMS", DEFAULT_EMBEDDER_DIMS)
        if not 16 <= embedder_dims <= 4096:
            raise RuntimeError("MEM0_EMBEDDER_DIMS must be within 16-4096")
        if (
            embedder_provider == "huggingface"
            and embedder_model != DEFAULT_EMBEDDER_MODEL
            and not dims_explicit
        ):
            raise RuntimeError(
                "MEM0_EMBEDDER_DIMS is not set but MEM0_EMBEDDER_MODEL was "
                f"changed to {embedder_model!r}. Qdrant collections are created "
                "with MEM0_EMBEDDER_DIMS vector dimensions — set it to the new "
                "model's output size (and note that collections created with a "
                "different dimensionality are not compatible)."
            )
        model_kwargs: Dict[str, Any] = {}
        raw_kwargs = _env_str("MEM0_EMBEDDER_MODEL_KWARGS")
        if raw_kwargs:
            try:
                parsed = json.loads(raw_kwargs)
            except ValueError as exc:
                raise RuntimeError(
                    f"MEM0_EMBEDDER_MODEL_KWARGS must be a JSON object, got {raw_kwargs!r}"
                ) from exc
            if not isinstance(parsed, dict):
                raise RuntimeError("MEM0_EMBEDDER_MODEL_KWARGS must be a JSON object")
            model_kwargs = parsed

        embedder_api_key: Optional[str] = None
        embedder_base_url: Optional[str] = None
        if embedder_provider == "openai":
            embedder_api_key = _env_str("MEM0_EMBEDDER_API_KEY")
            embedder_base_url = _env_str("MEM0_EMBEDDER_BASE_URL")
            if not embedder_api_key or not embedder_base_url:
                raise RuntimeError(
                    "MEM0_EMBEDDER_PROVIDER=openai requires MEM0_EMBEDDER_API_KEY "
                    "and MEM0_EMBEDDER_BASE_URL"
                )

        search_top_k = _env_int("MEM0_SEARCH_TOP_K", 5)
        if not 1 <= search_top_k <= 50:
            raise RuntimeError("MEM0_SEARCH_TOP_K must be within 1-50")
        search_threshold = _env_float("MEM0_SEARCH_THRESHOLD", 0.1)
        if not 0.0 <= search_threshold <= 1.0:
            raise RuntimeError("MEM0_SEARCH_THRESHOLD must be within 0.0-1.0")

        return cls(
            qdrant_host=qdrant_host,
            qdrant_port=qdrant_port,
            qdrant_api_key=_env_str("QDRANT_API_KEY"),
            qdrant_on_disk=_env_bool("MEM0_QDRANT_ON_DISK", False),
            collection_prefix=collection_prefix,
            llm_model=llm_model,
            llm_base_url=llm_base_url,
            llm_api_key=llm_api_key,
            llm_temperature=llm_temperature,
            llm_max_tokens=llm_max_tokens,
            embedder_provider=embedder_provider,
            embedder_model=embedder_model,
            embedder_dims=embedder_dims,
            embedder_model_kwargs=model_kwargs,
            embedder_api_key=embedder_api_key,
            embedder_base_url=embedder_base_url,
            history_db_path=_env_str("MEM0_HISTORY_DB_PATH", "/data/mem0/history.db"),
            custom_instructions=_env_str("MEM0_CUSTOM_INSTRUCTIONS"),
            default_search_top_k=search_top_k,
            search_threshold=search_threshold,
            api_key=_env_str("MEMORY_API_KEY"),
        )
