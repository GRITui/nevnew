# One Brain (#70) — PREP ONLY, no cutover

Design note for routing Telegram through ai-core with persistent,
cross-channel history. This sprint lands the plumbing only: the helper
exists but no handler calls it.

## Goal (from #70)

One continuous context across channels. Today Telegram history is
in-memory (last 20, resets on restart), Open-WebUI keeps its own, and
mem0 extraction only runs on the ai-core path — so Telegram chats never
become long-term memories.

## Target route (cutover work, NOT this sprint)

Telegram text → `POST http://ai-core:8000/chat` with:

```json
{
  "user_id": "telegram:<telegram_user_id>",
  "messages": [{"role": "user", "content": "..."}],
  "channel": "telegram",
  "store_memories": true
}
```

ai-core already supports this: persona + memory retrieval + tool loop +
background extraction, plus `/memory/*` proxies and
`POST /memory/users/{id}/reset` (see `ai-core/nevnew_ai_core/main.py`).
For this single-owner prep, `<telegram_user_id>` is `OWNER_ID`.

## Prep landed here

- `telegram-bot/bot.py::_run_aicore(messages)` — POSTs the namespaced
  payload with `Bearer AICORE_API_KEY`, 60s timeout; returns the reply
  string or `None` on any failure (logs, never raises). No handler calls
  it; history logic untouched.
- `docker-compose.yml` passes `AICORE_BASE_URL`, `AICORE_API_KEY`,
  `USE_AI_CORE=false` into `telegram-bot`.

## user_id mapping (open)

Open-WebUI user ids differ from Telegram ids. Cross-channel recall
("said in Telegram, ask in Open-WebUI") needs an explicit mapping table
owned by the orchestrator. Future work — this prep keeps the
`telegram:` namespace only.

## Fallback tiers

Fallback model tiers stay inside ai-core. Moving the cline/claude CLIs
into ai-core happens later, not this sprint.

## Rollback

`USE_AI_CORE=false` (default) keeps the current cline-primary CLI path.
Unset the helper by leaving the flag off.

## Out of scope

Cutover, handler wiring, memory writes from Telegram, Open-WebUI id
mapping, CLI migration into ai-core.
