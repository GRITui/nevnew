"""Telegram bot: NevNew's MVP mobile/push channel (issue #11).

Round-trip: Telegram message -> ai-core POST /chat (One Brain, #70:
persona + mem0 memories + tools, background extraction) when USE_AI_CORE
is true. Falls back to the `cline` CLI (9arm gateway / qwen3.8-27b-fp8)
and then the `claude` CLI (same gateway, same model) when ai-core is
unreachable — the whole bot runs on one provider/model, no separate
account/quota. This bypasses LiteLLM/OpenRouter entirely for the chat
path. Persona (NEVNEW_SYSTEM_PROMPT, from callbacks/nevnew_persona_prompt.py)
is applied server-side by ai-core on the primary path, and passed via CLI
flags on the fallback path, since the chat path is not routed through
LiteLLM's own persona callback (callbacks/nevnew_persona.py).

The CLI backends are full agent CLIs, not chat APIs, so both run
locked-down (an isolated scratch cwd they can't escape, no tool approval)
and their raw output goes through a fail-closed safety gate before ever
reaching the user — see _run_cline(), _run_claude_sonnet(), and
_gate_agent_reply() below. ai-core replies pass through _clean_reply_text
(length cap + markdown-image strip); its server-side tool loop is
intentional (that's the One Brain path).
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
# Fallback model. Defaults to "sonnet" (Anthropic account) for backward
# compat, but the container env pins it to the same 9arm gateway model as
# the primary (see docker-compose.yml) so the whole bot runs on one model.
CLAUDE_FALLBACK_MODEL = os.environ.get("CLAUDE_FALLBACK_MODEL", "sonnet")

# One Brain prep (#70): ai-core route plumbing. NOT wired into any handler
# this sprint (cutover is future work) — USE_AI_CORE=false keeps the
# current cline-primary CLI path. ai-core already serves POST /chat with
# persona + memory retrieval + extraction (see ai-core/nevnew_ai_core/).
AICORE_BASE_URL = os.environ.get("AICORE_BASE_URL", "http://ai-core:8000")
AICORE_API_KEY = os.environ.get("AICORE_API_KEY", "")
USE_AI_CORE = os.environ.get("USE_AI_CORE", "false").lower() == "true"
AICORE_TIMEOUT_SECONDS = 60

# Voice loop (issue #61): STT via Groq Whisper (OpenAI-compatible
# /audio/transcriptions, GROQ_API_KEY already in .env), TTS via gTTS
# (free, Thai-capable; swap seam for an OpenAI-compatible Thai TTS when
# one is available — see _run_tts). Voice notes arrive as OGG/Opus;
# replies go back as MP3 audio messages (OGG voice-bubble upgrade later).
VOICE_ENABLED = os.environ.get("VOICE_ENABLED", "true").lower() == "true"
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_STT_URL = os.environ.get("GROQ_STT_URL", "https://api.groq.com/openai/v1/audio/transcriptions")
GROQ_STT_MODEL = os.environ.get("GROQ_STT_MODEL", "whisper-large-v3-turbo")
STT_TIMEOUT_SECONDS = 60
TTS_LANG = os.environ.get("TTS_LANG", "th")
TTS_MAX_CHARS = 500

VOICE_TROUBLE_REPLY = "⚠️ Couldn't hear that voice note — try again or send text."
TTS_FALLBACK_PREFIX = "🔊 Voice reply failed — here it is in text:\n\n"

os.makedirs(CLINE_SCRATCH_CWD, exist_ok=True)
os.makedirs(CLAUDE_SONNET_SCRATCH_CWD, exist_ok=True)


async def _run_aicore(messages: list[dict], user_id: str) -> str | None:
    """POST `messages` to ai-core /chat (One Brain, issue #70).

    user_id is namespaced per channel (e.g. "telegram:<id>") so mem0
    memories stay isolated until the cross-channel mapping lands.
    Returns the reply string, or None on any failure (logs, never raises).
    """
    try:
        headers = {"Authorization": f"Bearer {AICORE_API_KEY}"} if AICORE_API_KEY else {}
        async with httpx.AsyncClient(timeout=AICORE_TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{AICORE_BASE_URL}/chat",
                json={
                    "user_id": user_id,
                    "messages": messages,
                    "channel": "telegram",
                    "store_memories": True,
                },
                headers=headers,
            )
            response.raise_for_status()
            return response.json().get("reply")
    except Exception as exc:  # noqa: BLE001 — helper must never raise
        logger.warning("ai-core chat failed: %s", exc)
        return None


async def _reset_aicore_memory(user_id: str) -> bool:
    """Best-effort ai-core memory reset (One Brain, issue #70).

    Returns True on success. Never raises — the /reset command clears
    local history regardless.
    """
    try:
        headers = {"Authorization": f"Bearer {AICORE_API_KEY}"} if AICORE_API_KEY else {}
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"{AICORE_BASE_URL}/memory/users/{user_id}/reset",
                headers=headers,
            )
            response.raise_for_status()
            return True
    except Exception as exc:  # noqa: BLE001 — reset must not fail the command
        logger.warning("ai-core memory reset failed: %s", exc)
        return False


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
    """Run `prompt` through the `claude` CLI, as a fallback when the cline
    primary fails or is rejected. Returns (text, had_tool_calls).

    The container env points the CLI at the 9arm gateway's Anthropic-format
    endpoint (ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN, see
    docker-compose.yml) running CLAUDE_FALLBACK_MODEL — by default the same
    model as the primary, so the whole bot runs on one provider/model with
    no separate account/quota. --allowedTools "" plus --permission-mode
    manual is the fail-closed backstop (no human present to approve
    anything headless).
    """
    cmd = [
        "claude", "-p", prompt,
        "--model", CLAUDE_FALLBACK_MODEL,
        "--system-prompt", NEVNEW_SYSTEM_PROMPT,
        "--output-format", "json",
        "--allowedTools", "",
        "--permission-mode", "manual",
        "--add-dir", CLAUDE_SONNET_SCRATCH_CWD,
        # The 9arm gateway (vllm backend) rejects the CLI's default
        # reasoning effort "high" — it only accepts xhigh/medium/low
        # (400 "Unexpected reasoning effort high"). medium keeps latency
        # down on the fallback path.
        "--effort", "medium",
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
    user_id = update.effective_user.id
    _history.pop(user_id, None)
    # Best-effort: also clear persisted mem0 memories for this channel
    # identity (One Brain, #70). Local history clears regardless.
    await _reset_aicore_memory(f"telegram:{user_id}")
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

    reply_text = await _answer_text(user.id, update.effective_chat.id, update.message.text, history, context)

    if reply_text is None:
        logger.error("All chat backends failed for user_id=%s", user.id)
        # Roll back the user turn we just recorded — it never got a reply,
        # so keeping it would desync history from what the model actually saw.
        history.pop()
        await update.message.reply_text(TROUBLE_REPLY)
        return

    history.append({"role": "assistant", "content": reply_text})
    history[:] = history[-MAX_HISTORY_MESSAGES:]
    await update.message.reply_text(reply_text)


async def _answer_text(
    user_id: int,
    chat_id: int,
    prompt_text: str,
    history: list[dict],
    context: ContextTypes.DEFAULT_TYPE,
) -> str | None:
    """Shared text→reply chain (text messages + voice transcripts, #61).

    ai-core primary → cline → claude fallback. Returns the reply or None
    if every backend failed. Never raises.
    """
    reply_text = None
    if USE_AI_CORE:
        # One Brain primary (#70): resident ai-core service (persona +
        # mem0 retrieval + tools + background extraction), ~1.3s typical
        # (#71). Typing indicator while generating; CLI path below stays
        # as the fallback tier.
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        except Exception as exc:  # noqa: BLE001 — cosmetic, never block
            logger.debug("typing indicator failed: %s", exc)
        raw_text = await _run_aicore(history, f"telegram:{user_id}")
        reply_text = _clean_reply_text(raw_text)
        if reply_text is None:
            logger.warning("ai-core primary failed — falling back to cline for user_id=%s", user_id)

    if reply_text is None:
        raw_text, had_tool_calls = await _run_cline(prompt_text)
        reply_text = _gate_agent_reply(raw_text, had_tool_calls, source="cline primary")

    if reply_text is None:
        logger.warning("Cline primary failed/rejected — falling back to claude sonnet for user_id=%s", user_id)
        raw_text, had_tool_calls = await _run_claude_sonnet(prompt_text)
        reply_text = _gate_agent_reply(raw_text, had_tool_calls, source="claude sonnet fallback")

    return reply_text


async def _run_stt(ogg_bytes: bytes) -> str | None:
    """Transcribe a Telegram voice note via Groq Whisper (#61).

    Returns the transcript text, or None on any failure (logs, never
    raises). OpenAI-compatible multipart endpoint.
    """
    if not GROQ_API_KEY:
        logger.error("GROQ_API_KEY not set — cannot run STT")
        return None
    try:
        async with httpx.AsyncClient(timeout=STT_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                GROQ_STT_URL,
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                data={"model": GROQ_STT_MODEL, "response_format": "json"},
                files={"file": ("voice.ogg", ogg_bytes, "audio/ogg")},
            )
            resp.raise_for_status()
            text = resp.json().get("text", "")
            return text.strip() or None
    except Exception as exc:  # noqa: BLE001 — STT must never raise
        logger.warning("STT failed: %s", exc)
        return None


def _clean_for_tts(text: str) -> str:
    """Make chat text TTS-friendly: no markdown/lists/emojis, capped."""
    cleaned = re.sub(r"[*_`#>|~]", "", text)
    cleaned = re.sub(
        "[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F]",
        "",
        cleaned,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > TTS_MAX_CHARS:
        cleaned = cleaned[:TTS_MAX_CHARS].rstrip()
    return cleaned


async def _run_tts(text: str) -> bytes | None:
    """Synthesize Thai speech via gTTS (#61).

    Swap seam: replace this body with an OpenAI-compatible Thai TTS call
    when one is available; callers only need MP3 bytes back. Returns None
    on any failure (logs, never raises).
    """
    try:
        from gtts import gTTS  # noqa: F401 — availability check

        speech = _clean_for_tts(text)
        if not speech:
            return None
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: _synthesize_mp3(speech))
    except Exception as exc:  # noqa: BLE001 — TTS must never raise
        logger.warning("TTS failed: %s", exc)
        return None


def _synthesize_mp3(speech: str) -> bytes:
    import io

    from gtts import gTTS

    buf = io.BytesIO()
    gTTS(text=speech, lang=TTS_LANG, slow=False).write_to_fp(buf)
    return buf.getvalue()


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or update.message is None or update.message.voice is None:
        return

    if user.id != OWNER_ID:
        logger.warning("Ignored voice from non-owner user_id=%s", user.id)
        return

    if not VOICE_ENABLED:
        await update.message.reply_text("Voice replies are off — send text.")
        return

    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="record_voice")
    except Exception as exc:  # noqa: BLE001 — cosmetic, never block
        logger.debug("record_voice indicator failed: %s", exc)

    tg_file = await update.message.voice.get_file()
    ogg_bytes = bytes(await tg_file.download_as_bytearray())

    transcript = await _run_stt(ogg_bytes)
    if not transcript:
        logger.warning("STT failed for user_id=%s", user.id)
        await update.message.reply_text(VOICE_TROUBLE_REPLY)
        return

    history = _history.setdefault(user.id, [])
    history.append({"role": "user", "content": f"[voice] {transcript}"})
    history[:] = history[-MAX_HISTORY_MESSAGES:]

    reply_text = await _answer_text(user.id, update.effective_chat.id, transcript, history, context)
    if reply_text is None:
        logger.error("All chat backends failed for voice user_id=%s", user.id)
        history.pop()
        await update.message.reply_text(TROUBLE_REPLY)
        return

    history.append({"role": "assistant", "content": reply_text})
    history[:] = history[-MAX_HISTORY_MESSAGES:]

    audio = await _run_tts(reply_text)
    if audio is None:
        await update.message.reply_text(TTS_FALLBACK_PREFIX + reply_text)
        return

    await context.bot.send_audio(
        chat_id=update.effective_chat.id,
        audio=audio,
        title="NevNew voice reply",
        caption=reply_text[:1024] if len(reply_text) > 1024 else reply_text,
    )


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
    app.add_handler(MessageHandler(filters.VOICE & ~filters.COMMAND, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, handle_photo))
    logger.info("NevNew Telegram bot starting (owner_id=%s, primary=cline/%s via %s)", OWNER_ID, CLINE_MODEL, CLINE_PROVIDER)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
