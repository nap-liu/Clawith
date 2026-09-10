"""Narrow endpoint-specific differences in otherwise standard chat requests."""

from typing import Any
from collections.abc import Mapping

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
