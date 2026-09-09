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

## Media understanding and generation

`read_media` and `generate_media` are independently opt-in builtin tools.
Both reference the existing enterprise `LLMModel` pool. Four default model IDs
in `tool_config:media_ai` are managed by the enterprise model UI; Agent/scene
tool settings may override those references. Explicit call `model_id` wins,
otherwise a same-purpose media session retains its selection, then the resolved
tool default applies. Admission validates tenant, enabled state and purpose,
and freezes the real model ID, resolved protocol and encrypted runtime settings.
The next input reads that selected model's current settings; queued inputs retain
their accepted snapshot. Model names are free configuration, not an allowlist.

`api_protocol` selects the shared understanding/dialogue transport independently
of the provider. NULL preserves the provider registry's existing default; changing
a provider to Responses requires configuring and validating that endpoint.
For actual Bailian endpoints, audio/video in the current input or retained history
selects Chat before sending because Bailian Responses currently accepts neither.
Text/image requests retain Responses. This capability routing never retries a
failed submission and does not change other providers' configured protocols.
Understanding preserves original supported image/audio/video inputs, without
silently discarding audio or sampling locally. Generation uses a small transport
facade: compatible services share standard image/audio/video endpoints; Qwen uses
its native generation adapter. A Responses setting does not imply every generation
operation uses `/responses`. Unsupported native operations fail explicitly.

Startup converts mutable legacy tenant and Agent connections to enterprise model
references and clears old global media defaults. Published scene revisions and
accepted task snapshots are not rewritten. A published legacy connection is
materialized into a matching tenant model on admission; disabled matches stay
disabled. The legacy transport reader remains for previously accepted tasks.
Enterprise media tests submit to the same child worker using an authorized existing
Agent workspace. The existing non-waking Subagent mode avoids an extra parent LLM
call; HTTP still returns a task receipt immediately. Existing image tools and
their assignments retain their original behavior and permissions.

Both entrypoints immediately return a `media_task` receipt (`task_id`, `session_id`,
`status`). They enqueue an input in an ordinary child Session with `executor=media`.
The shared SubagentRun lease/worker executes the provider adapter directly, without
an Agent planning round, Soul, memory search or a separate job queue. Each input is
one task and publishes its own completion, even when later inputs are queued.
Omitting `session_id` starts a one-shot job; supplying it continues the same media
conversation, with a fresh task ID and the same authorization boundary.

Admission stores effective configuration and the accepted scene revision on the
input anchor; connection credentials are encrypted. Generation intent, encrypted
provider task/result references and saved files live on that input's tool-call row
in `message_meta.media_job`. Recovery queries the original provider task; an
unconfirmed submission is never resubmitted. Interrupted understanding without a
stored result ends explicitly instead of silently paying for another invocation.
Outer-tool recovery restores the original enqueue receipt. Retryable provider
reads retain the same task and checkpoints honor the shared turn/lease fence.

AgentDir paths use existing storage signing, with an exact-file ticket fallback
for local storage. Third-party HTTP(S) URLs retain their original query parameters.
A supplied media kind avoids probing an opaque URL; otherwise bounded probing
determines its type. There is no platform input size/count cap or compulsory
Base64 conversion; the selected model and reachable storage determine supported
sizes. Existing URL access security rules still apply. Files use the execution
Agent's workspace, which may differ from the A2A history owner. Audio/video delivery
reuses the original parent's receipt and player. Task cards reuse the existing
subagent UI and query per-task status so later turns cannot rewrite earlier
outcomes. The shared frontend parser unwraps `media_generation.delivery`.
A2A returns the generated workspace file with a separate unsupported media-player
delivery status, following the existing channel capability; file generation still
succeeds and the file remains available for the normal A2A file exchange.

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
  Startup orphan cleanup uses the same catalog policy and lock order, rechecks
  assignments and Project references after locking, and preserves shared or
  unclassified catalogs even when they have never been assigned.
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
  Private discovery/check endpoints require the owning Agent scope. New tools
  retain the catalog's origin regardless of assignment creation or omitted upsert
  arguments. Unbound shared catalogs may be checked without creating bindings;
  an all-platform-disabled catalog cannot use this fallback.
  Checks and execution share configuration, placeholder/header rendering and
  exact Smithery installation routing. Known installations never borrow another
  installation's route or key, and a check cannot trigger recovery. Project
  checks use the Project workspace and source-creator credential rules; historical
  Project overrides are ignored even when the local binding is disabled. Temporary
  stdio discovery registrations have unique invocation identities and are removed
  independently of concurrent calls.
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
