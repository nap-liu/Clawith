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

- Optimize for prompt, working product delivery. Validation must answer a
  concrete acceptance question or protect a material regression risk; producing
  test code, increasing coverage, and expanding a test matrix are not outcomes.
- Start with the smallest useful check of the changed user-visible behavior.
  Reuse existing checks and focused runtime/browser inspection. Do not add test
  files for reversible low-impact changes, implementation-shaped assertions,
  repetitive permutations, or behavior already covered by an existing check.
- Add automated regression tests only where they materially protect behavior
  such as tenant access, durable state, concurrency, recovery, or external side
  effects. Keep the cases few and representative; do not build a new testing
  framework or fixture layer for a bounded product change.
- Once acceptance evidence and the applicable gates pass, finish the delivery.
  Do not broaden or repeat validation without a new change, failure, unresolved
  material risk, or explicit user request. Record unverified limits directly.
- Use the exact checkout mounted into one-shot containers and an isolated
  PostgreSQL database. Follow `.agents/rules/deploy.md` and
  `.agents/architecture/environments-and-operations.md`.
- Map the changed behavior and its callers to affected observable tests, and
  run that scope by default. A full suite is not a routine completion gate.
  Broaden only when shared dependencies or contracts create wider impact, an
  affected failure leaves a risk unresolved, or the user explicitly requests it;
  record the reason and selected scope before broadening.
- For UI or cross-layer behavior, validate through the local port-3008 path and
  the actual API/event/UI flow.
- Compare failing affected checks with the same checks on a measured baseline.
  Unrelated suite failures do not automatically block a bounded change or
  require a full baseline run. Record failures, interruptions, skipped and
  unrun checks honestly; never label a failing or interrupted suite green.

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

## 8. Release-preparation shorthand

When the user explicitly asks to "prepare everything before release" (or an
unambiguous equivalent), treat that phrase as authorization to finish the
release candidate, commit and push the reviewed source, build and push the
matching immutable backend/frontend images and cache artifacts, and record
their digests. This shorthand exists so routine release preparation does not
stall on separate confirmations for each preparatory artifact.

It does not authorize a Git tag or hosted Release, production inspection,
production backup or migration, external provider smoke messages, production
configuration changes, application cutover, or rollback. Those remain separate
explicit authority gates. Follow `.agents/rules/release.md` and the production
runbook for every release candidate.
