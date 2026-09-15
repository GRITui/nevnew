# Scope: ElysiaJS / Bun Gateway Adoption

Status: **proposed, not started**. Captures what adopting the
`~/.claude/CLAUDE.md` "Framework 3.0 / Autonomous Edge Engine" architecture
would mean concretely for NevNew, so it can be scheduled as a real sprint
instead of auto-bootstrapped.

## Why

Today NevNew has no unified API gateway or task-queue layer. Each Python
service (`ai-core`, `memory`, `telegram-bot`) talks to Open-WebUI/LiteLLM
directly or via n8n webhooks. There's no single place for:
- a REST/WebSocket surface external clients (a future web UI, mobile client,
  or external integrations) could call instead of going through Open-WebUI
- durable background job state (today: ad hoc `scripts/offload.sh` +
  polling, no queue, no retry/backoff, no audit trail)
- a consistent audit log of what the system did and when

That's the gap Bun + ElysiaJS + BullMQ + Drizzle is meant to fill globally.
Adopting it here is about closing that specific gap, not adopting the
framework for its own sake.

## What "adopt" means for this repo

| Piece | Role in NevNew | Reuse existing service? |
|---|---|---|
| Bun runtime | Hosts the new gateway process | New — no JS runtime in the stack today |
| ElysiaJS (`apps/gateway/`) | REST + `/stream` WS surface | New service in `docker-compose.yml` |
| BullMQ | Queue for offload/background jobs (replaces `scripts/offload.sh` polling) | Needs Redis — **reuse `newnew-redis`**, but on a separate logical DB index (LiteLLM already uses DB 0 for response cache; BullMQ needs its own, e.g. DB 1) |
| Drizzle ORM + Postgres | `tasks` / `task_events` audit tables | Needs Postgres — **reuse `newnew-postgres`**, but a separate database/schema from LiteLLM's `litellm` DB, not the same tables |
| Worker daemon (`apps/gateway/src/worker.ts`) | Executes queued jobs | New process (could run in the same container as the gateway, or a separate `gateway-worker` service) |

Net new Docker services: likely 1–2 (`gateway`, optionally `gateway-worker`).
Net new infra dependencies: none — Redis and Postgres are already running.

## Integration points with existing Python services

- `ai-core`'s existing tool-calling loop (`ai-core/nevnew_ai_core/`) would
  enqueue long-running work (research, document ingest, scheduled tasks —
  see #62/#79/#82, already shipped) onto BullMQ instead of handling it
  synchronously in-process, where that's still ad hoc.
- The gateway would front `ai-core` for anything that needs to be reachable
  from outside the current Telegram/Open-WebUI entry points.
- `task_events` audit log becomes the answer to "what did the system do
  today" — currently scattered across container logs.

## Explicitly out of scope for v1

- Rewriting any existing Python service in Bun/TS — Python stays
- Replacing Open-WebUI or LiteLLM
- The Tier 1/2/3 model-routing matrix and AST-pruning context engine from
  the global framework doc — that's a separate, much larger decision and
  not needed to get gateway + queue value
- Auto-merge / Zero-HITL verification pipeline from the global doc — this
  repo's existing PR + auditor/builder subagent workflow
  (`docs/SPRINT-PLAN-BACKLOG-2026-09-15.md`) stays as the review gate

## Open questions (need a decision before building)

1. **First real workload.** What's the first thing that actually needs a
   queue+gateway to be worth building — is it #67 (background jobs with
   status, Sprint 4) or something else? Recommend building this
   *alongside* #67 rather than as an empty scaffold.
2. **Worker process topology.** One container running both HTTP server and
   BullMQ worker, or split them? Split is more resilient (worker crash
   doesn't take down the API) but is one more service to operate.
3. **Auth on the new REST surface.** Nothing today authenticates
   cross-service calls inside the stack; the gateway would be the first
   service with an external-facing surface, so this needs an answer before
   it's exposed past `newnew-net` (ties into #73, LAN exposure hardening,
   Sprint 1).
4. **Where it lives.** `apps/gateway/` at repo root, per the global spec —
   confirm no naming clash with existing top-level dirs (`ai-core/`,
   `memory/`, `telegram-bot/` already follow a flat-service-per-dir
   convention, so `apps/gateway/` would be the first `apps/`-nested one).

## Suggested sequencing

Do not bootstrap this standalone. Fold it into the existing
`docs/SPRINT-PLAN-BACKLOG-2026-09-15.md` plan:

- Land Sprint 1 (security/reliability, #73/#80/#63/#78) first — the
  gateway shouldn't go on an unhardened stack.
- Build the gateway skeleton (Bun + ElysiaJS + BullMQ + Drizzle, reusing
  `newnew-redis`/`newnew-postgres`) as part of Sprint 4's #67 (background
  jobs with status), since that's the first feature that actually needs a
  durable queue+audit layer — not before.
- Defer the Tier 1/2/3 model-routing and AST-context-compression pieces of
  the global framework indefinitely; revisit only if a concrete pain point
  (token cost, context blowout) shows up.
