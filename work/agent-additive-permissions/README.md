# Agent additive permissions iteration

- Baseline: yybpc/company/main, `0cc8c01c1eb39a8c17f67f6333dbf701369aa677`.
- Branch: `feat/agent-additive-permissions`.
- Task worktree, relative to the original repository: `.worktrees/agent-additive-permissions`.
- Scope: normalized company/department/user grants; shared REST creation/settings
  and MCP writer; shared frontend editor; safe legacy normalization.
- Original checkout and unrelated worktrees were preserved. No production
  inspection, provider messaging, push, tag or deployment was performed.

## Validation

All application validation ran in Docker, with the exact task backend mounted
at `/app` and isolated PostgreSQL databases. No host Python or virtualenv was used.

- 60 affected tests passed: additive ACL (3), visibility, creation, provisioning,
  MCP configuration, contact relationships, group-session access and visibility.
- After the final test-only import/format cleanup, the affected MCP/contact
  subset passed again (34 tests).
- The original baseline MCP configuration suite passed (21 tests). Old test
  expectations for dormant grants and copied creator rows were updated to the
  accepted additive semantics. Contact tests now use PostgreSQL rather than
  SQLite; their mocked tool adapter runs in-process to share the fixture
  transaction, independently of execution-process integration tests.
- Focused Ruff checks passed. TypeScript checking and Vite production build
  passed; Vite retained its existing large-chunk advisory.
- `git diff --check` passed. Every changed/new hand-written source is at most
  800 physical lines; the largest is the existing schema module at 772 lines.

The initial migration was exercised against an isolated database bootstrapped by the actual
baseline checkout. Company/custom/private Agents had deliberately hidden grants.
Upgrade preserved other-user effective levels as use/manage/none respectively,
archived all three old rosters and retained only the two effective optional rows.
Its original unchanged-state downgrade was subsequently rejected by independent
audit and replaced by the stricter maintenance conversion described below.

Playwright used the isolated frontend nginx proxy on container port 3008, backed
by the task API and PostgreSQL. It verified:

- Company off/on preserves a named manager.
- Department management saves alongside company use and personal management.
- Creation uses the same directory picker and persists company use + manager.
- Nested custom-creation dialogs keep both the company select and department
  picker above their parent and fully interactive.

The browser account hit its default Agent quota on the first create attempt;
only that disposable test account's quota was raised, and the full flow passed.
Screenshots: `permissions-complete.png`, `permissions-create.png`, and
`permissions-modal.png`. Temporary browser authorization was removed after QA.

## Retained local resources and limits

Task-owned containers: `agent-permissions-test-pg`, `agent-permissions-test-api`,
`agent-permissions-test-web`; network: `agent-permissions-test`. The isolated
frontend is mapped to loopback port 3012, leaving the shared host port 3008 stack
untouched. Task dependency volumes are `agent-permissions-node-modules` and
`agent-permissions-browser-modules`. These resources and this dirty task worktree
are retained for the next authorized iteration; owner: this task.

Full-suite testing, production migration/cutover and production/provider smoke
tests were not performed. Migration requires a separately approved offline
writer cutover; old permission writers must remain stopped from before conversion
until the candidate alone starts. Legacy columns remain display compatibility
projections for the supported capability. Deprecated Plaza is out of scope.

## Independent audit fixes

See `AUDIT.md` for the initial findings and independent follow-up review.
Settings now derive editable state from complete grants, retaining explicit
administrator levels and inactive departments. Directory metadata does not
decide which grants exist.

The conversion rejects normal online Alembic/bootstrap execution on an existing
parent database. It requires `-x agent_permissions_cutover=offline`, an explicit
operator assertion of a separately approved and verified maintenance state.
It bounds lock waits to 5 seconds and statements to 60 seconds. Nonempty-database
downgrade always refuses; archived rosters are never automatically restored over
new decisions. Fresh empty-database bootstrap remains automatic.

Five focused PostgreSQL tests passed: the three existing additive ACL tests plus
two real Alembic cutover tests. These verify default online/bootstrap rejection
without changes, effective company/custom/private access after approved isolated
conversion, repeat bootstrap, rejection after a legacy roster replacement without
new-writer audit, and bounded failure while an old writer holds a conflicting lock.
The test harness creates and drops its own disposable PostgreSQL databases.

Exact affected test command inside the backend Docker image, with the final
backend checkout mounted read-only at /app and an isolated DATABASE_URL:

```sh
python -m pytest -p no:cacheprovider -q \
  tests/test_agent_permission_cutover.py tests/test_agent_additive_permissions.py
ruff check --no-cache app/services/agent_permissions.py \
  app/schemas/agent_permissions.py \
  alembic/versions/20260910_additive_agent_permissions.py \
  tests/test_agent_permission_cutover.py
```

Docker frontend `npm run build` passed its complete prebuild checks, TypeScript,
and Vite build. The pre-existing large-chunk advisory remains.
Initial harness attempts with the pytest executable missing the app import path
and Ruff's cache on a read-only mount failed before checks; the exact commands
above corrected the invocation and passed.

The final Docker Playwright run passed through nginx on container port 3008.
It verified company toggles and picker saves preserve every original grant,
including administrator **use** and inactive-department **manage**, and creation
persists the same normalized shape. Follow-up PostgreSQL evaluation verified
the demoted administrator keeps explicit use and the reactivated department
regains manage after other matching grants are removed. Screenshots were
visually inspected.

The first picker attempts used an incomplete directory fixture (no active member
count) and then an exact button name that omitted the rendered member count.
Adding real disposable memberships/counts and matching the department button's
name prefix corrected the harness. A follow-up access assertion initially
forgot that the active Operations management grant also covered the administrator;
removing that independent matching grant confirmed the expected personal-use
fallback. These were fixture/assertion corrections; no product behavior was
weakened to make checks pass. Temporary browser authorization was removed.

Independent follow-up review found no remaining blocking code issue within the
requested scope and confirmed canonical documentation matches the implementation.
The [maintenance release plan](RELEASE-PLAN.md) records the remaining authorization,
actual-production-state, immutable-image and backup/restore rehearsal gates.
Normal online release remains NO-GO.

## Company main integration

The permission implementation was saved as local commit `81fe5e13`, then
`yybpc/company/main` at `e7abac770197f493e0efbd8975164f766041918f` was merged
into this feature branch. One import conflict in PostHireSettingsModal was
resolved by retaining the shared permission editor and the new model-label
helpers together. Creation/model headers and both i18n additions merged intact.
Whitespace in the newly tracked editor CSS was cleaned before the first commit.

The unpublished ACL migration now follows `model_extra_headers`, keeping one
head: `additive_agent_permissions`. Its cutover tests use that current main
parent. A fresh task-owned PostgreSQL database, `test_agent_permissions_merged`,
was used instead of changing the earlier candidate's stamped database.

Docker validation after integration: **37 tests passed**, covering cutover,
additive permissions, Agent route creation/provisioning, MCP, model header API
configuration and serving-platform labels:

```sh
python -m pytest -p no:cacheprovider -q \
  tests/test_agent_permission_cutover.py tests/test_agent_additive_permissions.py \
  tests/test_agent_create_route.py tests/test_agent_provisioning.py \
  tests/test_mcp_config_tools.py tests/test_model_headers_config.py \
  tests/test_model_platform.py
```

Frontend `npm run build` passed all prebuild checks, TypeScript and Vite.
The single Alembic head, focused Ruff and staged diff checks passed; all source
files delivered from the original baseline remain within 800 lines (maximum 777).
No provider calls, full-suite or new browser run was needed for this import and
migration-order integration. The previous browser results concern the prior
candidate, and the retained long-running local API is not claimed as the merged
candidate's validation environment. No push, production inspection or deployment
was performed; unrelated original-checkout work remains untouched.

## Second company main integration

Merged four subsequent company-main commits through
`a6904c9615b6e04aabe9b329c3ea65a378f6ed4f` into the permission branch without
conflicts. They preserve media follow-up notification policy and refine model
brand/platform display labels. The permission behavior and migration graph did
not change.

Docker verification passed **17 tests**:
`test_media_notification_mode.py`, `test_subagent_group_origin.py`, and
`test_agent_additive_permissions.py`, using the isolated merged PostgreSQL
database and a temporary task-owned Redis. Frontend full prebuild, TypeScript
and Vite build passed; the existing chunk-size advisory remains. Staged diff
checks and every incoming source file's 800-line gate passed (largest incoming
source: 610 lines).

Two initial runs each had 2 media-test failures and 15 passes because Redis was
unavailable for workspace leases. The first selected local Redis image lacked
its server executable and exited; a verified working Redis image returned PONG,
and all 17 tests then passed without changing product code or assertions.
The temporary `agent-permissions-merge-redis` container and its anonymous volume
were removed after verification. No shared stack, browser/provider workflow or
production environment was changed, and no push or deployment was performed.
