"""Telegram bot: NevNew's MVP mobile/push channel (issue #11).

Round-trip: Telegram message -> `cline` CLI (9arm gateway / qwen3.8-27b-fp8,
the same provider+model as the Cline session this bot mirrors) -> reply.
This bypasses LiteLLM/OpenRouter entirely for the chat path. Persona
(NEVNEW_SYSTEM_PROMPT, from callbacks/nevnew_persona_prompt.py) is passed to
the chat backend via cline's -s system-prompt flag (and prepended into the
vision path's system prompt), since the chat path is not routed through
LiteLLM's own persona callback (callbacks/nevnew_persona.py).

If Cline fails or its output is rejected by the safety gate, falls back to
the `claude` CLI running Sonnet (a fully separate account/quota,
authenticated via CLAUDE_CODE_OAUTH_TOKEN — see _run_claude_sonnet()).
LiteLLM still serves Open-WebUI directly and is untouched.

Both backends are full agent CLIs, not chat APIs, so both are run
locked-down (an isolated scratch cwd they can't escape, no tool approval)
and their raw output goes through a fail-closed safety gate before ever
reaching the user — see _run_cline(), _run_claude_sonnet(), and
_gate_agent_reply() below.
"""

import asyncio
import base64
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

# Per-user in-memory conversation history, capped so context/cost stay
# bounded. Resets on bot restart — acceptable for MVP (see BACKLOG.md if this
# ever needs to survive restarts).
MAX_HISTORY_MESSAGES = 20
_history: dict[int, list[dict[str, str]]] = {}

TROUBLE_REPLY = "⚠️ Having some technical trouble reaching NevNew right now — try again in a bit."
VISION_TROUBLE_REPLY = "⚠️ Couldn't read that image right now — try again in a bit."
MAX_TELEGRAM_MESSAGE_LENGTH = 4000

# Image path bypasses the cline/claude agent CLIs entirely (see module
# docstring) — neither has a non-tool-call way to ingest an arbitrary image
# under this bot's calling convention, and the chat gate below rejects any
# reply involving a tool call. Instead this is a single deterministic
# OpenRouter vision call, same OPENROUTER_API_KEY already in .env / passed
# into this container via docker-compose's env_file.
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_VISION_MODEL = os.environ.get("OPENROUTER_VISION_MODEL", "google/gemini-2.5-flash")
OPENROUTER_VISION_MAX_TOKENS = 1024
OPENROUTER_VISION_TIMEOUT_SECONDS = 45
VISION_SYSTEM_SUFFIX = (
    "\n\nThe user just sent an image. First transcribe any visible text "
    "verbatim, then briefly respond about the image in your own voice."
)

# Both backends below are full agent CLIs, not chat APIs, so both run
# locked down: an isolated scratch cwd they can't escape, no tool approval,
# and the raw output goes through _gate_agent_reply() before ever reaching
# the user. Only the latest message text is sent (not full history).
#
# Primary is the Cline CLI, pinned to the same provider+model as the Cline
# session this bot mirrors: the 9arm OpenAI-compatible gateway
# (https://gateway.9arm.co/v1) serving qwen3.8-27b-fp8. The provider's
# baseUrl/model live in the baked CLINE_CONFIG_DIR providers.json (see
# Dockerfile); the API key is NINEARM_API_KEY, passed at runtime via -k from
# the container env (env_file: .env) so no secret is baked into the image.
NINEARM_API_KEY = os.environ.get("NINEARM_API_KEY", "")
CLINE_PROVIDER = os.environ.get("CLINE_PROVIDER", "openai-compatible")
CLINE_MODEL = os.environ.get("CLINE_MODEL", "qwen3.8-27b-fp8")
CLINE_CONFIG_DIR = os.environ.get("CLINE_CONFIG_DIR", "/app/cline-config")
CLINE_SCRATCH_CWD = "/app/cline-scratch"
CLINE_TIMEOUT_SECONDS = 45

CLAUDE_SONNET_SCRATCH_CWD = "/app/claude-scratch"
CLAUDE_SONNET_TIMEOUT_SECONDS = 45

os.makedirs(CLINE_SCRATCH_CWD, exist_ok=True)
os.makedirs(CLAUDE_SONNET_SCRATCH_CWD, exist_ok=True)


async def _run_cline(prompt: str) -> tuple[str | None, bool]:
    """Run `prompt` through the `cline` CLI (9arm gateway / qwen3.8-27b-fp8).

    Returns (text, had_tool_calls). Cline is a full agent CLI, so it runs
    locked down: -c pins it to an isolated scratch cwd it can only touch,
    and the gate below rejects the reply outright if ANY tool call was
    attempted (read from the iteration_end hadToolCalls/toolCallCount events
    in cline's --json stream). Auth is NINEARM_API_KEY passed via -k; the
    provider's baseUrl/model come from the baked CLINE_CONFIG_DIR
    providers.json. The persona is passed as the -s system prompt.
    """
    if not NINEARM_API_KEY:
        logger.error("NINEARM_API_KEY not set — cannot run cline primary")
        return None, False

    # cline's CLI treats a whitespace-free positional as a potential subcommand
    # (e.g. "Hi" -> "Unknown command or unquoted prompt: Hi"). Guarantee the
    # prompt is parsed as a prompt by ensuring it contains whitespace.
    prompt_arg = prompt if any(c.isspace() for c in prompt) else f" {prompt}"

    cmd = [
        "cline",
        "--config", CLINE_CONFIG_DIR,
        "--data-dir", f"{CLINE_CONFIG_DIR}/data",
        "--json",
        "-P", CLINE_PROVIDER,
        "-k", NINEARM_API_KEY,
        "-m", CLINE_MODEL,
        "-s", NEVNEW_SYSTEM_PROMPT,
        "-c", CLINE_SCRATCH_CWD,
        "-t", str(CLINE_TIMEOUT_SECONDS),
        prompt_arg,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=CLINE_SCRATCH_CWD,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=CLINE_TIMEOUT_SECONDS + 10,
        )
    except (asyncio.TimeoutError, OSError) as exc:
        logger.error("cline primary failed to run: %s", exc)
        return None, False

    if proc.returncode != 0:
        logger.error(
            "cline primary exited %s: %s", proc.returncode, stderr.decode(errors="replace")[-2000:]
        )
        return None, False

    had_tool_calls = False
    run_result_text = None
    run_result_finish = None
    done_text = None
    for line in stdout.decode(errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = event.get("type", "")
        if etype == "run_result":
            run_result_text = event.get("text")
            run_result_finish = event.get("finishReason")
            continue
        if etype != "agent_event":
            continue
        inner = event.get("event") or {}
        itype = inner.get("type", "")
        if itype == "iteration_end":
            if inner.get("hadToolCalls") or inner.get("toolCallCount", 0) > 0:
                had_tool_calls = True
        elif itype == "done":
            done_text = inner.get("text")
        elif itype in ("content_start", "content_end") and inner.get("contentType") == "tool":
            # Defensive: a tool-typed content event also means a tool was used.
            had_tool_calls = True

    if run_result_finish == "error":
        logger.error("cline primary returned an error result: %s", (run_result_text or "")[:500])
        return None, False

    final_text = run_result_text if run_result_text is not None else done_text
    return final_text, had_tool_calls


async def _run_claude_sonnet(prompt: str) -> tuple[str | None, bool]:
    """Run `prompt` through the `claude` CLI (Sonnet), as a fallback when
    OpenCode Go is unavailable. Returns (text, had_tool_calls).

    Auth is CLAUDE_CODE_OAUTH_TOKEN (set in the container env — see
    docker-compose.yml), NOT an API key, and NOT --bare: --bare only reads
    ANTHROPIC_API_KEY/apiKeyHelper and never OAuth, which would silently
    break auth here. --allowedTools "" plus --permission-mode manual is the
    fail-closed backstop (no human present to approve anything headless).
    """
    cmd = [
        "claude", "-p", prompt,
        "--model", "sonnet",
        "--system-prompt", NEVNEW_SYSTEM_PROMPT,
        "--output-format", "json",
        "--allowedTools", "",
        "--permission-mode", "manual",
        "--add-dir", CLAUDE_SONNET_SCRATCH_CWD,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=CLAUDE_SONNET_SCRATCH_CWD,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=CLAUDE_SONNET_TIMEOUT_SECONDS + 10
        )
    except (asyncio.TimeoutError, OSError) as exc:
        logger.error("claude sonnet fallback failed to run: %s", exc)
        return None, False

    if proc.returncode != 0:
        logger.error(
            "claude sonnet fallback exited %s: %s",
            proc.returncode, stderr.decode(errors="replace")[-2000:],
        )
        return None, False

    try:
        result = json.loads(stdout.decode(errors="replace"))
    except json.JSONDecodeError as exc:
        logger.error("claude sonnet fallback returned unparseable output: %s", exc)
        return None, False

    had_tool_calls = bool(result.get("permission_denials"))
    if result.get("is_error"):
        had_tool_calls = had_tool_calls or result.get("subtype") == "error_max_turns"
        return None, had_tool_calls
    return result.get("result"), had_tool_calls


def _clean_reply_text(raw_text: str | None) -> str | None:
    """Shared cleanup: strip stray markdown-image syntax and cap length.

    Used by both the agent-CLI chat gate below and the vision path, which
    has no had_tool_calls concept of its own (a single API call can't
    attempt a tool call).
    """
    if not raw_text or not raw_text.strip():
        return None
    text = raw_text.strip()
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text).strip()
    if not text:
        return None
    if len(text) > MAX_TELEGRAM_MESSAGE_LENGTH:
        text = text[:MAX_TELEGRAM_MESSAGE_LENGTH].rstrip() + "…"
    return text


def _gate_agent_reply(raw_text: str | None, had_tool_calls: bool, *, source: str) -> str | None:
    """Safety gate between an agent CLI's output and the Telegram user.

    Both OpenCode and the Claude CLI are full agents, not chat APIs — this
    rejects anything that isn't a plain conversational answer, so a reply
    never leaks tool-call attempts or an empty/runaway response straight to
    the user. Fails closed: any rejection here means the caller falls
    through to the next backend, or to the ordinary "technical trouble"
    reply if there is none.
    """
    if had_tool_calls:
        logger.warning("%s attempted a tool call — rejected by chat gate", source)
        return None
    return _clean_reply_text(raw_text)


async def _run_vision_ocr(image_bytes: bytes, caption: str | None) -> str | None:
    """Send an image to a vision-capable OpenRouter model, return the reply.

    Deliberately bypasses opencode/claude (see module docstring + the
    OPENROUTER_VISION_MODEL comment above) — this is a single non-agentic
    HTTP call, so there's no tool-call surface to gate against.
    """
    if not OPENROUTER_API_KEY:
        logger.error("OPENROUTER_API_KEY not set — cannot run vision OCR")
        return None

    image_b64 = base64.b64encode(image_bytes).decode()
    user_content: list[dict] = [
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
    ]
    user_content.append({"type": "text", "text": caption or "What does this image show?"})

    payload = {
        "model": OPENROUTER_VISION_MODEL,
        "max_tokens": OPENROUTER_VISION_MAX_TOKENS,
        "messages": [
            {"role": "system", "content": NEVNEW_SYSTEM_PROMPT + VISION_SYSTEM_SUFFIX},
            {"role": "user", "content": user_content},
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=OPENROUTER_VISION_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
                json=payload,
            )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
        logger.error("vision OCR call failed: %s", exc)
        return None


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

    raw_text, had_tool_calls = await _run_cline(update.message.text)
    reply_text = _gate_agent_reply(raw_text, had_tool_calls, source="cline primary")

    if reply_text is None:
        logger.warning("Cline primary failed/rejected — falling back to claude sonnet for user_id=%s", user.id)
        raw_text, had_tool_calls = await _run_claude_sonnet(update.message.text)
        reply_text = _gate_agent_reply(raw_text, had_tool_calls, source="claude sonnet fallback")

    if reply_text is None:
        logger.error("Both cline primary and claude sonnet fallback failed for user_id=%s", user.id)
        # Roll back the user turn we just recorded — it never got a reply,
        # so keeping it would desync history from what the model actually saw.
        history.pop()
        await update.message.reply_text(TROUBLE_REPLY)
        return

    history.append({"role": "assistant", "content": reply_text})
    history[:] = history[-MAX_HISTORY_MESSAGES:]
    await update.message.reply_text(reply_text)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or update.message is None or not update.message.photo:
        return

    if user.id != OWNER_ID:
        logger.warning("Ignored photo from non-owner user_id=%s", user.id)
        return

    caption = update.message.caption
    photo = update.message.photo[-1]
    tg_file = await photo.get_file()
    image_bytes = bytes(await tg_file.download_as_bytearray())

    history = _history.setdefault(user.id, [])
    history.append({"role": "user", "content": f"[sent an image]{f' {caption}' if caption else ''}"})
    history[:] = history[-MAX_HISTORY_MESSAGES:]

    raw_text = await _run_vision_ocr(image_bytes, caption)
    reply_text = _clean_reply_text(raw_text)

    if reply_text is None:
        logger.error("Vision OCR failed for user_id=%s", user.id)
        history.pop()
        await update.message.reply_text(VISION_TROUBLE_REPLY)
        return

    history.append({"role": "assistant", "content": reply_text})
    history[:] = history[-MAX_HISTORY_MESSAGES:]
    await update.message.reply_text(reply_text)


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, handle_photo))
    logger.info("NevNew Telegram bot starting (owner_id=%s, primary=cline/%s via %s)", OWNER_ID, CLINE_MODEL, CLINE_PROVIDER)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
