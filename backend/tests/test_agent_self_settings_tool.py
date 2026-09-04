from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import select

from app.database import async_session, engine
from app.models.agent import Agent
from app.models.llm import LLMModel
from app.models.participant import Participant
from app.models.tenant import Tenant
from app.models.user import Identity, User


@pytest.fixture(autouse=True)
async def _isolate_engine():
    await engine.dispose()
    yield
    await engine.dispose()


async def _seed_runtime():
    async with async_session() as db:
        tenant = Tenant(name="Self Settings", slug=f"self-{uuid.uuid4().hex[:10]}")
        other_tenant = Tenant(name="Other", slug=f"other-{uuid.uuid4().hex[:10]}")
        db.add_all([tenant, other_tenant])
        await db.flush()
        identity = Identity(
            username=f"self_{uuid.uuid4().hex[:10]}",
            email=f"{uuid.uuid4().hex[:10]}@self.local",
            password_hash="x",
        )
        db.add(identity)
        await db.flush()
        user = User(
            identity_id=identity.id,
            display_name="Owner",
            role="member",
            is_active=True,
            tenant_id=tenant.id,
        )
        db.add(user)
        await db.flush()
        model = LLMModel(
            tenant_id=tenant.id,
            provider="test",
            model="self-model",
            api_key_encrypted="x",
            label="Self Model",
            enabled=True,
        )
        foreign_model = LLMModel(
            tenant_id=other_tenant.id,
            provider="test",
            model="foreign-model",
            api_key_encrypted="x",
            label="Foreign Model",
            enabled=True,
        )
        db.add_all([model, foreign_model])
        await db.flush()
        agent = Agent(
            name="Before",
            creator_id=user.id,
            tenant_id=tenant.id,
            agent_type="native",
            scope="standard",
            status="idle",
            temperature=0.8,
            fallback_model_id=model.id,
            daily_memory_load_days=3,
            im_thinking_output_enabled=True,
        )
        db.add(agent)
        await db.flush()
        db.add(Participant(type="agent", ref_id=agent.id, display_name=agent.name))
        await db.commit()
        return agent.id, user.id, model.id, foreign_model.id


async def test_seeded_self_settings_tool_executes_zero_false_and_null_patch():
    from app.models.tool import Tool
    from app.services.agent_tools import execute_tool, get_agent_tools_for_llm
    from app.services.tool_seeder import seed_builtin_tools

    agent_id, user_id, model_id, _foreign_model_id = await _seed_runtime()
    # Exercise the upgrade path too: a previously-known tool becoming default
    # must be assigned to all existing standard Digital Employees.
    async with async_session() as db:
        existing = (
            await db.execute(select(Tool).where(Tool.name == "update_self_settings"))
        ).scalar_one_or_none()
        if existing:
            existing.is_default = False
            await db.commit()
    await seed_builtin_tools()

    runtime_tools = await get_agent_tools_for_llm(agent_id)
    self_tool = next(
        tool for tool in runtime_tools
        if tool.get("function", {}).get("name") == "update_self_settings"
    )
    properties = self_tool["function"]["parameters"]["properties"]
    assert "agent_id" not in properties
    assert "autonomy_policy" not in properties
    assert "imagination" in properties
    assert "relative managed path" in properties["avatar_url"]["description"]
    assert "/api/agents/{agent_id}/files/download" in properties["avatar_url"]["description"]

    result = await execute_tool(
        "update_self_settings",
        {
            "name": "After",
            "primary_model": str(model_id),
            "fallback_model": None,
            "imagination": 0,
            "daily_memory_load_days": 0,
            "im_thinking_output_enabled": False,
        },
        agent_id,
        user_id,
    )
    payload = json.loads(result)
    assert payload["status"] == "updated"
    assert set(payload["changed_fields"]) >= {
        "name",
        "primary_model",
        "fallback_model",
        "imagination",
        "daily_memory_load_days",
        "im_thinking_output_enabled",
    }

    async with async_session() as db:
        agent = (await db.execute(select(Agent).where(Agent.id == agent_id))).scalar_one()
        participant = (
            await db.execute(
                select(Participant).where(
                    Participant.type == "agent",
                    Participant.ref_id == agent_id,
                )
            )
        ).scalar_one()
        assert agent.name == "After"
        assert agent.primary_model_id == model_id
        assert agent.fallback_model_id is None
        assert agent.temperature == 0
        assert agent.daily_memory_load_days == 0
        assert agent.im_thinking_output_enabled is False
        assert participant.display_name == "After"


async def test_self_settings_avatar_requires_relative_path_for_managed_agent_file(monkeypatch):
    from app.services.agent_tools import execute_tool

    monkeypatch.setenv("PUBLIC_BASE_URL", "https://ai.example.com")
    agent_id, user_id, _model_id, _foreign_model_id = await _seed_runtime()
    managed_path = (
        f"/api/agents/{agent_id}/files/download"
        "?path=workspace/uploads/avatar.jpg"
    )

    rejected = await execute_tool(
        "update_self_settings",
        {"avatar_url": f"https://ai.example.com{managed_path}"},
        agent_id,
        user_id,
    )
    assert "参数无效" in rejected
    assert "relative /api/agents/" in rejected
    assert "remove the scheme and host" in rejected

    updated = json.loads(await execute_tool(
        "update_self_settings",
        {"avatar_url": managed_path},
        agent_id,
        user_id,
    ))
    assert updated["status"] == "updated"
    assert "avatar_url" in updated["changed_fields"]

    async with async_session() as db:
        agent = (await db.execute(select(Agent).where(Agent.id == agent_id))).scalar_one()
        participant = (
            await db.execute(
                select(Participant).where(
                    Participant.type == "agent",
                    Participant.ref_id == agent_id,
                )
            )
        ).scalar_one()
        assert agent.avatar_url == managed_path
        assert participant.avatar_url == managed_path


async def test_self_settings_avatar_allows_external_url_with_managed_shaped_path():
    from app.services.agent_tools import execute_tool

    agent_id, user_id, _model_id, _foreign_model_id = await _seed_runtime()
    external_url = (
        f"https://cdn.example.com/api/agents/{agent_id}/files/download"
        "?path=workspace/uploads/avatar.jpg"
    )

    updated = json.loads(await execute_tool(
        "update_self_settings",
        {"avatar_url": external_url},
        agent_id,
        user_id,
    ))
    assert updated["status"] == "updated"

    async with async_session() as db:
        agent = (await db.execute(select(Agent).where(Agent.id == agent_id))).scalar_one()
        assert agent.avatar_url == external_url


async def test_self_settings_patch_is_atomic_for_foreign_model_and_rejects_target_smuggling():
    from app.services.agent_tools import execute_tool

    agent_id, user_id, _model_id, foreign_model_id = await _seed_runtime()
    rejected = await execute_tool(
        "update_self_settings",
        {"name": "Must Not Persist", "primary_model": str(foreign_model_id)},
        agent_id,
        user_id,
    )
    assert "未更新" in rejected

    smuggled = await execute_tool(
        "update_self_settings",
        {"agent_id": str(uuid.uuid4()), "name": "Also Must Not Persist"},
        agent_id,
        user_id,
    )
    assert "参数无效" in smuggled

    async with async_session() as db:
        agent = (await db.execute(select(Agent).where(Agent.id == agent_id))).scalar_one()
        assert agent.name == "Before"
        assert agent.primary_model_id is None


async def test_self_settings_tool_rollback_is_idempotent():
    from app.models.tool import AgentTool, Tool
    from app.scripts.rollback_agent_self_settings import rollback_agent_self_settings
    from app.services.tool_seeder import seed_builtin_tools

    agent_id, _user_id, _model_id, _foreign_model_id = await _seed_runtime()
    await seed_builtin_tools()

    async with async_session() as db:
        tool = (
            await db.execute(select(Tool).where(Tool.name == "update_self_settings"))
        ).scalar_one()
        assignment = (
            await db.execute(
                select(AgentTool).where(
                    AgentTool.agent_id == agent_id,
                    AgentTool.tool_id == tool.id,
                )
            )
        ).scalar_one_or_none()
        if assignment is None:
            assignment = AgentTool(agent_id=agent_id, tool_id=tool.id, enabled=True)
            db.add(assignment)
            assert assignment.enabled is True
            await db.commit()
        else:
            assert assignment.enabled is True

    assignment_count, tool_count = await rollback_agent_self_settings()
    assert assignment_count >= 1
    assert tool_count == 1
    assert await rollback_agent_self_settings() == (0, 0)

    async with async_session() as db:
        assert (
            await db.execute(select(Tool).where(Tool.name == "update_self_settings"))
        ).scalar_one_or_none() is None
