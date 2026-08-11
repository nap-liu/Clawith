from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.services.toolscall import runtime


class _Redis:
    def __init__(self):
        self.values = {}

    async def set(self, key, value, *, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def get(self, key):
        return self.values.get(key)


@pytest.mark.asyncio
async def test_signing_seed_is_created_once_and_shared_through_runtime(monkeypatch):
    redis = _Redis()
    monkeypatch.setattr(runtime, "get_redis", AsyncMock(return_value=redis))

    first = await runtime.get_toolscall_signing_seed()
    second = await runtime.get_toolscall_signing_seed()

    assert len(first) == 32
    assert second == first


@pytest.mark.asyncio
async def test_scope_is_server_side_and_checked_by_opaque_reference(monkeypatch):
    redis = _Redis()
    monkeypatch.setattr(runtime, "get_redis", AsyncMock(return_value=redis))

    scope_id = await runtime.register_toolscall_scope(
        standard_tool_names={"alpha", "beta"},
        ttl_seconds=60,
    )

    assert await runtime.toolscall_scope_allows(
        scope_id=scope_id, tool_name="alpha"
    )
    assert not await runtime.toolscall_scope_allows(
        scope_id=scope_id, tool_name="gamma"
    )


def test_compose_endpoint_uses_internal_backend_service():
    assert runtime.bridge_url_for_endpoint(
        aio_base_url="http://aio-sandbox:8080",
        platform_base_url="http://localhost:3008",
    ) == "http://backend:8000/api/internal/toolscall/v1"


def test_utm_endpoint_uses_host_gateway_and_platform_port():
    assert runtime.bridge_url_for_endpoint(
        aio_base_url="http://192.168.64.3:8080",
        platform_base_url="http://local.example:3008",
    ) == "http://192.168.64.1:3008/api/internal/toolscall/v1"


def test_utm_endpoint_maps_platform_backend_fallback_to_default_frontend():
    assert runtime.bridge_url_for_endpoint(
        aio_base_url="http://192.168.64.3:8080",
        platform_base_url="http://localhost:8000",
    ) == "http://192.168.64.1:3008/api/internal/toolscall/v1"


def test_unknown_single_label_endpoint_is_not_assumed_to_share_compose_network():
    with pytest.raises(runtime.ToolscallRuntimeUnavailable, match="cannot reach"):
        runtime.bridge_url_for_endpoint(
            aio_base_url="http://custom-sandbox:8080",
            platform_base_url="http://localhost:8000",
        )


def test_remote_endpoint_uses_platform_address():
    assert runtime.bridge_url_for_endpoint(
        aio_base_url="https://sandbox.example.net",
        platform_base_url="https://work.example.com",
    ) == "https://work.example.com/api/internal/toolscall/v1"


def test_remote_endpoint_rejects_unreachable_loopback_platform_address():
    with pytest.raises(runtime.ToolscallRuntimeUnavailable, match="cannot reach"):
        runtime.bridge_url_for_endpoint(
            aio_base_url="https://sandbox.example.net",
            platform_base_url="http://localhost:3008",
        )
