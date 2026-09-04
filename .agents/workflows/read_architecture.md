# Read architecture workflow

Use this workflow at the beginning of every repository task. Repository files
are the complete knowledge source; do not consult personal agent memory.

## 1. Establish the baseline

Read, in order:

1. `ARCHITECTURE_SPEC_EN.md`;
2. `.agents/rules/design_and_dev.md`;
3. the task-specific documents selected below.

`AGENTS.md` is the entry point that sent you here and need not be re-read in the
same session. Do not treat dates, historical image tags, hostnames, credentials,
line numbers, file counts, or old test totals as architecture. Verify changing
facts from the current repository and authorized local Docker environment.

## 2. Route by task

| Task surface | Required architecture document | Additional rule/workflow |
|---|---|---|
| sessions, turns, WebSocket, A2A, triggers, history, recovery, active-turn control | `.agents/architecture/conversations-and-turns.md` | — |
| IM channels, delivery, files/media, confirmation lifecycle, recall | `.agents/architecture/conversations-and-turns.md` | — |
| identity, SSO, SCIM, organization directories, user normalization, channel-user binding | `.agents/architecture/identity-directory-and-channel-bindings.md` | — |
| context budgets, compaction, summaries, model usage, memory loading, LLM timeouts | `.agents/architecture/context-and-memory.md` | — |
| model selection, imagination, reasoning effort, provider generation parameters | `.agents/architecture/model-reasoning-controls.md` | — |
| published pages, page management, page authorization filters or search | `.agents/architecture/published-page-management.md` | — |
| builtin tools, MCP, CLI uploads, sandbox execution, enablement | `.agents/architecture/tools-and-sandboxes.md` | — |
| Docker, tests, local stack, browser E2E | `.agents/architecture/environments-and-operations.md` | `.agents/rules/deploy.md` |
| implementation or multi-file engineering change | all documents selected for its surfaces | `.agents/workflows/engineering_change.md` |
| production configuration or deployment | `.agents/architecture/environments-and-operations.md` | `.agents/rules/deploy.md`, `.agents/rules/release.md`, then `.agents/runbooks/production_release.md` in full |
| Git branch, commit, merge, or PR | — | `.agents/rules/github.md` |

Read multiple rows when a change crosses boundaries. If a task surface has no
dedicated document, use the system overview and current implementation instead
of searching external memory.

## 3. Verify the present implementation

Architecture documents state invariants and known traps, not guaranteed current
function names. Before editing:

- locate current code with `rg`;
- inspect current models/schemas, migrations, seeds, and all relevant callers;
- inspect the working tree and preserve unrelated changes;
- verify runtime claims through local Docker behavior rather than source-string
  assertions;
- distinguish a documented invariant from a bug in the present implementation.

## 4. Maintain the handbook

When a task changes a durable invariant or workflow, update the canonical
architecture/rule/workflow in the same change and remove contradictions. Never
store the lesson only in a chat, personal memory, or dated release note.

`.agents/external-memory-migration.md` is a closed historical audit record. It
is never required reading and cannot supply current instructions.
