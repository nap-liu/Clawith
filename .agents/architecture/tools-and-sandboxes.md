# Tools and sandboxes

## Runtime schema and enablement

The database is the normal runtime source for LLM tool name, description, and parameter schema. Startup seeding synchronizes builtin rows from `backend/app/services/tool_seeder.py::BUILTIN_TOOLS`; definitions in `agent_tools.py` are fallback/implementation metadata and are not sufficient by themselves.

First-party tools interpret offset-free datetime inputs in the Agent's effective
timezone (Agent override, then tenant default, then UTC). Human-readable tool
results include the numeric offset and IANA timezone name. Durable database
timestamps, audit records, cursors, and machine contracts remain canonical UTC;
when a machine result needs local context, it keeps the original field and adds
a sibling `*_local` projection instead of rewriting or dropping source data.

For every builtin schema change, validate in Docker:

1. seed against PostgreSQL;
2. inspect the persisted `tools` row;
3. call `get_agent_tools_for_llm(agent_id)` and inspect what the model receives;
4. exercise dispatch and observable state/result.

Tool availability requires an explicit enabled assignment: the `AgentTool` row,
or the published scene assignment described below. `is_default` only drives
assignment at creation/seeding time. Rollouts must preserve this explicit audit
trail. Seed values must respect the actual database column lengths.

Do not hard-code one tool's name in unrelated tool descriptions, because disabled tools can leak back into model context through prose.

Builtin tools must inherit company configuration through the current Agent's
tenant; `read_image` resolves that shared base before applying its existing
tightening-only Agent overrides, rather than bypassing the company layer.

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

## Scene tool configuration

Scenes reuse the Project/Agent tool panel and its enabled/config assignment
contract, including MCP overrides. `tools=null` follows the Agent; an explicit
list defines the scene's complete configuration, with an empty list leaving
only required protocol tools. Scene configuration lives in existing draft and
revision JSON, never in the Agent's live assignments or another Agent copy.

At the shared failover boundary, one task-local snapshot supplies tool schemas,
configuration reads, MCP execution and extension prompts. It is reset on normal
completion, cancellation and failure; concurrent Sessions cannot share mutable
configuration. The existing tool loop and dispatch enforce availability.
Platform tool retirement, tenant visibility and required-tool rules still apply.
The catalog, scene-save validator and runtime registry share one tenant-safe
visibility predicate, including builtin and explicitly assigned tools. CLI
injection uses the same effective scene assignment and configuration as the
model schema. Existing CLI launchers can remain on disk, but a disabled tool
receives no signed execution context and cannot use a previous turn's identity.
The toolscall HTTP adapter reloads the scene revision from its signed turn
anchor and enters the same task-local scope before dispatch. It closes the
database read transaction first and resets the scope on exit.
Newly published settings take effect on the next turn; recovery and in-turn
inputs retain the root anchor's scene revision. Management responses mask scene
credentials, and browser chat manifests omit runtime configuration entirely.

Project children restore that same anchor before preparing tools. Scene
assignments pass through the existing Project capability, role and status
projection; authorized Project protocol tools remain governed by their own
runtime scope. A generic scene-name intersection must not remove those tools.
Explicit empty prepared tools for a prose-only correction remain empty.

## MCP transports

HTTP/streamable MCP and stdio MCP share discovery, persistence, assignment, and
LLM tool contracts, while transport execution stays separate. Stdio/npx servers
are hosted by the AIO sandbox under the Agent workspace; the platform owns
registration, lifecycle deadlines, child cleanup, and truthful error forwarding.

- Persist discovered tools and explicit Agent assignment; a transient discovery
  response alone is incomplete.
- Company-level manual import performs one remote discovery, ends that provider
  wait before opening the persistence transaction, and atomically stores the
  MCP server plus its complete discovered catalog. It must not fan out into one
  client request or transaction per tool.
- `MCPServer.display_name` and `Tool.display_name` are mutable local labels.
  Internal server identity, `Tool.name`, and `mcp_tool_name` remain stable;
  `mcp_tool_name` is the remote method used for dispatch. Runtime LLM schemas
  include bounded current group/tool labels in the MCP tool description so a
  rename is visible from the next turn without changing function identity.
- Tenant-wide MCP definitions linked to admin tools are writable only by a
  platform administrator or that tenant's organization administrator. Literal
  secrets in header templates are write-only: control-plane responses return a
  stable mask, placeholder templates remain visible, and sending the unchanged
  mask preserves the stored value.
- Legacy company-tool backfill treats tenant, URL, and legacy group name as one
  complete group identity. It creates an independent server for each safe
  group and never reuses another server merely because its URL matches.
- A newly imported tool becomes available on the next turn because the tool set
  is assembled at turn start. Do not claim same-turn availability.
- Platform `Tool.enabled` is a veto for MCP schemas, extension prompts,
  execution (canonical names and remote aliases), and Agent inventory.
  Tenant visibility covers both the tool and its referenced server. Scene
  settings may replace Agent enablement, but cannot override this veto.
- `list_installed_mcp_servers` reports actual persistent installations:
  `tool_count` counts platform-visible installed tools; `enabled_tool_count`
  additionally applies the current Agent/scene selection. A locally disabled
  installation remains visible with zero enabled tools, while a group with no
  platform-visible tools is omitted. Scene-only tools do not create an
  installation. Removability means self-install provenance, not refresh
  authorization or provider health. An authorized owner can still uninstall a
  hidden self-installation by its known server ID.
- Single-binding deletion requires management access to the owning Agent,
  including the same tenant boundary used by Agent settings. Removing one
  binding preserves other Agents' installations. Only a private MCP tool may
  be deleted when its final binding is removed; shared catalogs survive unbinding.
  Alias resolution only reserves canonical
  MCP names visible to the current Agent, so another tenant's names cannot
  suppress its available remote aliases.
- Agent refresh requires a visible installation and isolated ownership before
  provider discovery. Read-only planning includes any existing private target
  of a legacy shared installation; an existing target catalog with no
  platform-visible enabled tools also vetoes Agent refresh. An empty target
  catalog may receive the authorized migration. Discovery holds no database transaction;
  a short final transaction locks and rechecks catalog, assignments, project
  references, and resolved configuration before any migration or persistence.
  Changed state discards discovery results. Catalog deletion shares the lock
  order: Agent (when applicable), servers, project references, tools,
  assignments, overrides. Provider failure must not leave a private clone.
- The platform bulk toggle operates on the requested tool IDs; it is not a
  persistent server-wide switch for future tools. Explicit administrator
  discovery remains allowed with current tools disabled and preserves their
  flags. Revocation rejects calls reaching authorization after its commit;
  already admitted provider calls may finish. Refresh does not repair CLI
  credentials, and CLI enablement remains an independent capability.
- Preserve tenant, Agent, server, and workspace isolation in names, queries, and
  sandbox registration. Global display-name deduplication is not an identity
  boundary.
- Private HTTP/stdio installation identity includes the complete connection
  configuration, including credentials, headers and environment. Serialize
  imports per Agent; reuse an unchanged installation and allocate an independent
  server ID plus a display-name suffix for different configurations. Never put
  credentials or their hashes in names, or infer a stdio display name from an
  arbitrary command argument. New function names fit the provider's 64-character
  boundary. Existing connection overrides prevent silent reuse of an installation.
- Company/platform MCP catalogs are shared definitions. Their owning org/platform
  administrators alone may modify definitions or refresh the catalog. Agents may
  filter tool enablement and override credential/header/environment configuration;
  URL, command, arguments and instructions remain canonical. Runtime ignores
  historical shared definition overrides, including scene overrides. Agent
  connection checks use effective private credentials without persisting discovery
  results. `Tool.source` identifies catalog origin; binding provenance alone never
  turns an admin catalog into a private one. Empty or unclassified catalogs are
  treated conservatively as shared.
- Smithery connections and recovery belong to exact private installations, never
  to matching URLs or display names. Execution prefers the installation's key.
  Concurrent imports must retain the winning installation's routing and must not
  return another connection's authorization URL or mix its discovered tools.
- Legacy bulk configuration resolves an exact server ID, or an unambiguous name
  within the target tenant. Updating a URL never merges another server's catalog.
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
