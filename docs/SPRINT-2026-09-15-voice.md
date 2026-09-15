# Voice loop close-out — 2026-09-15 (issue #61)

Scope: finish issue #61 ("Voice loop: STT + TTS on Telegram"). Most of the
STT/TTS round trip already landed in `043bde3` (PR #128, 2026-09-15 00:21
+0700 — "web search + security hardening + Telegram voice loop") but the
issue stayed open because that commit skipped the audio-format conversion
the acceptance criteria call for: it sent Telegram's raw OGG/Opus bytes
straight to Groq Whisper (worked, since Whisper accepts ogg, but not per
spec) and replied with gTTS's MP3 as a generic audio attachment
(`send_audio`) instead of a native Telegram voice bubble
(`send_voice`, which requires OGG/Opus). This sprint adds the missing
ffmpeg conversion both directions.

## What shipped

- `telegram-bot/bot.py`:
  - `_run_ffmpeg()` — shared async helper that pipes bytes through `ffmpeg`
    via stdin/stdout (no disk I/O), used by both conversions below.
  - `_ogg_to_wav()` — converts an incoming Telegram voice note (OGG/Opus)
    to 16kHz mono WAV before it's sent to Groq's Whisper endpoint. If the
    conversion fails (ffmpeg missing/errors), `_run_stt` falls back to
    sending the original OGG bytes rather than failing the turn outright.
  - `_mp3_to_ogg_opus()` — converts gTTS's MP3 output to OGG/Opus
    (Telegram's native voice-note codec) so replies arrive as a proper
    voice bubble via `send_voice` instead of a downloadable audio file.
    If this conversion fails, `handle_voice` falls back to the previous
    behavior (`send_audio` with the MP3), and if TTS itself fails, it
    falls back further to a plain text reply (`TTS_FALLBACK_PREFIX`) — the
    existing three-tier fallback chain from #128 is preserved, just with
    ffmpeg as an additional non-fatal step in the middle of each leg.
- `telegram-bot/Dockerfile`: added `ffmpeg` to the apt-get install line
  (kept in the final image — unlike `curl`/`gnupg` it isn't purged after
  use, since `bot.py` shells out to it at runtime).
- `.env.example`: no change needed — `GROQ_API_KEY` was already documented
  there (line 26) ahead of this work, per the issue's provider decision.

## Known limitations

- **Thai transcription accuracy**: not independently benchmarked this
  session — Groq's `whisper-large-v3-turbo` is generally strong on Thai
  but the PO should sanity-check a few real voice notes (see manual
  verification below) rather than assume parity with English.
- **Latency budget**: the acceptance criteria target ~15s round trip for a
  ~10s note. STT (Groq, fast) + ai-core chat turn (~1.2–5.8s per Sprint 7's
  numbers) + gTTS (network call to Google, not instant) + two ffmpeg
  passes (sub-second each for short clips) should comfortably fit under
  15s in the common case, but this wasn't load-tested; if it's ever slow,
  the ai-core leg is the most likely bottleneck (see #71's history), not
  the new ffmpeg steps.
- **gTTS has no SLA** — it's the free, keyless Google Translate TTS
  endpoint, not an official API. It can rate-limit or change without
  notice; `_run_tts`'s try/except already treats any failure as "fall
  back to text," so a gTTS outage degrades the bot to text-only replies
  for voice notes rather than breaking anything.
- **ffmpeg failures degrade gracefully but silently** (to the user) — a
  failed OGG/Opus reply conversion still delivers audio (as an MP3
  attachment), and a failed WAV conversion still attempts STT on the raw
  OGG. Both paths log a warning; nothing is user-visible unless every
  fallback in that leg also fails.

## Manual verification steps (for the PO, live)

1. Rebuild and recreate the `telegram-bot` container (required — the
   Dockerfile changed): `docker compose build telegram-bot && docker
   compose up -d telegram-bot`.
2. Confirm `GROQ_API_KEY` is set in the real `.env` (already required by
   the pre-existing STT path; unchanged by this work).
3. Send a ~10s Thai voice note to the bot on Telegram from the owner
   account.
4. Expect: a spoken reply arrives as a native Telegram voice bubble
   (waveform icon, not a downloadable audio file) within roughly 15s.
5. Check `docker logs nevnew-telegram-bot-1` (or the actual container
   name) for `ffmpeg conversion failed` warnings — none expected on a
   healthy run; if either conversion warning appears, the fallback still
   should have delivered a reply (MP3 audio or text), just check the
   reply arrived instead of silence.
6. Edge case: temporarily unset `GROQ_API_KEY` (in a throwaway shell, not
   `.env`) to confirm the "couldn't hear that" text fallback still fires
   without touching real config, then restore.
