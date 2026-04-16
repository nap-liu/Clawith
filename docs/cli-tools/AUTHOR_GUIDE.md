# CLI Tools — Author Guide

Audience: engineers authoring a CLI tool uploaded to the platform and
driven by an agent. Tells you **what the sandbox allows, what it
refuses, how to configure the tool, and how to debug**.

Source of truth:

- Config — `backend/app/services/cli_tools/schema.py`
- Runtime — `backend/app/services/sandbox/local/binary_runner.py`
- Placeholders — `backend/app/services/cli_tools/placeholders.py`
- Executor — `backend/app/services/cli_tool_executor.py`
- Sandbox image — `backend/cli_sandbox/Dockerfile`

---

## 1. Sandbox contract

### Accepted binaries

Upload rejects anything whose first bytes don’t match one of:

| Magic | Format |
|-------|--------|
| `\x7fELF` | ELF (Linux) |
| `\xfe\xed\xfa\xce` / `…\xcf` / `\xce\xfa\xed\xfe` / `\xcf\xfa\xed\xfe` / `\xca\xfe\xba\xbe` | Mach-O variants |
| `#!` | Shebang script |

Hard size cap: **100 MB**. Storage is content-addressed
(`/data/cli_binaries/<tenant>/<tool>/<sha>.bin`, mode 0555).

### Isolation (every run)

- `uid=65534` (nobody), `gid=65534`
- `--cap-drop=ALL`, `--security-opt=no-new-privileges`
- Read-only rootfs; `/tmp` is tmpfs, **64 MB** cap (`mode=1777`)
- `--pids-limit` on, no ptrace
- **No network unless** `sandbox.network=true` + `egress_allowlist`
  (hostnames only, no IPs/CIDRs)
- Defaults: CPU `1.0`, memory `512m`

### Backend choice

| Backend | Cold start | Isolation | Use when |
|---------|-----------|-----------|----------|
| `docker` (default) | ~300 ms | Full container, seccomp | Any production tool |
| `bwrap` | ~30 ms | Namespaces, less net separation | Linux host, trusted first-party, latency-sensitive |

Set via `sandbox.backend`. Env contract is the same, swap is safe.

### What you cannot do

- Install packages at runtime (rootfs is read-only).
- Write outside `/tmp` or `/home/sandbox` (the latter only when
  `persistent_home=true`).
- Open raw sockets, `setuid`, fork-bomb, touch `/dev/*`.
- Resolve DNS when network is off.

---

## 2. Environment variables

| Name | Value | When |
|------|-------|------|
| `HOME` | `/tmp` or `/home/sandbox` | always (latter when `persistent_home=true`) |
| `TMPDIR` | `/tmp` | always |
| `XDG_CACHE_HOME` / `XDG_CONFIG_HOME` / `XDG_DATA_HOME` / `XDG_STATE_HOME` | `<HOME>/.cache` `/.config` `/.local/share` `/.local/state` | always |
| `PATH` | `/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin` | always |
| `CLAWITH_TRACE_ID` | UUID unique per execution | always — log it |
| `CLAWITH_EGRESS_ALLOWLIST` | comma-separated hosts | only when `sandbox.network=true` with non-empty list |
| keys in `runtime.env_inject` | your values (placeholders resolved) | always |

`CLAWITH_EGRESS_ALLOWLIST` is **advisory** — the backend enforces
egress via DNS + nftables. The var exists so tools that want to
pre-check hosts in their own code can.

**Log `CLAWITH_TRACE_ID`** in your tool’s stderr/logs as `trace_id`.
It ties the audit row, Prometheus, gateway, and your tool’s output
together for incident response.

---

## 3. Placeholders

`args_template` and `env_inject` entries that are a bare `$root.field`
token are replaced wholesale. Anything else (e.g. `--foo=$user.id`)
passes through literally.

Tokens: `$user.id`, `$user.phone`, `$user.email`, `$agent.id`,
`$tenant.id`, `$params.<name>`.

### List flattening in argv

`$params.X` may resolve to a list. In `args_template` the list is
flattened into multiple argv entries:

```jsonc
// Config
"args_template": ["svc", "$params.command", "--json"]
// Agent call
{ "command": ["report", "list"] }
// Actual argv
["svc", "report", "list", "--json"]
```

In `env_inject`, list values are `json.dumps`’d (env must be scalar):
`"SVC_ARGS": "$params.command"` → `SVC_ARGS='["report","list"]'`.

Never pass multi-segment commands as a single space-joined string;
they become one argv.

Unknown tokens (typos) pass through literally — test-run before
shipping.

---

## 4. Persistent HOME (`runtime.persistent_home=true`)

- Writable bind mount at `/home/sandbox`, surviving across runs.
- Scoped **(tenant, tool, user)** — see `state_storage.py` for why.
- Use for: login tokens, warmed caches (svc login, gh auth, kubectl).
- Do **not** use for: large artefacts — `home_quota_mb` (default
  500 MB) refuses new runs over the cap; admin must
  `DELETE /api/tools/cli/{id}/home-cache?user_id=…`.
- **Runs without `user_id` are refused** with `VALIDATION_ERROR` — per-
  user HOME can’t be computed, and we won’t fall back to a shared
  path. Security invariant, do not work around.

---

## 5. Limits & timeouts (`runtime` fields)

| Field | Default | Meaning |
|-------|---------|---------|
| `rate_limit_per_minute` | 60 | Sliding 60s window per (tool, agent, user). `0` = unlimited. Fails **open** on Redis outage — not a security boundary. |
| `timeout_seconds` | 30 (1–600) | Wall-clock cap; SIGKILL on expiry. |
| `home_quota_mb` | 500 (0–100000) | Checked pre-run. `0` = unchecked. |

---

## 6. Debugging

- **Test Run** — wizard Step 3 in the admin UI, or
  `POST /api/tools/cli/{id}/test-run`. Shows stdout/stderr/exit/
  duration + `error_class`.
- **Metrics** — `GET /api/metrics` (platform_admin):
  `clawith_cli_tool_executions_total{tool_name, tenant_id, outcome}`,
  `clawith_cli_tool_execution_duration_seconds{tool_name}`.
- **Audit** — enterprise audit page, filter
  `action=cli_tool.execute`. `details.trace_id`, `exit_code`,
  `duration_ms`, `stderr_tail` all there.

### Common traps

- **Runs locally, fails in sandbox with `no such file or directory`.**
  Missing dynamic lib; base is `debian:bookworm-slim` (stock glibc
  2.36). Statically link, or ship a custom `sandbox.image` (keep the
  uid/cap invariants).
- **`EROFS` writing `$HOME/.config/…`.** Forgot `persistent_home=true`.
- **Multi-segment CLI becomes one argv.** Declare the param as
  `"type": "array"` and use `["svc", "$params.cmd"]`.
- **Connectivity fails despite `network=true`.** Downstream service
  likely has its own allowlist; ask ops for the sandbox NAT IP.
- **Exit 137 (OOM).** Raise `sandbox.memory_limit`.

---

## 7. Admin CLI cheatsheet

`API=https://api.example.com`, `TOKEN=…` (org_admin or platform_admin).

### Create

```bash
curl -X POST "$API/api/tools/cli" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "name": "svc",
    "display_name": "Service CLI",
    "description": "Issue reports via svc",
    "parameters_schema": {
      "type": "object",
      "properties": { "command": {"type": "array", "items": {"type": "string"}} },
      "required": ["command"]
    },
    "runtime": {
      "args_template": ["svc", "$params.command"],
      "env_inject": { "SVC_USER": "$user.email" },
      "timeout_seconds": 60,
      "persistent_home": true,
      "rate_limit_per_minute": 30,
      "home_quota_mb": 200
    },
    "sandbox": {
      "cpu_limit": "0.5", "memory_limit": "256m",
      "network": true, "egress_allowlist": ["api.example.com"],
      "backend": "docker"
    }
  }'
```

### Upload binary

```bash
curl -X POST "$API/api/tools/cli/$TOOL_ID/binary" \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@./dist/svc"
```

### Update runtime/sandbox (never binary via PATCH)

```bash
curl -X PATCH "$API/api/tools/cli/$TOOL_ID" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{ "runtime": { "timeout_seconds": 90, "rate_limit_per_minute": 60 } }'
```

PATCH with a `binary` or `config` key returns **422** by design — keeps
a compromised admin token from repointing the sandbox at a different
sha.

### Test-run

```bash
curl -X POST "$API/api/tools/cli/$TOOL_ID/test-run" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{ "params": { "command": ["report", "list"] } }'
```

Response includes `exit_code`, `stdout`, `stderr`, `duration_ms`, and
(on short-circuit) `error_class` + `error_message`
(`validation_error`, `resource_limit`, `sandbox_failed`, `timeout`, …).

---

## 8. Pre-ship checklist

- [ ] Binary is static or all libs are in the sandbox image.
- [ ] `args_template` placeholders match `parameters_schema` keys.
- [ ] `env_inject` has no plaintext production secrets.
- [ ] `persistent_home` only if the tool caches real state.
- [ ] `sandbox.network` off unless required; if on, narrow `egress_allowlist`.
- [ ] `timeout_seconds` fits worst honest case with a buffer.
- [ ] Test Run exits 0 with a representative payload.
- [ ] Tool logs `CLAWITH_TRACE_ID` as `trace_id`.
