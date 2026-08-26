from __future__ import annotations

import asyncio
import json
import uuid

import httpx
import pytest

from app.database import async_session, engine
from app.main import app
from app.mcp_server import mcp
from app.models.tenant import Tenant
from app.models.user import Identity, User
from app.services.active_turns import (
    ensure_active_turn,
    reset_active_turns_for_testing,
)
from app.services.pat_service import issue_pat

pytestmark = pytest.mark.asyncio


def _jsonrpc_payload(response: httpx.Response) -> dict:
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" not in content_type:
        return response.json()
    data_lines = [
        line.removeprefix("data: ")
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert data_lines
    return json.loads(data_lines[-1])


def _tool_text(payload: dict) -> str:
    content = payload["result"]["content"]
    assert content[0]["type"] == "text"
    return content[0]["text"]


async def _seed_user_and_tokens() -> tuple[User, str, str]:
    async with async_session() as db:
        tenant = Tenant(name="MCP transport", slug=f"mcp-{uuid.uuid4().hex[:10]}")
        identity = Identity(
            username=f"mcp_{uuid.uuid4().hex[:12]}",
            email=f"{uuid.uuid4().hex[:12]}@mcp.test",
            password_hash="x",
        )
        db.add_all([tenant, identity])
        await db.flush()
        user = User(
            identity_id=identity.id,
            display_name="MCP transport user",
            role="member",
            is_active=True,
            tenant_id=tenant.id,
        )
        db.add(user)
        await db.flush()
        read_token, _ = await issue_pat(db, user=user, name="transport-read")
        write_token, _ = await issue_pat(
            db, user=user, name="transport-write", scope="write"
        )
        await db.commit()
        await db.refresh(user)
        return user, read_token, write_token


async def test_active_turn_tools_work_over_real_streamable_http_transport():
    await reset_active_turns_for_testing()
    user, read_token, write_token = await _seed_user_and_tokens()
    ready = asyncio.Event()

    async def running_turn() -> None:
        await ensure_active_turn(
            owner_user_id=user.id,
            agent_id=uuid.uuid4(),
            session_id="transport-live-turn",
            turn_type="task",
            title="Transport live turn",
        )
        ready.set()
        await asyncio.Event().wait()

    active_task = asyncio.create_task(running_turn())
    await ready.wait()
    headers = {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
        "authorization": f"Bearer {read_token}",
    }
    transport = httpx.ASGITransport(app=app)
    async with mcp.session_manager.run(), httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        initialized = await client.post(
            "/mcp/",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "turn-test", "version": "1"},
                },
            },
        )
        assert (
            _jsonrpc_payload(initialized)["result"]["serverInfo"]["name"]
            == "Digital Employee Platform"
        )
        session_id = initialized.headers["mcp-session-id"]
        session_headers = {**headers, "mcp-session-id": session_id}

        await client.post(
            "/mcp/",
            headers=session_headers,
            json={
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            },
        )
        listed = _jsonrpc_payload(
            await client.post(
                "/mcp/",
                headers=session_headers,
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            )
        )
        names = {tool["name"] for tool in listed["result"]["tools"]}
        assert {"list_active_turns", "stop_turn"} <= names
        schemas = {
            tool["name"]: tool["inputSchema"] for tool in listed["result"]["tools"]
        }
        assert schemas["list_active_turns"].get("required", []) == []
        assert schemas["stop_turn"]["required"] == ["turn_id"]

        listed_turns = _jsonrpc_payload(
            await client.post(
                "/mcp/",
                headers=session_headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "list_active_turns",
                        "arguments": {},
                    },
                },
            )
        )
        listed_text = _tool_text(listed_turns)
        record_id = listed_text.split("turn_id=", 1)[1].split(" ·", 1)[0]
        assert "Transport live turn" in listed_text

        read_only_stop = _jsonrpc_payload(
            await client.post(
                "/mcp/",
                headers=session_headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "stop_turn",
                        "arguments": {"turn_id": record_id},
                    },
                },
            )
        )
        assert "write 范围" in _tool_text(read_only_stop)
        assert not active_task.done()

        write_headers = {
            **session_headers,
            "authorization": f"Bearer {write_token}",
        }
        stopped = _jsonrpc_payload(
            await client.post(
                "/mcp/",
                headers=write_headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {
                        "name": "stop_turn",
                        "arguments": {"turn_id": record_id},
                    },
                },
            )
        )
        assert "已终止" in _tool_text(stopped)
        with pytest.raises(asyncio.CancelledError):
            await active_task
    await engine.dispose()
    await reset_active_turns_for_testing()
