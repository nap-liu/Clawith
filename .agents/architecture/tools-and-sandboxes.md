# Tools and sandboxes

## Runtime schema and enablement

The database is the normal runtime source for LLM tool name, description, and parameter schema. Startup seeding synchronizes builtin rows from `backend/app/services/tool_seeder.py::BUILTIN_TOOLS`; definitions in `agent_tools.py` are fallback/implementation metadata and are not sufficient by themselves.

For every builtin schema change, validate in Docker:

1. seed against PostgreSQL;
2. inspect the persisted `tools` row;
3. call `get_agent_tools_for_llm(agent_id)` and inspect what the model receives;
4. exercise dispatch and observable state/result.

Tool availability requires an explicit enabled `AgentTool` row. `is_default` only drives assignment at creation/seeding time. Rollouts must preserve this explicit audit trail. Seed values must respect the actual database column lengths.

Do not hard-code one tool's name in unrelated tool descriptions, because disabled tools can leak back into model context through prose.

Builtin runtime-override fields must stay synchronized between database seeds
and in-code fallback schemas. `run_subagent` and trigger-management tools accept
the normalized model reference forms and optional 0–2 temperature override
used by the runtime resolver.

Ordinary Digital Employee settings use one validated patch service across the
REST settings page, MCP `update_agent`, and the builtin
`update_self_settings` tool. The self tool has no target identifier and can
only update its runtime-injected standard Digital Employee. It does not expose
permissions, credentials, approval/autonomy policy, lifecycle state, Soul, or
Core Memory. Public tool fields call temperature “imagination”; database and
provider adapters may retain the internal `temperature` name.

## CLI execution models

The platform supports two intentionally distinct command models:

- Uploaded executable: a versioned artifact executed through the subprocess backend with a constrained environment.
- AIO sandbox: shell/code execution in an isolated sandbox with language runtimes and its own workspace/network lifecycle.

CLI tools may appear as independent LLM functions while sharing a sandbox execution backend with generic code execution. The execution backend is an implementation detail; authentication/identity injection and per-tool enablement remain explicit platform responsibilities.

Tenant-specific CLI names and credentials live in tool/configuration data, never in generic platform code, UI placeholders, or fixtures.

Large uploaded CLI binaries use a resumable, server-tracked upload lifecycle.
Chunk ownership, ordering, completion, cancellation, and cleanup remain scoped
to the tenant and tool. UI progress is a projection of that lifecycle, not a
second upload protocol. Production storage/backup decisions must account for
both finalized binaries and resumable state when their schema or semantics
change.

## MCP transports

HTTP/streamable MCP and stdio MCP share discovery, persistence, assignment, and
LLM tool contracts, while transport execution stays separate. Stdio/npx servers
are hosted by the AIO sandbox under the Agent workspace; the platform owns
registration, lifecycle deadlines, child cleanup, and truthful error forwarding.

- Persist discovered tools and explicit Agent assignment; a transient discovery
  response alone is incomplete.
- A newly imported tool becomes available on the next turn because the tool set
  is assembled at turn start. Do not claim same-turn availability.
- Preserve tenant, Agent, server, and workspace isolation in names, queries, and
  sandbox registration. Global display-name deduplication is not an identity
  boundary.
- Do not mask nested MCP/TaskGroup failures with a generic HTTP error; surface a
  bounded actionable leaf error to the shared tool loop.
- HTTP behavior must remain unchanged when adding stdio lifecycle support.

## Security and history

- Tool execution permissions and session visibility are separate checks.
- Persist truthful tool errors and results; do not replace them with fabricated success or prompt-only patches.
- Sanitization for display/logging must not mutate durable tool results replayed to the LLM.
- Repetitive-call and round-limit guards belong in the shared loop so all entry points receive the same protection.
- File tools use exact canonical virtual paths and report the failing stage
  truthfully. Fuzzy filename repair and cross-tool canned failure counters hide
  evidence and are not recovery mechanisms.
