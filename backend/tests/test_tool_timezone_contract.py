"""Observable timezone behavior for LLM-facing builtin tool contracts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import select

from app.database import async_session
from app.models.agent import Agent
from app.models.chat_compaction import ChatCompaction  # noqa: F401 -- register FK target
from app.models.chat_session import ChatSession
from app.models.tenant import Tenant
from app.models.tool import AgentTool, Tool
from app.models.trigger import AgentTrigger
from app.services.agent_tools_trigger_ops import (
    _handle_list_triggers,
    _handle_set_trigger,
)
from app.services.timezone_utils import (
    add_local_datetime_projections,
    format_datetime_for_agent,
    normalize_datetime_arguments,
    parse_datetime_for_agent,
)
from app.services.tools.session_introspection import (
    handle_list_sessions,
    handle_read_session_messages,
    handle_search_sessions,
)
from app.services.trigger_time_contract import (
    normalize_trigger_time_config,
    project_trigger_config,
    resolve_once_trigger_at,
)
from session_introspection_support import (
    _isolate_async_engine_between_tests,
    _seed_agent,
    _seed_message,
    _seed_session,
    _seed_tenant,
    _seed_user,
)


def test_shared_datetime_contract_interprets_naive_in_effective_timezone():
    local = parse_datetime_for_agent("2026-09-04T09:39:39", "Asia/Shanghai")
    explicit = parse_datetime_for_agent("2026-09-04T03:39:39+02:00", "America/New_York")

    assert local == datetime(2026, 9, 4, 1, 39, 39, tzinfo=timezone.utc)
    assert explicit == datetime(2026, 9, 4, 1, 39, 39, tzinfo=timezone.utc)
    assert format_datetime_for_agent(local, "Asia/Shanghai") == (
        "2026-09-04T09:39:39+08:00 [Asia/Shanghai]"
    )

    normalized = normalize_datetime_arguments(
        {"due_at": "2026-09-04T09:39:39", "title": "keep"},
        "Asia/Shanghai",
        "due_at",
    )
    assert normalized == {
        "due_at": "2026-09-04T01:39:39+00:00",
        "title": "keep",
    }


def test_machine_json_projection_keeps_canonical_value_and_adds_local_sibling():
    payload = {
        "created_at": "2026-09-04T01:39:39+00:00",
        "nested": [{"finished_at": "2026-09-04T03:00:00Z"}],
    }
    projected = add_local_datetime_projections(payload, "Asia/Shanghai")

    assert projected["created_at"] == payload["created_at"]
    assert projected["created_at_local"] == "2026-09-04T09:39:39+08:00 [Asia/Shanghai]"
    assert projected["nested"][0]["finished_at"] == payload["nested"][0]["finished_at"]
    assert projected["nested"][0]["finished_at_local"].endswith("[Asia/Shanghai]")


async def _set_timezones_and_activity(tenant, agent, session, activity):
    async with async_session() as db:
        tenant_row = await db.get(Tenant, tenant.id)
        agent_row = await db.get(Agent, agent.id)
        session_row = await db.get(ChatSession, session.id)
        tenant_row.timezone = "Asia/Shanghai"
        agent_row.timezone = None
        session_row.last_message_at = activity
        await db.commit()


async def test_session_tools_use_inherited_timezone_and_preserve_canonical_raw_data():
    tenant = await _seed_tenant()
    user = await _seed_user(role="platform_admin", tenant_id=tenant.id)
    agent = await _seed_agent(user.id, tenant_id=tenant.id, access_mode="company")
    session = await _seed_session(agent.id, user.id, title="Timezone session")
    activity = datetime(2026, 9, 4, 1, 39, 39, tzinfo=timezone.utc)
    await _set_timezones_and_activity(tenant, agent, session, activity)
    await _seed_message(
        agent.id,
        user.id,
        session.id,
        "user",
        "TIMEZONE-MARKER",
        created_at=activity,
    )

    listed = await handle_list_sessions(agent.id, user.id, str(session.id), {})
    read = await handle_read_session_messages(
        agent.id,
        user.id,
        str(session.id),
        {"session_id": str(session.id)},
    )
    searched = await handle_search_sessions(
        agent.id,
        user.id,
        str(session.id),
        {"query": "TIMEZONE-MARKER", "since": "2026-09-04T09:00:00"},
    )
    raw = json.loads(
        await handle_list_sessions(
            agent.id,
            user.id,
            str(session.id),
            {"raw": True, "limit": 10},
        )
    )

    expected = "2026-09-04T09:39:39+08:00 [Asia/Shanghai]"
    assert expected in listed
    assert expected in read
    assert expected in searched
    assert raw["effective_timezone"] == "Asia/Shanghai"
    item = next(value for value in raw["items"] if value["id"] == str(session.id))
    assert item["last_message_at"].endswith("+00:00")
    assert item["last_message_at_local"] == expected
    assert raw["page"]["snapshot_at"].endswith("+00:00")
    assert raw["page"]["snapshot_at_local"].endswith("[Asia/Shanghai]")


async def test_session_time_filters_distinguish_naive_local_from_offset_input():
    tenant = await _seed_tenant()
    user = await _seed_user(role="platform_admin", tenant_id=tenant.id)
    agent = await _seed_agent(user.id, tenant_id=tenant.id, access_mode="company")
    session = await _seed_session(agent.id, user.id, title="Filter session")
    activity = datetime(2026, 9, 4, 1, 39, 39, tzinfo=timezone.utc)
    await _set_timezones_and_activity(tenant, agent, session, activity)
    await _seed_message(
        agent.id,
        user.id,
        session.id,
        "user",
        "FILTER-MARKER",
        created_at=activity,
    )

    local_bound = await handle_list_sessions(
        agent.id,
        user.id,
        str(session.id),
        {"since": "2026-09-04T09:00:00"},
    )
    absolute_bound = await handle_list_sessions(
        agent.id,
        user.id,
        str(session.id),
        {"since": "2026-09-04T09:00:00+00:00"},
    )
    before_hit = await handle_search_sessions(
        agent.id,
        user.id,
        str(session.id),
        {"query": "FILTER-MARKER", "until": "2026-09-04T09:30:00"},
    )

    assert str(session.id) in local_bound
    assert str(session.id) not in absolute_bound
    assert str(session.id) not in before_hit


async def test_agent_timezone_override_wins_over_tenant_timezone():
    tenant = await _seed_tenant()
    user = await _seed_user(role="platform_admin", tenant_id=tenant.id)
    agent = await _seed_agent(user.id, tenant_id=tenant.id, access_mode="company")
    session = await _seed_session(agent.id, user.id, title="Override session")
    activity = datetime(2026, 9, 4, 1, 39, 39, tzinfo=timezone.utc)
    await _set_timezones_and_activity(tenant, agent, session, activity)
    async with async_session() as db:
        agent_row = await db.get(Agent, agent.id)
        agent_row.timezone = "America/New_York"
        await db.commit()

    listed = await handle_list_sessions(agent.id, user.id, str(session.id), {})
    assert "2026-09-03T21:39:39-04:00 [America/New_York]" in listed


async def test_once_trigger_handler_persists_utc_and_lists_agent_local_projection():
    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user.id, tenant_id=tenant.id)
    async with async_session() as db:
        tenant_row = await db.get(Tenant, tenant.id)
        tenant_row.timezone = "Asia/Shanghai"
        await db.commit()

    trigger_name = f"timezone_{agent.id.hex[:8]}"
    result = await _handle_set_trigger(
        agent.id,
        {
            "name": trigger_name,
            "type": "once",
            "config": {"at": "2026-09-04T09:39:39"},
            "reason": "verify timezone contract",
        },
        user_id=user.id,
    )

    assert "created" in result
    async with async_session() as db:
        trigger = await db.scalar(
            select(AgentTrigger).where(
                AgentTrigger.agent_id == agent.id,
                AgentTrigger.name == trigger_name,
            )
        )
        assert trigger.config["at"] == "2026-09-04T01:39:39+00:00"
        assert trigger.config["_input_timezone"] == "Asia/Shanghai"

    listed = await _handle_list_triggers(agent.id)
    assert "2026-09-04T09:39:39+08:00 [Asia/Shanghai]" in listed


async def test_once_trigger_uses_agent_timezone_and_preserves_display_source(monkeypatch):
    canonical = normalize_trigger_time_config(
        "once",
        {"at": "2026-09-04T09:39:39"},
        "Asia/Shanghai",
    )
    assert canonical["at"] == "2026-09-04T01:39:39+00:00"
    assert project_trigger_config(canonical, "Asia/Shanghai") == {
        "at": "2026-09-04T01:39:39+00:00",
        "at_local": "2026-09-04T09:39:39+08:00 [Asia/Shanghai]",
    }

    async def fake_timezone(_agent_id):
        return "Asia/Shanghai"

    monkeypatch.setattr("app.services.trigger_time_contract.get_agent_timezone", fake_timezone)
    legacy = SimpleNamespace(agent_id="agent")
    resolved = await resolve_once_trigger_at(legacy, {"at": "2026-09-04T09:39:39"})
    assert resolved == datetime(2026, 9, 4, 1, 39, 39, tzinfo=timezone.utc)


async def test_seeded_session_time_contract_reaches_llm_runtime():
    from app.services.agent_tools import get_agent_tools_for_llm
    from app.services.tool_seeder import seed_builtin_tools

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user.id, tenant_id=tenant.id)
    await seed_builtin_tools()

    expected_names = {"list_sessions", "read_session_messages", "search_sessions"}
    async with async_session() as db:
        rows = (
            await db.execute(select(Tool).where(Tool.name.in_(expected_names)))
        ).scalars().all()
        assert {row.name for row in rows} == expected_names
        for row in rows:
            assignment = await db.scalar(
                select(AgentTool).where(
                    AgentTool.agent_id == agent.id,
                    AgentTool.tool_id == row.id,
                )
            )
            if assignment is None:
                db.add(AgentTool(agent_id=agent.id, tool_id=row.id, enabled=True))
            else:
                assignment.enabled = True
        await db.commit()

    runtime = {
        item["function"]["name"]: item["function"]
        for item in await get_agent_tools_for_llm(agent.id)
        if item["function"]["name"] in expected_names
    }
    assert set(runtime) == expected_names
    assert "effective Agent timezone" in runtime["list_sessions"]["description"]
    assert "without an offset" in runtime["search_sessions"]["parameters"]["properties"]["since"]["description"]
