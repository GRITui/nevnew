# NevNew AI Core service (issue #39) — FastAPI harness middleware.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY nevnew_ai_core ./nevnew_ai_core

# callbacks/nevnew_persona_prompt.py is mounted here read-only by
# compose-snippet.yml (same pattern as the telegram-bot service) so the
# persona system prompt is REUSED, not duplicated.
EXPOSE 8000

CMD ["uvicorn", "nevnew_ai_core.main:app", "--host", "0.0.0.0", "--port", "8000"]
