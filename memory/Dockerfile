# NevNew memory service (issue #37) — mem0 + Qdrant long-term memory.
FROM python:3.12-slim

# HF_HOME / SENTENCE_TRANSFORMERS_HOME: cache the embedding model in the
# /data volume (mounted by compose-snippet.yml) so the ~470 MB model is
# downloaded once, not on every container start.
# MEM0_TELEMETRY: mem0 phones home by default; this is a private local stack.
# PYTHONUNBUFFERED: docker logs appear immediately.
ENV HF_HOME=/data/hf \
    SENTENCE_TRANSFORMERS_HOME=/data/hf \
    MEM0_TELEMETRY=false \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install CPU-only torch first so sentence-transformers (in requirements.txt)
# doesn't pull the CUDA-bundled PyPI wheel on x86_64. On linux/arm64 the
# regular PyPI wheel is already CPU-only (the pytorch.org CPU index has no
# aarch64 linux builds).
RUN ARCH=$(uname -m) \
    && if [ "$ARCH" = "x86_64" ]; then \
        pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu; \
    else \
        pip install --no-cache-dir torch; \
    fi

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY nevnew_memory ./nevnew_memory

RUN mkdir -p /data/mem0 /data/hf

EXPOSE 8000

CMD ["uvicorn", "nevnew_memory.service:app", "--host", "0.0.0.0", "--port", "8000"]
