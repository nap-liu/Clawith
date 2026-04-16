# CLI Tools — Operator Handbook

Audience: platform ops. Covers where state lives, what to watch, how
to investigate, and which admin endpoints exist. Read alongside
`docs/cli-tools/AUTHOR_GUIDE.md`.

Source anchors:

- API — `backend/app/api/cli_tools.py`
- Executor — `backend/app/services/cli_tool_executor.py`
- Storage — `backend/app/services/cli_tools/{storage,state_storage}.py`
- GC — `backend/app/services/cli_tools/{gc,gc_scheduler}.py`
- Metrics — `backend/app/services/cli_tools/metrics.py`
- Rate limit — `backend/app/services/cli_tools/rate_limiter.py`

---

## 1. Storage layout

```text
/data/cli_binaries/               <- volume: ${PROJECT}_cli_binaries
  <tenant_id>|_global/<tool_id>/<sha256>.bin   (ro 0555)

/data/cli_state/                  <- volume: ${PROJECT}_cli_tool_state
  <tenant_id>|_global/<tool_id>/<user_id>/     (mounted rw at /home/sandbox
                                                when persistent_home=true)
```

`_global` is the literal tenant segment for platform-scoped tools.

### Host paths & disk usage

```bash
docker volume inspect ${COMPOSE_PROJECT_NAME:-clawith}_cli_binaries \
  --format '{{ .Mountpoint }}'
docker volume inspect ${COMPOSE_PROJECT_NAME:-clawith}_cli_tool_state \
  --format '{{ .Mountpoint }}'

# Per-tenant binary cost
docker exec backend sh -c 'cd /data/cli_binaries && du -sh */ | sort -h'

# Per-(tool, user) HOME cost
docker exec backend sh -c 'cd /data/cli_state && du -sh */*/*/ | sort -h | tail -20'
```

### Orphan sweep (out of band)

Scheduler runs daily, `age_threshold_days=30`. Manual run:

```bash
docker exec backend python -c '
import asyncio
from pathlib import Path
from app.database import async_session_maker
from app.services.cli_tools.storage import BinaryStorage
from app.services.cli_tools.gc import gc_cli_binaries

async def main():
    async with async_session_maker() as db:
        n = await gc_cli_binaries(
            db=db,
            storage=BinaryStorage(root=Path("/data/cli_binaries")),
            age_threshold_days=30,
        )
        print(f"deleted {n} orphans")

asyncio.run(main())
'
```

Rule: delete iff **unreferenced by any live Tool row AND mtime older
than the threshold**. Referenced files are never touched.

---

## 2. Monitoring

### Scrape config

`/api/metrics` requires a platform_admin bearer token:

```yaml
scrape_configs:
  - job_name: clawith-backend
    metrics_path: /api/metrics
    scheme: https
    static_configs: [{ targets: ['api.example.com'] }]
    authorization: { type: Bearer, credentials: <platform_admin token> }
```

### Key series

| Metric | Labels | Alert on |
|--------|--------|----------|
| `clawith_cli_tool_executions_total` | `tool_name, tenant_id, outcome` | `rate(outcome="binary_failed"[5m])` > 0.5/s; any `outcome="internal_error"` sustained |
| `clawith_cli_tool_execution_duration_seconds` | `tool_name` | P95 > `0.8 * timeout_seconds` |

`outcome` values: `ok`, `internal_error`, `binary_failed`, `timeout`,
`sandbox_failed`, `validation_error`, `permission_denied`, `not_found`,
`resource_limit`.

### Drill down to audit rows

```sql
-- Spike at 14:05 UTC on tool_name=svc with outcome=binary_failed
SELECT id, user_id, created_at,
       details->>'trace_id'    AS trace_id,
       details->>'exit_code'   AS exit_code,
       details->>'duration_ms' AS duration_ms,
       details->>'stderr_tail' AS stderr_tail
FROM audit_logs
WHERE action = 'cli_tool.execute'
  AND details->>'outcome'   = 'binary_failed'
  AND details->>'tool_name' = 'svc'
  AND created_at BETWEEN '2026-04-16 14:00' AND '2026-04-16 14:15'
ORDER BY created_at DESC
LIMIT 50;
```

`stderr_tail` is the last ~2 KB; for more, pivot by `trace_id`.

---

## 3. Audit

Every execution writes one row: `action='cli_tool.execute'`. `details`
JSON has `tool_id`, `tool_name`, `tenant_id`, `agent_id`, `args_hash`
(sha256 of rendered argv — **args themselves are not stored**, to
avoid PII leaks), `outcome`, `exit_code`, `duration_ms`, `stdout_len`,
`stderr_tail`, `trace_id`.

`trace_id` is propagated through:

- HTTP response header `X-Trace-Id`
- Sandbox env `CLAWITH_TRACE_ID`
- Backend `logger.extra={'trace_id': …}` on every line during the run

Correlate end-to-end:

```bash
docker logs backend 2>&1 | grep '"trace_id":"f8c1…"'
docker logs gateway 2>&1 | grep 'f8c1'
```

```sql
SELECT action, created_at, details FROM audit_logs
WHERE details->>'trace_id' = 'f8c1...' ORDER BY created_at;
```

---

## 4. Incident playbook

### svc reports IP-allowlist denied

1. `GET /api/tools/cli/$TOOL_ID` — confirm `sandbox.network=true`
   and `egress_allowlist` contains `api.example.com`.
2. If set correctly, the sandbox is egressing fine — the downstream
   service’s own allowlist is rejecting. Grab the outbound NAT IP
   (`docker exec <sandbox-container> curl -s https://ifconfig.me` or
   ask network ops) and forward to the downstream team.

### EROFS / read-only filesystem

Tool tried to write outside `/tmp` or `/home/sandbox`. Fix by enabling
`runtime.persistent_home=true` or pointing the tool at `$TMPDIR`.
**Never** disable `readonly_fs` — that opens `/etc`, `/usr`, etc.

### Rate-limited agent

```bash
redis-cli ZCARD "cli-tools:rl:$TOOL_ID:$AGENT_ID:$USER_ID"
redis-cli ZRANGE "cli-tools:rl:$TOOL_ID:$AGENT_ID:$USER_ID" 0 -1 WITHSCORES
```

Preferred fix: raise `runtime.rate_limit_per_minute` on the tool.
Emergency override:

```bash
redis-cli DEL "cli-tools:rl:$TOOL_ID:$AGENT_ID:$USER_ID"
```

Redis outage does not block executions — limiter fails open.

### HOME quota exhausted

Symptom: `validation_error` with `home quota exceeded`.

```bash
curl -sH "Authorization: Bearer $TOKEN" \
  "$API/api/tools/cli/$TOOL_ID/home-usage?user_id=$USER_ID" | jq

curl -X DELETE -H "Authorization: Bearer $TOKEN" \
  "$API/api/tools/cli/$TOOL_ID/home-cache?user_id=$USER_ID"
```

If routine, raise `home_quota_mb`.

### Binary disk filling

Retention is `age_threshold_days=30` plus keep-N-recent. To reclaim
aggressively: temporarily lower the threshold and rerun the GC snippet
in §1. Cross-reference unreferenced shas against live rows:

```sql
SELECT id, name, config->'binary'->>'sha256' AS sha
FROM tools WHERE type = 'cli';
```

### Legacy flat-config row surfaces

Pre-`dda8c9e` configs had flat keys (`binary_sha256`, `args_template`
top-level). `CliToolConfig._accept_legacy_shapes` silently lifts them
on read; the next PATCH writes them back in nested form. No migration
needed — if you spot one, do a no-op PATCH to normalise.

### Tenant deleted but files linger

Cascading GC emits `logger.info("cli-tools.gc")`. If you see
`failed to fully remove … (partial rmtree)`, clean up manually:

```bash
docker exec backend rm -rf /data/cli_binaries/<tenant_id>
docker exec backend rm -rf /data/cli_state/<tenant_id>
```

DB row is already gone; no other side-effect.

---

## 5. Admin endpoints

All require org_admin (own tenant) or platform_admin (any).

| Endpoint | Purpose |
|----------|---------|
| `GET /api/tools/cli` | List visible CLI tools |
| `POST /api/tools/cli` | Create metadata |
| `POST /api/tools/cli/{id}/binary` | Upload/replace binary (multipart) |
| `GET /api/tools/cli/{id}` | Detail (env masked) |
| `PATCH /api/tools/cli/{id}` | Update runtime / sandbox / flags (never binary) |
| `DELETE /api/tools/cli/{id}` | Delete + cascading GC of binary + state |
| `POST /api/tools/cli/{id}/test-run` | Synchronous exec, optional `mock_env` |
| `GET /api/tools/cli/{id}/home-usage?user_id=…` | Per-user HOME size |
| `DELETE /api/tools/cli/{id}/home-cache?user_id=…` | Wipe per-user HOME |

Task I (version / rollback, pending):

| Endpoint | Status |
|----------|--------|
| `GET /api/tools/cli/{id}/versions` | Task I |
| `POST /api/tools/cli/{id}/rollback` | Task I |

---

## 6. Deployment checklist

### Compose / k8s env

- `CLI_BINARIES_VOLUME_NAME` — default
  `${COMPOSE_PROJECT_NAME:-clawith}_cli_binaries`. Must be the **docker
  volume name** so `HostPathResolver` can rewrite paths for the daemon.
- `CLI_STATE_VOLUME_NAME` — default
  `${COMPOSE_PROJECT_NAME:-clawith}_cli_tool_state`.
- `CLI_STATE_ROOT` — container path (default `/data/cli_state`).
- Redis — required for rate limiting. Outage is tolerated (fail-open)
  but you lose the safety cap.

### Backend image

- `docker` CLI + docker socket already in compose.
- `bwrap` backend (optional): `apt-get install -y bubblewrap` in the
  backend Dockerfile. Default backend is `docker`.

### Sandbox image

- Tag: `clawith-cli-sandbox:stable`
- Build: `backend/cli_sandbox/Dockerfile` + `Makefile`
- Base: `debian:bookworm-slim`, apt-upgraded at build time.
- Rebuild monthly for Debian security fixes:
  `cd backend/cli_sandbox && make build push`, then restart backend.
- **Don’t rename the tag** without updating
  `BinaryRunner(default_image=…)` in
  `backend/app/services/sandbox/local/binary_runner.py` and callers.

### Prometheus

- Scrape `/api/metrics` with a platform_admin token.
- Suggested alerts:
  - `rate(clawith_cli_tool_executions_total{outcome="internal_error"}[5m]) > 0` → page
  - P95 duration > 80 % of tool timeout → warn

### Smoke test after rollout

1. `docker volume inspect ${PROJECT}_cli_binaries` returns a mountpoint.
2. `curl -H "Authorization: Bearer $TOKEN" $API/api/tools/cli` → 200.
3. Upload + test-run a known-good binary; confirm counter increments
   and an audit row lands.
