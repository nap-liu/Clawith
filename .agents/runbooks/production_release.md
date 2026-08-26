# Production release runbook

This is the canonical executable release workflow for the digital employee
platform.  A release plan is incomplete unless it instantiates every section
below with the actual release SHA, image digests, backup decision, validation
evidence, owners, and rollback anchors.

Production hosts, credentials, registry authentication, domains, and absolute
data paths live in the approved operations inventory.  Keep them out of Git.
Use the symbolic values in this document when preparing a plan and resolve them
only inside the authorized release session.

## 1. Authorization and roles

Production release, push, tag, PR merge, and production configuration changes
each require explicit authorization.  Naming a plan or asking for an audit does
not authorize execution.

Assign these roles before the window; one person may hold multiple roles, but
the release owner and rollback decision must be explicit:

- release owner: owns GO/NO-GO and the immutable release record;
- operator: builds, pushes, pulls, and performs the cutover;
- verifier: runs the acceptance matrix and records evidence;
- data owner: approves backup omissions and any point-in-time restore;
- rollback owner: can stop the release immediately when a stop condition fires.

Record the maintenance window, expected write interruption, rollback deadline,
communication channel, and user notification text before execution.

## 2. Freeze the authoritative source

1. Fetch only the selected company remote and inspect `company/main`.
2. Verify the reviewed candidate is based on the current authoritative main.
3. If main moved, integrate it, rerun the change audit and all required Docker
   validation, and choose a new final SHA.
4. Prefer a linear/fast-forward integration that preserves the reviewed SHA.
   If repository policy creates a merge or squash commit, that new commit is the
   release SHA and all SHA-bound gates must run again.
5. Confirm the release SHA is reachable from `company/main`, the tree is clean,
   and backend and frontend `VERSION` files still match the current upstream
   semantic version.

The private release identifier is:

```text
v<upstream-version>-<release-sha7>
```

Never increment the semantic version for private work.  Create the annotated
Git tag at the explicit immutable SHA, never at a mutable branch name.  The Git
tag, backend image, frontend image, active Compose references, and recorded
digests must all identify the same SHA.

The generic `.github/workflows/release.yml` is not the production release
authority for a private iteration: it currently increments semantic versions,
creates release metadata, and does not build application images.  Do not run it
unchanged for a private release.  Use this runbook, or first change and review
the workflow so it accepts an exact SHA-suffixed version and implements every
gate in this document.

## 3. Release-candidate gates

All validation runs in Docker against the exact release checkout.  Production
is not a development test target.

Required gates:

1. `git diff --check` and a clean worktree;
2. neutral review of the real authoritative-main-to-release-SHA diff;
3. migrations applied to an isolated PostgreSQL database;
4. backend compile/import checks and the full backend test suite;
5. frontend prebuild checks, TypeScript, and production build;
6. the repository user-visible wording scanner;
7. local 3008 health and feature-level API/IM/browser validation appropriate to
   the change;
8. rendered frontend nginx template validation in Docker when proxy behavior,
   environment substitution, uploads, WebSocket, or MCP routing is in scope;
9. a tested rollback helper for any newly seeded builtin tool or irreversible
   enablement step.

Record exact commands, counts, skipped tests, known baseline warnings, and the
review verdict.  A raw `Traceback` match is not a deployment failure signal;
use health, startup completion, precise error signatures, and behavior.

## 4. Build and publish immutable images

The standard path is: build on the authorized local build machine, push to the
registry, then let production pull.  Do not build application source on the
production host except under separately approved emergency recovery.

1. Create a clean build context from the exact release SHA.
2. Generate a `COMMIT` provenance file containing the full SHA in the backend
   build context and add OCI revision/version labels to both images.
3. Build backend and frontend together even when only one source tree changed.
4. On Apple Silicon, use `docker buildx build --platform linux/amd64 --push`.
   A normal local Compose build is an arm64 development artifact and must never
   be pushed as a production image.
5. Use existing build caches.  If a dependency layer misses cache, keep plain
   progress logs, clear stale proxy build arguments, and repair the local
   builder rather than silently switching to a production-host build.
6. Rebuild the AIO sandbox only when its source/base image changed or the user
   explicitly requested it.  Otherwise retain its current tag and digest.
7. Record immutable backend/frontend index digests and verify each published
   manifest contains `linux/amd64`.

Build template:

```bash
RELEASE_SHA=<full-release-sha>
RELEASE_ID=v<upstream-version>-<release-sha7>
REGISTRY=<approved-registry-namespace>

docker buildx build --platform linux/amd64 --progress=plain \
  --label org.opencontainers.image.revision="$RELEASE_SHA" \
  --label org.opencontainers.image.version="$RELEASE_ID" \
  --tag "$REGISTRY/backend:$RELEASE_ID" --push <clean-context>/backend

docker buildx build --platform linux/amd64 --progress=plain \
  --label org.opencontainers.image.revision="$RELEASE_SHA" \
  --label org.opencontainers.image.version="$RELEASE_ID" \
  --tag "$REGISTRY/frontend:$RELEASE_ID" --push <clean-context>/frontend
```

Do not treat a mutable tag as proof.  Capture the registry index digest for
each image and use `image:tag@sha256:digest` in the candidate deployment file.

## 5. Prepare production without cutting over

Resolve current production facts from the secure operations inventory and the
running containers; never copy remembered credentials or old tags into a plan.

Before downtime:

1. confirm the Compose project/directory and preserve the active Compose file;
2. record current backend, frontend, and independent AIO tag plus digest;
3. record current database migration revision, health, restart counts, and a
   short baseline of HTTP and IM delivery errors;
4. render the candidate Compose configuration and inspect its diff;
5. verify frontend has non-empty `API_UPSTREAM`; verify the optional object
   storage upstream when that route is enabled;
6. pull both candidate application images by digest while the old stack remains
   live;
7. verify sufficient disk space for the database dump and workspace archive;
8. prepare a timestamped backup directory under the approved production data
   root, but do not take the authoritative data backup yet;
9. prepare a rollback script that references the previous immutable digests and
   does not delete volumes.

The frontend nginx truth is the template baked into the frontend image and
rendered at container start.  Do not assume a similarly named repository or
host file controls production.  Validate the rendered candidate template with
the actual environment values before cutover.

## 6. Drain and freeze writers

Application restart can interrupt active turns.  Use the supported active-turn
inspection when available; otherwise choose a quiet window and inspect recent
activity.  Stop accepting new ingress, wait for active work to drain within the
approved bound, and record any work that must be recovered.

Production currently requires no old/new backend overlap for process-local
turn reservations and cancellation state.  Rolling replacement is forbidden
unless the release explicitly proves a distributed implementation.

Stop frontend ingress and every application writer, including the API backend,
trigger/worker/connector roles, schedules, and any separately deployed writer.
Keep PostgreSQL, Redis, and object storage running for backup.  Confirm the old
backend process count is zero before starting a candidate backend.

Do not use `docker compose down`; do not remove volumes.

## 7. Take the authoritative cutover backup

The rollback backup is taken only after writers stop.  An earlier preparation
snapshot is supplemental and cannot replace this cutover snapshot.

The default full backup set is:

- custom-format PostgreSQL dump;
- Redis RDB snapshot;
- agent workspace/archive or an equivalent storage snapshot;
- object-storage snapshot/manifest when object storage is authoritative;
- active Compose file and a rendered candidate Compose file;
- previous and candidate backend/frontend tag plus digest;
- securely stored environment/config snapshot when it changes;
- `README` containing timestamps, revisions, omissions, and restore commands;
- executable rollback script using the previous immutable digests;
- SHA-256 manifest for every backup artifact.

At minimum, validate the PostgreSQL dump with `pg_restore --list`, validate all
checksums, and confirm archives are readable.  A restore drill against an
isolated database/storage target must have passed before the production window.

Skipping database, Redis, workspace, or object-storage backup requires explicit
user authorization for this release and a recorded reason.  No-schema-change
does not automatically mean no-backup: cutover backup also protects writes and
seeded state.

## 8. Cut over

Only the configured bootstrap role may mutate schema.  The current backend
entrypoint performs table checks, safe patches, and `alembic upgrade head`
before starting the application.  Do not run an extra manual migration path
unless the exact release source or migration plan requires it.

1. Activate the candidate Compose file pinned to both new image digests.
2. Start the one bootstrap-capable backend with the candidate image.
3. If migration/startup fails, do not start frontend or another backend; enter
   rollback immediately.
4. Wait for migration completion, builtin-tool seeding, Uvicorn readiness, and
   backend health.
5. Confirm exactly one backend process/replica where process-local turn state
   requires it.
6. Start frontend and any separately approved worker/connector roles in the
   topology defined by the candidate Compose file.
7. Confirm no container unexpectedly uses a mutable or mismatched image.

## 9. Acceptance matrix

The release is not complete when containers merely start.  Record evidence for:

### Platform

- backend health returns 200 with the upstream version;
- the version/provenance endpoint reports the release SHA;
- frontend home/login returns 200 through the public proxy;
- authenticated API, WebSocket reconnect, session history, and one normal turn
  succeed;
- `/api`, `/ws`, `/mcp`, uploads, and object-storage proxy routes relevant to
  the release resolve through the rendered nginx configuration;
- restart count remains zero after stabilization.

### IM delivery and recall

- proactive sends persist one normalized receipt;
- replay/concurrent claim does not send twice and reports the stored lifecycle
  state truthfully;
- supported adapters complete send then recall for their configured P2P/group
  semantics;
- unsupported transports return `unsupported` without a false provider call;
- repeated recall is idempotent;
- a user cannot recall inbound, cross-agent, or unauthorized messages;
- media caption/title and all other user-visible fields use the shared sanitizer;
- no `pending` receipt remains beyond the two-minute delivery lease without
  becoming an explicit uncertain state;
- provider success followed by receipt failure never triggers fallback duplicate
  output.

Run real-provider production smoke only in dedicated test conversations and only
when explicitly authorized.  Do not create unrelated production users, PATs,
sessions, or test data merely to prove health.

## 10. Observation and release record

Observe closely for at least 30 minutes and retain a 24-hour follow-up watch.
Compare against the captured baseline:

- HTTP 5xx and latency;
- backend/frontend restart count;
- IM delivery `failed`, `unknown`, `partial`, and stale `pending` counts;
- recall failures by transport;
- provider duplicate-send reports;
- database, Redis, workspace, and object-storage errors;
- active-turn interruption or recovery errors.

Immediate rollback conditions are: migration failure, unhealthy startup beyond
the approved timeout, authentication/tenant-isolation regression, data loss or
corruption, wrong-message recall, duplicate provider output, or inability to
execute the prepared rollback.

After the observation gate, store a release record containing source/main SHA,
tag, both image digests, independent AIO digest, backup ID, migration before and
after, validation evidence, known warnings, owners, timestamps, and final GO.
Publish release notes only after these values are fixed.

## 11. Rollback

Rollback backend and frontend as one release set.  Do not leave mixed SHAs.

1. Stop frontend ingress and all candidate application writers.
2. Before starting a binary that predates a newly seeded builtin tool, run the
   candidate image's idempotent rollback helper against the current database.
   For normalized IM recall:

   ```bash
   python -m app.scripts.rollback_im_recall
   ```

3. Restore the saved Compose file or set both application images to their
   previous immutable digests.
4. Start the previous backend, wait for health, then start frontend and other
   roles; confirm no candidate process remains.
5. Repeat the platform and IM smoke subset and record the rollback result.

Default application rollback keeps current database/workspace data when the old
binary is schema-compatible.  Do not automatically run Alembic downgrade.

Restore the cutover PostgreSQL/workspace/object-storage snapshot only for an
explicitly approved data/schema rollback.  Such a restore discards post-cutover
writes and therefore requires the data owner's decision.  Restore aligned data
components to the same point in time; do not combine an old database with newer
workspace/object state.  Redis restore is likewise an explicit consistency
decision, not a reflexive step.

## 12. Required plan output

Every concrete release plan derived from this runbook must include:

- change range and authoritative branch;
- exact final SHA and release ID, or the rule for recomputing them after merge;
- included/excluded components and migration/seed impact;
- Docker validation and neutral-audit evidence;
- backend/frontend build and immutable digest capture commands;
- production topology and no-overlap decision;
- drain method and maintenance communication;
- cutover backup set, path placeholder, validation, and approved omissions;
- ordered cutover commands and pass/fail gates;
- feature-specific acceptance matrix;
- observation duration and rollback thresholds;
- previous digests, candidate rollback helper, and data-restore decision tree;
- named owners and explicit authorization checkpoints.
