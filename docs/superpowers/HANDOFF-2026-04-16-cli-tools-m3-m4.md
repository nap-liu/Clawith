# CLI Tools Management — Handoff Notes (to continue M3 + M4)

**Date:** 2026-04-16
**Branch:** `feat/cli-tools-management` (local only, not pushed)
**Worktree:** `/Users/liuxi/Projects/clawith/.worktrees/feat-cli-tools`
**Previous session:** brainstorming → spec → plans → M1 + M2 implementation

## Status

Done:
- Design spec: `docs/superpowers/specs/2026-04-16-cli-tools-management-design.md` (v4, 388 lines, 14 sections)
- Four plans: `docs/superpowers/plans/2026-04-16-cli-tools-m{1,2,3,4}-*.md`
- M1 (Storage + Sandbox base) — 4 commits, 6 mock tests passed
- M2 (Backend API + execution hardening) — 5 commits, 31 unit tests passed

Pending:
- **M3** (Frontend `tools` tab extension) — not started
- **M4** (GC + migration) — not started
- **M2 Task 13** (permission matrix integration test) — skipped; project has no API test harness; tracked as follow-up

## Commits on this branch (since `company/main` HEAD `c246d04`)

```
1c199dc feat(cli-tools): CRUD + upload + test-run API (M2 tasks 8-12)
d469a00 feat(cli-tools): rewrite executor + rewire agent_tools (M2 tasks 6-7)
34a66b6 feat(cli-tools): BinaryStorage — content-addressed filesystem blobs
3513bfd feat(cli-tools): schema + crypto + placeholders + errors (M2 tasks 1-4)
7d66129 feat(cli-tools): BinaryRunner — sandboxed binary execution
b7ffd2a feat(cli-tools): publishing runbook for sandbox image
73db88e feat(cli-tools): minimal debian-slim sandbox image
58edc21 feat(cli-tools): add cli_binaries volume for uploaded binaries
7241ba6 docs(cli-tools): implementation plans for M1-M4
8bdbb0a docs(cli-tools): add v3 design spec for CLI tools management
```

## Diffs from the plans worth knowing

Read the plan first; these are the places reality diverged.

### 1. Sandbox image tag used for the *local* test build

Plan says `clawith-cli-sandbox:debian-bookworm-slim-YYYYMMDD` via Make. Locally
during M1 we built the image as `clawith-cli-sandbox:local-test` (via plain
`docker build -t`) because no internal registry is reachable from the dev
machine. Production will use the Make workflow.

### 2. BinaryRunner tests are mock-based, not end-to-end

The plan wrote a happy-path test that builds a real shebang script under
`pytest.tmp_path` and passes it to a real docker run. In Docker-Desktop-on-Mac,
the container running pytest sees paths like `/tmp/pytest-.../script.sh`,
which the outer docker daemon cannot bind-mount because it is the path
inside the pytest container, not on the host.

Workaround: the production tests mock `docker.from_env` and assert the
call-arg shape (image, user, cap_drop, cpu_limit, etc.). A single
end-to-end test sits behind `@pytest.mark.integration` and is skipped by
default. This pattern is intentional — do not revert to host-tmp tests.

`pyproject.toml` now has a registered `integration` pytest mark.

### 3. `Tool.cli_config` field name

Plans say `cli_config`; reality uses the pre-existing `Tool.config` JSON
column. `app.services.cli_tools.schema.CliToolConfig` is the
Pydantic-validated shape of whatever is stored there when `Tool.type == "cli"`.

### 4. `Tool.is_active` vs. `Tool.enabled`

The spec talks about `is_active`; the existing Tool model field is actually
`enabled`. The executor reads `tool.enabled`. The API accepts `is_active` in
request bodies (to match spec wording), maps to `tool.enabled` internally,
returns `is_active` in responses.

### 5. AgentTool per-agent override dropped

The pre-M2 `_try_execute_cli_tool` merged `AgentTool.config` into `Tool.config`
before execute. New executor takes only `Tool` — per-agent override is **not**
supported in v1. This is intentional: spec §3 Non-goals lists per-user
ownership as out-of-scope; per-agent is the same kind of fine-grained
ownership. Re-enable later by copying the Tool row with a different tenant
scope.

### 6. API subpath `/tools/cli` instead of `/tools?type=cli`

Spec §5.4 writes `GET /api/tools?type=cli`. The existing `app/api/tools.py`
router already owns `/tools` with `GET /tools` (list-all) and a few
`/tools/{id}` verbs. To avoid monkey-patching it we registered a new router
at prefix `/tools/cli`, so paths are:

    GET    /api/tools/cli
    POST   /api/tools/cli
    GET    /api/tools/cli/{id}
    PATCH  /api/tools/cli/{id}
    DELETE /api/tools/cli/{id}
    POST   /api/tools/cli/{id}/binary
    POST   /api/tools/cli/{id}/test-run

The M3 plan says `cliToolsApi.list() = fetchJson('/tools?type=cli')` — the
implementer **must** change this to `/tools/cli`. Same for detail/update/
delete: strip the `?type=cli` query and use the subpath.

### 7. `encrypt_data` signature took two args

Plan referenced `encrypt_data(plaintext) -> str`. Real signature is
`encrypt_data(plaintext: str, key: str) -> str`. `app.services.cli_tools.crypto`
wraps it with `get_settings().SECRET_KEY`.

### 8. `AuditLog` field shape

Plan assumed `AuditLog(user_id, action, resource_type, resource_id, detail)`.
Real model has `(user_id, agent_id, action, details, ip_address, created_at)`
— no `resource_type`/`resource_id` columns. We pack them into the `details`
JSON dict.

### 9. Local backend now mounts the feat worktree

We recreated `clawith-backend-1` via
`docker compose -p clawith up -d --force-recreate backend` from this worktree
so the new `cli_binaries` volume is live. This means the local backend is
running M1 + M2 code (and the rest of `feat/cli-tools-management`). Compose
project name is still `clawith`.

## What to do next

### M3 — Frontend (plan file: `docs/superpowers/plans/2026-04-16-cli-tools-m3-frontend.md`)

Eight tasks. Before starting, apply these adjustments from the divergences above:

- Replace every `/tools?type=cli` in the M3 plan's `api.ts` with `/tools/cli`
  and adjust URL construction throughout:
  - `list:    fetchJson('/tools/cli')`
  - `get:     fetchJson(`/tools/cli/${id}`)`
  - `create:  fetchJson('/tools/cli', { method: 'POST', body })`
  - `update:  fetchJson(`/tools/cli/${id}`, { method: 'PATCH', body })`
  - `delete:  fetchJson(`/tools/cli/${id}`, { method: 'DELETE' })`
  - `testRun: fetchJson(`/tools/cli/${id}/test-run`, { method: 'POST', body })`
  - `uploadBinary: fetch(`${base}/api/tools/cli/${id}/binary`, ...)`
- The M3 plan mentions an "agent count" in the disable-confirmation text.
  The backend does **not** currently return that count — either leave the
  confirmation with just the tool name, or add a cheap count field to
  `CliToolOut` (requires a small backend change: count AgentTool rows
  referencing the tool id, add to the out schema, update mask_env call).
  Recommendation: ship M3 without the count; track "disable shows agent
  count" as a follow-up since spec §11.4 mentions it but it's not a hard
  requirement.

Project front-end has no automated test harness. Each task ends with a
manual pass-through; run `docker compose -p clawith up -d --force-recreate frontend`
and click through the enterprise settings page on the local frontend URL
(whatever host/port the local `docker-compose.yml` maps for the frontend).

### M4 — GC + migration (plan file: `docs/superpowers/plans/2026-04-16-cli-tools-m4-gc-migration.md`)

Four tasks. All straightforward. The migration runbook includes the
command shape for the legacy tool — do not hard-code the legacy binary's
name or path anywhere in code (internal-info rule).

### Before either milestone starts

Resume in a fresh Claude Code session rooted at the worktree:

```
cd /Users/liuxi/Projects/clawith/.worktrees/feat-cli-tools
```

Confirm the branch:

```
git log -1 --oneline
# expect: 1c199dc feat(cli-tools): CRUD + upload + test-run API (M2 tasks 8-12)
```

Read this file (`docs/superpowers/HANDOFF-...`). Then read the relevant
milestone plan. Then use `superpowers:executing-plans` or
`superpowers:subagent-driven-development` to proceed.

## Push state

The feat branch may be pushed to any remote this repository tracks. A
push does not force-merge anything; it's a safe way to back up progress
and share with teammates. The implementer decides when to push based on
their review workflow.

## Open questions / decisions deferred

- **AgentTool per-agent config override** — dropped in v1; revisit after
  v1 ships if there is real demand.
- **Per-tenant concurrent-execute cap** — mentioned in spec §13 as a
  follow-up; unchanged.
- **Multi-architecture binary support** — spec §11.3 guarantees
  linux/amd64 only; unchanged.
- **API integration test harness** — project baseline has none; adding
  one is a cross-cutting task outside this feature.
