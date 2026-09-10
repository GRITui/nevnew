# n8n tools (issues #2, #5)

Assets for the "NevNew macOS + GitHub Tools" n8n workflow, capped with an
MCP Server Trigger node so NevNew can call these as tools (same exposure
pattern validated by #1).

- `macos_tools.applescript` — Reminders/Calendar/Notes via AppleScript/EventKit
  (issue #2). Invoked as `osascript macos_tools.applescript <subcommand> [args...]`.
- `mcp-tools.json` — n8n workflow export. One `MCP Server Trigger` node plus
  `toolCode` nodes: 5 wrap `macos_tools.applescript` (issue #2), 7 wrap
  `../scripts/gh_tool.sh`, a read/triage-only allowlisted `gh` wrapper
  (issue #5, bundled in since it shares this workflow and trigger).

## Manual steps required (cannot be automated from here)

1. **macOS Automation permission** — the first time `macos_tools.applescript`
   runs, macOS will prompt to grant the process driving `osascript` (n8n,
   running natively on the host) access to Reminders/Calendar/Notes. Approve
   once in System Settings → Privacy & Security → Automation.
2. **Import and activate the workflow** — in the n8n UI: Import from File →
   `mcp-tools.json`, then activate it. Note the new workflow's MCP Server
   Trigger production URL (`http://localhost:5678/mcp/<its-path>`) and update
   `docker-compose.yml`'s `mcpo` service command to point at it (it currently
   points at the older "NevNew MCP Spike" workflow from #6/#3's testing), then
   `docker compose up -d mcpo` to pick up the change.

No cloud auth for the macOS tools — zero new secrets. The GitHub tools reuse
whatever `gh` auth already exists on the host running n8n.

## Known limitation

`append_note`'s HTML-escaping (`&`/`<`/`>`) correctly blocks arbitrary markup
injection into note bodies, but Notes.app's own body-property re-serialization
strips the trailing `;` off entities on write (e.g. `&amp;` round-trips as
`&amp`, `"` becomes a bare `&quot`) — a macOS Notes quirk, not a bug in this
script. Cosmetic only: text containing `&`, `<`, `>`, or `"` will show mangled
entity codes in the note, but nothing executes as markup.

## O2O Copilot Middleware (sales dashboard + Telegram)

`workflows/o2o-copilot-middleware.json` — a generic copilot endpoint backed by LiteLLM.

Pipeline: Webhook (POST `/webhook/o2o-copilot`, CORS `*`, responds via Respond-to-Webhook) → "Build Messages" Code node (system prompt enforcing a STRICT JSON answer `{"reply": string, "viewSettings": object}`; `viewSettings` keys allow-listed to `channel`, `ops`, `from`, `to`, `trendGranularity`, `showLabels`) → HTTP Request to `http://litellm:4000/v1/chat/completions` with model `NevNew`, temperature 0.2, max_tokens 1024, `Authorization: Bearer {{ $env.LITELLM_MASTER_KEY }}` → "Parse Reply" Code node (strips code fences, JSON-parses, drops unknown viewSettings keys, falls back to a friendly error reply) → Respond to Webhook.

Request body: `{schemaVersion, source, prompt, viewSettings, dataContext, allowedOps}`. Response: `{reply, viewSettings}`. Consumers: the O2O sales dashboard's "Apply with Copilot" box and the Telegram bot `@thkrit_cfg_bot`.

Manual steps:
1. `docker compose up -d n8n` — the compose file now passes `LITELLM_MASTER_KEY` through to the n8n container (needed by the HTTP node's Authorization header). If your n8n blocks env access in nodes (`N8N_BLOCK_ENV_ACCESS_IN_NODE=true`), create a Header Auth credential with the master key and attach it to the "LiteLLM Chat" node instead.
2. In the n8n UI: Import from File → `workflows/o2o-copilot-middleware.json`, then activate it.
3. Smoke test:
```sh
curl -s http://localhost:5678/webhook/o2o-copilot \
  -H 'Content-Type: application/json' \
  -d '{"schemaVersion":1,"source":"test","prompt":"Say OK","viewSettings":{},"dataContext":"Demo context","allowedOps":[]}'
```
Expect `{"reply":"...","viewSettings":{}}`.