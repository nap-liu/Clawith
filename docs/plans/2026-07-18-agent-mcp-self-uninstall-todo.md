# Agent MCP Self-Uninstall — Immutable Iteration TODO

> Created: 2026-07-18
> Branch: `feat/agent-mcp-self-uninstall`
> Baseline: `company/main@43c0ae71`
> Status: complete — architecture implemented and verified

## Governance

- The eight workstreams below are the frozen scope for this iteration. Their count and direction must not be changed without explicit user approval.
- Reuse the existing MCP installation identity, AgentTool provenance, and deduplication paths. Do not introduce a second ownership model or a database migration.
- Listing takes no arguments. Uninstall accepts only the exact `mcp_server_id`; names and fuzzy identifiers are never execution locators.
- The current Agent may inspect config stored on its own installed binding. Observable history and logs preserve the full diagnostic shape and mask only explicit credential values.
- Production data may be read into an isolated local database for migration verification. No local backend, IM connector, webhook consumer, worker, trigger, scheduler, or timer may run against that data.

## Frozen workstreams

1. [x] **Reuse canonical existing identity** — Keep MCPServer, Tool, AgentTool, current import idempotency, and `installed_by_agent_id`; remove `owner_agent_id + installation_key` and all schema changes.

2. [x] **Exact list/uninstall Agent contract** — Add zero-argument `list_installed_mcp_servers` and exact-UUID `uninstall_mcp_server`; make both default tools for new and existing Agents.

3. [x] **Binding-local config and observable redaction** — Store self-install config on the existing AgentTool binding; return it only for the current Agent's own installed binding and mask only explicit credential values in diagnostic projections.

4. [x] **Immediate authorization and minimal cleanup** — Re-check live AgentTool before execution; remove only the caller's self-installed binding, delete an unreferenced Tool through existing logic, and always preserve the shared MCPServer.

5. [x] **Minimal import-path completion** — Preserve direct HTTP, stdio, and Smithery import behavior/deduplication; save binding-local config and return/link the existing exact MCPServer ID.

6. [x] **Shared Agent space with precise AIO cleanup** — Give all stdio calls for one Agent the same workspace/permissions/network space, use a unique entry only as the process handle, enforce timeout inside AIO, and reap only that invocation's child.

7. [x] **No-migration production-snapshot verification** — Prove there is no Alembic/schema delta; verify only the two default Tool seeds and AgentTool assignments against an isolated production reconstruction without starting runtime services.

8. [x] **Final regression and branch close** — Complete compilation, Backend/AIO regression, shared-workspace and stuck-child tests, linux/amd64 builds, environment cleanup, and local feature-branch commit without merge, push, or deployment.

## Verification evidence

- Neutral architecture audit: PASS.
- Relative to `company/main@43c0ae71`: no Alembic or model delta; Alembic remains `canonical_tenant_user (head)`.
- Backend MCP/tool regression: `321 passed`; compileall and `git diff --check` passed.
- Isolated production reconstruction: 29 Agents; both lifecycle tools seeded as default with exact schemas and 29/29 AgentTool assignments; no runtime service was started.
- Linux/amd64 AIO image: `sha256:41c96b6c64163bcbb17f15b79e73847df039168a010d0dcc8bf590e4b6794e3f` built from the pinned 1.9.3 base.
- Image-native black box: three concurrent one-second MCP timeouts reaped every child and follow-up MCP remained healthy; two separate stdio MCP processes shared one workspace and exchanged a file; shell hard-timeout preserved the pre-existing background job and repeated follow-up calls.
- VM test container, image, and temporary build context removed after validation.
