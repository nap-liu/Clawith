# Published Page Direct Rendering — Production Release Plan

> Prepared: 2026-08-26
> Authoritative baseline: `company/main@26bcd4b551dc12a2b1e90856be6a9867b6b402c1`
> Reviewed candidate: `fix/published-page-direct-render@77d5fbc7c7e299cc84130765e510ef3d40329c08`
> Release ID: `v1.10.3-77d5fbc`
> Code readiness: GO
> Production execution: NO-GO until the authorization and dynamic-value gates below are bound

This plan and the evidence update are stored in a later documentation-only
commit. They are release records, not application inputs and not part of the
release image. The immutable application release SHA remains `77d5fbc...`.

This document is a plan and does not authorize a push, tag, merge, production
login, production mutation, or deployment.

## 1. Release scope

Change range: `company/main@26bcd4b5..77d5fbc7`.

Included:

- backend published-page authorization and direct HTML response;
- frontend direct published-page proxy and removal of the platform viewer;
- the published-page SDK's current-context OAuth behavior;
- backend and frontend images built from the same exact SHA.

Explicitly excluded:

- no Alembic migration, ORM schema change, database seed change, or tool-description change;
- no Redis key-format or persistence change;
- no AgentData or object-storage format change;
- no AIO sandbox source/base-image change, so its current immutable digest is retained;
- no broad refactor outside the published-page path.

`publish_page.description` remains byte-for-byte equal to the authoritative
baseline in both the database seeder and runtime fallback. Only the successful
publication result states the actual runtime watermark behavior.

## 2. Roles, window, and authorization gates

Before production preparation, record real names in the immutable release
record for release owner, operator, verifier, data owner, and rollback owner.
The requester is the decision authority for database and AgentData backup
omissions; the remaining operational roles must be bound before GO.

Also bind:

- maintenance window and communication channel;
- user notice for the brief ingress interruption;
- maximum active-turn drain time;
- rollback deadline;
- explicit authorization for Git tag/push, registry push, production login,
  Compose change, cutover, and any rollback.

If any value is unbound, the production state remains NO-GO.

## 3. Exact-SHA release-candidate evidence

- The exact `77d5fbc` release checkout was clean and
  `git diff --check company/main..77d5fbc` passed. The later documentation-only
  release-record commit is deliberately excluded from the image build context.
- Neutral implementation and change audit: PASS; P0/P1/P2 are all zero.
- `backend/app/services/tool_seeder.py` has zero diff from `company/main`.
- Docker focused backend tests: 36 passed.
- Docker full backend suite at `77d5fbc`: 2,401 passed, 28 skipped.
- Frontend linux/amd64 production Docker build at `77d5fbc` completed,
  including prebuild checks, SDK tests, TypeScript, and Vite build; cache reuse
  is permitted and was recorded by BuildKit.
- The rendered candidate nginx started successfully with the production-style
  `API_UPSTREAM`/`MINIO_UPSTREAM` contract and proxied the feature test.
- At the user's explicit direction, the final browser repetition used the
  Browser capability with local Chrome instead of building another Docker
  Chromium/font layer. This approved task-specific exception overrides the
  repository's Docker-only browser default for this repetition. Local Chrome
  opened the exact candidate at `http://localhost:3008/p/browserdirect`.
  Top-level context, non-null origin, localStorage, cookies, SDK/CSS/API access,
  ES dynamic import, Worker, fonts, forms, download declaration, media support,
  navigation, report CSS, and explicit SDK watermark all passed. Page errors
  and browser console errors were empty.
- The original local 3008 container was restored and returned HTTP 200. All
  candidate containers, isolated databases, test volume, and local candidate
  image were removed.
- Earlier same-scope Docker Chromium evidence remains supplemental: the runtime
  rendering, proxy, and SDK code did not change between that evidence and
  `77d5fbc`; the later commits only restored the existing tool description,
  adjusted its assertions, and recorded evidence.

Before image publication, run and attach the output from the exact immutable
checkout:

```bash
git status --short
git diff --check company/main..77d5fbc7c7e299cc84130765e510ef3d40329c08
docker run --rm -v "$PWD:/repo:ro" -w /repo <approved-backend-test-image> \
  python scripts/check_user_visible_keyword.py
```

Apply `alembic upgrade head` to an isolated logical clone of the current local
schema and record that it is a no-op at the existing head. The repository's
unrelated empty-database migration-chain duplicate-column defect is not changed
by this candidate and must not be "fixed" inside this release.

If `company/main` moves, integrate it first. Any merge/squash SHA becomes the
new release SHA and invalidates every SHA-bound test, audit, tag, and image.

## 4. Build and publish immutable images

From a clean archive/worktree at the exact release SHA, add the full SHA to the
backend `COMMIT` provenance file without changing the Git tree used for review.
Build and push both application images for linux/amd64:

```bash
RELEASE_SHA=77d5fbc7c7e299cc84130765e510ef3d40329c08
RELEASE_ID=v1.10.3-77d5fbc
REGISTRY=yeyecha-registry.cn-hangzhou.cr.aliyuncs.com/public

docker buildx build --platform linux/amd64 --progress=plain \
  --label org.opencontainers.image.revision="$RELEASE_SHA" \
  --label org.opencontainers.image.version="$RELEASE_ID" \
  -t "$REGISTRY/clawith-backend:$RELEASE_ID" --push <clean-context>/backend

docker buildx build --platform linux/amd64 --progress=plain \
  --label org.opencontainers.image.revision="$RELEASE_SHA" \
  --label org.opencontainers.image.version="$RELEASE_ID" \
  -t "$REGISTRY/clawith-frontend:$RELEASE_ID" --push <clean-context>/frontend
```

Record each registry index digest and verify that each manifest contains
`linux/amd64`. Candidate Compose must use `image:tag@sha256:digest`; tags alone
are not accepted. Do not rebuild or retag AIO.

## 5. Production facts and rollback anchors

During the authorized session use `ssh clawith` and the approved Compose
directory `/alidata/clawith/config`. Resolve all current facts dynamically;
do not reuse remembered tags or digests.

Create, without cutting over:

```text
/alidata/clawith/config/backups/<YYYYMMDD-HHMMSS>-published-page-direct-render-pre/
```

Store:

- active `docker-compose.yml.before` and rendered candidate Compose;
- current and candidate backend/frontend tags plus immutable digests;
- retained AIO tag plus digest;
- current Alembic revision, health, restart counts, and short HTTP error baseline;
- production `.env.before` in the approved secure location (never Git);
- object-storage versioning status and a read-only object inventory manifest;
- Redis RDB snapshot metadata and copied RDB artifact;
- `rollback.sh`, restore notes, timestamps, approved omissions, and SHA-256 manifest.

The rollback helper must pin the previous backend and frontend digests, update
the stopped frontend reference before the stopped backend reference, never
delete volumes, and be syntax-tested before ingress is stopped.

### Backup decision

- PostgreSQL: **omitted by explicit product decision**. This candidate changes
  no schema, seed, description row, or affected business table, so the requested
  affected-table hot-backup set is empty. Record this reason in the release log.
- AgentData/workspace: **omitted by explicit product decision**.
- Redis: not omitted. Run an online `BGSAVE` only after checking memory/COW and
  disk headroom and confirming it will not breach the production latency gate;
  wait for completion, copy the RDB to the backup directory, and checksum it.
  If the headroom gate fails, stop and obtain explicit omission authorization;
  do not degrade production to satisfy the backup step.
- Object storage: do not copy report data for this change. Capture the
  read-only versioning/inventory manifest allowed by the runbook and checksum it.
- Configuration and binary rollback anchors: required and checksummed.

No PostgreSQL writer stop is performed for a dump because no database dump is
taken. The ordinary cutover still drains writers to preserve process-local turn
state and prevent old/new backend overlap.

## 6. Prepare while production remains live

1. Verify branch ancestry, exact tag target, and backend/frontend `VERSION=1.10.3`.
2. Inspect the running `config-*` topology, current digests, restart counts,
   disk/memory headroom, health, active turns, and recent errors.
3. Render candidate Compose and inspect the diff. Only backend/frontend image
   references may change. Confirm frontend has `API_UPSTREAM=backend:8000` and
   `MINIO_UPSTREAM=minio:9000` where enabled.
4. Pull both candidate images by digest while the old stack remains live.
5. Run the candidate frontend image's rendered `nginx -t` with actual production
   environment values before cutover.
6. Prepare and syntax-test rollback assets. Validate the backup commands,
   destination, headroom gates, and checksum procedure while production remains
   live; the authoritative Redis artifact and object manifest are captured and
   checksummed only after writers stop in section 7. Do not stop ingress until
   all preparation gates are green.

## 7. Drain and ordered cutover

1. Announce the window and stop frontend ingress from accepting new work.
2. Inspect active turns and drain within the approved bound. Record any turn
   requiring recovery.
3. Stop backend and every application writer/worker/connector/scheduler found in
   the running topology. Keep PostgreSQL, Redis, MinIO, and AIO running.
4. Confirm the old backend process count is zero. Old/new overlap is forbidden.
5. Take and validate the authorized rollback artifacts from section 5.
6. Activate candidate Compose pinned to both new image digests.
7. Start exactly one bootstrap-capable backend. Do not run a manual migration;
   its entrypoint performs the expected head/no-op check and unchanged seeding.
8. Wait at least 65 seconds and require completed startup, healthy backend,
   zero restarts, expected `1.10.3` version, and exact release provenance.
9. Start frontend, then only the previously inventoried writer roles. Require
   every application container to use the candidate SHA/digest pair.
10. If backend startup or provenance fails, do not start frontend; rollback.

## 8. Production acceptance

Run read-only checks first, then the minimum authorized feature smoke:

- backend health/version and frontend home/login return 200;
- `/p/43j7pIO6` returns the stored report directly as top-level HTML;
- report asset scripts/CSS load without sandbox, CORS, cookie, storage, or CSP
  errors caused by the platform;
- response is `text/html`, `no-store`, and does not contain route-owned CSP,
  XFO, COOP, COEP, CORP, Permissions-Policy, or `X-Content-Type-Options`;
- public, authenticated, and restricted authorization behave correctly;
- cross-tenant and unapproved access remain denied and live revocation works;
- original SDK `ready`, `onReady`, OAuth callback/exchange, query cleanup,
  `triggerHook`, and report-authored `data-watermark` remain operational;
- visit count and visitor audit increment once for the canonical response;
- the compatibility `/api/pages/<short_id>/viewer-context` response remains
  authorized, no-store, unrestricted, and does not increment the view count;
- normal authenticated API, WebSocket reconnect, session history, and one normal
  turn pass; relevant `/api`, `/ws`, `/mcp`, MinIO, and upload proxy routes pass;
- restart counts remain zero and HTTP 5xx/latency do not regress from baseline.

Do not create unrelated production users, sessions, reports, PATs, or provider
messages for smoke testing.

## 9. Observation and stop conditions

Observe closely for 30 minutes and retain a 24-hour follow-up watch. Compare
HTTP 5xx/latency, container restarts, authentication failures, published-page
4xx/5xx, database/Redis/storage errors, and interrupted-turn recovery with the
pre-cutover baseline.

Rollback immediately on unhealthy startup beyond 65 seconds, wrong/mixed SHA,
authentication or tenant-isolation regression, report corruption, SDK breakage,
material 5xx/latency regression, data loss, repeated restarts, or failure of the
prepared rollback helper.

## 10. Rollback

1. Stop frontend ingress, candidate backend, and all candidate application
   writers. Confirm candidate backend count reaches zero.
2. While all application processes remain stopped, restore the frontend image
   reference to its previously captured immutable digest first.
3. Restore the backend image reference to its previous immutable digest second,
   then render and validate Compose. Do not start either side until both old
   references are present and no mixed SHA remains.
4. Start the previous backend first and wait for health; then start the previous
   frontend and the inventoried writer roles.
5. Repeat the platform and published-page smoke subset and record the result.

There is no seeded-tool rollback helper, Alembic downgrade, PostgreSQL restore,
AgentData restore, or object-data restore for this candidate. Redis restore is
performed only if Redis corruption is independently diagnosed and the data
owner explicitly approves the consistency point; it is not part of normal
binary rollback.

The final immutable release record must contain source/main SHA, release tag,
both image digests, retained AIO digest, rollback-anchor directory, backup
omissions, Redis/object-manifest checksums, migration revision before/after,
test/audit/browser evidence, owners, timestamps, observation result, and final
GO or rollback decision.
