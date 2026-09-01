#!/usr/bin/env python3
"""Capture deterministic runtime contracts used during mechanical module splits."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from fastapi.routing import APIRoute, APIWebSocketRoute
from starlette.routing import Mount

from app.main import app
from app.services.tool_seeder import BUILTIN_TOOLS


def _callable_name(value: Any) -> str | None:
    if value is None:
        return None
    module = getattr(value, "__module__", None)
    qualname = getattr(value, "__qualname__", None) or getattr(value, "__name__", None)
    if module and qualname:
        return f"{module}.{qualname}"
    return repr(value)


def _type_name(value: Any) -> str | None:
    if value is None:
        return None
    module = getattr(value, "__module__", None)
    qualname = getattr(value, "__qualname__", None)
    if module and qualname:
        return f"{module}.{qualname}"
    value_type = type(value)
    return f"{value_type.__module__}.{value_type.__qualname__}"


def _dependency_names(route: APIRoute) -> list[str | None]:
    return [_callable_name(dependency.call) for dependency in route.dependencies]


def _route_contract(index: int, route: Any) -> dict[str, Any]:
    contract: dict[str, Any] = {
        "index": index,
        "type": type(route).__name__,
        "path": getattr(route, "path", None),
        "name": getattr(route, "name", None),
        "endpoint": _callable_name(getattr(route, "endpoint", None)),
    }
    if isinstance(route, APIRoute):
        contract.update(
            {
                "methods": sorted(route.methods or ()),
                "operation_id": route.operation_id,
                "status_code": route.status_code,
                "include_in_schema": route.include_in_schema,
                "response_model": _type_name(route.response_model),
                "response_class": _type_name(route.response_class),
                "dependencies": _dependency_names(route),
                "tags": list(route.tags),
                "deprecated": route.deprecated,
                "path_format": route.path_format,
                "path_regex": route.path_regex.pattern,
            }
        )
    elif isinstance(route, APIWebSocketRoute):
        contract.update(
            {
                "path_format": route.path_format,
                "path_regex": route.path_regex.pattern,
            }
        )
    elif isinstance(route, Mount):
        contract.update(
            {
                "routes": len(route.routes or ()),
                "app": _type_name(route.app),
            }
        )
    return contract


def _write_json(path: Path, value: Any) -> str:
    serialized = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path.write_text(serialized, encoding="utf-8")
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    contracts = {
        "openapi": app.openapi(),
        "routes": [_route_contract(index, route) for index, route in enumerate(app.router.routes)],
        "builtin_tools": BUILTIN_TOOLS,
    }
    for name, value in contracts.items():
        path = args.output_dir / f"{name}.json"
        digest = _write_json(path, value)
        print(f"{name}: {path} sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
