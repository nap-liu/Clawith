# Environments, Docker validation, and operations

## Local topology

The repository root compose stack contains PostgreSQL, Redis, backend, and frontend. The supported browser/API entry point is `http://localhost:3008`; backend routes are reached through the frontend nginx proxy. Do not assume a host-exposed backend port.

The persistent backend container can be useful for live integration, but its bind mount may point at another checkout. Confirm mounts before trusting it. Authoritative code validation uses a one-shot container with the exact current `backend/` mounted at `/app`.

## Docker-only backend test pattern

Use an image with development test dependencies and an isolated PostgreSQL database on the local compose network. The pattern is:

```bash
docker run --rm --entrypoint python \
  --network <local-compose-network> \
  -v "<exact-checkout>/backend:/app:ro" \
  -v "<exact-checkout>/frontend:/frontend:ro" -w /app \
  -e PYTHONPATH=/app \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -e DATABASE_URL="postgresql+asyncpg://<local-user>:<local-password>@<postgres-service>:5432/<isolated-test-db>" \
  -e AGENT_DATA_DIR=/tmp/agents \
  <backend-test-image> -m pytest <tests> -q -p no:cacheprovider
```

- Override the production entrypoint so tests do not run migrations implicitly.
- Use PostgreSQL for JSONB, constraints, and async database behavior; SQLite is not an acceptance environment.
- Create/clone only an isolated test database. Never drop, migrate, or seed the local development database as test setup.
- Runtime images may omit pytest/ruff; create or reuse a dedicated test image instead of installing on the host.
- Import the complete relevant SQLAlchemy model graph in standalone scripts when foreign-key resolution requires it.
- Frontend is mounted read-only because the backend scene tests consume the same
  mini-program URI fixture as the frontend. Do not copy that contract fixture.
- `python -m app.scripts.bootstrap_db` and online Alembic CLI `upgrade head(s)`
  share the schema boundary in `alembic/env.py`; the container entrypoint calls
  the former. `app.models.registry` is the complete metadata import graph.
- The asyncpg migration connection sets `lock_timeout=5s` and
  `statement_timeout=60s` through `server_settings`, covering the initial
  bootstrap advisory lock as well as DDL. It does not rely on libpq PGOPTIONS.
  A lock timeout aborts migration without suppressing the failure or starting
  application roles against an unverified schema.
- A truly empty database creates current metadata and guards, then stamps heads
  in one transaction. A PostgreSQL session advisory lock serializes bootstrap
  across concurrent-index migration commits. Versioned databases run upgrades
  without a preceding `create_all`; application lifespan only seeds data.
- A populated database without a revision is rejected without mutation. It needs
  a verified legacy baseline, never a blind stamp. Raw historical revision
  targets and offline SQL retain Alembic semantics; the current-schema shortcut
  only applies to the bootstrap command and online CLI upgrades to head(s).
- Validate new, repeated and previous-version paths. Historical migrations are
  not rewritten to fix current `create_all` collisions. New schema constraints
  and indexes must exist in metadata as well as the incremental migration;
  non-metadata guards use `database_guards`. Previously stamped databases need a
  new repair revision; `create_all` does not repair indexes on existing tables.

For a compile/import check, use the same mounted container and run `python -m compileall` or a focused import there. This rule applies even when host Python happens to be available.

## Browser and live integration

Use the local 3008 stack for end-to-end channel/UI checks. Mint test authorization inside a backend container or use an existing local signed-in browser session. Validate the actual API/event/UI behavior, not source shape.

Do not restart the shared stack casually. A separate backend container should omit worker/connector roles when sharing local data, so it cannot double-run triggers or channel connectors. Prefer a fully isolated compose project when migrations or destructive setup are involved.

## Production boundary

Production layout, credentials, and current image tags are operational secrets/changeable facts and are intentionally not copied from personal memory into Git. Obtain them from the approved deployment configuration at execution time.

Stable production invariants are:

- compose data/configuration lives on the data volume, not an ephemeral root filesystem;
- application data, CLI upload state, Redis, PostgreSQL, object storage, and
  agent workspaces each require an explicit change-impact and backup/rollback
  decision;
- prepare and validate candidate/rollback artifacts while the old release is
  live; do not pre-stop or `down` the application;
- take any required change-scoped snapshot online and close to cutover. If an
  affected store cannot be captured consistently without stopped writers, the
  default release is NO-GO until a compatible or explicitly approved topology
  exists;
- backend and frontend share one release SHA;
- production images target `linux/amd64`;
- frontend nginx configuration is baked from `frontend/nginx.conf.template`.

The full production topology checklist, image-build/cache path, online backup
decision, one-command replacement, acceptance matrix, and rollback tree are canonical in
`.agents/runbooks/production_release.md`.  Do not reconstruct a release plan
from this architecture summary alone.
