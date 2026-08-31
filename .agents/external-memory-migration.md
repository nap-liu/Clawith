# External project-memory migration record

Status: **closed** on 2026-08-31. This file is provenance only and is not part of
the mandatory instruction chain.

## Scope and completion

The prior Claude project-memory collection was reviewed once as migration input:
117 Markdown files total, consisting of one `MEMORY.md` index, 112 indexed
records, and four records omitted from that index (`aio_sandbox_mcp_hosting.md`,
`dingtalk_doc_mcp_via_curl.md`, `file_tool_truthful_errors_prod_2026_07_21.md`,
and `mcp_stdio_aio_host_iteration.md`). The index summaries and the four
unindexed records were included in the review.

No future task should reopen that external collection. A missing or stale fact
must be resolved from tracked documentation, current code/tests/migrations,
local Docker behavior, Git history, or the authorized operations inventory.

## Durable material promoted

| Repository destination | Consolidated durable subjects |
|---|---|
| `AGENTS.md` and `.agents/README.md` | self-contained authority, Docker-only validation, external-memory prohibition, user-work preservation, completion standard |
| `.agents/workflows/engineering_change.md` | authorization framing, worktree baseline, current-code inspection, shared-capability design, Docker evidence, review/handoff |
| `.agents/rules/design_and_dev.md` | modular-first design, source-fix over prompt patches, observable tests, evidence quality, tenant-neutral platform code, 800-line source-file gate |
| `.agents/rules/github.md` | company-main scope, worktree ownership, explicit staging and authorization boundaries |
| `.agents/architecture/conversations-and-turns.md` | shared loop, P2P/group/A2A identity, connection-independent turns, active-turn authority, confirmations, durable delivery and recall |
| `.agents/architecture/context-and-memory.md` | model-aware budgets, real usage, complete-turn compaction, finite overflow recovery, lossless archive fallback, tool-output views, Agent memory loading |
| `.agents/architecture/tools-and-sandboxes.md` | database-seeded schemas, explicit enablement, two CLI execution models, resumable binaries, HTTP/stdio MCP lifecycle, truthful file/tool errors |
| `.agents/architecture/environments-and-operations.md` | port-3008 topology, exact-checkout mounts, isolated PostgreSQL tests, shared-stack safety, production boundary |
| `.agents/rules/deploy.md` | Docker-only/local-only gates, migration isolation, no shared-stack cleanup, deployment authorization |
| `.agents/rules/release.md` and `.agents/runbooks/production_release.md` | same-SHA images, linux/amd64, registry cache recipe, no-prestop atomic role replacement, change-scoped online backup, compatibility migrations, digest-pinned rollback |

This includes the reusable lessons previously spread across `feedback_*`,
conversation/A2A/IM records, context and compaction records, tool/CLI/MCP/AIO
records, local-stack/test records, and production release/build records.

## Material intentionally not copied as instructions

- Credentials, access tokens, private endpoints, host aliases, IP addresses,
  personal filesystem paths, and backup locations remain outside Git.
- Dated release SHAs, image digests, historical test totals, fleet inventories,
  production usage snapshots, and incident timestamps are evidence in Git or
  approved operational records, not durable architecture.
- Feature-specific implementation details that are already represented by code,
  migrations, and observable tests were not duplicated as prose. Their reusable
  boundary or failure lesson was promoted to the relevant architecture file.
- Unfinished historical plans and worktree states were not treated as current
  truth. Any still-desired work requires a fresh inspection and authorization.
- Third-party service recipes tied to personal MCP endpoints or credentials were
  excluded; repository capabilities are documented by normalized transport
  contracts instead.

## Ongoing policy

The migration source is retired, not synchronized. Do not add new Claude/Codex
personal memories as a parallel handbook. Update the canonical repository file
in the same change whenever a durable invariant, workflow, or operational gate
changes.
