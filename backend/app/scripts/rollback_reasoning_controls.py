"""Prepare builtin tool schemas for rollback before reasoning controls existed."""

from __future__ import annotations

import argparse
import asyncio
import json
from copy import deepcopy
from typing import Any

from sqlalchemy import select, update

from app.database import async_session
from app.models.tool import Tool


LEGACY_TOOL_NAMES = ("run_subagent", "set_trigger", "update_trigger")


def legacy_parameters_schema(schema: dict[str, Any] | None) -> tuple[dict[str, Any], bool]:
    """Remove only the reasoning argument introduced by this release."""
    normalized = deepcopy(schema or {})
    properties = normalized.get("properties")
    if not isinstance(properties, dict) or "reasoning_effort" not in properties:
        return normalized, False

    properties.pop("reasoning_effort", None)
    required = normalized.get("required")
    if isinstance(required, list):
        normalized["required"] = [item for item in required if item != "reasoning_effort"]
    return normalized, True


async def inspect_or_apply(*, apply: bool) -> dict[str, Any]:
    async with async_session() as db:
        rows = (
            await db.execute(select(Tool).where(Tool.name.in_(LEGACY_TOOL_NAMES)))
        ).scalars().all()
        by_name = {row.name: row for row in rows}
        missing = [name for name in LEGACY_TOOL_NAMES if name not in by_name]
        if missing:
            raise RuntimeError(f"missing builtin tools: {', '.join(missing)}")
        invalid = [name for name, row in by_name.items() if row.source != "builtin"]
        if invalid:
            raise RuntimeError(f"rollback targets are not builtin tools: {', '.join(invalid)}")

        present = 0
        changed = 0
        for name in LEGACY_TOOL_NAMES:
            tool = by_name[name]
            legacy_schema, had_reasoning = legacy_parameters_schema(tool.parameters_schema)
            present += int(had_reasoning)
            if apply and had_reasoning:
                await db.execute(
                    update(Tool)
                    .where(Tool.id == tool.id)
                    .values(parameters_schema=legacy_schema)
                )
                changed += 1
        if apply:
            await db.commit()
        return {
            "mode": "apply" if apply else "status",
            "targets": len(LEGACY_TOOL_NAMES),
            "reasoning_fields_present": present,
            "changed": changed,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("status", "apply"))
    args = parser.parse_args()
    result = asyncio.run(inspect_or_apply(apply=args.action == "apply"))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
