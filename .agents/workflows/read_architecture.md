# Read architecture workflow

Use this workflow at the beginning of every Clawith task.

## 1. Establish the baseline

Read, in order:

1. `AGENTS.md`
2. `ARCHITECTURE_SPEC_EN.md`
3. `.agents/rules/design_and_dev.md`

Do not treat dates, historical image tags, hostnames, credentials, line numbers, file counts, or old test totals as architecture. Verify changing facts from the current repository and local Docker environment.

## 2. Route by task

| Task surface | Required architecture document | Additional rule |
|---|---|---|
| sessions, turns, WebSocket, A2A, triggers, history, recovery | `.agents/architecture/conversations-and-turns.md` | — |
| IM channels, delivery, files, message lifecycle | `.agents/architecture/conversations-and-turns.md` | — |
| builtin tools, MCP tools, CLI, sandbox, enablement | `.agents/architecture/tools-and-sandboxes.md` | — |
| Docker, tests, local stack, browser E2E | `.agents/architecture/environments-and-operations.md` | `.agents/rules/deploy.md` |
| production configuration or deployment | `.agents/architecture/environments-and-operations.md` | `.agents/rules/deploy.md` and `.agents/rules/release.md` |
| Git branch, commit, merge, or PR | — | `.agents/rules/github.md` |

Read multiple rows when a change crosses boundaries.

## 3. Verify the present implementation

The architecture documents state invariants and known traps, not guaranteed current function names. Before editing:

- locate current code with `rg`;
- inspect current model/schema and all relevant call sites;
- inspect the working tree and preserve unrelated changes;
- for runtime claims, verify through local Docker behavior rather than source-string assertions.

## 4. Consult provenance only when needed

`.agents/architecture/claude-memory-provenance.md` records which durable Claude project memories were consolidated and where. It is an audit trail, not an additional mandatory instruction layer.
