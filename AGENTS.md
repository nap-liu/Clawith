# Digital Employee Platform Agent Instructions

This file is the stable entry point for every coding agent working in this repository. Detailed rules and architecture live under `.agents/`; do not duplicate or override them in tool-specific instruction files.

## Mandatory read order

Before inspecting or changing code:

1. Read `.agents/workflows/read_architecture.md`.
2. Read `.agents/rules/design_and_dev.md` for every engineering task.
3. Read the additional rule for the work being performed:
   - environment, Docker, or deployment: `.agents/rules/deploy.md`
   - Git, branches, commits, or pull requests: `.agents/rules/github.md`
   - image tags, versioning, or production release: first
     `.agents/rules/release.md`, then the complete executable workflow in
     `.agents/runbooks/production_release.md`
4. Follow the architecture routing table in the workflow and read only the relevant files under `.agents/architecture/`.

`ARCHITECTURE_SPEC_EN.md` is the canonical system overview. More specific rules under `.agents/rules/` win if documents conflict. User instructions for the current task win over repository defaults.

## Non-negotiable repository defaults

- Run backend commands, Python, lint, tests, build checks, integration scripts, and browser validation only in Docker. Never create or use a host venv for validation.
- Validate code against local Docker environments, not production.
- Preserve tenant isolation, authorization boundaries, and the shared LLM turn loop.
- Prefer one normalized platform capability with transport adapters over channel-specific copies. Preserve real P2P/group semantic differences at adapter boundaries.
- Tests must assert observable behavior through APIs, databases, events, or UI. Source-text/regex shape tests are not accepted.
- Treat existing uncommitted files and unrelated worktrees as user-owned. Do not clean, reset, reformat, stop shared stacks, push, or deploy outside the authorized scope.
- Tool schemas shown to the LLM come from the database seeded by `backend/app/services/tool_seeder.py`; an in-code fallback alone is not a completed tool change.

## Collaboration preference

At the end of a completed task, play a short local completion sound (for example, `afplay /System/Library/Sounds/Glass.aiff`) unless the user asks to skip it.
