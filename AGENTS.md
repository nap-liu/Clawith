# Digital Employee Platform Repository Instructions

`AGENTS.md` is the canonical entry point for every coding agent working in this
repository. The repository is self-contained: engineering decisions must be
derivable from tracked files, current code, and authorized runtime inspection.

## Authority and independence

- The user's current request has highest priority.
- More specific rules under `.agents/rules/` override architecture summaries;
  the production runbook is authoritative for an authorized release.
- Never use personal/global Claude or Codex memory, chat history, another
  checkout, or an untracked local note as repository authority. In particular,
  do not read `~/.claude/**` or `~/.codex/**` to recover project knowledge.
- `CLAUDE.md` is a compatibility pointer only. It must not contain a second set
  of rules or refer to external memory.
- Verify changing facts—branches, versions, image tags, hosts, credentials,
  line numbers, test counts, and running topology—from the current repository
  and authorized environment. Never commit secrets or personal paths.

The one-time external-memory migration is closed. Its non-normative audit record
is `.agents/external-memory-migration.md`; agents do not need it to do work.

## Highest-priority engineering gate: 800 lines

This is the highest-priority repository engineering gate. It overrides delivery
speed, implementation convenience, architecture-local conventions, and every
conflicting rule elsewhere in this repository. No task-specific guideline may
waive it. Only an explicit current user instruction that amends this gate may
change it. A code task is not complete while any source file delivered by that
task violates it.

- Every hand-written source file must be at most 800 physical lines, including
  application code, tests, migrations with executable logic, and engineering
  scripts.
- Only declarative configuration/data and machine-generated files are exempt.
- Pre-existing oversized files are technical debt, not grandfathered
  exceptions. Before or while changing one, split it into standard
  architecture-aligned modules and leave every delivered source file at 800
  lines or fewer.
- Never evade the limit through minification, multiple statements per line,
  giant embedded strings, generated-looking hand-written files, or moving logic
  into configuration.
- Verify physical line counts for every changed or newly created source file
  before handoff. Failure of this gate blocks completion regardless of other
  tests passing.

## Mandatory start sequence

Before inspecting implementation code or making changes:

1. Read `.agents/workflows/read_architecture.md` and follow its baseline and
   task-routing table.
2. For an implementation task, also read
   `.agents/workflows/engineering_change.md`.
3. Read every additional rule selected by the task:
   - environment, Docker, tests, browser validation, or deployment:
     `.agents/rules/deploy.md`
   - Git, branches, commits, merges, or pull requests:
     `.agents/rules/github.md`
   - image tags, versioning, production cutover, or rollback: first
     `.agents/rules/release.md`, then
     `.agents/runbooks/production_release.md` in full
4. Inspect `git status` before editing. Existing modified/untracked files and
   unrelated worktrees are user-owned.

`.agents/README.md` is the handbook index and maintenance guide.

## Non-negotiable engineering defaults

- Run backend commands, Python, lint, tests, builds, integration scripts, and
  browser validation only in Docker. Never create or use a host virtualenv.
- Validate code against isolated local Docker environments, never production.
- Preserve tenant isolation, authorization boundaries, durable audit state, and
  the shared LLM/tool turn loop.
- Prefer one normalized platform capability with transport adapters over
  channel-specific copies. Preserve real P2P/group and provider differences at
  adapter boundaries.
- End database read transactions before provider waits or long tool loops; use
  short independent transactions for durable intermediate state.
- Tests must assert observable behavior through APIs, databases, events,
  providers, or UI. Source-text/regex shape tests are not accepted.
- Tool schemas shown to the LLM come from database rows seeded by
  `backend/app/services/tool_seeder.py`; an in-code fallback alone is not a
  completed tool change.
- Product code, prompts, UI, fixtures, and examples must not hard-code
  tenant-specific tool or CLI names.
- Do not clean/reset unrelated changes, stop shared stacks, push, tag, deploy,
  or mutate production without the authorization required by the relevant
  workflow.

## Completion standard

A completed engineering change includes proportional Docker validation,
observable-behavior evidence, a review of the final diff, updated canonical
documentation when an invariant or workflow changed, and an explicit report of
anything not verified. Verify the 800-line source-file gate for every changed or
new source file. At the end of a completed task, play a short local
completion sound unless the user asks to skip it.
