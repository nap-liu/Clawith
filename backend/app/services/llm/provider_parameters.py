"""Narrow endpoint-specific differences in otherwise standard chat requests."""

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

import httpx

from app.services.llm.reasoning import is_bailian_endpoint


def merge_request_headers(
    defaults: Mapping[str, str], extra: Mapping[str, str] | None,
) -> dict[str, str]:
    """Merge protocol defaults with explicitly configured headers, ignoring case."""
    headers = httpx.Headers(defaults)
    for name, value in (extra or {}).items():
        headers[name] = value
    return dict(headers)


def request_headers_event_hooks(
    base_url: str | None, extra_headers: Mapping[str, str] | None,
) -> dict[str, list]:
    """Keep configured credentials on the configured origin during redirects."""
    if not extra_headers:
        return {}
    origin_url = httpx.URL(base_url or "")
    origin = (origin_url.scheme, origin_url.host, origin_url.port)
    names = tuple(extra_headers)

    async def strip_cross_origin_headers(request: httpx.Request) -> None:
        target = (request.url.scheme, request.url.host, request.url.port)
        if target != origin:
            for name in names:
                request.headers.pop(name, None)

    return {"request": [strip_cross_origin_headers]}


def supports_default_tool_choice(*, model: str | None, base_url: str | None) -> bool:
    return not (
        is_bailian_endpoint(base_url)
        and str(model or "").lower() == "stepfun/step-3.7-flash"
    )


def normalize_chat_tools(
    tools: list[dict[str, Any]], *, model: str | None, base_url: str | None,
) -> list[dict[str, Any]]:
    """Project canonical tools into Bailian Kimi's JSON Schema dialect.

    Moonshot's Bailian adapter rejects a parent ``type`` beside ``anyOf`` or
    ``oneOf`` and requires that type on every union branch instead. Keep the
    database schema canonical and apply the lossless rewrite only at the final
    provider boundary.
    """
    model_name = str(model or "").lower()
    is_kimi = model_name == "kimi-k3" or model_name.startswith("kimi/")
    if not (is_bailian_endpoint(base_url) and is_kimi):
        return tools

    projected = deepcopy(tools)
    for tool in projected:
        function = tool.get("function")
        if isinstance(function, dict):
            parameters = function.get("parameters")
            if isinstance(parameters, dict):
                _move_union_parent_types(parameters)
    return projected


def _move_union_parent_types(schema: dict[str, Any]) -> None:
    parent_type = schema.get("type")
    for keyword in ("anyOf", "oneOf"):
        branches = schema.get(keyword)
        if parent_type is not None and isinstance(branches, list):
            schema.pop("type", None)
            for branch in branches:
                if isinstance(branch, dict):
                    branch.setdefault("type", parent_type)

    mapping_keywords = (
        "properties", "patternProperties", "$defs", "definitions", "dependentSchemas",
    )
    for keyword in mapping_keywords:
        children = schema.get(keyword)
        if isinstance(children, dict):
            for child in children.values():
                if isinstance(child, dict):
                    _move_union_parent_types(child)

    schema_keywords = (
        "items", "additionalProperties", "contains", "not", "if", "then", "else",
        "propertyNames", "unevaluatedProperties",
    )
    for keyword in schema_keywords:
        child = schema.get(keyword)
        if isinstance(child, dict):
            _move_union_parent_types(child)

    for keyword in ("allOf", "anyOf", "oneOf", "prefixItems"):
        children = schema.get(keyword)
        if isinstance(children, list):
            for child in children:
                if isinstance(child, dict):
                    _move_union_parent_types(child)


def validate_chat_parameters(payload: dict[str, Any], *, base_url: str | None) -> None:
    """Do not silently pretend a fixed sampling setting honors a user override."""
    if not is_bailian_endpoint(base_url):
        return
    model = str(payload.get("model") or "")
    if model.lower() != "minimax/minimax-m3":
        return
    for parameter, fixed in (("temperature", 1.0), ("top_p", 0.95)):
        if parameter in payload and payload[parameter] != fixed:
            raise ValueError(f"model {model} only supports {parameter}={fixed}")
