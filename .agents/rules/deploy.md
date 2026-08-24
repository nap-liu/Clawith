# Environment and deployment rules

## Validation boundary

- Run Python, pytest, lint, type checks, builds, backend scripts, integration tests, and browser validation only inside Docker containers.
- Do not create, activate, or use host venvs. Do not use host Python as a validation shortcut.
- Validate changes only against local Docker environments. Production is not a development test target.
- Use an isolated PostgreSQL test database; do not substitute SQLite for PostgreSQL behavior and do not write test data into the local development database.

## Local stack safety

- The local application is reached through `http://localhost:3008`; `/api` and `/ws` are proxied by the frontend container.
- A long-running backend container may mount a different checkout. For authoritative validation, launch a one-shot container and mount the exact current `backend/` at `/app`.
- Inspect other worktrees and active containers before stopping or rebuilding a shared compose stack.
- Never run `docker compose down`, remove volumes, or mutate another worktree merely to simplify a test.
- New migrations must be tested in an isolated database/compose project. Do not advance a shared development database to a revision unavailable to the main checkout.

## Deployment authorization

- Production configuration changes and deployment require explicit user authorization after a reviewed plan.
- Prepare images before cutover. At cutover, stop writers and then take the authoritative consistent database/workspace backup so rollback has no write gap.
- Keep secrets in environment/config stores. Architecture and runbooks committed to Git must use symbolic hosts, users, and credentials.
- Production nginx behavior comes from the template baked into the frontend image; validate the rendered template in Docker.

The concrete local test recipe is in `.agents/architecture/environments-and-operations.md`.
