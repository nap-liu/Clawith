# Design and development rules

## Scope and authorization

- A request to inspect, diagnose, or propose a solution does not authorize implementation, commit, push, deployment, or production mutation.
- Once implementation is authorized, complete the safe in-scope work and verify it proportionally.
- Preserve unrelated worktree changes and user-owned untracked files.

## Architecture first

- Product-complete delivery is the first priority. Prefer the smallest shared
  lifecycle contract that closes the end-to-end path; keep channel code as thin
  adapters and do not add parallel mechanisms for the same behavior.
- Fix the authoritative source instead of layering prompt or channel-specific patches.
- When behavior appears in two or more places, extract a shared service/component and keep provider or UI differences in adapters.
- Normalize contracts and lifecycle states, not genuine semantics. P2P and group conversations have different identity and provider behavior.
- Keep transport, domain lifecycle, persistence, and presentation boundaries explicit.
- Platform code, UI, fixtures, and examples must not hard-code tenant-specific tool names.
- The legacy product keyword defined by the central user-output sanitizer is
  forbidden case-insensitively in product messages, notifications, user-facing
  copy, fixtures, and new engineering documentation. Apply the shared sanitizer
  at persistence and transport boundaries; do not leave empty ASCII or CJK
  wrappers behind. Do not mechanically rename infrastructure identifiers,
  repository paths, protocols, or compatibility contracts.
- Compatibility exceptions must be explicit and narrow. They currently cover:
  repository URLs and checkout-directory names; existing public SDK/window,
  browser storage/event, and audio-worklet identifiers; environment variables;
  persisted filesystem paths, database roles/names, Redis namespaces, and
  container/network names; Helm chart/release/namespace, image, service, PVC,
  secret, and example command identifiers; and immutable historical provenance.
  These identifiers may appear only where required for installation, operation,
  migration, or existing-client compatibility, never as product prose or labels.

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

- Hand-written application code, tests, migrations with executable logic, and
  engineering scripts are limited to 800 physical lines per file. Declarative
  configuration/data and machine-generated outputs are exempt. This is a hard
  completion gate, not a style preference.
- Never grow an already oversized source file. If a task touches one, extract
  cohesive modules/components until every source file delivered by the task is
  at most 800 lines. Pre-existing violations are technical debt, not a legacy
  exemption or allowlist. Do not evade the rule with minified code, multiple
  statements per line, giant strings, or misplaced logic in config files.
- Imports belong at file scope unless avoiding a real circular dependency.
- Do not run blanket formatters over pre-existing large files; keep formatting churn separate.
- Tests assert observable inputs, outputs, persisted state, API responses, provider-adapter behavior, events, or UI.
- Do not add tests that read source files and regex-match an implementation shape.
- Report evidence and distinguish observed results from inference.
