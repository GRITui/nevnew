FROM python:3.12-slim

WORKDIR /app

# Node.js + the cline CLI, for the 429-rate-limit fallback path (bot.py
# shells out to `cline` instead of hitting LiteLLM/OpenRouter). Pinned
# version, matching how open-webui/config.yaml pin theirs — see BACKLOG.md.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g cline@3.0.61 \
    && apt-get purge -y curl gnupg \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

CMD ["python", "bot.py"]
