# Environments, Docker validation, and operations

## Local topology

The repository root compose stack contains PostgreSQL, Redis, backend, and frontend. The supported browser/API entry point is `http://localhost:3008`; backend routes are reached through the frontend nginx proxy. Do not assume a host-exposed backend port.

The persistent backend container can be useful for live integration, but its bind mount may point at another checkout. Confirm mounts before trusting it. Authoritative code validation uses a one-shot container with the exact current `backend/` mounted at `/app`.

## Docker-only backend test pattern

Use an image with development test dependencies and an isolated PostgreSQL database on the local compose network. The pattern is:

```bash
docker run --rm --entrypoint python \
  --network <local-compose-network> \
  -v "<exact-checkout>/backend:/app" -w /app \
  -e PYTHONPATH=/app \
  -e DATABASE_URL="postgresql+asyncpg://<local-user>:<local-password>@<postgres-service>:5432/<isolated-test-db>" \
  -e AGENT_DATA_DIR=/tmp/agents \
  <backend-test-image> -m pytest <tests> -q -p no:cacheprovider
```

- Override the production entrypoint so tests do not run migrations implicitly.
- Use PostgreSQL for JSONB, constraints, and async database behavior; SQLite is not an acceptance environment.
- Create/clone only an isolated test database. Never drop, migrate, or seed the local development database as test setup.
- Runtime images may omit pytest/ruff; create or reuse a dedicated test image instead of installing on the host.
- Import the complete relevant SQLAlchemy model graph in standalone scripts when foreign-key resolution requires it.

For a compile/import check, use the same mounted container and run `python -m compileall` or a focused import there. This rule applies even when host Python happens to be available.

## Browser and live integration

Use the local 3008 stack for end-to-end channel/UI checks. Mint test authorization inside a backend container or use an existing local signed-in browser session. Validate the actual API/event/UI behavior, not source shape.

Do not restart the shared stack casually. A separate backend container should omit worker/connector roles when sharing local data, so it cannot double-run triggers or channel connectors. Prefer a fully isolated compose project when migrations or destructive setup are involved.

## Production boundary

Production layout, credentials, and current image tags are operational secrets/changeable facts and are intentionally not copied from personal memory into Git. Obtain them from the approved deployment configuration at execution time.

Stable production invariants are:

- compose data/configuration lives on the data volume, not an ephemeral root filesystem;
- application data, Redis, PostgreSQL, and agent workspaces require an explicit backup/rollback decision;
- a cutover backup is taken after stopping writers;
- backend and frontend share one release SHA;
- production images target `linux/amd64`;
- frontend nginx configuration is baked from `frontend/nginx.conf.template`.
