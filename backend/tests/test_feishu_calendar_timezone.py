"""Observable timezone behavior for Feishu calendar tools."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.database import async_session
from app.models.channel_config import ChannelConfig
from app.models.tool import AgentTool, Tool
from app.services import agent_tools_feishu_calendar as calendar
from session_introspection_support import (
    _isolate_async_engine_between_tests,
    _seed_agent,
    _seed_tenant,
    _seed_user,
)


class _Response:
    def __init__(self, payload: dict):
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _CalendarClient:
    calls: list[tuple[str, str, dict]] = []
    list_items: list[dict] = []
    busy_items: list[dict] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def get(self, url: str, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return _Response({"code": 0, "data": {"items": self.list_items}})

    async def post(self, url: str, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if url.endswith("/freebusy/list"):
            return _Response({"code": 0, "data": {"freebusy_list": self.busy_items}})
        return _Response({"code": 0, "data": {"event": {"event_id": "event-1"}}})

    async def patch(self, url: str, **kwargs):
        self.calls.append(("PATCH", url, kwargs))
        return _Response({"code": 0})

    async def delete(self, url: str, **kwargs):
        self.calls.append(("DELETE", url, kwargs))
        return _Response({"code": 0})


@pytest.fixture(autouse=True)
def _calendar_runtime(monkeypatch):
    _CalendarClient.calls = []
    _CalendarClient.list_items = []
    _CalendarClient.busy_items = []
    monkeypatch.setattr(calendar.httpx, "AsyncClient", _CalendarClient)
    monkeypatch.setattr(
        calendar,
        "_get_feishu_credentials",
        AsyncMock(return_value=("app-id", "app-secret")),
    )
    monkeypatch.setattr(
        type(calendar.feishu_service),
        "get_tenant_access_token",
        AsyncMock(return_value="tenant-token"),
    )
    monkeypatch.setattr(
        calendar,
        "_get_agent_calendar_id",
        AsyncMock(return_value=("calendar-id", None)),
    )
    monkeypatch.setattr(
        calendar,
        "get_agent_timezone",
        AsyncMock(return_value="Asia/Shanghai"),
    )
    sender_token = calendar.channel_feishu_sender_open_id.set(None)
    yield
    calendar.channel_feishu_sender_open_id.reset(sender_token)


def _event_create_call() -> tuple[str, str, dict]:
    return next(
        call
        for call in _CalendarClient.calls
        if call[0] == "POST" and call[1].endswith("/events")
    )


def test_agent_tools_facade_routes_calendar_to_split_module():
    from app.services import agent_tools

    assert agent_tools._feishu_calendar_list is calendar._feishu_calendar_list
    assert agent_tools._feishu_calendar_create is calendar._feishu_calendar_create
    assert agent_tools.channel_feishu_sender_open_id is calendar.channel_feishu_sender_open_id


@pytest.mark.asyncio
async def test_create_interprets_naive_times_in_agent_effective_timezone():
    result = await calendar._feishu_calendar_create(
        uuid.uuid4(),
        {
            "summary": "Local meeting",
            "start_time": "2026-09-04T09:00:00",
            "end_time": "2026-09-04T10:00:00",
        },
    )

    body = _event_create_call()[2]["json"]
    expected_start = int(datetime(2026, 9, 4, 1, 0, tzinfo=timezone.utc).timestamp())
    expected_end = int(datetime(2026, 9, 4, 2, 0, tzinfo=timezone.utc).timestamp())
    assert body["start_time"] == {
        "timestamp": str(expected_start),
        "timezone": "Asia/Shanghai",
    }
    assert body["end_time"] == {
        "timestamp": str(expected_end),
        "timezone": "Asia/Shanghai",
    }
    assert "2026-09-04T09:00:00+08:00 [Asia/Shanghai]" in result
    assert "2026-09-04T10:00:00+08:00 [Asia/Shanghai]" in result


@pytest.mark.asyncio
async def test_create_preserves_absolute_instants_from_offset_inputs():
    result = await calendar._feishu_calendar_create(
        uuid.uuid4(),
        {
            "summary": "Offset meeting",
            "start_time": "2026-09-04T09:00:00+02:00",
            "end_time": "2026-09-04T10:00:00+02:00",
        },
    )

    body = _event_create_call()[2]["json"]
    expected_start = int(datetime(2026, 9, 4, 7, 0, tzinfo=timezone.utc).timestamp())
    assert body["start_time"]["timestamp"] == str(expected_start)
    assert body["start_time"]["timezone"] == "Asia/Shanghai"
    assert "2026-09-04T15:00:00+08:00 [Asia/Shanghai]" in result


@pytest.mark.asyncio
async def test_list_uses_agent_timezone_for_range_and_projects_provider_times():
    event_start = datetime(2026, 9, 4, 1, 30, tzinfo=timezone.utc)
    event_end = datetime(2026, 9, 4, 2, 30, tzinfo=timezone.utc)
    _CalendarClient.list_items = [
        {
            "summary": "Daily sync",
            "event_id": "event-2",
            "start_time": {"timestamp": str(int(event_start.timestamp()))},
            "end_time": {"timestamp": str(int(event_end.timestamp()))},
        }
    ]
    _CalendarClient.busy_items = [
        {
            "start_time": "2026-09-04T01:30:00+00:00",
            "end_time": "2026-09-04T02:30:00+00:00",
        }
    ]
    sender_token = calendar.channel_feishu_sender_open_id.set("sender-open-id")
    try:
        result = await calendar._feishu_calendar_list(
            uuid.uuid4(),
            {
                "start_time": "2026-09-04T09:00:00",
                "end_time": "2026-09-04T18:00:00",
            },
        )
    finally:
        calendar.channel_feishu_sender_open_id.reset(sender_token)

    freebusy_call = next(call for call in _CalendarClient.calls if call[1].endswith("/freebusy/list"))
    list_call = next(call for call in _CalendarClient.calls if call[0] == "GET")
    expected_start = int(datetime(2026, 9, 4, 1, 0, tzinfo=timezone.utc).timestamp())
    expected_end = int(datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc).timestamp())
    assert freebusy_call[2]["json"]["time_min"] == "2026-09-04T01:00:00+00:00"
    assert freebusy_call[2]["json"]["time_max"] == "2026-09-04T10:00:00+00:00"
    assert list_call[2]["params"] == {
        "start_time": str(expected_start),
        "end_time": str(expected_end),
    }
    assert "2026-09-04T09:30:00+08:00 [Asia/Shanghai]" in result
    assert "2026-09-04T10:30:00+08:00 [Asia/Shanghai]" in result


@pytest.mark.asyncio
async def test_update_uses_explicit_timezone_for_naive_input():
    result = await calendar._feishu_calendar_update(
        uuid.uuid4(),
        {
            "event_id": "event-3",
            "start_time": "2026-09-04T09:00:00",
            "timezone": "America/New_York",
        },
    )

    call = next(item for item in _CalendarClient.calls if item[0] == "PATCH")
    body = call[2]["json"]
    expected = int(datetime(2026, 9, 4, 13, 0, tzinfo=timezone.utc).timestamp())
    assert body["start_time"] == {
        "timestamp": str(expected),
        "timezone": "America/New_York",
    }
    assert "2026-09-04T09:00:00-04:00 [America/New_York]" in result


@pytest.mark.asyncio
async def test_create_rejects_invalid_explicit_timezone_before_provider_write():
    result = await calendar._feishu_calendar_create(
        uuid.uuid4(),
        {
            "summary": "Invalid timezone",
            "start_time": "2026-09-04T09:00:00",
            "end_time": "2026-09-04T10:00:00",
            "timezone": "Asia/Not-A-Zone",
        },
    )

    assert result == "❌ invalid IANA timezone: Asia/Not-A-Zone"
    assert _CalendarClient.calls == []


@pytest.mark.asyncio
async def test_seeded_calendar_timezone_contract_reaches_llm_runtime():
    from app.services.agent_tools import get_agent_tools_for_llm
    from app.services.tool_seeder import seed_builtin_tools

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user.id, tenant_id=tenant.id)
    await seed_builtin_tools()

    names = {
        "feishu_calendar_list",
        "feishu_calendar_create",
        "feishu_calendar_update",
        "feishu_calendar_delete",
    }
    async with async_session() as db:
        db.add(
            ChannelConfig(
                agent_id=agent.id,
                channel_type="feishu",
                app_id="test-app",
                app_secret="test-secret",
                is_configured=True,
            )
        )
        rows = (
            await db.execute(select(Tool).where(Tool.name.in_(names)))
        ).scalars().all()
        assert {row.name for row in rows} == names
        for row in rows:
            db.add(AgentTool(agent_id=agent.id, tool_id=row.id, enabled=True))
        await db.commit()

    runtime = {
        item["function"]["name"]: item["function"]
        for item in await get_agent_tools_for_llm(agent.id)
        if item["function"]["name"] in names
    }
    assert set(runtime) == names
    create_properties = runtime["feishu_calendar_create"]["parameters"]["properties"]
    update_properties = runtime["feishu_calendar_update"]["parameters"]["properties"]
    list_properties = runtime["feishu_calendar_list"]["parameters"]["properties"]
    assert "Agent effective timezone" in create_properties["timezone"]["description"]
    assert "timezone" in update_properties
    assert "without an offset" in list_properties["start_time"]["description"]
