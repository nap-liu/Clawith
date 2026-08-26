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

## CLI execution models

The platform supports two intentionally distinct command models:

- Uploaded executable: a versioned artifact executed through the subprocess backend with a constrained environment.
- AIO sandbox: shell/code execution in an isolated sandbox with language runtimes and its own workspace/network lifecycle.

CLI tools may appear as independent LLM functions while sharing a sandbox execution backend with generic code execution. The execution backend is an implementation detail; authentication/identity injection and per-tool enablement remain explicit platform responsibilities.

Tenant-specific CLI names and credentials live in tool/configuration data, never in generic platform code, UI placeholders, or fixtures.

## Security and history

- Tool execution permissions and session visibility are separate checks.
- Persist truthful tool errors and results; do not replace them with fabricated success or prompt-only patches.
- Sanitization for display/logging must not mutate durable tool results replayed to the LLM.
- Repetitive-call and round-limit guards belong in the shared loop so all entry points receive the same protection.
