# Digital Employee Platform Architecture Specification

## Purpose

This is a multi-tenant digital-employee platform. Agents have durable identity and workspace state, run a shared tool-capable LLM loop, and communicate through Web, IM, A2A, triggers, tasks, and MCP-facing entry points.

This document defines stable system boundaries. Operational recipes and engineering rules are routed from `.agents/workflows/read_architecture.md`.

## Architectural boundaries

```text
Web / IM / A2A / Trigger / Task / MCP
                 |
          entry-point adapters
                 |
      shared turn + tool execution core
                 |
  sessions/messages/tools/workspace persistence
                 |
 delivery events, WebSocket updates, IM transports
```

- Entry points authenticate and normalize transport context; they must not grow independent LLM/tool loops.
- The shared turn core owns model failover, tool rounds, history assembly, persistence, and completion semantics.
- Transport adapters own provider payloads, identifiers, rate limits, and provider-specific error interpretation.
- `ChatSession` and `ChatMessage` form the durable conversation audit trail. Remote provider identifiers belong in normalized message metadata, not scattered side stores.
- Every tenant-scoped query must enforce tenant and agent/session ownership. A creator `user_id` is not proof that a background or A2A turn has human-admin authority.

## Backend

The FastAPI backend is organized under `backend/app/`:

- `api/`: inbound HTTP, WebSocket, IM, and control-plane adapters.
- `services/llm/`: the shared model and tool-calling loop.
- `services/`: business services, routing, persistence, tools, channel delivery, and recovery.
- `models/` and `schemas/`: durable data and API contracts.
- `alembic/`: PostgreSQL schema evolution.

The central execution path is `call_llm` / `call_llm_with_failover`. Web, IM, A2A, trigger, and task behavior should converge on this core. Long-running turns must be connection-independent; WebSocket disconnects are delivery events, not authorization to destroy a turn.

Model generation settings use provider-neutral runtime values.  Reasoning uses
the nullable seven-level `reasoning_effort` contract and is translated only by
the selected provider adapter; see
`.agents/architecture/model-reasoning-controls.md`.

Read-only runtime configuration is snapshotted before dispatch and the inbound
database transaction ends before provider waits or the tool loop. Tool results,
usage, compaction, delivery state, and other intermediate outcomes use explicit
short transactions so a slow model response does not hold an idle PostgreSQL
transaction or lock.

## Frontend

Agent company, department and personal access grants combine at their highest
level. `agent_permissions` is authoritative; legacy mode fields are compatibility
projections. Creation, settings and MCP writes share one grant service. See
`.agents/architecture/agent-permissions.md` for semantics and migration boundaries.

The React/TypeScript frontend renders durable sessions and live events. Authorization to view, write, or monitor a session is decided by backend policy. Read-only monitoring may receive live events but must remain server-enforced read-only.

Project creation may copy any selectable, visible native Agent in the tenant;
an Agent `use` grant is sufficient and must not be upgraded to a management
requirement. The bootstrap picker and create validation share this eligibility
boundary so the UI cannot offer a source that the create API will reject.

Production nginx configuration is built from `frontend/nginx.conf.template`; a similarly named deployment file is not automatically the production source.

## Conversation model

- Human Web/IM sessions belong to an agent and an authenticated/mapped human identity.
- P2P identity is session-scoped; group identity is message-scoped. Do not erase that distinction to make code look uniform.
- A2A sessions normalize the agent pair. Message queries for A2A history use `conversation_id`, not a caller-specific `ChatMessage.agent_id` filter.
- Non-human turns (`agent`, `trigger`, task-like contexts) do not inherit a creator's administrative visibility.
- Completion/recovery ordering must not rely solely on PostgreSQL transaction-start timestamps.

## Context and memory

- Context budgets derive from the selected model's configured context window,
  configured usage ratio, and provider-reported usage.
- Every durable initiator uses the same context-recovery loop. Historical
  compaction first preserves the configured recent suffix. When the protected
  current turn itself exceeds the budget, recovery may compact all closed old
  assistant/tool rounds inside it without a protected-round floor; the durable
  user anchor and any open tail remain exact. Source material is archived
  before replacement with a validated or lossless-fallback summary.
- Explicit provider overflow recovery is finite and may reduce protected
  historical turns to zero only before the current turn has produced external
  side effects; it retries the same current input unchanged.
- Textual tool results have a bounded model-facing view with a truthful
  truncation marker and exact full-content path. Durable originals and
  multimodal payloads remain intact.
- Product Agent memory is repository/workspace state with explicit loading
  policy. Coding-agent project knowledge is owned by `AGENTS.md` and `.agents/`,
  never personal assistant memory.

## Tools and sandboxes

- Runtime LLM tool definitions normally come from database `tools` rows.
- Builtin database definitions are seeded from `backend/app/services/tool_seeder.py`.
- Explicit tool assignments define runtime enablement: normally `AgentTool`, or a published scene's complete tool-panel configuration for that turn. `is_default` is a seeding template, not a runtime permission fallback.
- Platform code remains neutral to tenant-specific tool or CLI names.
- Uploaded executable tools and AIO sandbox shell/code execution are distinct execution models with different security and lifecycle constraints.

## Channel delivery

All durable outbound visible artifacts produce one normalized delivery receipt attached to a local `ChatMessage`. Ordinary text uses an assistant row; tool-created files/media may reuse the exact outbound tool-call row so the provider side effect and its idempotency intent have one lifecycle anchor. A receipt may contain multiple transport parts because one logical response can produce multiple remote messages.

The pending anchor commits before provider I/O. Commands, acknowledgements, welcome messages, background notifications, files, media, and confirmation artifacts use the same lifecycle; channel code only adapts provider results into parts. Ephemeral protocol indicators such as typing and reactions are explicitly control-plane state, not durable messages.

Provider operations such as recall act through transport adapters over receipt parts. Unsupported capability is explicit data, not a silent success. Provider-native P2P/group mechanics can differ while sharing the same lifecycle contract.

## Inline workspace images

An Agent expresses an inline workspace image with ordinary Markdown and an
Agent-directory-relative path, for example
`![chart](workspace/reports/chart.png)`. The durable `ChatMessage` keeps that
relative reference; platform hosts, login credentials, and expiring storage
signatures are transport projections and must never be persisted into message
content.

Web and H5 render the relative reference through the existing Agent file
download route. Browser image requests use the same login JWT carried in an
HttpOnly cookie; the Authorization header and cookie are two transports for
the same credential, not separate authorization systems. The download route
continues to enforce the current user, Agent access, safe workspace resolution,
and inline file response policy. Image paths use POSIX AgentDir-relative
semantics: no prefix, `/`, and `./` normalize to the same relative path. The
resolver never guesses or prepends a product directory such as `workspace/`.

IM delivery rewrites relative image references only in the outbound provider
payload. It validates the referenced AgentDir image and uses the configured
storage backend's existing `presign_download_url` policy to produce an absolute
temporary URL with an inline response and correct image content type. That URL
is neither uploaded to the provider nor written back to the message. External
HTTP(S) image references pass through unchanged. Markdown parsing, workspace
validation, and projection are shared capabilities; channel adapters must not
implement independent copies. If the local storage backend cannot create an
object-store URL, the same projection emits a short-lived JWT-scoped image URL;
the route serves only the signed Agent, exact normalized path, storage key, and
image content.

## Environment and validation

The supported local integration entry point is the Docker stack exposed through the frontend proxy on port 3008. Backend validation uses containers with the repository mounted at `/app` and an isolated PostgreSQL test database. Host Python/venv validation is unsupported.

See `.agents/architecture/environments-and-operations.md` for the exact safe workflow.

Context budgeting, compaction, tool-result views, and Agent memory loading are
specified in `.agents/architecture/context-and-memory.md`.

## Standard system integrations

External applications use OAuth 2.0 Client Credentials and versioned OpenAPI
business APIs. Client management, delegated identity, employee resource links
and page-independent temporary login follow `.agents/architecture/openapi.md`.
The ordinary identity, permission and conversation owners remain authoritative;
external integrations do not introduce a second chat or authorization runtime.
