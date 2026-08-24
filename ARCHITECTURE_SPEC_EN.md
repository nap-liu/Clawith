# Clawith Architecture Specification

## Purpose

Clawith is a multi-tenant digital-employee platform. Agents have durable identity and workspace state, run a shared tool-capable LLM loop, and communicate through Web, IM, A2A, triggers, tasks, and MCP-facing entry points.

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

## Frontend

The React/TypeScript frontend renders durable sessions and live events. Authorization to view, write, or monitor a session is decided by backend policy. Read-only monitoring may receive live events but must remain server-enforced read-only.

Production nginx configuration is built from `frontend/nginx.conf.template`; a similarly named deployment file is not automatically the production source.

## Conversation model

- Human Web/IM sessions belong to an agent and an authenticated/mapped human identity.
- P2P identity is session-scoped; group identity is message-scoped. Do not erase that distinction to make code look uniform.
- A2A sessions normalize the agent pair. Message queries for A2A history use `conversation_id`, not a caller-specific `ChatMessage.agent_id` filter.
- Non-human turns (`agent`, `trigger`, task-like contexts) do not inherit a creator's administrative visibility.
- Completion/recovery ordering must not rely solely on PostgreSQL transaction-start timestamps.

## Tools and sandboxes

- Runtime LLM tool definitions normally come from database `tools` rows.
- Builtin database definitions are seeded from `backend/app/services/tool_seeder.py`.
- `AgentTool(enabled=True)` is the explicit per-agent enablement boundary; `is_default` is a seeding template, not a runtime permission fallback.
- Platform code remains neutral to tenant-specific tool or CLI names.
- Uploaded executable tools and AIO sandbox shell/code execution are distinct execution models with different security and lifecycle constraints.

## Channel delivery

All outbound visible artifacts should produce one normalized delivery receipt attached to the local `ChatMessage`. A receipt may contain multiple transport parts because one logical response can produce multiple remote messages.

Provider operations such as recall act through transport adapters over receipt parts. Unsupported capability is explicit data, not a silent success. Provider-native P2P/group mechanics can differ while sharing the same lifecycle contract.

## Environment and validation

The supported local integration entry point is the Docker stack exposed through the frontend proxy on port 3008. Backend validation uses containers with the repository mounted at `/app` and an isolated PostgreSQL test database. Host Python/venv validation is unsupported.

See `.agents/architecture/environments-and-operations.md` for the exact safe workflow.
