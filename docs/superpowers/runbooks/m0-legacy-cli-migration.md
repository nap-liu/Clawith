# M0 — Legacy CLI tool migration

**When:** After M1-M4 are deployed to production. Before tenant admins
manage the legacy CLI tool through the new UI.

**What:** Moves the production legacy CLI tool off its hard-coded binary
path onto the new upload model. Idempotent.

## Prerequisites

- `platform_admin` account + JWT
- SSH access to the production host
- The legacy tool's Tool row UUID
- The binary's path inside the currently-running backend container

## Procedure

### 1. Capture current state

Inside the production host:

```
docker exec <backend-container> ls -la /path/to/legacy-binary
docker exec <postgres-container> psql -U clawith -d clawith \
  -c "SELECT id, name, type, config FROM tools WHERE type = 'cli';"
```

Record the Tool UUID and the binary path. Confirm the binary is executable.

### 2. Ensure the binary is readable from inside the backend container

The migration script runs inside the backend container and reads the
source path there. If the binary is already at a path inside the image
(e.g. `/usr/local/bin/<name>`), no copy is needed. Otherwise, copy it in:

```
docker cp /host/path <backend-container>:/tmp/legacy-binary
```

### 3. Run the migration script

```
docker exec <backend-container> \
  python -m scripts.migrate_legacy_cli_tool \
    --tool-id <UUID> \
    --source-path /path/inside/container
```

Expected output:
```
migrated tool=<UUID> sha=<64-hex> bytes=<N>
```

### 4. Verify

```
docker exec <postgres-container> psql -U clawith -d clawith \
  -c "SELECT id, config FROM tools WHERE id = '<UUID>';"
```

Expect `config.binary_sha256` set and `config.binary_uploaded_at` set.

```
docker exec <backend-container> ls /data/cli_binaries/_global/<UUID>/
```

A single `<sha>.bin` file, mode 0555.

### 5. Smoke test via the new execution path

Trigger one real invocation (an agent that uses this tool) and watch
backend logs for any `CliToolError` class (`BINARY_FAILED`,
`SANDBOX_FAILED`, etc). No new errors → migration succeeded.

### 6. Rollback

The migration only wrote a new file + updated the Tool.config JSON. To
revert, paste the old config back:

```
docker exec <postgres-container> psql -U clawith -d clawith \
  -c "UPDATE tools SET config = '<OLD JSON>'::json WHERE id = '<UUID>';"
```

The newly-uploaded binary becomes an orphan and is garbage-collected 30
days later by the daily GC task.

## Notes

- The script is idempotent: running it twice with the same source writes
  the file only once (content-addressed) and rewrites Tool.config with
  identical data.
- This procedure is **platform_admin** only: `org_admin` cannot manage
  the legacy tool's row because its `tenant_id` is `NULL` (global).
