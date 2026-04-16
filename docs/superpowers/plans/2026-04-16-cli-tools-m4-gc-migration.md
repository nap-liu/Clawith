# CLI Tools — M4: GC + Legacy Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Daily GC cron deletes orphaned `.bin` files safely; the legacy CLI tool is migrated off its hard-coded path onto the new upload model.

**Architecture:** A pure `gc_cli_binaries` service function — given the DB + the `BinaryStorage` — computes the set of referenced SHAs and deletes orphans older than the age threshold. The scheduler layer (project's existing background-task mechanism) wires it to run daily. Legacy migration is an operator runbook + a one-shot Python script, not automated.

**Tech Stack:** SQLAlchemy async · `BinaryStorage` from M2 · project's existing scheduler.

**Spec:** `docs/superpowers/specs/2026-04-16-cli-tools-management-design.md` — §5.2 GC policy, §11.5 M0 migration, §12 M4.

**Depends on:** M1, M2 merged (plans `2026-04-16-cli-tools-m1-sandbox-base.md`, `2026-04-16-cli-tools-m2-backend-api.md`).

---

## File structure

| Path | Purpose |
|---|---|
| `backend/app/services/cli_tools/gc.py` | Pure `gc_cli_binaries(db, storage, ...)` function |
| `backend/app/services/cli_tools/gc_scheduler.py` | Scheduler hook — calls `gc_cli_binaries` daily |
| `backend/app/main.py` | Register the scheduler hook on startup |
| `backend/tests/test_cli_tools_gc.py` | Hard-reference rule tests |
| `backend/scripts/migrate_legacy_cli_tool.py` | Operator-run one-shot migration |
| `docs/superpowers/runbooks/m0-legacy-cli-migration.md` | Operator runbook |

Total: 6 new, 1 modified.

---

## Task 1: `gc_cli_binaries` — hard-reference rule + age gate

**Files:**
- Create: `backend/app/services/cli_tools/gc.py`
- Create: `backend/tests/test_cli_tools_gc.py`

- [ ] **Step 1: Write the failing test**

```python
"""GC rule tests — hard-reference check + age gate."""

from __future__ import annotations

import io
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.cli_tools.gc import gc_cli_binaries
from app.services.cli_tools.storage import BinaryStorage


_SHEBANG = b"#!/bin/sh\necho hi\n"


def _age_file(path: Path, days: int) -> None:
    past = time.time() - days * 86400
    os.utime(path, (past, past))


@pytest.mark.asyncio
async def test_gc_deletes_orphan_older_than_threshold(tmp_path):
    storage = BinaryStorage(root=tmp_path)
    sha_a, _ = await storage.write(tenant_key="t1", tool_id="tool1", stream=io.BytesIO(_SHEBANG), max_bytes=1_000_000)
    sha_b, _ = await storage.write(tenant_key="t1", tool_id="tool1", stream=io.BytesIO(_SHEBANG + b"\n"), max_bytes=1_000_000)

    orphan_path = storage.resolve("t1", "tool1", sha_b)
    _age_file(orphan_path, days=31)

    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(scalars=lambda: MagicMock(all=lambda: [
        MagicMock(config={"binary_sha256": sha_a}),
    ])))

    deleted = await gc_cli_binaries(db=db, storage=storage, age_threshold_days=30)
    assert deleted == 1
    assert storage.resolve("t1", "tool1", sha_a).exists()
    assert not orphan_path.exists()


@pytest.mark.asyncio
async def test_gc_keeps_referenced_even_when_old(tmp_path):
    storage = BinaryStorage(root=tmp_path)
    sha_a, _ = await storage.write(tenant_key="t1", tool_id="tool1", stream=io.BytesIO(_SHEBANG), max_bytes=1_000_000)
    _age_file(storage.resolve("t1", "tool1", sha_a), days=365)

    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(scalars=lambda: MagicMock(all=lambda: [
        MagicMock(config={"binary_sha256": sha_a}),
    ])))

    deleted = await gc_cli_binaries(db=db, storage=storage, age_threshold_days=30)
    assert deleted == 0
    assert storage.resolve("t1", "tool1", sha_a).exists()


@pytest.mark.asyncio
async def test_gc_keeps_young_orphan(tmp_path):
    storage = BinaryStorage(root=tmp_path)
    sha, _ = await storage.write(tenant_key="t1", tool_id="tool1", stream=io.BytesIO(_SHEBANG), max_bytes=1_000_000)
    # File is fresh (not aged). Orphan (no references).

    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(scalars=lambda: MagicMock(all=lambda: [])))

    deleted = await gc_cli_binaries(db=db, storage=storage, age_threshold_days=30)
    assert deleted == 0
    assert storage.resolve("t1", "tool1", sha).exists()
```

- [ ] **Step 2: Run test — fails on missing module**

```
cd backend
python -m pytest tests/test_cli_tools_gc.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Write the GC module**

```python
"""Daily GC for orphaned CLI-tool binaries.

Rule (see spec §5.2):
    A `.bin` file is deleted iff
      (a) no Tool row in the DB references it via config.binary_sha256, AND
      (b) mtime is older than `age_threshold_days`.

The AND is strict: a still-referenced binary is never deleted regardless of age.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tool import Tool
from app.services.cli_tools.storage import BinaryStorage

logger = logging.getLogger(__name__)


async def gc_cli_binaries(
    *,
    db: AsyncSession,
    storage: BinaryStorage,
    age_threshold_days: int = 30,
) -> int:
    """Delete orphaned + aged `.bin` files. Returns number of files deleted."""
    result = await db.execute(select(Tool).where(Tool.type == "cli"))
    tools = result.scalars().all()
    referenced: set[str] = set()
    for tool in tools:
        sha = (tool.config or {}).get("binary_sha256")
        if isinstance(sha, str) and len(sha) == 64:
            referenced.add(sha)

    cutoff = time.time() - age_threshold_days * 86400
    to_delete: list[Path] = []
    for orphan_path in storage.iter_orphans(referenced_shas=referenced):
        try:
            if orphan_path.stat().st_mtime <= cutoff:
                to_delete.append(orphan_path)
        except FileNotFoundError:
            continue

    deleted = storage.delete_orphans(to_delete)
    if deleted:
        logger.info("cli_tools.gc deleted %d orphaned binaries", deleted)
    return deleted
```

- [ ] **Step 4: Run test**

```
cd backend
python -m pytest tests/test_cli_tools_gc.py -v
```

Expected: 3 PASS.

- [ ] **Step 5: Commit**

```
git add backend/app/services/cli_tools/gc.py backend/tests/test_cli_tools_gc.py
git commit -m "feat(cli-tools): GC for orphaned + aged binaries"
```

---

## Task 2: Scheduler hook

**Files:**
- Create: `backend/app/services/cli_tools/gc_scheduler.py`
- Modify: `backend/app/main.py`

- [ ] **Step 1: Find the project's existing scheduler entry point**

```
grep -rn "APScheduler\|scheduler.add_job\|@app.on_event" backend/app | head -10
```

Identify how other recurring jobs register (typically on `@app.on_event("startup")` or via `app/services/scheduler.py`).

- [ ] **Step 2: Write the hook**

```python
"""Register the daily CLI-tools GC job against the project's scheduler."""

from __future__ import annotations

import logging
from pathlib import Path

from app.database import async_session
from app.services.cli_tools.gc import gc_cli_binaries
from app.services.cli_tools.storage import BinaryStorage

logger = logging.getLogger(__name__)


async def run_daily_gc() -> None:
    storage = BinaryStorage(root=Path("/data/cli_binaries"))
    async with async_session() as db:
        await gc_cli_binaries(db=db, storage=storage, age_threshold_days=30)


def register(scheduler) -> None:
    """Call this from app startup with the project's scheduler object."""
    scheduler.add_job(
        run_daily_gc,
        trigger="cron",
        hour=3,
        minute=17,
        id="cli_tools_gc",
        replace_existing=True,
    )
    logger.info("registered cli_tools_gc daily at 03:17")
```

- [ ] **Step 3: Wire into main.py**

Find where the project initialises the scheduler (often `scheduler = AsyncIOScheduler(...)` or `SCHEDULER` import). After that instantiation, add:

```python
from app.services.cli_tools.gc_scheduler import register as register_cli_gc
register_cli_gc(scheduler)
```

If the project uses a different mechanism (e.g. `@app.on_event("startup")` with `asyncio.create_task(...)`), adapt the call — keep the *behaviour* (run once a day) equivalent.

- [ ] **Step 4: Smoke test — app boots, GC job is registered**

Boot the backend locally, then inside the container:

```
python -c "from app.main import app; print('ok')"
```

Grep logs for `registered cli_tools_gc`. Expected: present.

- [ ] **Step 5: Commit**

```
git add backend/app/services/cli_tools/gc_scheduler.py backend/app/main.py
git commit -m "feat(cli-tools): daily GC scheduled via project scheduler"
```

---

## Task 3: Legacy-tool migration script

**Files:**
- Create: `backend/scripts/migrate_legacy_cli_tool.py`

- [ ] **Step 1: Write the script**

```python
#!/usr/bin/env python3
"""One-shot migration: move a legacy path-based CLI tool onto the upload model.

Usage (inside the backend container):
    python -m scripts.migrate_legacy_cli_tool \\
        --tool-id <uuid> \\
        --source-path /path/inside/container/to/legacy-binary

The script copies the legacy binary into /data/cli_binaries/_global/<tool_id>/
under its SHA-256 name, then updates the Tool row's config to point at it.

Idempotent: running twice with the same source-path is a no-op after the
first run (the target file is content-addressed).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from app.database import async_session
from app.models.tool import Tool
from app.services.cli_tools.schema import CliToolConfig
from app.services.cli_tools.storage import BinaryStorage


async def _run(tool_id: str, source_path: str) -> None:
    src = Path(source_path)
    if not src.is_file():
        raise SystemExit(f"source binary does not exist: {src}")

    with src.open("rb") as f:
        data = f.read()
    sha = hashlib.sha256(data).hexdigest()

    storage = BinaryStorage(root=Path("/data/cli_binaries"))

    async with async_session() as db:
        tool = await db.get(Tool, tool_id)
        if tool is None:
            raise SystemExit(f"tool {tool_id} not found")
        if tool.type != "cli":
            raise SystemExit(f"tool {tool_id} is type={tool.type}, expected cli")

        tenant_key = str(tool.tenant_id) if tool.tenant_id is not None else "_global"
        target = storage.resolve(tenant_key, str(tool.id), sha)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            with target.open("wb") as f:
                f.write(data)
            target.chmod(0o555)

        # Build the new config shape, dropping any legacy fields.
        old = tool.config or {}
        new = CliToolConfig(
            binary_sha256=sha,
            binary_size=len(data),
            binary_original_name=src.name,
            binary_uploaded_at=datetime.now(timezone.utc),
            args_template=old.get("args_template", []),
            env_inject=old.get("env_inject", {}),
            timeout_seconds=old.get("timeout_seconds", 30),
        )
        tool.config = new.model_dump(mode="json")
        await db.commit()

    print(f"migrated tool={tool_id} sha={sha} bytes={len(data)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tool-id", required=True)
    parser.add_argument("--source-path", required=True)
    args = parser.parse_args()
    asyncio.run(_run(args.tool_id, args.source_path))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify it imports without side effects**

```
cd backend
python -m scripts.migrate_legacy_cli_tool --help
```

Expected: usage output, no tracebacks.

- [ ] **Step 3: Commit**

```
git add backend/scripts/migrate_legacy_cli_tool.py
git commit -m "feat(cli-tools): one-shot legacy-tool migration script"
```

---

## Task 4: M0 legacy migration runbook

**Files:**
- Create: `docs/superpowers/runbooks/m0-legacy-cli-migration.md`

- [ ] **Step 1: Write the runbook**

```markdown
# M0 — Legacy CLI tool migration

**When:** After M1-M4 are deployed to production. Before tenant admins
can see the CLI tab on the legacy tool.

**What:** Moves the production legacy CLI tool off its hard-coded binary
path onto the new upload model. Idempotent.

## Prerequisites

- `platform_admin` account + JWT
- ssh access to the production host
- Knowledge of: the legacy tool's Tool row UUID, and the binary's path
  inside the currently-running backend container

## Procedure

### 1. Capture current state

```
ssh <prod-host>
docker exec <backend-container> ls -la /path/to/legacy-binary
docker exec <postgres-container> psql -U clawith -d clawith \\
  -c "SELECT id, name, type, config FROM tools WHERE type = 'cli';"
```

Record the Tool UUID and the binary path. Confirm the binary is executable.

### 2. Copy the binary somewhere the migration script can read

The script runs inside the backend container and reads from a path
accessible to that container. If the binary is already in the image at
`/usr/local/bin/<name>`, no copy is needed.

### 3. Run the migration script

```
docker exec <backend-container> \\
  python -m scripts.migrate_legacy_cli_tool \\
    --tool-id <UUID> \\
    --source-path /usr/local/bin/<name>
```

Expected output: `migrated tool=<UUID> sha=<64-hex> bytes=<N>`.

### 4. Verify

```
docker exec <postgres-container> psql -U clawith -d clawith \\
  -c "SELECT id, config FROM tools WHERE id = '<UUID>';"
```

`config.binary_sha256` is set, `config.binary_uploaded_at` is set.

```
docker exec <backend-container> ls /data/cli_binaries/_global/<UUID>/
```

A single `<sha>.bin` file, mode 0555.

### 5. Smoke test via the new execution path

Have an agent that uses this tool run one invocation in production.
Watch logs for `CliToolError` classes.

If `BINARY_FAILED` or `SANDBOX_FAILED` appears: roll back by reverting
the Tool.config update (step 6).

### 6. Rollback

The migration only wrote a new file + updated the Tool.config JSON.
Revert by pasting the old config back:

```
docker exec <postgres-container> psql -U clawith -d clawith \\
  -c "UPDATE tools SET config = '<OLD JSON>'::json WHERE id = '<UUID>';"
```

The newly-uploaded binary at `/data/cli_binaries/_global/<UUID>/<sha>.bin`
becomes an orphan and is garbage-collected 30 days later.

## Notes

- The script is idempotent: running it twice with the same source writes
  the file only once (content-addressed) and updates the DB twice with
  identical values.
- This procedure is `platform_admin`-only. `org_admin` users cannot see
  the legacy tool's Tool row until its tenant_id is set; for the global
  legacy tool, tenant_id is NULL and only `platform_admin` manages it.
```

- [ ] **Step 2: Commit**

```
git add docs/superpowers/runbooks/m0-legacy-cli-migration.md
git commit -m "docs(cli-tools): M0 legacy migration runbook"
```

---

## M4 Exit Criteria

- [ ] `gc_cli_binaries` unit tests pass (3 scenarios)
- [ ] Scheduler registers the job on app startup — grep logs for `registered cli_tools_gc`
- [ ] `migrate_legacy_cli_tool --help` runs cleanly inside the backend container
- [ ] Runbook is committed and reviewed by the operator who will execute M0

## End of feature

M1-M4 complete. The CLI Tools management feature is fully shipped:
uploaded binaries are content-addressed; they run inside isolated docker
containers; `org_admin` manages per-tenant tools from the UI; platform
upgrades are smooth via the `:stable` alias + per-tool image pinning;
orphaned blobs are cleaned up automatically; the legacy tool has been
migrated off its hard-coded path.
