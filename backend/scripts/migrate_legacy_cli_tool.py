#!/usr/bin/env python3
"""One-shot migration: move a legacy path-based CLI tool onto the upload model.

Usage (inside the backend container):
    python -m scripts.migrate_legacy_cli_tool \\
        --tool-id <uuid> \\
        --source-path /path/inside/container/to/legacy-binary

The script copies the legacy binary into /data/cli_binaries/<scope>/<tool_id>/
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

from app.database import async_session
from app.models.tool import Tool
from app.services.cli_tools.schema import (
    BinaryMetadata,
    CliToolConfig,
    RuntimeConfig,
    SandboxConfig,
)
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

        # Round-trip the old shape through CliToolConfig so M1/M2 flat
        # keys get lifted into the right subtree, then overwrite binary.
        normalised = CliToolConfig.model_validate(tool.config or {})
        new = CliToolConfig(
            binary=BinaryMetadata(
                sha256=sha,
                size=len(data),
                original_name=src.name,
                uploaded_at=datetime.now(timezone.utc),
            ),
            runtime=normalised.runtime or RuntimeConfig(),
            sandbox=normalised.sandbox or SandboxConfig(),
        )
        tool.config = new.model_dump(mode="json")
        await db.commit()

    print(f"migrated tool={tool_id} sha={sha} bytes={len(data)}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate a legacy path-based CLI tool onto the upload model.",
    )
    parser.add_argument("--tool-id", required=True, help="UUID of the Tool row")
    parser.add_argument("--source-path", required=True, help="Path to the legacy binary inside this container")
    args = parser.parse_args()
    asyncio.run(_run(args.tool_id, args.source_path))


if __name__ == "__main__":
    main()
