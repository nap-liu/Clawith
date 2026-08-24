# Design and development rules

## Scope and authorization

- A request to inspect, diagnose, or propose a solution does not authorize implementation, commit, push, deployment, or production mutation.
- Once implementation is authorized, complete the safe in-scope work and verify it proportionally.
- Preserve unrelated worktree changes and user-owned untracked files.

## Architecture first

- Fix the authoritative source instead of layering prompt or channel-specific patches.
- When behavior appears in two or more places, extract a shared service/component and keep provider or UI differences in adapters.
- Normalize contracts and lifecycle states, not genuine semantics. P2P and group conversations have different identity and provider behavior.
- Keep transport, domain lifecycle, persistence, and presentation boundaries explicit.
- Platform code, UI, fixtures, and examples must not hard-code tenant-specific tool names.

## Conversation and security invariants

- All entry points converge on the shared LLM/tool loop.
- Enforce tenant and agent/session ownership in every query.
- Determine human-in-the-loop authority from the current session source, not merely a populated `user_id`.
- Query A2A messages by conversation, because normalized A2A `ChatMessage.agent_id` is not the active caller identity.
- Persist externally visible outcomes before depending on them for history, recovery, UI, or provider lifecycle operations.

## Builtin tool changes

A builtin tool is incomplete until all of these agree:

1. the implementation and dispatch;
2. fallback schema where retained;
3. `BUILTIN_TOOLS` in `backend/app/services/tool_seeder.py`;
4. explicit `AgentTool` enablement behavior;
5. the actual `get_agent_tools_for_llm()` output after seeding in Docker.

Keep seeded string values within database column limits. Do not infer runtime visibility from a grep of source code.

## Code and tests

- Imports belong at file scope unless avoiding a real circular dependency.
- Do not run blanket formatters over pre-existing large files; keep formatting churn separate.
- Tests assert observable inputs, outputs, persisted state, API responses, provider-adapter behavior, events, or UI.
- Do not add tests that read source files and regex-match an implementation shape.
- Report evidence and distinguish observed results from inference.
