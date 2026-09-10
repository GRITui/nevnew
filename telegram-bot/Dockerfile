FROM python:3.12-slim

WORKDIR /app

# Node.js + CLI backends bot.py shells out to (chat path bypasses LiteLLM
# entirely as of 2026-09-09 — see bot.py's module docstring):
# - opencode (OpenCode Go quota) — primary.
# - @anthropic-ai/claude-code (Sonnet, via CLAUDE_CODE_OAUTH_TOKEN) — fallback.
# Pinned versions, matching how open-webui/config.yaml pin theirs — see BACKLOG.md.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g opencode-ai@1.18.29 @anthropic-ai/claude-code@2.1.263 \
    && apt-get purge -y curl gnupg \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

CMD ["python", "bot.py"]
