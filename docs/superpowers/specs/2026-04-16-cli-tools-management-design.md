# CLI Tools Management — Design v3

**Date**: 2026-04-16
**Status**: Draft — awaiting author review
**Scope**: Single feature branch (`feat/cli-tools-management`), 4 milestones.

---

## 1. Summary

Extend the existing CLI-tool execution plumbing with a full management surface: binary upload + replacement, CRUD API, admin UI, tenant-isolated sandboxed execution. Primary manager role is tenant `org_admin`; `platform_admin` retains a global-tool escape hatch.

## 2. Motivation

The codebase already implements ~90% of the execution path:

- `Tool.type = "cli"` model field exists
- `cli_tool_executor.py` provides async-subprocess runner with env injection, injection-character blacklist, timeout, and working-directory isolation
- `agent_tools._try_execute_cli_tool()` wires CLI tools into the agent tool-calling loop

The management surface is 0% implemented:

- No CRUD API (create / update / delete / list)
- No admin UI
- No binary upload — binaries are expected to pre-exist in the backend container filesystem at a hard-coded path
- No execute-time tenant-boundary check (a risk already identified)
- No encryption of env_inject values
- No sandbox isolation — binaries run in-process inside the backend container

## 3. Non-goals (YAGNI)

| Not doing | Reason |
|---|---|
| Cross-tenant sharing / marketplace | Requires curation + discovery workflows; P3 |
| Version UI / rollback workflow | SHA-content-addressed storage gives implicit versioning; rollback is a single DB field update |
| Stronger sandboxing (gVisor, kata, firecracker) | Docker + capability drops + no-new-privileges + read-only rootfs is sufficient for trusted-admin threat model |
| Virus scanning of uploaded binaries | Threat model assumes `org_admin` is trusted |
| Runtime-specific sandbox images (Python / Node / Go) | Start with plain debian-slim; add variants only when demand appears |
| Graphical JSON-Schema builder | Raw JSON editor is adequate |
| Per-user (not per-tenant) CLI tool ownership | Existing permission model is role-based; per-user adds surface without proportional value |
| Multi-architecture binary support | v1 assumes all binaries are linux/amd64. arm64 / multi-arch is tracked as follow-up |
| Automatic binary static analysis (ABI / syscall scan) on upload | Too noisy and platform-specific for v1; operator judgement suffices |
| Automatic disable-on-failure | A tool failing after a platform upgrade stays enabled and surfaces `SANDBOX_FAILED` / `BINARY_FAILED` per §9; operators intervene manually |

## 4. Use cases

- **UC-1** `org_admin` uploads a binary and configures a CLI tool for their tenant; optionally enables it on specific agents.
- **UC-2** At runtime, an agent calls a CLI tool; the backend validates permissions and parameters, then executes it inside a one-shot sandbox container and returns stdout to the agent.
- **UC-3** `org_admin` replaces the binary of an existing tool (bug fix, feature). Existing agent references continue to work without edits.
- **UC-4** `org_admin` test-runs a newly configured tool from the admin UI before enabling it for agents.
- **UC-5** `platform_admin` creates a tool with no tenant scope ("global" tool). All tenants see and may enable it, but cannot edit it.
- **UC-6** `org_admin` disables a misbehaving tool without deleting it. Execute calls immediately fail; agents relying on it surface an error; the tool can be re-enabled later.
- **UC-7** The platform is upgraded (backend code, sandbox base image, or host OS). Previously-uploaded binaries continue to work without re-upload. The platform explicitly states what is guaranteed to stay compatible and what the tenant operator must verify.

## 5. Architecture

### 5.1 Data model

Reuse the existing `Tool` table. Extend the JSON `cli_config` column with a normalized shape (no SQL migration beyond ensuring the column is nullable):

```json
{
  "binary_sha256": "<hex 64 chars>",
  "binary_size": 12345678,
  "binary_original_name": "my-tool-linux-amd64",
  "binary_uploaded_at": "2026-04-16T10:00:00Z",
  "args_template": ["--user", "{user.id}", "--action", "{params.action}"],
  "env_inject": {
    "API_KEY": "<encrypted:...>",
    "USER_PHONE": "{user.phone}"
  },
  "timeout_seconds": 30,
  "sandbox": {
    "cpu_limit": "1.0",
    "memory_limit": "512m",
    "network": false,
    "readonly_fs": true,
    "image": null
  }
}
```

Reused existing fields:

- `Tool.parameters_schema` (JSON Schema) — validated against caller-provided params before execute
- `Tool.is_active` — drives the enable/disable toggle (UC-6)
- `Tool.tenant_id` — nullable; `NULL` means global; execute-time check enforces `Tool.tenant_id IS NULL OR Tool.tenant_id == agent.tenant_id`

### 5.2 Storage layout

A dedicated docker volume is mounted at `/data/cli_binaries` inside the backend container:

```
/data/cli_binaries/
├── <tenant_uuid>/
│   └── <tool_uuid>/
│       ├── <sha256>.bin          # current or historical
│       └── <older_sha256>.bin
├── _global/
│   └── <tool_uuid>/
│       └── <sha256>.bin
```

- Binaries are content-addressed by SHA-256
- File permissions fixed at `0555` (read + execute, not writable)
- Replacing a binary writes a new file and atomically updates `cli_config.binary_sha256`; the old file is retained until GC
- **GC policy** (the one non-obvious rule): a daily cron removes `.bin` files where **(a) no `Tool.cli_config.binary_sha256` anywhere in the DB references the file, AND (b) mtime is older than 30 days**. The AND is strict: any still-referenced binary is never deleted regardless of age.

### 5.3 Sandbox execution

Reuse `SandboxType.DOCKER` + `DockerBackend` in `app/services/sandbox/`. Each execute call starts an ephemeral container:

```
docker run --rm \
  --network <none|bridge>                 \  # per-tool configurable, default none
  --read-only                              \
  --tmpfs /tmp:rw,size=64m,mode=1777       \
  --memory <limit> --cpus <limit>          \
  --pids-limit 100                         \
  --user 65534:65534                       \  # nobody
  --security-opt no-new-privileges         \
  --cap-drop ALL                           \
  -v /data/cli_binaries/<t>/<tool>/<sha>.bin:/binary:ro \
  -e <KEY>=<decrypted_value> ...           \
  <sandbox_image>:<tag>                    \
  /binary <rendered_args>
```

**Sandbox image**: a minimal image built from `debian:bookworm-slim` with no additional packages, published to the project's internal registry.

**Image tag strategy** (relevant to §11 platform-upgrade smoothness):

- Images are tagged by base + date: `clawith-cli-sandbox:debian-bookworm-slim-YYYYMMDD`
- A rolling alias `clawith-cli-sandbox:stable` points at the current platform-default tag; this is what new tools get unless overridden
- Per-tool pinning: `cli_config.sandbox.image` may specify an exact tag (e.g. `debian-bookworm-slim-20260416`). If set, the tool stays on that image across platform upgrades. If unset, the tool follows `:stable`
- Platform upgrade of the default image = moving the `:stable` alias. Tools without an explicit pin migrate automatically; tools with a pin stay untouched
- Old image tags are retained in the registry indefinitely to allow rollback and pinned-tool continuity

**Container lifetime**: single-execution. `--rm` removes container after exit or timeout.

**Cold-start latency**: ~1–2 seconds per invocation. CLI tools are not on the hot request path, so this is acceptable for v1.

**SLO**: P95 end-to-end latency < 3 seconds. If exceeded in production, the upgrade path is "per-agent long-lived sandbox container with `docker exec` invocations", reusing `AgentManager` patterns. This is tracked as a follow-up, not part of v1.

### 5.4 API endpoints

Seven endpoints, consistent with existing `IdentityProvider` CRUD conventions:

| Method | Path | Purpose | Required role |
|---|---|---|---|
| GET | `/api/tools?type=cli` | List tools visible to caller (own tenant + global) | member |
| POST | `/api/tools/cli` | Create tool metadata (no binary yet) | org_admin |
| POST | `/api/tools/{id}/binary` | Upload binary (multipart) | org_admin of owning tenant |
| GET | `/api/tools/{id}` | Detail (env values masked) | member |
| PATCH | `/api/tools/{id}/cli` | Update metadata (name, env, args, schema, sandbox, is_active) | org_admin of owning tenant |
| DELETE | `/api/tools/{id}` | Delete tool (binary flagged for GC) | org_admin of owning tenant |
| POST | `/api/tools/{id}/test-run` | Test execute with caller-provided params / optional mock env | org_admin of owning tenant |

`platform_admin` has all powers above across tenants, and additionally may set `tenant_id = NULL` on create to produce a global tool.

**Upload flow**:

1. `POST /api/tools/cli` with metadata → returns `tool_id`
2. `POST /api/tools/{tool_id}/binary` with multipart body:
   - stream-compute SHA-256
   - validate magic number (ELF 32/64, Mach-O 32/64, shebang `#!`)
   - validate size < 100 MB
   - write `/data/cli_binaries/<tenant>/<tool_id>/<sha>.bin`, chmod `0555`
   - update `Tool.cli_config.binary_sha256`
   - write audit log entry

## 6. UI

Embed in the existing `EnterpriseSettings > tools` tab, which already handles `mcp` and `builtin` types. Add `cli` as a filter option and an entry point.

### 6.1 List view

Filter bar: `[MCP | Built-in | CLI | All]`.

Row columns: Name · Type · Scope (global / tenant name) · Status (active / disabled) · Updated · Actions.

Global tools, when viewed in a tenant's context, show read-only icons (no edit / delete / disable).

### 6.2 Create / Edit: 3-step wizard

A wizard rather than a single large form — the earlier design review flagged that the combined form was hard to complete.

**Step 1 — Basic info**

- Name (required, unique within tenant)
- Description
- Scope selector: hidden for `org_admin` (fixed to their tenant); visible for `platform_admin` with default "tenant" to avoid accidental global-tool creation

**Step 2 — Binary**

- Upload component (drag-drop or click), shows upload progress
- After upload: displays SHA-256, original filename, size
- When editing: a "Replace binary" button restarts this step; previous binary flagged for GC

**Step 3 — Configuration & test**

- args template (JSON array editor)
- env vars (key–value grid; values masked after save; "use mock value for test-run" toggle per row)
- timeout (seconds)
- resource limits: CPU (default 1.0), memory (default 512m)
- network (checkbox, **default off**, with prominent inline help: "Enable only if the tool needs to call external APIs or download resources")
- parameters schema (raw JSON editor, validated on save with `jsonschema.Draft7Validator.check_schema`)
- Test Run panel: params JSON input → "Run" button → shows stdout / stderr / exit_code / duration. Env values default to the configured (encrypted) values with a per-row override for mock values.

## 7. Security

### 7.1 Defense in depth

Three layers:

**Config-time**

- env values encrypted via existing `encrypt_data` / `decrypt_data` (same mechanism used by `LLMModel.api_key_encrypted`)
- args-template and env-value placeholders restricted to a whitelist: `{user.id}`, `{user.phone}`, `{user.email}`, `{agent.id}`, `{tenant.id}`, `{params.<name>}`
- binary magic-number validation on upload (reject anything other than ELF / Mach-O / shebang)

**Execute-time**

- tenant check: `Tool.tenant_id IS NULL OR Tool.tenant_id == agent.tenant_id` (fixes an existing gap)
- params validated against `Tool.parameters_schema` with `jsonschema`
- existing injection-character blacklist preserved in argument rendering
- `is_active == False` → execution rejected

**Runtime isolation** (sandbox container flags, §5.3)

- `--cap-drop ALL`
- `--security-opt no-new-privileges`
- `--user 65534:65534` (nobody)
- `--read-only` rootfs, tmpfs-only for `/tmp`
- `--network none` by default, per-tool opt-in
- `--pids-limit`, `--memory`, `--cpus` quotas
- `--rm` ensures no residue

### 7.2 Audit

Write to `audit_logs` on: tool create, metadata update, binary upload, binary replace, delete, disable, enable. Metadata changes log a concise diff (old vs new values). Per-execute events are **not** audited — business logs cover them, and the cardinality is too high.

### 7.3 Master encryption key

`env_inject` values use the project's existing `encrypt_data` function, driven by the existing application secret (the same key that protects `LLMModel.api_key_encrypted`). Key rotation is out of scope for this spec.

## 8. Observability

Per execute call, structured log at INFO level with: `tool_id`, `agent_id`, `user_id`, `tenant_id`, `binary_sha256`, `duration_ms`, `exit_code`, `error_class` (if any — see §9).

stdout / stderr are logged at DEBUG only (may contain sensitive output). Users see stdout via the normal agent response channel.

No new Prometheus metrics endpoint introduced in v1. If the project later adopts Prometheus globally, `cli_tool_execute_duration_seconds` (histogram) and `cli_tool_execute_total{status}` (counter) are the natural adds.

## 9. Error model

Execute errors map to explicit classes logged for ops and surfaced to the agent:

| Class | Cause | Surface |
|---|---|---|
| `VALIDATION_ERROR` | params fail `parameters_schema` or contain disallowed placeholder | "Invalid arguments" |
| `PERMISSION_DENIED` | tenant mismatch, role insufficient, or tool is disabled | "Not permitted" |
| `NOT_FOUND` | tool id does not exist | "Tool not found" |
| `TIMEOUT` | execution exceeded `timeout_seconds` | "Tool execution timed out" |
| `RESOURCE_LIMIT` | container OOM-killed or pids-limit hit | "Tool exceeded resource limits" |
| `BINARY_FAILED` | binary exited non-zero | "Tool returned error: {stderr tail 200 chars}" |
| `SANDBOX_FAILED` | container failed to start (image missing, docker daemon error) | "Internal error" (full detail in logs only) |

## 10. Concurrency

- The same tool may be invoked concurrently by multiple agents; each execute creates a separate container and mounts the same read-only binary file — no shared mutable state.
- **Per-tenant concurrency cap**: **not enforced in v1**. The host's finite resources act as an implicit cap. Tracked in §13 risks for a follow-up feature.
- **Binary replacement is safe under concurrency**: the `.bin` file is not deleted when `cli_config.binary_sha256` is updated (GC runs only on unreferenced + aged files). In-flight executes using the previous SHA continue to run unaffected.

## 11. Compatibility & evolution

Explicitly addresses **UC-7**: previously-uploaded binaries must keep working across platform upgrades.

### 11.1 `cli_config` schema evolution rules

- **Fields are only added, never renamed or removed.** Reading code tolerates missing keys by falling back to documented defaults.
- New fields always have a sensible default that preserves pre-upgrade behaviour (example: adding a future `cli_config.sandbox.cpu_pinning` must default to "no pinning", matching today's behaviour).
- No `schema_version` field — the add-only discipline and explicit defaults make it unnecessary, and avoids a second source of truth.
- Breaking changes (renaming / removing / semantic shifts) are forbidden in the JSON shape; if ever unavoidable, a named migration lives in alembic and is reviewed as a separate proposal.

### 11.2 Sandbox image upgrades

- New image tags follow the date scheme in §5.3. The `:stable` alias moves when the platform chooses to upgrade the default.
- **Tools without `cli_config.sandbox.image`** (the common case) pick up the new default on the next execute after the alias moves — no user action required.
- **Tools with a pinned `cli_config.sandbox.image`** keep running on the pinned tag until the `org_admin` (or `platform_admin`) explicitly unpins or repins.
- The platform keeps **all previously-published tags** in the registry indefinitely. This makes rollback (moving `:stable` back) and pinned-tool continuity both trivially available.
- Upgrade-day runbook lives outside this spec but follows a common shape: publish new tag → announce change window → move `:stable` → observe error-class rates (§9) → roll back by reverting the alias if `SANDBOX_FAILED` or `BINARY_FAILED` rates spike.

### 11.3 Binary architecture

- v1 guarantees **linux/amd64** only. The sandbox image is built and deployed as linux/amd64; uploaded binaries are expected to match.
- The upload API does not enforce architecture (it would require parsing ELF `e_machine`); mismatched binaries surface a `BINARY_FAILED` or `SANDBOX_FAILED` at first execute. This is acceptable for v1 since operators control the deployment arch.
- Multi-arch support (arm64 hosts, cross-platform images) is tracked as follow-up, not v1.

### 11.4 UI signalling for disruptive actions

The admin UI makes upgrade-adjacent operations explicit:

- **Replace binary** shows a confirmation: "This replaces the binary for all agents using `<tool name>`. The new version takes effect on the next execute."
- **Pin / unpin sandbox image** surfaces the current `:stable` tag and a list of other available tags; picking one pins, "Follow platform default" unpins.
- **Disable** shows how many agents currently have the tool enabled.

### 11.5 M0 — Legacy tool migration (one-time)

Production currently runs one legacy CLI tool whose binary is baked into the backend container image and referenced by a hard-coded path. To converge on the upload model **without dual-mode code paths**:

1. `platform_admin` extracts the legacy binary from the running backend container.
2. Creates a `platform_admin`-owned global tool via the new API.
3. Uploads the extracted binary via `POST /api/tools/{id}/binary`.
4. Updates the legacy `Tool` row's `cli_config` to the new shape (pointing at the newly uploaded SHA).
5. Removes any residual hard-coded path field.

Result: production continues to serve the legacy tool, now via the new execution path. No `mode: "path"` / `mode: "uploaded"` branching in production code.

## 12. Milestones

Strict sequence — no cross-milestone parallelism.

### M1 — Storage + sandbox base

- Add `cli_binaries` docker volume to `docker-compose.yml`
- Build and publish the sandbox image with a date-scheme tag `clawith-cli-sandbox:debian-bookworm-slim-YYYYMMDD`, and point the `:stable` alias at it
- Extend `DockerBackend` (if needed) to accept the "run a mounted binary" invocation shape
- Backend-side resolution: `cli_config.sandbox.image or "clawith-cli-sandbox:stable"`
- Unit tests for the backend extension

### M2 — Backend API + execution hardening

- 7 API endpoints (list / create / upload binary / detail / update / delete / test-run)
- Multipart streaming upload with SHA-256 + magic-number + size validation
- Env encryption on save, masked on read, decrypted only at execute
- Execute-time tenant check + `jsonschema` params validation
- Error-class mapping per §9
- Audit-log wiring
- Integration tests: CRUD permission matrix + execution happy-path + each error class

### M3 — Frontend `tools` tab extension

- `CLI` filter option in the tab's filter bar
- List view with Scope / Status / Updated columns
- 3-step wizard (Basic info / Binary / Configuration & Test)
- Upload component with progress and SHA display
- Env value masking with per-row mock override for Test Run
- Enable / Disable toggle
- **Replace-binary confirmation** surfacing the impact wording from §11.4
- **Sandbox image pin/unpin** control showing current `:stable` tag and available dated tags
- Disable action shows count of agents currently referencing the tool

### M4 — Test Run UI + GC + M0 migration

- Test Run panel end-to-end (UI consumes `/test-run`)
- Daily GC cron implementing §5.2 policy (hard-reference check + orphan + aged)
- M0 migration script / runbook for the legacy tool

## 13. Known risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Docker cold start exceeds P95 SLO | Medium | Monitor in staging and production; well-defined upgrade path (per-agent exec) |
| User-uploaded binary exploits a runc / kernel 0-day | Low | `org_admin` is trusted; base image kept current; project subscribed to CVE feeds |
| env var leakage via stdout during Test Run | Medium | Mock-value override in Test Run UI |
| Single tenant consumes disproportionate host resources | Low | Per-tenant concurrency cap tracked as follow-up |
| Orphan `.bin` files grow unbounded | Low | Daily GC with hard-reference check |
| Legitimate binary exceeds 100 MB cap | Low | Size cap is a config; raise on justified request |
| Master encryption key rotation | Medium | Out of scope for this spec; tracked separately |
| Sandbox image upgrade breaks binaries compiled against older glibc | Medium | Per-tool `sandbox.image` pin (§11.2); old tags retained for rollback; upgrade-day observability on error-class rates |
| Uploaded binary has wrong architecture (e.g. arm64 on amd64 host) | Low | Surfaces as `BINARY_FAILED` at first execute (§11.3); multi-arch is follow-up |
| `cli_config` schema change breaks old records | Low | Add-only schema rule (§11.1); no renames / removes permitted |

## 14. Testing strategy

| Layer | Coverage |
|---|---|
| **Unit** | `cli_tool_executor` (happy path + each error class); magic-number validator; `jsonschema` validator; encryption round-trip; placeholder-whitelist checker |
| **API** | FastAPI TestClient: CRUD permission matrix (platform_admin / org_admin / member / cross-tenant denial); multipart upload size + magic-number rejection; `test-run` with mock env |
| **Integration** | Actual docker daemon: end-to-end with a small real binary (e.g. shebang script wrapping `/bin/echo`); resource-limit enforcement; timeout behavior |
| **Frontend** | Manual pass-through each wizard step and error state on local docker build (project baseline has no frontend e2e harness) |

---

## Approval gate

On approval, the next step is the `writing-plans` skill to produce per-milestone implementation plans, followed by iterative execution on the `feat/cli-tools-management` branch.
