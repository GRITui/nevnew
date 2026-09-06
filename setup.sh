#!/usr/bin/env bash
#
# setup.sh — bootstrap the นิวนิว (NevNew) local Web-UI AI Agent stack.
#
# Sets up directory structure, permissions, and environment config, then
# brings up the Open-WebUI + LiteLLM proxy stack via Docker/OrbStack.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

echo "==> นิวนิว (NevNew) stack setup starting in: $PROJECT_ROOT"

# ---------------------------------------------------------------------------
# 1. Verify Docker / OrbStack is available
# ---------------------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: 'docker' CLI not found. Install OrbStack (https://orbstack.dev) or Docker Desktop first." >&2
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "ERROR: Docker daemon is not running. Start OrbStack and try again." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 2. Directory structure
# ---------------------------------------------------------------------------
echo "==> Ensuring directory structure..."
mkdir -p "$PROJECT_ROOT/callbacks"

# ---------------------------------------------------------------------------
# 3. Permissions
# ---------------------------------------------------------------------------
echo "==> Setting permission flags..."
chmod 700 "$PROJECT_ROOT/setup.sh"
chmod 644 "$PROJECT_ROOT/config.yaml" "$PROJECT_ROOT/callbacks/nevnew_persona.py"
if [ -f "$PROJECT_ROOT/.env" ]; then
  chmod 600 "$PROJECT_ROOT/.env"
fi

# ---------------------------------------------------------------------------
# 4. Environment config
# ---------------------------------------------------------------------------
if [ ! -f "$PROJECT_ROOT/.env" ]; then
  if [ -f "$PROJECT_ROOT/.env.example" ]; then
    echo "==> No .env found — copying .env.example to .env"
    cp "$PROJECT_ROOT/.env.example" "$PROJECT_ROOT/.env"
    chmod 600 "$PROJECT_ROOT/.env"
    echo ""
    echo "!!! IMPORTANT: edit .env now and set OPENROUTER_API_KEY and LITELLM_MASTER_KEY"
    echo "!!! before continuing, otherwise the LiteLLM proxy will fail to authenticate."
    echo ""
    read -r -p "Press Enter once .env is filled in, or Ctrl+C to abort and edit it first... "
  else
    echo "ERROR: .env.example is missing; cannot bootstrap .env." >&2
    exit 1
  fi
else
  echo "==> Existing .env found — checking for newly-added keys..."
  while IFS= read -r line; do
    case "$line" in
      \#*|'') continue ;;
    esac
    key="${line%%=*}"
    if ! grep -q "^${key}=" "$PROJECT_ROOT/.env"; then
      echo "==> Adding missing key ${key} to .env (placeholder — fill it in)"
      echo "$line" >> "$PROJECT_ROOT/.env"
    fi
  done < "$PROJECT_ROOT/.env.example"
fi

# shellcheck disable=SC1091
set -a
source "$PROJECT_ROOT/.env"
set +a

if [ -z "${OPENROUTER_API_KEY:-}" ] || [ "$OPENROUTER_API_KEY" = "your-openrouter-api-key-here" ]; then
  echo "ERROR: OPENROUTER_API_KEY is not set in .env. Both gemma-4-31b (chat)"
  echo "       and offload_to_minimax.sh (engineering offload) need it." >&2
  exit 1
fi

if [ -z "${LITELLM_MASTER_KEY:-}" ] || [ "$LITELLM_MASTER_KEY" = "sk-newnew-change-me" ]; then
  echo "ERROR: LITELLM_MASTER_KEY is not set (or still the placeholder) in .env." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 5. Bring the stack up
# ---------------------------------------------------------------------------
echo "==> Starting นิวนิว (NevNew) stack via docker compose..."
docker compose pull
docker compose up -d

echo ""
echo "==> Done. Services:"
echo "    Open-WebUI : http://localhost:3000"
echo "    LiteLLM    : http://localhost:4000  (health: /health/liveliness)"
echo ""
echo "==> Tail logs with: docker compose logs -f"
