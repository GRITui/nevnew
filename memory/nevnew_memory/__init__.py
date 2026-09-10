"""NevNew long-term memory package (issue #37).

Importable library + FastAPI service over mem0 (mem0ai) with per-user
Qdrant collections. See README.md for the architecture.
"""

from .config import MemorySettings
from .store import (
    MemoryBackendError,
    MemoryNotFound,
    MemoryStore,
    MemoryValidationError,
)

__all__ = [
    "MemorySettings",
    "MemoryStore",
    "MemoryNotFound",
    "MemoryValidationError",
    "MemoryBackendError",
]
