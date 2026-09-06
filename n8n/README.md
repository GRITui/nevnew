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
