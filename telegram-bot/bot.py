"""Telegram bot: NevNew's MVP mobile/push channel (issue #11).

Round-trip: Telegram message -> LiteLLM (/v1/chat/completions, model=NevNew)
-> reply. Persona is injected by LiteLLM's own callback (see
callbacks/nevnew_persona.py) on this path — no duplicate persona logic here.

On a 429 (OpenRouter's shared free-tier daily cap exhausted), falls back to
the `cline` CLI (a separate account/quota) instead — see _run_cline() and
_gate_cline_reply() below. That path does duplicate the persona prompt
(imported from callbacks/nevnew_persona_prompt.py, not re-typed) since it
bypasses LiteLLM entirely.
"""

import asyncio
import json
import logging
import os
import re

import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from nevnew_persona_prompt import NEVNEW_SYSTEM_PROMPT

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("nevnew-telegram-bot")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OWNER_ID = int(os.environ["TELEGRAM_OWNER_ID"])
LITELLM_MASTER_KEY = os.environ["LITELLM_MASTER_KEY"]
LITELLM_BASE_URL = os.environ.get("LITELLM_BASE_URL", "http://litellm:4000/v1")
MODEL_NAME = os.environ.get("NEVNEW_MODEL_NAME", "NevNew")

# Per-user in-memory conversation history, capped so context/cost stay
# bounded. Resets on bot restart — acceptable for MVP (see BACKLOG.md if this
# ever needs to survive restarts).
MAX_HISTORY_MESSAGES = 20
_history: dict[int, list[dict[str, str]]] = {}

TROUBLE_REPLY = "⚠️ Having some technical trouble reaching NevNew right now — try again in a bit."

# cline fallback (rotate to the already-authenticated Cline account — a
# separate quota pool from the OpenRouter key LiteLLM uses — when LiteLLM
# 429s from OpenRouter's shared daily free-model cap). `cline` is a full
# coding agent CLI, not a chat API, so this is intentionally locked down:
# no tool approval, an empty scratch cwd, and the raw output goes through
# _gate_cline_reply() below before ever reaching the user.
CLINE_CONFIG_DIR = os.environ.get("CLINE_CONFIG_DIR", "/cline-home")
CLINE_SCRATCH_CWD = "/app/cline-scratch"
CLINE_TIMEOUT_SECONDS = 45
MAX_TELEGRAM_MESSAGE_LENGTH = 4000

os.makedirs(CLINE_SCRATCH_CWD, exist_ok=True)


async def _run_cline(prompt: str) -> tuple[str | None, bool]:
    """Run `prompt` through the cline CLI. Returns (text, had_tool_calls)."""
    cmd = [
        "cline",
        # A bare single-word prompt (e.g. "hi") makes cline's own CLI arg
        # parser bail with "Unknown command or unquoted prompt" — it's
        # apparently ambiguous with a subcommand name internally. A leading
        # space works around it without changing what the model sees.
        # Reproduced directly against `cline` 3.0.61 on 2026-09-05; harmless
        # if a future version fixes it upstream.
        f" {prompt}",
        "--provider", "cline",
        "--auto-approve", "false",
        "--json",
        "--cwd", CLINE_SCRATCH_CWD,
        "--config", CLINE_CONFIG_DIR,
        "--data-dir", f"{CLINE_CONFIG_DIR}/data",
        "--system", NEVNEW_SYSTEM_PROMPT,
        "--timeout", str(CLINE_TIMEOUT_SECONDS),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=CLINE_TIMEOUT_SECONDS + 10
        )
    except (asyncio.TimeoutError, OSError) as exc:
        logger.error("cline fallback failed to run: %s", exc)
        return None, False

    if proc.returncode != 0:
        logger.error(
            "cline fallback exited %s: %s", proc.returncode, stderr.decode(errors="replace")[-2000:]
        )
        return None, False

    text = None
    had_tool_calls = False
    for line in stdout.decode(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "run_result":
            text = event.get("text")
        elif event.get("type") == "agent_event":
            inner = event.get("event", {})
            if inner.get("type") == "iteration_end" and inner.get("hadToolCalls"):
                had_tool_calls = True
    return text, had_tool_calls


def _gate_cline_reply(raw_text: str | None, had_tool_calls: bool) -> str | None:
    """Safety gate between cline's agentic output and the Telegram user.

    cline is a coding agent, not a chat API — this rejects anything that
    isn't a plain conversational answer, so a fallback reply never leaks
    tool-call attempts or an empty/runaway response straight to the user.
    Fails closed: any rejection here means the caller falls through to the
    ordinary "technical trouble" reply instead.
    """
    if had_tool_calls:
        logger.warning("cline fallback attempted a tool call — rejected by chat gate")
        return None
    if not raw_text or not raw_text.strip():
        return None

    text = raw_text.strip()
    # Persona is text-only (see NEVNEW_SYSTEM_PROMPT) — strip stray
    # markdown-image syntax in case the model emits it anyway.
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text).strip()
    if not text:
        return None
    if len(text) > MAX_TELEGRAM_MESSAGE_LENGTH:
        text = text[:MAX_TELEGRAM_MESSAGE_LENGTH].rstrip() + "…"
    return text


def _is_owner(update: Update) -> bool:
    user = update.effective_user
    return user is not None and user.id == OWNER_ID


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    _history.pop(update.effective_user.id, None)
    await update.message.reply_text("นิวนิว here. Send me anything.")


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    _history.pop(update.effective_user.id, None)
    await update.message.reply_text("Conversation history cleared.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or update.message is None or update.message.text is None:
        return

    if user.id != OWNER_ID:
        logger.warning("Ignored message from non-owner user_id=%s", user.id)
        return

    history = _history.setdefault(user.id, [])
    history.append({"role": "user", "content": update.message.text})
    history[:] = history[-MAX_HISTORY_MESSAGES:]

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{LITELLM_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {LITELLM_MASTER_KEY}"},
                json={"model": MODEL_NAME, "messages": history},
            )
            response.raise_for_status()
            data = response.json()
        reply_text = data["choices"][0]["message"]["content"]
    except httpx.HTTPStatusError as exc:
        # Fall back to cline on any upstream/LiteLLM failure that's likely
        # transient or quota-driven — 429 (rate limit, #11), 402 (credits,
        # #41/#42/#43), and 5xx (upstream overload, #33/#34). 4xx other than
        # 402/429 (e.g. 401/403) are auth errors and shouldn't waste a
        # cline call.
        if exc.response.status_code in (402, 429, 500, 502, 503, 504):
            logger.warning(
                "LiteLLM returned %d — falling back to cline for user_id=%s",
                exc.response.status_code, user.id,
            )
            raw_text, had_tool_calls = await _run_cline(update.message.text)
            fallback_text = _gate_cline_reply(raw_text, had_tool_calls)
            if fallback_text:
                history.append({"role": "assistant", "content": fallback_text})
                history[:] = history[-MAX_HISTORY_MESSAGES:]
                await update.message.reply_text(fallback_text)
                return
        logger.error("LiteLLM round-trip failed: %s", exc)
        # Roll back the user turn we just recorded — it never got a reply,
        # so keeping it would desync history from what the model actually saw.
        history.pop()
        await update.message.reply_text(TROUBLE_REPLY)
        return
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
        logger.error("LiteLLM round-trip failed: %s", exc)
        history.pop()
        await update.message.reply_text(TROUBLE_REPLY)
        return

    history.append({"role": "assistant", "content": reply_text})
    history[:] = history[-MAX_HISTORY_MESSAGES:]
    await update.message.reply_text(reply_text)


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("NevNew Telegram bot starting (owner_id=%s, base_url=%s)", OWNER_ID, LITELLM_BASE_URL)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
