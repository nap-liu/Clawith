from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

import app.api.toolscall as toolscall_api
from app.services.toolscall.capability import (
    ToolscallUnavailable,
    prepare_toolscall_launcher,
)

_SIGNING_SEED = b"t" * 32


@pytest.fixture(autouse=True)
def runtime_signing_seed(monkeypatch):
    monkeypatch.setattr(
        toolscall_api,
        "get_toolscall_signing_seed",
        AsyncMock(return_value=_SIGNING_SEED),
    )
    monkeypatch.setattr(
        toolscall_api,
        "toolscall_scope_allows",
        AsyncMock(side_effect=lambda **kwargs: kwargs["tool_name"] == "sample"),
    )


def _token(*, standard=None, native=None):
    prepared = prepare_toolscall_launcher(
        {
            "kind": "toolscall",
            "name": "toolscall",
            "endpoint": "http://bridge.test/api/internal/toolscall/v1",
            "signing_seed": _SIGNING_SEED,
            "scope": "scope-bridge",
            "agent": "00000000-0000-0000-0000-000000000011",
            "user": "00000000-0000-0000-0000-000000000012",
            "session": "session-bridge",
            "turn": "00000000-0000-0000-0000-000000000013",
            "standard": standard or {"sample": {}},
            "native": native or [],
        },
        ttl_seconds=60,
    )
    return prepared["context_token"]


@pytest.fixture
def app():
    instance = FastAPI()
    instance.include_router(toolscall_api.router, prefix="/api")
    return instance


@pytest.mark.asyncio
async def test_bridge_calls_existing_executor_and_returns_exact_text(app, monkeypatch):
    execute = AsyncMock(return_value='{"unchanged":true}')
    monkeypatch.setattr(toolscall_api, "execute_tool", execute)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/internal/toolscall/v1/call/sample",
            content=b'{"value":7}',
            headers={
                "Authorization": f"Bearer {_token()}",
                "Content-Type": "application/json",
            },
        )

    assert response.status_code == 200
    assert response.content == b'{"unchanged":true}'
    kwargs = execute.await_args.kwargs
    assert execute.await_args.args == ("sample", {"value": 7})
    assert kwargs["agent_id"] == uuid.UUID("00000000-0000-0000-0000-000000000011")
    assert kwargs["user_id"] == uuid.UUID("00000000-0000-0000-0000-000000000012")
    assert kwargs["session_id"] == "session-bridge"
    assert kwargs["turn_anchor_id"] == uuid.UUID(
        "00000000-0000-0000-0000-000000000013"
    )
    assert kwargs["tool_call_id"].startswith("toolscall_")


@pytest.mark.asyncio
async def test_bridge_rejects_tools_outside_signed_standard_scope(app, monkeypatch):
    execute = AsyncMock()
    monkeypatch.setattr(toolscall_api, "execute_tool", execute)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/internal/toolscall/v1/call/native-tool",
            json={},
            headers={"Authorization": f"Bearer {_token(native=['native-tool'])}"},
        )

    assert response.status_code == 403
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_bridge_rejects_non_object_json(app, monkeypatch):
    execute = AsyncMock()
    monkeypatch.setattr(toolscall_api, "execute_tool", execute)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/internal/toolscall/v1/call/sample",
            json=[1, 2],
            headers={"Authorization": f"Bearer {_token()}"},
        )

    assert response.status_code == 400
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_bridge_reports_runtime_initialization_failure(app, monkeypatch):
    token = _token()
    monkeypatch.setattr(
        toolscall_api,
        "get_toolscall_signing_seed",
        AsyncMock(side_effect=ToolscallUnavailable("runtime unavailable")),
    )
    execute = AsyncMock()
    monkeypatch.setattr(toolscall_api, "execute_tool", execute)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/internal/toolscall/v1/call/sample",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 503
    execute.assert_not_awaited()
