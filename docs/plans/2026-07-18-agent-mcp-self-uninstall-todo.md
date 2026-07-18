# Agent MCP Self-Uninstall — Immutable Iteration TODO

> Created: 2026-07-18
> Branch: `feat/agent-mcp-self-uninstall`
> Baseline: `company/main@43c0ae71`
> Status: complete

## Governance

- The eight workstreams below are the frozen scope for this iteration. Their count and direction must not be changed without explicit user approval.
- The platform provides exact installation identity, authorization, lifecycle cleanup, isolation, truthful errors, and secret boundaries. The Agent decides what to install, inspect, use, and uninstall.
- Listing takes no arguments. Uninstall accepts only the exact `mcp_server_id`; names and fuzzy identifiers are never execution locators.
- Full private connection configuration is visible only to the Agent that created it, only in that current tool result. Shared/enterprise configuration is never projected, and private values never enter history, WebSocket events, logs, or workspace overflow files.
- Production data may be read into an isolated local database for migration verification. No local backend, IM connector, webhook consumer, worker, trigger, scheduler, or timer may run against that data.

## Frozen workstreams

1. [x] **Canonical private installation identity** — Add `owner_agent_id + installation_key`; keep one stable private server per creator/logical installation and isolate it from shared enterprise servers.

2. [x] **Exact list/uninstall Agent contract** — Add zero-argument `list_installed_mcp_servers` and exact-UUID `uninstall_mcp_server`; make both default tools for new and existing Agents.

3. [x] **Creator-only configuration projection** — Store private runtime values in the creator Agent override; redact owner-private override values from REST/admin responses, history, events, logs, and output materialization.

4. [x] **Immediate authorization and cascade cleanup** — Re-check live `AgentTool` assignment before every MCP call; remove only the caller's self-installed assignment and cascade-delete an unreferenced private server, tools, overrides, and assignments without affecting other Agents.

5. [x] **Import-path normalization** — Route direct HTTP, stdio, and Smithery imports through the same private lifecycle authority; return the exact server ID and make retries/reauthorizations idempotent.

6. [x] **Concurrent stdio isolation and AIO-owned timeout** — Use one runtime registration per invocation; enforce the request timeout inside AIO around the complete MCP session and reap stuck stdio children on timeout.

7. [x] **Migration and production-snapshot verification** — Deterministically collapse legacy duplicate tool/assignment rows, enforce uniqueness and cascade constraints, upgrade an isolated read-only production reconstruction, and verify seeding/idempotency without starting any runtime service.

8. [x] **Independent final audit and branch close** — Pass the neutral second audit with no unresolved blocker; complete compilation, linux/amd64 image builds, targeted regression, isolated-environment destruction, and a local feature-branch commit without merge, push, or deployment.

## Verification evidence

- Isolated production reconstruction upgraded to `agent_owned_mcp_installations`; one real duplicate server/tool group and its assignment collision were deterministically consolidated. The management-tool seed assigned both tools to all existing Agents and reran idempotently.
- Related backend regression: 342 passed. Focused lifecycle/security/concurrency regression: 55 passed.
- Real x86 AIO black-box test: three concurrent stuck stdio MCP sessions timed out, every recorded child PID was reaped, and a follow-up browser MCP request succeeded with 21 tools.
- Final linux/amd64 backend and AIO overlay images build successfully; Python compilation and `git diff --check` pass.
- Neutral final delta audit verdict: PASS, with no unresolved release blocker.
