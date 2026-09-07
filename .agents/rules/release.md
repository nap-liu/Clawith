# Release rules

Read `.agents/runbooks/production_release.md` in full for every release,
production cutover, or rollback plan. These invariants are not a substitute for
the runbook.

- An explicit request to "prepare everything before release" authorizes the
  reviewed release-candidate commit, authoritative-branch push, immutable
  backend/frontend image and cache build, registry push, and digest capture.
  It does not authorize a Git tag or hosted Release, production inspection,
  configuration changes, migrations, backup, cutover, external smoke messages,
  or rollback. Outside this defined shorthand, each operation requires its
  applicable explicit authorization.
- This product forked at upstream `1.10.3` and now maintains its version
  independently. Do not follow upstream release numbers or change VERSION as
  part of an ordinary release. Use the repository's independent version plus
  the exact commit SHA suffix; changing the version requires an explicit
  product version decision.
- Build and publish backend and frontend from the same immutable release SHA,
  even when only one side changed. Pin deployment and rollback to digests.
- Select Docker validation from the final diff and affected behavior, including
  callers and compatibility boundaries. Full test suites are not the default
  release gate; broaden only for demonstrated wider impact, unresolved affected
  failures, or an explicit user request. Record scope, evidence, and omissions
  under the runbook's release-candidate gates.
- Build production images for `linux/amd64`. Rebuild AIO only when its source or
  base image changed or the user explicitly requests it.
- For a backend code-only release, reuse the previous trusted backend image by
  immutable digest as `CLAWITH_DEPS_IMAGE` after the runbook's dependency-input
  equality gates pass. Mutable BuildKit cache tags are performance hints, never
  dependency authority. A dependency refresh is an explicit release mode.
  Never use `docker compose build` or `docker compose up --build` to produce
  production artifacts.
- Prepare, pull, render, and verify candidate and rollback images/configuration
  while the old release remains live. The cutover window contains no build,
  image download, or ad-hoc compose editing.
- Do not pre-stop or `down` the production application. Replace backend,
  worker, connector, and frontend through one explicit compose command with
  `--no-deps --no-build`; never use `--remove-orphans`. Rollback uses the same
  one-command four-role pattern and the previous digest-pinned compose file.
- A single-replica Compose replacement can cause a brief request/turn
  interruption and is not strict zero downtime. If a release requires no
  request interruption, it is blocked until a tested blue-green/rolling
  topology and atomic traffic switch exist.
- Determine backup scope from actual schema, seed, state, and rollback impact.
  Save compose/digests/release evidence every time; snapshot only affected data
  stores online and close to cutover. Any omitted affected store requires the
  data owner's explicit decision. If consistency requires stopped writers, the
  default no-prestop topology is NO-GO until a compatible snapshot/migration or
  separately approved maintenance/blue-green plan exists.
- Apply only backward-compatible migrations online while the old application is
  serving. Use bounded lock/statement timeouts and prove the previous image can
  run against the upgraded schema. An incompatible migration blocks cutover.
- Drain or observe active turns while the old release remains live. Do not call
  a restart “lossless”; record the authorized interruption policy and recovery
  evidence.
- Roll back backend/frontend/worker/connector as one release set. Default
  rollback keeps compatible additive schema and post-cutover data; data restore
  or Alembic downgrade requires an explicit, tested data-owner decision.
- Before an old binary sees a newly seeded builtin or incompatible project
  schema, run the candidate's reviewed idempotent compatibility helper as
  specified by the runbook.
- The generic GitHub Release workflow currently increments semantic versions
  and does not implement these application-image and cutover gates. Do not run
  it unchanged for a private SHA-suffixed production release.
- Never commit production credentials, private endpoints, current tags, or
  operations-inventory values to the repository.
