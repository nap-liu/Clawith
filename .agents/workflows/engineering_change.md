# Engineering change workflow

Use this workflow for diagnosis that may lead to code changes and for every
authorized implementation task.

## 1. Frame the outcome and authority

- Restate the observable outcome, affected users/tenants, and acceptance
  evidence.
- Separate inspection, implementation, Git operations, external writes, and
  deployment authorization. Permission for one does not imply the others.
- Identify security, data migration, provider side-effect, and rollback risk
  before choosing the implementation shape.

## 2. Establish a safe baseline

- Follow `.agents/workflows/read_architecture.md`.
- Read the applicable rules before using the associated commands.
- Inspect `git status`, the selected baseline, relevant worktrees, and active
  local containers. Preserve user-owned work.
- For a new multi-file task, prefer a dedicated `.worktrees/<task-name>/`
  checkout unless the user has already established active changes in the
  current checkout.

## 3. Verify the present implementation

- Locate code with `rg`; inspect models/schemas, migrations, services, adapters,
  callers, tests, and seeded database definitions that participate in the
  behavior.
- Treat names and line numbers in architecture documents as navigation hints,
  not proof of current behavior.
- Reproduce through an observable boundary when feasible. Diagnose the
  authoritative source before proposing a patch.

## 4. Design the smallest complete change

- Make all entry points converge on shared domain behavior; keep protocol and
  provider differences in adapters.
- Define durable state transitions, idempotency, tenant/ownership checks,
  failure truth, and recovery behavior before implementing side effects.
- Plan migrations and compatibility in both forward and rollback directions.
- Update implementation, runtime schema/seeding, UI, and observable tests as one
  coherent capability when the contract crosses those layers.
- Design modules so every hand-written source file remains at or below 800
  physical lines. Treat an oversized file in scope as a required decomposition,
  not a place to append another branch.

## 5. Implement without collateral churn

- Keep edits scoped; do not blanket-format large existing files.
- Preserve unrelated changes and avoid modifying another worktree.
- Keep credentials and environment-specific values in approved configuration,
  never source, fixtures, logs, or documentation.
- When a discovered fact changes a durable rule or architecture invariant,
  update the canonical handbook document in the same change.

## 6. Validate in Docker

- Use the exact checkout mounted into one-shot containers and an isolated
  PostgreSQL database. Follow `.agents/rules/deploy.md` and
  `.agents/architecture/environments-and-operations.md`.
- Start focused observable tests, then broaden in proportion to blast radius.
- For UI or cross-layer behavior, validate through the local port-3008 path and
  the actual API/event/UI flow.
- Compare failures with a measured baseline. Never label a suite green when it
  has failures; distinguish pre-existing failures with evidence.

## 7. Review and hand off

- Inspect the final diff for tenant leakage, authorization gaps, duplicated
  mechanisms, partial schema changes, unsafe migrations, and unrelated churn.
- Run `git diff --check` and verify documentation links touched by the change.
- Count every changed/new hand-written source file and fail the handoff if any
  exceeds 800 physical lines; configuration and generated artifacts are the
  only exceptions.
- Report the outcome first, then changed files, validation evidence, known
  limitations, and any step that remains unauthorized or unverified.
- Commit, push, tag, release, and deploy only under their explicit authority
  gates.
