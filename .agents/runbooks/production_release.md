# Production release runbook

This is the canonical executable release workflow for the digital employee
platform. A release is incomplete unless every applicable section has evidence,
an owner, an authority checkpoint, and a tested rollback anchor.

Production hosts, credentials, private endpoints, registry authentication,
domains, and absolute data paths live in the approved operations inventory.
Resolve them only inside the authorized release session; never copy remembered
values into this repository.

## 1. Authorization and release record

Production inspection, backup, migration, external smoke messages, configuration
mutation, cutover, rollback, push, and tag are separate authority boundaries.
Planning or auditing grants none of them.

Record before GO:

```text
RELEASE_OWNER=
OPERATOR=
VERIFIER=
DATA_OWNER=
ROLLBACK_OWNER=
WINDOW_START=
WINDOW_END=
AUTHORIZED_INTERRUPTION_POLICY=
CURRENT_RELEASE_SHA=
CURRENT_BACKEND_DIGEST=
CURRENT_FRONTEND_DIGEST=
CURRENT_AIO_DIGEST=
RELEASE_SHA=
UPSTREAM_VERSION=
RELEASE_ID=v<UPSTREAM_VERSION>-<RELEASE_SHA7>
NEW_BACKEND_DIGEST=
NEW_FRONTEND_DIGEST=
COMPOSE_PROJECT=
BACKUP_ID=
```

The release owner makes GO/NO-GO decisions. The data owner approves backup
scope and any restore that discards writes. The rollback owner may stop the
release when a declared condition fires.

## 2. Freeze the authoritative source

1. Fetch only the selected company remote and inspect `company/main`.
2. Verify the reviewed candidate is based on current authoritative main.
3. If main moves, integrate it, repeat the diff audit and required Docker gates,
   and select a new final SHA.
4. Confirm the candidate checkout is clean and backend/frontend `VERSION` files
   match the current upstream semantic version.
5. Run the user-visible wording scanner and confirm changed/new hand-written
   source files comply with the 800-line gate.

The private release identifier is `v<upstream-version>-<release-sha7>`. Tag the
explicit immutable SHA, never a branch name. Backend, worker, connector,
frontend, compose references, OCI labels, and the release record must agree on
that SHA.

The generic `.github/workflows/release.yml` is not the authority for a private
release unless it has first been changed and reviewed to implement every gate in
this runbook.

## 3. Release-candidate gates

All validation runs in Docker against the exact release checkout and isolated
state. Production is not a development test target.

Required evidence:

1. `git diff --check`, clean worktree, and final diff review;
2. migrations from the current production parent to head on an isolated
   PostgreSQL database plus a production-schema-derived copy;
3. backend compile/import checks and the full backend test suite;
4. frontend prebuild checks, TypeScript, and production build;
5. user-visible wording/i18n checks;
6. focused API, database, event, IM-adapter, and browser behavior for the change;
7. local port-3008 validation for UI/cross-layer changes;
8. rendered `frontend/nginx.conf.template` validation when proxy, WebSocket,
   uploads, object storage, or MCP routing is in scope;
9. tenant/ownership isolation tests for every changed query or capability;
10. previous-image compatibility against the migrated schema;
11. rollback helper drills for new seeded tools or compatibility boundaries.

Record exact commands, pass/fail counts, skipped tests, and baseline comparison.
Never report a suite as green when it has failures. Raw `Traceback` text alone
is not a health signal; use startup completion, health, precise errors, and
observable behavior.

## 4. Build and publish immutable images

Build on the authorized build machine from a clean context at `RELEASE_SHA`.
Production-host source builds are emergency-only and require separate approval.

1. Put the full SHA in the backend `COMMIT` build-context file.
2. Build backend and frontend together for `linux/amd64` with OCI revision and
   version labels.
3. Select and record the backend dependency mode described below. Use
   release-scoped cache references; never overwrite the cache being consumed.
4. Rebuild AIO only when its source/base changed or explicitly requested.
5. Push immutable release tags, then record index digests and prove the manifest
   contains `linux/amd64`.

### 4.1 Backend dependency mode

The mutable `buildcache-amd64` tag is not a dependency artifact. A concurrent
or later build can replace its cache graph, and harmless build-argument changes
can change cache keys. Treat registry cache as a speed hint only.

For the normal code-only release, use the current trusted backend image by its
`linux/amd64` manifest digest as `CLAWITH_DEPS_IMAGE`. The Dockerfile copies only
its `/usr/local` dependency tree; candidate application code is copied from the
clean release context afterward. This path is allowed only when all gates pass:

1. `backend/pyproject.toml` is unchanged from `CURRENT_RELEASE_SHA` to
   `RELEASE_SHA`;
2. its checksum equals `/app/pyproject.toml` inside the dependency image;
3. the dependency image is the running trusted release, is digest-pinned, and
   is `linux/amd64`;
4. its Python version equals the candidate Dockerfile's digest-pinned Python
   base; and
5. review confirms that the dependency-build commands, required runtime
   libraries, and `/usr/local` copy contract did not change.

Example gate (resolve the exact image and base references first):

```bash
BACKEND_DEPS_IMAGE=<current-backend-linux-amd64-image@sha256:digest>
TARGET_PYTHON_BASE=<candidate-digest-pinned-python-base>
CANDIDATE_DEPS_SHA=$(sha256sum <clean-context>/backend/pyproject.toml | awk '{print $1}')
CARRIER_DEPS_SHA=$(docker run --rm --platform linux/amd64 \
  --entrypoint sha256sum "$BACKEND_DEPS_IMAGE" /app/pyproject.toml | awk '{print $1}')
test "$CANDIDATE_DEPS_SHA" = "$CARRIER_DEPS_SHA"
test "$(docker run --rm --platform linux/amd64 --entrypoint python \
  "$BACKEND_DEPS_IMAGE" --version 2>&1)" = \
  "$(docker run --rm --platform linux/amd64 --entrypoint python \
  "$TARGET_PYTHON_BASE" --version 2>&1)"
```

If any gate fails, omit `CLAWITH_DEPS_IMAGE`. That explicitly selects the
Dockerfile's `deps-build` stage and is a dependency-refresh release. Review the
resolved package set before publish; package downloads are expected only in
this mode. Never force the carrier path across changed or uncertain dependency
inputs.

### 4.2 Immutable cache chain and build template

Each release writes new cache tags keyed by its release SHA and reads only the
current release's cache tags. Never use the same cache reference for input and
output, and never publish again to the legacy mutable `buildcache-amd64` tag.
The first release adopting this scheme may use the legacy cache as a read-only
hint or omit it; backend dependency reuse still comes from the verified
immutable dependency image.

Template (resolve registry, current cache tags, and reviewed build arguments
from current configuration):

```bash
RELEASE_SHA=<full-release-sha>
RELEASE_SHA7=${RELEASE_SHA:0:7}
UPSTREAM_VERSION=<upstream-version>
RELEASE_ID="v${UPSTREAM_VERSION}-${RELEASE_SHA7}"
BACKEND_REPOSITORY=<approved-backend-image-repository>
FRONTEND_REPOSITORY=<approved-frontend-image-repository>
CURRENT_SHA7=<current-release-sha7>
BACKEND_CACHE_FROM="$BACKEND_REPOSITORY:buildcache-$CURRENT_SHA7-amd64"
BACKEND_CACHE_TO="$BACKEND_REPOSITORY:buildcache-$RELEASE_SHA7-amd64"
FRONTEND_CACHE_FROM="$FRONTEND_REPOSITORY:buildcache-$CURRENT_SHA7-amd64"
FRONTEND_CACHE_TO="$FRONTEND_REPOSITORY:buildcache-$RELEASE_SHA7-amd64"
BACKEND_DEPS_IMAGE=<verified-current-backend-image@sha256:digest>

docker buildx build --builder <verified-builder> \
  --platform linux/amd64 --progress=plain \
  --cache-from type=registry,ref="$BACKEND_CACHE_FROM" \
  --cache-to type=registry,ref="$BACKEND_CACHE_TO",mode=max \
  --build-arg "CLAWITH_DEPS_IMAGE=$BACKEND_DEPS_IMAGE" \
  <reviewed-stable-backend-build-args> \
  --label org.opencontainers.image.revision="$RELEASE_SHA" \
  --label org.opencontainers.image.version="$RELEASE_ID" \
  --tag "$BACKEND_REPOSITORY:$RELEASE_ID" --push <clean-context>/backend

docker buildx build --builder <verified-builder> \
  --platform linux/amd64 --progress=plain \
  --cache-from type=registry,ref="$FRONTEND_CACHE_FROM" \
  --cache-to type=registry,ref="$FRONTEND_CACHE_TO",mode=max \
  --label org.opencontainers.image.revision="$RELEASE_SHA" \
  --label org.opencontainers.image.version="$RELEASE_ID" \
  --tag "$FRONTEND_REPOSITORY:$RELEASE_ID" --push <clean-context>/frontend
```

Never use `docker compose build` or `docker compose up --build` for production
artifacts. In carrier mode the build graph must not execute the backend
`deps-build` stage or download Python packages. If it does, cancel promptly,
confirm no detached build remains, and diagnose the selected image/build args.
In dependency-refresh mode, inspect and record the resolved packages rather
than describing their download as an unexpected cache failure.

## 5. Prepare production while the old release stays live

Resolve facts from the current deployment and secure inventory, not memory:

- explicit Compose project/directory and active rendered configuration;
- current backend/frontend/AIO tags, digests, revisions, and restart counts;
- database migration revision and relevant schema/default/constraint state;
- active-turn/trigger baseline, health, HTTP 5xx/latency, and channel errors;
- disk capacity for selected snapshots;
- authoritative stores affected by this diff.

Create digest-pinned candidate and rollback compose files. Each must describe
backend, worker, connector, and frontend as one release set; backend/worker/
connector use the same backend digest and frontend uses the matching release
SHA. Preserve unchanged PostgreSQL, Redis, object storage, and AIO services.

While the old application remains live:

1. pull candidate and rollback images by digest;
2. verify manifests, OCI revision, image `COMMIT`, and architecture;
3. render both compose files with the explicit production project name;
4. validate required environment such as frontend `API_UPSTREAM` and optional
   object-storage upstream;
5. validate the rendered nginx template from the candidate image;
6. prepare the exact cutover and rollback commands;
7. confirm the cutover window contains no build, pull, or compose improvisation.

## 6. Decide and take change-scoped online backups

Derive backup scope from the actual diff and rollback contract. Record each
decision, including an explicit reason for every omission.

| Changed authority | Default online protection |
|---|---|
| no migration, seed, or data correction | compose, old/new digests, revision, checksums, rollback command; no automatic database dump |
| PostgreSQL schema/data | one consistent custom-format dump of affected tables or the full database when cross-table rollback requires it |
| Redis semantics/recovery | fresh persisted snapshot and checksum |
| Agent workspace format/content | storage snapshot/archive or approved storage-native version |
| object storage authority | storage snapshot/version manifest |
| CLI binaries/resumable upload state | finalized binary store and upload-state snapshot |
| environment/configuration | secure config snapshot outside Git plus redacted manifest |

Take required snapshots online, after image preparation and as close to cutover
as practical. Use one PostgreSQL snapshot for related tables, bounded lock wait,
and no destructive dump options. Validate custom dumps with
`pg_restore --list`, validate archives/readability, and checksum every artifact.
Restore drills run only against isolated targets, never production.

If a changed authority cannot be snapshotted consistently while live, or a
rollback would require an unplanned cross-store point-in-time restore, declare
NO-GO. Design a compatible snapshot/migration, blue-green topology, or a
separately authorized maintenance release. Do not solve this by an ad-hoc
`stop`/`down`.

## 7. Apply backward-compatible migrations online

Only online-compatible migrations may run before the application replacement
while the old release still serves traffic. Prove this in isolated concurrent
read/write tests.

- Use short `lock_timeout` and bounded `statement_timeout`; lock contention must
  fail quickly and leave the old application healthy.
- Avoid table rewrites and long validation locks. For large constraints, add
  them unvalidated, normalize data, then validate in an explicit bounded step.
- Run migration from the candidate image with its entrypoint overridden so a
  one-shot command cannot accidentally start another backend:

```bash
docker compose -p <production-compose-project> \
  -f <candidate-compose> run --rm --no-deps \
  --entrypoint alembic backend upgrade heads < /dev/null
```

- Confirm exactly one expected Alembic head, schema/default/constraint values,
  tenant behavior, and old-application health after migration.
- Start the previous image against the upgraded schema in an isolated drill.

An incompatible migration or failed compatibility drill is NO-GO. Do not start
the candidate and do not automatically downgrade production schema.

## 8. Active work and interruption decision

Observe active turns, triggers, tasks, and connector work while the old release
remains live. Prefer a quiet window and allow work to drain within the approved
bound without closing ingress early. Record any active operation that may need
durable recovery.

The standard single-replica Compose replacement may cause a brief interruption.
It is permitted only when the release record explicitly authorizes that policy.
If the requirement is strict request/turn continuity, this topology is NO-GO;
deploy only after a tested blue-green/rolling design with atomic traffic switch.

## 9. One-command application replacement

After every gate passes, run exactly one replacement command for all application
roles:

```bash
docker compose -p <production-compose-project> \
  -f <candidate-compose> up -d --no-deps --no-build \
  backend worker connector frontend
```

Hard rules:

- no pre-cutover `docker compose stop` or `down`;
- no backend-first or role-by-role start sequence;
- no `--remove-orphans`, build, pull, or dependency restart;
- PostgreSQL, Redis, object storage, and unchanged AIO remain running;
- do not claim that one Compose command is strict zero downtime.

When the command returns, verify all four roles together. Only after acceptance
may the already-validated candidate compose atomically replace the canonical
compose file; that file replacement must not cause another restart.

## 10. Acceptance matrix

### Release identity and health

- all four application roles run the intended digest/release SHA;
- backend, worker, and connector are healthy; frontend returns 200;
- restart counts remain zero and exactly the intended connector count exists;
- `/api/health` and version/provenance report the expected result;
- authenticated API, session history, one normal turn, and WebSocket reconnect
  work through the public/frontend proxy;
- relevant `/api`, `/ws`, `/mcp`, upload, and storage routes use the rendered
  candidate nginx configuration;
- database revision and changed seeded/configured values are correct.

### Conversation and delivery

- accepted turns persist completion/recovery state across disconnects;
- provider waits do not leave long idle database transactions;
- tool output, context compaction, and memory behavior relevant to the release
  match local evidence;
- durable outbound messages have one normalized receipt, no duplicate provider
  send, and no stale `pending` beyond its lifecycle without explicit `unknown`;
- recall and confirmation preserve ownership, source Session, provider
  capability truth, and P2P/group semantics.

Real-provider or production-data smoke is limited to dedicated authorized test
identities/conversations. Do not create unrelated users, PATs, sessions, or
messages merely to prove health.

## 11. Observation and release record

Observe closely for at least 30 minutes and retain a 24-hour follow-up watch.
Compare with the pre-cutover baseline:

- HTTP 5xx/latency and application restart counts;
- database locks, long idle transactions, migration/storage errors;
- LLM/tool/context/recovery errors and interrupted turns;
- delivery `failed`, `unknown`, `partial`, and stale `pending` counts;
- duplicate sends, recall failures, and connector errors.

Immediate rollback conditions include unhealthy startup, wrong SHA/digest,
migration failure, authentication or tenant-isolation regression, data loss,
wrong-message recall, duplicate external output, unbounded context recovery, or
inability to run the prepared rollback.

Store an immutable release record with the source/main SHA, tag, application and
AIO digests, migration before/after, backup ID/scope/omissions, validation
evidence, owners, timestamps, interruption observation, warnings, and final GO.

## 12. One-command rollback

Rollback backend, worker, connector, and frontend as one release set with the
prepared previous digest-pinned compose file:

```bash
docker compose -p <production-compose-project> \
  -f <rollback-compose> up -d --no-deps --no-build \
  backend worker connector frontend
```

Do not pre-stop/down the stack, roll roles back separately, rebuild, pull, remove
orphans, or mix SHAs during rollback.

When compatible additive schema is retained, an older image may not contain the
newer Alembic revision file. Its rollback Compose must therefore start only
non-bootstrap roles and must not hide migration errors with
`ALLOW_MIGRATION_FAILURE`. Prove that topology against the upgraded schema in
the isolated old-image drill. Resume bootstrap ownership only with an image
whose migration graph recognizes the database revision.

Before rollback to a binary that predates a newly seeded builtin tool, run the
candidate image's reviewed idempotent compatibility helper. For normalized IM
recall, the existing helper is:

```bash
python -m app.scripts.rollback_im_recall
```

For Agent self-service settings, use the same exact-tool cleanup contract:

```bash
python -m app.scripts.rollback_agent_self_settings
```

Before rollback to a binary that predates project-scoped Agents, follow the
candidate helper's documented apply/status/restore lifecycle:

```bash
python -m app.scripts.project_legacy_rollback apply
python -m app.scripts.project_legacy_rollback status
# after legacy roles are gone and before candidate roles return:
python -m app.scripts.project_legacy_rollback restore
python -m app.scripts.project_legacy_rollback status
```

The full credential/role setup and verification must be instantiated from the
current helper and secure inventory during release planning. If a compatibility
helper requires a writer freeze, that requirement must already have a separately
authorized maintenance/blue-green procedure; do not improvise it during an
incident.

Default binary rollback keeps compatible additive schema and current data. Do
not automatically run Alembic downgrade or restore snapshots. A point-in-time
data restore discards post-cutover writes and requires the data owner's explicit
decision; restore all coupled stores to one consistent point.

After rollback, repeat the health/identity and relevant conversation/delivery
acceptance subset, record the result, and continue observation.

## 13. Required concrete plan output

Every release plan derived from this runbook includes:

- authoritative branch, diff range, final SHA, and release ID;
- included/excluded components, 800-line gate, migration/seed/state impact;
- exact Docker validation and independent review evidence;
- backend dependency mode, immutable cache-chain build commands, and immutable
  image digest capture commands;
- current/candidate/rollback topology and explicit Compose project;
- interruption requirement and why single-replica or blue-green is acceptable;
- online backup matrix, evidence, omissions, and data-owner approvals;
- compatibility migration commands, timeouts, and old-image drill;
- the one cutover command, pass/fail acceptance, and observation thresholds;
- the one rollback command, compatibility helpers, and data-restore decision;
- named owners and every external authorization checkpoint.
