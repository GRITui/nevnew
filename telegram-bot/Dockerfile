FROM python:3.12-slim

WORKDIR /app

# Node.js + CLI backends bot.py shells out to (chat path bypasses LiteLLM
# entirely — see bot.py's module docstring):
# - cline (Cline CLI, 9arm gateway / qwen3.8-27b-fp8 — same provider+model as
#   the Cline session this bot mirrors) — primary.
# - @anthropic-ai/claude-code (Sonnet, via CLAUDE_CODE_OAUTH_TOKEN) — fallback.
# Pinned versions, matching how open-webui/config.yaml pin theirs — see BACKLOG.md.
#
# ffmpeg (kept in the final image, not purged like curl/gnupg) is required
# for the voice loop (#61): converts incoming Telegram OGG/Opus voice notes
# to WAV for Groq Whisper, and gTTS's MP3 output back to OGG/Opus for the
# native Telegram voice-bubble reply — see bot.py's _run_ffmpeg().
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg ffmpeg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g cline@3.0.61 @anthropic-ai/claude-code@2.1.263 \
    && apt-get purge -y curl gnupg \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# Cline CLI provider config: the `openai-compatible` provider pointing at the
# 9arm gateway (same provider/model as the Cline session this bot mirrors).
# Non-secret — no API key is baked in; the key is passed at runtime via -k
# from NINEARM_API_KEY (env_file: .env). The updatedAt/tokenSource fields are
# required or the CLI silently rejects the provider and falls back to
# api.openai.com. bot.py runs cline with --config /app/cline-config
# --data-dir /app/cline-config/data, so providers.json must live at
# <config>/data/settings/providers.json.
COPY cline-providers.json /app/cline-config/data/settings/providers.json

CMD ["python", "bot.py"]
