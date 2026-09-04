"""Behavior tests for the normalized durable Subagent runtime."""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, func, select

from app.api.tools import AgentToolUpdate, get_agent_tools_with_config, update_agent_tools
from app.api.websocket import _has_active_subagent_event_turn
from app.database import async_session, engine
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_compaction import ChatCompaction  # noqa: F401
from app.models.chat_session import ChatSession
from app.models.llm import LLMModel
from app.models.mcp_server import MCPServer  # noqa: F401 - register Tool FK target
from app.models.participant import Participant  # noqa: F401
from app.models.project import Project, ProjectMemberSnapshot, ProjectRun
from app.models.subagent_run import SubagentRun
from app.models.tenant import Tenant
from app.models.tool import AgentTool, Tool
from app.models.user import Identity, User
from app.services import channel_llm
from app.services import subagent_runtime as runtime
from app.services.agent_tools import get_agent_tools_for_llm
from app.services.project_collaboration_prompt import build_project_runtime_context
from app.services.project_member_runtime import (
    clone_source_agent_tool_dependencies,
    merge_project_member_runtime_config,
)
from app.services.project_service import freeze_run_members
from app.services.tool_enablement import SUBAGENT_TOOL_NAMES
from app.services.tool_seeder import seed_builtin_tools
from app.services.user_project_tools import (
    USER_PROJECT_TOOL_NAMES,
    USER_PROJECT_TOOL_SEEDS,
)
from app.services.workload_capacity import WorkloadCapacity, WorkloadKind

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    await engine.dispose()
    yield
    await engine.dispose()


async def _make_context(*, parent_channel: str = "web", project: bool = False):
    suffix = uuid.uuid4().hex[:10]
    async with async_session() as db:
        tenant = Tenant(name=f"subagent-{suffix}", slug=f"subagent-{suffix}")
        db.add(tenant)
        await db.flush()
        identity = Identity(
            username=f"subagent-{suffix}",
            email=f"subagent-{suffix}@test.local",
            password_hash="x",
        )
        db.add(identity)
        await db.flush()
        user = User(
            identity_id=identity.id,
            display_name="Subagent Tester",
            role="member",
            is_active=True,
            tenant_id=tenant.id,
        )
        db.add(user)
        await db.flush()
        model = LLMModel(
            tenant_id=tenant.id,
            provider="openai",
            model="Readable-Test-Model",
            api_key_encrypted="unused",
            label="Readable model",
            enabled=True,
            context_window=128000,
        )
        db.add(model)
        await db.flush()
        agent = Agent(
            name=f"Agent {suffix}",
            creator_id=user.id,
            tenant_id=tenant.id,
            primary_model_id=model.id,
            context_window_size=128000,
            status="idle",
        )
        db.add(agent)
        await db.flush()
        project_row = None
        if project:
            project_row = Project(
                tenant_id=tenant.id,
                owner_user_id=user.id,
                name="Professional collaboration quality",
                goal="Produce an evidence-backed release decision",
                success_criteria=["role-specific conclusion", "verifiable evidence"],
                status="running",
            )
            db.add(project_row)
            await db.flush()
            db.add(
                ProjectMemberSnapshot(
                    tenant_id=tenant.id,
                    project_id=project_row.id,
                    agent_id=agent.id,
                    name_snapshot="Release reviewer",
                    role_snapshot="Assess release evidence and operational risk",
                    is_leader=False,
                    is_enabled=True,
                )
            )
        parent = ChatSession(
            agent_id=agent.id,
            project_id=project_row.id if project_row is not None else None,
            user_id=user.id if parent_channel in {"web", "dingtalk"} else None,
            title="Parent",
            source_channel=parent_channel,
            is_primary=False,
            is_group=False,
        )
        db.add(parent)
        await db.flush()
        message_time = datetime.now(UTC)
        prior_user = ChatMessage(
            agent_id=agent.id,
            user_id=user.id,
            sender_user_id=user.id,
            role="user",
            content="earlier user context",
            conversation_id=str(parent.id),
            message_meta={"attachments": []},
            created_at=message_time,
        )
        db.add(prior_user)
        await db.flush()
        prior = ChatMessage(
            agent_id=agent.id,
            user_id=user.id,
            sender_agent_id=agent.id,
            role="assistant",
            content="earlier context",
            conversation_id=str(parent.id),
            message_meta={"attachments": []},
            created_at=message_time + timedelta(microseconds=1),
        )
        db.add(prior)
        await db.flush()
        anchor = ChatMessage(
            agent_id=agent.id,
            user_id=user.id,
            sender_user_id=user.id,
            role="user",
            content="parent request",
            conversation_id=str(parent.id),
            message_meta={"attachments": []},
            created_at=message_time + timedelta(microseconds=2),
        )
        db.add(anchor)
        await db.commit()
        return agent.id, user.id, parent.id, anchor.id


async def test_project_run_child_uses_frozen_config_model_rounds_instruction_and_tools(monkeypatch):
    agent_id, user_id, parent_id, anchor_id = await _make_context(parent_channel="project", project=True)
    tool_name = f"frozen_tool_{uuid.uuid4().hex[:10]}"
    async with async_session() as db:
        parent = await db.get(ChatSession, parent_id)
        project = await db.get(Project, parent.project_id)
        member = (
            await db.execute(
                select(ProjectMemberSnapshot).where(
                    ProjectMemberSnapshot.project_id == project.id,
                    ProjectMemberSnapshot.agent_id == agent_id,
                )
            )
        ).scalar_one()
        frozen_model = LLMModel(
            tenant_id=project.tenant_id,
            provider="openai",
            model=f"Frozen-{uuid.uuid4().hex[:8]}",
            api_key_encrypted="unused",
            label="Frozen model",
            enabled=True,
            context_window=128000,
        )
        tool = Tool(
            name=tool_name,
            display_name="Frozen tool",
            description="Read frozen evidence",
            type="builtin",
            source="builtin",
            enabled=True,
            parameters_schema={"type": "object", "properties": {}},
        )
        db.add_all([frozen_model, tool])
        await db.flush()
        member.config_snapshot = {
            **dict(member.config_snapshot or {}),
            "primary_model_id": str(frozen_model.id),
            "temperature": 0.2,
            "reasoning_effort": "high",
            "max_tool_rounds": 7,
            "project_instruction": "Use the release checklist captured at run start.",
        }
        assignment = AgentTool(agent_id=agent_id, tool_id=tool.id, enabled=True)
        db.add(assignment)
        project_run = ProjectRun(
            tenant_id=project.tenant_id,
            project_id=project.id,
            agent_id=agent_id,
            initiated_by_user_id=user_id,
            execution_user_id=user_id,
            status="queued",
            trigger_type="manual",
        )
        db.add(project_run)
        await db.flush()
        await freeze_run_members(db, project, project_run)
        member.config_snapshot = {
            **dict(member.config_snapshot or {}),
            "primary_model_id": None,
            "temperature": 1.1,
            "reasoning_effort": "none",
            "max_tool_rounds": 99,
            "project_instruction": "This later edit must not affect the old run.",
        }
        assignment.enabled = False
        await db.commit()
        project_run_id = project_run.id

    child, created = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id=f"frozen-runtime-{uuid.uuid4()}",
        task="Review the frozen release evidence",
        mode="async",
        turn_anchor_id=anchor_id,
        project_run_id=project_run_id,
        input_metadata={"project_dispatch": True},
    )
    assert created is True
    async with async_session() as db:
        child_session = await db.get(ChatSession, child.id)
        child_run = await db.get(SubagentRun, child.id)
        runtime_config = dict(child_session.im_config or {})
    assert runtime_config["project_run_frozen"] is True
    assert runtime_config["member_config_snapshot"]["primary_model_id"] == str(frozen_model.id)
    assert runtime_config["member_config_snapshot"]["temperature"] == 0.2
    assert child_run.temperature == 0.2
    assert runtime_config["member_config_snapshot"]["reasoning_effort"] == "high"
    assert child_run.reasoning_effort == "high"
    assert runtime_config["member_config_snapshot"]["max_tool_rounds"] == 7
    assert "release checklist captured at run start" in build_project_runtime_context(runtime_config)
    tool_names = {
        item["function"]["name"]
        for item in await runtime.prepare_subagent_tools(
            agent_id,
            child.id,
            execution_user_id=user_id,
        )
    }
    assert tool_name in tool_names

    model_dispatches: list[dict] = []

    async def fake_provider_dispatch(**kwargs):
        model_dispatches.append(kwargs)
        return "Frozen model selected"

    monkeypatch.setattr("app.services.llm.call_llm_with_failover", fake_provider_dispatch)
    async with async_session() as db:
        child_session = await db.get(ChatSession, child.id)
        await channel_llm._call_agent_llm(
            db,
            agent_id,
            "Verify the saved release decision",
            session_id=str(child.id),
            user_id=user_id,
            prepared_tools=[],
            include_soul=False,
            include_memory=False,
            broadcast_web=False,
            runtime_session=child_session,
            max_tool_rounds_override=7,
        )
    assert model_dispatches[0]["primary_model"].id == frozen_model.id
    assert model_dispatches[0]["max_tool_rounds_override"] == 7

    invocations: list[dict] = []

    async def fake_tools(_agent_id, _session_id=None, execution_user_id=None):
        del _agent_id, _session_id, execution_user_id
        return []

    async def fake_llm(_db, _agent_id, _text, **kwargs):
        invocations.append(kwargs)
        return "结论：冻结配置已生效；依据是当前运行保存的发布检查清单。"

    monkeypatch.setattr(runtime, "prepare_subagent_tools", fake_tools)
    monkeypatch.setattr("app.services.channel_llm._call_agent_llm", fake_llm)
    assert await runtime._claim_subagent(child.id) == child.id
    await runtime.execute_claimed_subagent(child.id)
    assert invocations[0]["max_tool_rounds_override"] == 7
    assert invocations[0]["runtime_session"].im_config["member_config_snapshot"]["primary_model_id"] == str(
        frozen_model.id
    )


async def test_project_agent_tool_clone_groups_mcp_server_and_excludes_credentials_and_legacy_rows():
    source_agent_id, user_id, parent_id, _anchor_id = await _make_context(parent_channel="project", project=True)
    suffix = uuid.uuid4().hex[:10]
    async with async_session() as db:
        parent = await db.get(ChatSession, parent_id)
        project = await db.get(Project, parent.project_id)
        target_id = uuid.uuid4()
        target = Agent(
            id=target_id,
            name=f"Project copy {suffix}",
            creator_id=user_id,
            tenant_id=project.tenant_id,
            scope="project",
            project_id=project.id,
            source_agent_id=source_agent_id,
            agent_dir=f".agents/{target_id}",
            status="idle",
        )
        server = MCPServer(
            tenant_id=project.tenant_id,
            name=f"server-{suffix}",
            display_name="Evidence service",
            base_url_template="https://example.invalid/mcp",
            headers_template={},
        )
        db.add_all([target, server])
        await db.flush()
        mcp_tools = [
            Tool(
                name=f"mcp_{suffix}_{index}",
                display_name=f"Evidence query {index}",
                type="mcp",
                source="admin",
                tenant_id=project.tenant_id,
                mcp_server_id=server.id,
                enabled=True,
                parameters_schema={"type": "object", "properties": {}},
            )
            for index in range(2)
        ]
        legacy = Tool(
            name=f"legacy_mcp_{suffix}",
            display_name="Legacy MCP",
            type="mcp",
            source="admin",
            tenant_id=project.tenant_id,
            enabled=True,
            parameters_schema={"type": "object", "properties": {}},
        )
        db.add_all([*mcp_tools, legacy])
        await db.flush()
        db.add_all(
            [
                AgentTool(
                    agent_id=source_agent_id,
                    tool_id=tool.id,
                    enabled=True,
                    config={"secret": "must-not-copy"},
                )
                for tool in [*mcp_tools, legacy]
            ]
        )
        await db.flush()

        bindings = await clone_source_agent_tool_dependencies(
            db,
            project,
            source_agent_id=source_agent_id,
            project_agent_id=target.id,
        )
        target_assignments = (
            (
                await db.execute(select(AgentTool).where(AgentTool.agent_id == target.id))
            )
            .scalars()
            .all()
        )

    assert len(bindings) == 1
    assert bindings[0].capability_type == "mcp"
    assert bindings[0].capability_id == server.id
    assert bindings[0].capability_name == "Evidence service"
    assert {row.tool_id for row in target_assignments} == {tool.id for tool in mcp_tools}
    assert all(row.config == {} for row in target_assignments)


async def test_project_member_runtime_model_validation_preserves_tenant_boundary_and_global_catalog():
    agent_id, _user_id, parent_id, _anchor_id = await _make_context(parent_channel="project", project=True)
    suffix = uuid.uuid4().hex[:10]
    async with async_session() as db:
        parent = await db.get(ChatSession, parent_id)
        project = await db.get(Project, parent.project_id)
        member = (
            await db.execute(
                select(ProjectMemberSnapshot).where(
                    ProjectMemberSnapshot.project_id == project.id,
                    ProjectMemberSnapshot.agent_id == agent_id,
                )
            )
        ).scalar_one()
        other_tenant = Tenant(name=f"other-{suffix}", slug=f"other-{suffix}")
        db.add(other_tenant)
        await db.flush()
        other_model = LLMModel(
            tenant_id=other_tenant.id,
            provider="openai",
            model=f"Other-{suffix}",
            api_key_encrypted="unused",
            label="Other model",
            enabled=True,
        )
        global_model = LLMModel(
            tenant_id=None,
            provider="openai",
            model=f"Global-{suffix}",
            api_key_encrypted="unused",
            label="Global model",
            enabled=True,
        )
        db.add_all([other_model, global_model])
        await db.flush()

        with pytest.raises(HTTPException) as denied:
            await merge_project_member_runtime_config(
                db,
                project,
                member,
                {"primary_model_id": str(other_model.id)},
            )
        assert denied.value.status_code == 422
        merged = await merge_project_member_runtime_config(
            db,
            project,
            member,
            {
                "primary_model_id": str(global_model.id),
                "max_tool_rounds": 12,
                "project_instruction": "Use only tenant-visible evidence.",
            },
        )

    assert merged["primary_model_id"] == str(global_model.id)
    assert merged["max_tool_rounds"] == 12


@pytest.mark.parametrize(
    ("corrected_reply", "still_low_value"),
    (
        (
            "结论：暂缓发布。依据 run a1b2c3d4 的错误率为 8%，超过 2% 阈值；建议运维负责人先回滚。",
            False,
        ),
        ("已同步，继续推进。", True),
    ),
)
async def test_project_reply_quality_guard_rewrites_once_without_tools_or_broadcast(
    monkeypatch,
    corrected_reply: str,
    still_low_value: bool,
):
    agent_id, user_id, parent_id, anchor_id = await _make_context(
        parent_channel="project",
        project=True,
    )
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-project-quality-guard",
        task="Decide whether the release evidence supports production rollout",
        mode="async",
        turn_anchor_id=anchor_id,
    )
    assert await runtime._claim_subagent(run.id) == run.id
    invocations: list[dict] = []

    async def fake_tools(_agent_id, _session_id=None, execution_user_id=None):
        del _agent_id, _session_id, execution_user_id
        return [{"type": "function", "function": {"name": "read_evidence", "parameters": {}}}]

    async def fake_llm(_db, _agent_id, user_text, **kwargs):
        invocations.append({"user_text": user_text, **kwargs})
        if len(invocations) == 1:
            return "收到，我会继续推进。"
        assert kwargs["prepared_tools"] == []
        assert kwargs["broadcast_web"] is False
        assert kwargs.get("before_round") is None
        assert "single bounded self-correction" in user_text
        assert "Your immutable project role is: Assess release evidence and operational risk" in user_text
        assert "risk-based verification" in user_text
        assert kwargs["history"][-1] == {"role": "assistant", "content": "收到，我会继续推进。"}
        return corrected_reply

    monkeypatch.setattr(runtime, "prepare_subagent_tools", fake_tools)
    monkeypatch.setattr("app.services.channel_llm._call_agent_llm", fake_llm)

    await runtime.execute_claimed_subagent(run.id)

    assert len(invocations) == 2
    async with async_session() as db:
        final = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id == str(run.id),
                    ChatMessage.message_meta["kind"].as_string() == runtime.SUBAGENT_COMPLETION,
                )
            )
        ).scalar_one()
    assert final.content == corrected_reply
    assert final.message_meta["reply_quality"] == {
        "correction_attempted": True,
        "correction_applied": True,
        "initial_reasons": ["acknowledgement_only", "no_professional_substance"],
        "final_needs_correction": still_low_value,
    }


async def test_child_parent_message_tool_respects_standard_agent_tool_toggle(monkeypatch):
    agent_id, _, _, _ = await _make_context()
    # The suite must be self-contained on a fresh disposable database; do not
    # depend on another test process having seeded the built-in tools first.
    await seed_builtin_tools()

    async def normal_tools(_agent_id):
        assert _agent_id == agent_id
        return [
            {
                "type": "function",
                "function": {
                    "name": "ordinary_tool",
                    "description": "ordinary",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

    monkeypatch.setattr(
        "app.services.agent_tools.get_agent_tools_for_llm",
        normal_tools,
    )

    async with async_session() as db:
        tool = (await db.execute(select(Tool).where(Tool.name == "send_message_to_parent"))).scalar_one()
        tool_id = tool.id
        assignment = (
            await db.execute(
                select(AgentTool).where(
                    AgentTool.agent_id == agent_id,
                    AgentTool.tool_id == tool_id,
                )
            )
        ).scalar_one_or_none()
        if assignment is None:
            assignment = AgentTool(agent_id=agent_id, tool_id=tool_id, enabled=False)
            db.add(assignment)
        else:
            assignment.enabled = False
        await db.commit()

    # Startup seeding must preserve an explicit manual opt-out.
    await seed_builtin_tools()
    disabled_names = {item["function"]["name"] for item in await runtime.prepare_subagent_tools(agent_id)}
    assert disabled_names == {"ordinary_tool"}

    async with async_session() as db:
        assignment = (
            await db.execute(
                select(AgentTool).where(
                    AgentTool.agent_id == agent_id,
                    AgentTool.tool_id == tool_id,
                )
            )
        ).scalar_one()
        assignment.enabled = True
        await db.commit()

    enabled_names = {item["function"]["name"] for item in await runtime.prepare_subagent_tools(agent_id)}
    assert enabled_names == {"ordinary_tool", "send_message_to_parent"}


async def test_subagent_panel_group_toggle_updates_all_four_tools():
    agent_id, user_id, _, _ = await _make_context()
    await seed_builtin_tools()

    async with async_session() as db:
        user = await db.get(User, user_id)
        tools = (await db.execute(select(Tool).where(Tool.name.in_(SUBAGENT_TOOL_NAMES)))).scalars().all()
        assert {tool.name for tool in tools} == set(SUBAGENT_TOOL_NAMES)
        assert {tool.category for tool in tools} == {"subagent"}
        run_tool = next(tool for tool in tools if tool.name == "run_subagent")
        run_schema = run_tool.parameters_schema
        assert run_schema["required"] == ["name", "task"]
        assert run_schema["properties"]["soul"]["default"] is True
        assert run_schema["properties"]["memory"]["default"] is True
        assert "load_soul" not in run_schema["properties"]
        assert "load_memory" not in run_schema["properties"]

        await update_agent_tools(
            agent_id,
            [AgentToolUpdate(tool_id=str(run_tool.id), enabled=False)],
            current_user=user,
            db=db,
        )
        disabled = (
            (
                await db.execute(
                    select(AgentTool).where(
                        AgentTool.agent_id == agent_id,
                        AgentTool.tool_id.in_([tool.id for tool in tools]),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(disabled) == 4
        assert all(assignment.enabled is False for assignment in disabled)

        await update_agent_tools(
            agent_id,
            [AgentToolUpdate(tool_id=str(run_tool.id), enabled=True)],
            current_user=user,
            db=db,
        )
        enabled = (
            (
                await db.execute(
                    select(AgentTool).where(
                        AgentTool.agent_id == agent_id,
                        AgentTool.tool_id.in_([tool.id for tool in tools]),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(enabled) == 4
        assert all(assignment.enabled is True for assignment in enabled)


async def test_standard_agent_project_tool_group_reports_disabled_partial_enabled():
    agent_id, user_id, _, _ = await _make_context()
    await seed_builtin_tools()

    async with async_session() as db:
        user = await db.get(User, user_id)
        project_tools = (
            (
                await db.execute(
                    select(Tool).where(Tool.name.in_(USER_PROJECT_TOOL_NAMES))
                )
            )
            .scalars()
            .all()
        )
        assert {tool.name for tool in project_tools} == set(USER_PROJECT_TOOL_NAMES)
        assert {tool.category for tool in project_tools} == {"project_management"}

        async def assert_group_contract(state: str, enabled_count: int) -> None:
            response = await get_agent_tools_with_config(
                agent_id,
                current_user=user,
                db=db,
            )
            group = [
                tool for tool in response if tool["category"] == "project_management"
            ]
            expected = {
                "key": "project_management",
                "state": state,
                "member_count": len(USER_PROJECT_TOOL_NAMES),
                "enabled_count": enabled_count,
                "complete": True,
            }
            assert {tool["name"] for tool in group} == set(USER_PROJECT_TOOL_NAMES)
            assert all(tool["capability_group"] == expected for tool in group)
            llm_project_tools = {
                item["function"]["name"]
                for item in await get_agent_tools_for_llm(agent_id)
                if item["function"]["name"] in USER_PROJECT_TOOL_NAMES
            }
            assert len(llm_project_tools) == enabled_count
            assert llm_project_tools <= set(USER_PROJECT_TOOL_NAMES)

        await assert_group_contract("disabled", 0)

        first_tool = project_tools[0]
        db.add(AgentTool(agent_id=agent_id, tool_id=first_tool.id, enabled=True))
        await db.commit()
        await assert_group_contract("partial", 1)

        await update_agent_tools(
            agent_id,
            [AgentToolUpdate(tool_id=str(first_tool.id), enabled=True)],
            current_user=user,
            db=db,
        )
        await assert_group_contract("enabled", len(USER_PROJECT_TOOL_NAMES))

        expected_descriptions = {
            seed["name"]: seed["description"] for seed in USER_PROJECT_TOOL_SEEDS
        }
        persisted_descriptions = {
            tool.name: tool.description
            for tool in (
                (
                    await db.execute(
                        select(Tool).where(Tool.name.in_(USER_PROJECT_TOOL_NAMES))
                    )
                )
                .scalars()
                .all()
            )
        }
        assert persisted_descriptions == expected_descriptions

    llm_tools = await get_agent_tools_for_llm(agent_id)
    llm_descriptions = {
        tool["function"]["name"]: tool["function"]["description"]
        for tool in llm_tools
        if tool["function"]["name"] in USER_PROJECT_TOOL_NAMES
    }
    assert llm_descriptions == expected_descriptions
