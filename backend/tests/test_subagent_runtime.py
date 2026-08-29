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
        runtime_config = dict(child_session.im_config or {})
    assert runtime_config["project_run_frozen"] is True
    assert runtime_config["member_config_snapshot"]["primary_model_id"] == str(frozen_model.id)
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
        target = Agent(
            name=f"Project copy {suffix}",
            creator_id=user_id,
            tenant_id=project.tenant_id,
            scope="project",
            project_id=project.id,
            agent_dir=f".agents/{suffix}",
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


async def test_create_uses_one_id_readable_model_and_idempotent_fork():
    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, created = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-one",
        name="Research helper",
        task="focused child task",
        mode="async",
        model="  readable-test-model ",
        fork=True,
        soul=False,
        memory=False,
        turn_anchor_id=anchor_id,
    )
    replay, replay_created = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-one",
        name="ignored replay name",
        task="ignored replay text",
        mode="sync",
    )

    assert created is True
    assert replay_created is False
    assert replay.id == run.id
    assert run.model == "Readable-Test-Model"
    async with async_session() as db:
        child = await db.get(ChatSession, run.id)
        lifecycle = await db.get(SubagentRun, run.id)
        rows = (
            (
                await db.execute(
                    select(ChatMessage)
                    .where(ChatMessage.conversation_id == str(run.id))
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                )
            )
            .scalars()
            .all()
        )
    assert child is not None and child.id == lifecycle.id == run.id
    assert child.source_channel == "subagent"
    assert child.title == "Research helper"
    assert lifecycle.soul is False
    assert lifecycle.memory is False
    assert [row.content for row in rows][-3:] == [
        "earlier user context",
        "earlier context",
        "focused child task",
    ]
    assert all(row.content != "parent request" for row in rows)
    assert rows[-1].message_meta["subagent_input_state"] == "pending"


async def test_standard_subagent_claim_does_not_enter_project_query_path(monkeypatch):
    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _created = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="standard-claim-project-isolation",
        task="keep standard child work available",
        mode="async",
        turn_anchor_id=anchor_id,
    )

    def broken_project_exists(*_args, **_kwargs):
        raise RuntimeError("projects relation unavailable")

    monkeypatch.setattr(runtime, "exists", broken_project_exists)

    assert await runtime._claim_subagent(run.id) == run.id


async def test_round_boundary_drains_append_and_stop_wins():
    from app.services.conversation_turn_lifecycle import (
        get_conversation_turn_snapshot,
    )

    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-inbox",
        task="first",
        mode="async",
        turn_anchor_id=anchor_id,
    )
    assert await runtime._claim_subagent(run.id) == run.id
    claimed = await runtime._load_or_start_input(run.id)
    assert claimed is not None
    anchor, recovering = claimed
    assert recovering is False
    await runtime.append_subagent_message(
        agent_id=agent_id,
        parent_session_id=str(parent_id),
        subagent_id=str(run.id),
        message="second",
        execution_user_id=user_id,
        origin_tool_call_id="append-second",
    )
    # Recovery replays a still-running tool with the same provider call id.
    # The already-committed inbox side effect must remain exactly once.
    await runtime.append_subagent_message(
        agent_id=agent_id,
        parent_session_id=str(parent_id),
        subagent_id=str(run.id),
        message="second",
        execution_user_id=user_id,
        origin_tool_call_id="append-second",
    )
    assert await runtime._drain_subagent_inbox(run.id, anchor.id) == [{"role": "user", "content": "second"}]
    assert (
        await runtime.stop_subagent(
            agent_id=agent_id,
            parent_session_id=str(parent_id),
            subagent_id=str(run.id),
            execution_user_id=user_id,
        )
        == "cancelled"
    )
    assert not await runtime._finish_subagent_turn(
        run_id=run.id,
        anchor_id=anchor.id,
        reply="stale final",
        failed=False,
    )
    async with async_session() as db:
        fresh = await db.get(SubagentRun, run.id)
        stale_final = (
            await db.execute(
                select(ChatMessage.id).where(
                    ChatMessage.conversation_id == str(run.id),
                    ChatMessage.content == "stale final",
                )
            )
        ).scalar_one_or_none()
        current_turn = await get_conversation_turn_snapshot(
            db,
            agent_id=agent_id,
            conversation_id=str(run.id),
        )
    assert fresh.status == "cancelled"
    assert stale_final is None
    assert current_turn.anchor_id == anchor.id
    assert current_turn.status == "cancelled"
    assert current_turn.revision >= 2


async def test_control_plane_cancel_is_terminal_not_requeued(monkeypatch):
    from app.services import channel_llm
    from app.services.active_turns import (
        cancel_active_turn,
        ensure_active_turn,
        list_active_turns,
        reset_active_turns_for_testing,
    )
    from app.services.conversation_turn_lifecycle import (
        get_conversation_turn_snapshot,
    )

    await reset_active_turns_for_testing()

    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-control-cancel",
        task="long child task",
        mode="async",
        turn_anchor_id=anchor_id,
    )
    assert await runtime._claim_subagent(run.id) == run.id
    ready = asyncio.Event()

    async def fake_call(*args, **kwargs):
        await ensure_active_turn(
            owner_user_id=user_id,
            agent_id=agent_id,
            session_id=str(run.id),
            turn_type="subagent",
            turn_anchor_id=kwargs["turn_anchor_id"],
        )
        ready.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(channel_llm, "_call_agent_llm", fake_call)
    worker = asyncio.create_task(runtime.execute_claimed_subagent(run.id))
    await ready.wait()
    record = (await list_active_turns(owner_user_id=user_id))[0]
    await cancel_active_turn(record.turn_id, owner_user_id=user_id)
    with pytest.raises(asyncio.CancelledError):
        await worker

    async with async_session() as db:
        fresh = await db.get(SubagentRun, run.id)
        input_rows = (
            (
                await db.execute(
                    select(ChatMessage).where(
                        ChatMessage.conversation_id == str(run.id),
                        ChatMessage.message_meta["kind"].as_string() == runtime.SUBAGENT_INPUT,
                    )
                )
            )
            .scalars()
            .all()
        )
        current_turn = await get_conversation_turn_snapshot(
            db,
            agent_id=agent_id,
            conversation_id=str(run.id),
        )
    assert fresh.status == runtime.RUN_CANCELLED
    assert all(row.message_meta["subagent_input_state"] == runtime.INPUT_CANCELLED for row in input_rows)
    assert current_turn.anchor_id == input_rows[0].id
    assert current_turn.status == "cancelled"
    assert current_turn.revision >= 2
    await reset_active_turns_for_testing()


async def test_fresh_turn_uses_exact_prefix_when_later_input_is_already_queued(
    monkeypatch,
):
    from app.services.conversation_turn_lifecycle import (
        get_conversation_turn_snapshot,
    )

    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-concurrent-prefix",
        task="first queued project input",
        mode="async",
        turn_anchor_id=anchor_id,
    )
    await runtime.append_subagent_message(
        agent_id=agent_id,
        parent_session_id=str(parent_id),
        subagent_id=str(run.id),
        message="later queued project input",
        execution_user_id=user_id,
        origin_tool_call_id="append-concurrent-prefix",
    )
    assert await runtime._claim_subagent(run.id) == run.id

    captured: dict = {}

    async def fake_tools(_agent_id, _session_id=None, execution_user_id=None):
        del _agent_id, _session_id, execution_user_id
        return []

    async def fake_llm(_db, _agent_id, user_text, **kwargs):
        captured["user_text"] = user_text
        captured["history"] = list(kwargs["history"])
        captured["inbox"] = await kwargs["before_round"](0)
        return "handled queued project inputs"

    monkeypatch.setattr(runtime, "prepare_subagent_tools", fake_tools)
    monkeypatch.setattr("app.services.channel_llm._call_agent_llm", fake_llm)

    await runtime.execute_claimed_subagent(run.id)

    assert captured["user_text"] == "first queued project input"
    assert all(row.get("content") != "later queued project input" for row in captured["history"])
    assert captured["inbox"] == [{"role": "user", "content": "later queued project input"}]
    async with async_session() as db:
        fresh = await db.get(SubagentRun, run.id)
        inputs = (
            (
                await db.execute(
                    select(ChatMessage)
                    .where(
                        ChatMessage.conversation_id == str(run.id),
                        ChatMessage.message_meta["kind"].as_string() == runtime.SUBAGENT_INPUT,
                    )
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                )
            )
            .scalars()
            .all()
        )
        current_turn = await get_conversation_turn_snapshot(
            db,
            agent_id=agent_id,
            conversation_id=str(run.id),
        )
    assert fresh.status == runtime.RUN_COMPLETED
    assert [row.message_meta["subagent_input_state"] for row in inputs] == [
        runtime.INPUT_DONE,
        runtime.INPUT_DONE,
    ]
    assert current_turn.anchor_id == inputs[0].id
    assert current_turn.status == "completed"
    assert current_turn.revision >= 2


async def test_provider_round_releases_preflight_database_transaction(
    monkeypatch,
):
    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-release-provider-connection",
        task="perform one long provider request",
        mode="async",
        turn_anchor_id=anchor_id,
    )
    assert await runtime._claim_subagent(run.id) == run.id
    transaction_state: list[bool] = []

    async def fake_tools(_agent_id, _session_id=None, execution_user_id=None):
        del _agent_id, _session_id, execution_user_id
        return []

    async def fake_llm(db, _agent_id, _user_text, **kwargs):
        # Mirror the unified channel preflight, which resolves Agent/model/scene
        # records before the first provider round.
        await db.execute(select(ChatSession.id).where(ChatSession.id == run.id))
        transaction_state.append(bool(db.in_transaction()))
        await kwargs["before_round"](0)
        transaction_state.append(bool(db.in_transaction()))
        return "provider request completed"

    monkeypatch.setattr(runtime, "prepare_subagent_tools", fake_tools)
    monkeypatch.setattr("app.services.channel_llm._call_agent_llm", fake_llm)

    await runtime.execute_claimed_subagent(run.id)

    assert transaction_state == [True, False]
    async with async_session() as db:
        fresh = await db.get(SubagentRun, run.id)
    assert fresh.status == runtime.RUN_COMPLETED


async def test_project_capacity_wait_does_not_retain_database_session(
    monkeypatch,
):
    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-project-capacity",
        task="wait for project capacity",
        mode="async",
        turn_anchor_id=anchor_id,
    )
    assert await runtime._claim_subagent(run.id) == run.id
    async with async_session() as db:
        tenant_id = (await db.get(Agent, agent_id)).tenant_id

    capacity = WorkloadCapacity(
        global_limit=1,
        tenant_limit=1,
        category_limits={kind: 1 for kind in WorkloadKind},
        default_timeout_seconds=5,
        instance_id="subagent-capacity-test",
    )
    blocker = await capacity.acquire(WorkloadKind.PROJECT, tenant_id)
    monkeypatch.setattr(runtime, "get_workload_capacity", lambda: capacity)

    real_async_session = runtime.async_session
    open_sessions: set[int] = set()

    @asynccontextmanager
    async def tracking_session():
        async with real_async_session() as db:
            marker = id(db)
            open_sessions.add(marker)
            try:
                yield db
            finally:
                open_sessions.discard(marker)

    async def fake_tools(_agent_id, _session_id=None, execution_user_id=None):
        del _agent_id, _session_id, execution_user_id
        return []

    async def fake_llm(_db, _agent_id, _user_text, **kwargs):
        snapshot = await capacity.snapshot()
        assert snapshot.categories[WorkloadKind.PROJECT.value].active == 1
        assert snapshot.tenants[str(tenant_id)].active == 1
        await kwargs["before_round"](0)
        return "project capacity admitted"

    monkeypatch.setattr(runtime, "async_session", tracking_session)
    monkeypatch.setattr(runtime, "prepare_subagent_tools", fake_tools)
    monkeypatch.setattr("app.services.channel_llm._call_agent_llm", fake_llm)

    worker = asyncio.create_task(runtime.execute_claimed_subagent(run.id))
    try:
        for _ in range(100):
            snapshot = await capacity.snapshot()
            if snapshot.categories[WorkloadKind.PROJECT.value].waiting == 1:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("subagent did not wait for project capacity")

        assert not open_sessions
        await blocker.release()
        await worker
    finally:
        await blocker.release()
        if not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    snapshot = await capacity.snapshot()
    project_metrics = snapshot.categories[WorkloadKind.PROJECT.value]
    tenant_metrics = snapshot.tenants[str(tenant_id)]
    assert project_metrics.admitted_total == 2
    assert project_metrics.completed_total == 2
    assert tenant_metrics.admitted_total == 2
    assert tenant_metrics.completed_total == 2


async def test_project_capacity_timeout_requeues_claimed_turn(monkeypatch):
    from app.services.conversation_turn_lifecycle import (
        get_conversation_turn_snapshot,
    )

    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-project-capacity-timeout",
        task="retry after project admission timeout",
        mode="async",
        turn_anchor_id=anchor_id,
    )
    assert await runtime._claim_subagent(run.id) == run.id
    async with async_session() as db:
        tenant_id = (await db.get(Agent, agent_id)).tenant_id

    capacity = WorkloadCapacity(
        global_limit=1,
        tenant_limit=1,
        category_limits={kind: 1 for kind in WorkloadKind},
        default_timeout_seconds=0.01,
        instance_id="subagent-capacity-timeout-test",
    )
    blocker = await capacity.acquire(WorkloadKind.PROJECT, tenant_id)
    monkeypatch.setattr(runtime, "get_workload_capacity", lambda: capacity)
    try:
        await runtime.execute_claimed_subagent(run.id)
    finally:
        await blocker.release()

    async with async_session() as db:
        fresh = await db.get(SubagentRun, run.id)
        input_row = await db.scalar(
            select(ChatMessage).where(
                ChatMessage.conversation_id == str(run.id),
                ChatMessage.message_meta["kind"].as_string() == runtime.SUBAGENT_INPUT,
            )
        )
        current_turn = await get_conversation_turn_snapshot(
            db,
            agent_id=agent_id,
            conversation_id=str(run.id),
        )
        exact_turn = await get_conversation_turn_snapshot(
            db,
            agent_id=agent_id,
            conversation_id=str(run.id),
            turn_anchor_id=input_row.id,
        )
    assert fresh.status == runtime.RUN_QUEUED
    assert fresh.lease_owner is None
    assert input_row.message_meta["subagent_input_state"] == runtime.INPUT_PENDING
    assert current_turn == exact_turn
    assert current_turn.anchor_id == input_row.id
    assert current_turn.status == "running"
    snapshot = await capacity.snapshot()
    assert snapshot.categories[WorkloadKind.PROJECT.value].rejected_total == 1
    assert snapshot.tenants[str(tenant_id)].rejected_total == 1


async def test_failed_turn_continues_when_parent_input_is_pending():
    from app.services.conversation_turn_lifecycle import (
        get_conversation_turn_snapshot,
    )

    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-failed-pending",
        task="first",
        mode="async",
        turn_anchor_id=anchor_id,
    )
    assert await runtime._claim_subagent(run.id) == run.id
    claimed = await runtime._load_or_start_input(run.id)
    assert claimed is not None
    first_anchor, recovering = claimed
    assert recovering is False
    await runtime.append_subagent_message(
        agent_id=agent_id,
        parent_session_id=str(parent_id),
        subagent_id=str(run.id),
        message="continue despite failure",
        execution_user_id=user_id,
        origin_tool_call_id="append-after-failure",
    )

    assert not await runtime._finish_subagent_turn(
        run_id=run.id,
        anchor_id=first_anchor.id,
        reply="[LLM Error] first turn failed",
        failed=True,
    )
    next_claim = await runtime._load_or_start_input(run.id)
    assert next_claim is not None
    next_anchor, next_recovering = next_claim
    assert next_recovering is False
    assert next_anchor.content == "continue despite failure"

    async with async_session() as db:
        fresh = await db.get(SubagentRun, run.id)
        first = await db.get(ChatMessage, first_anchor.id)
        current_turn = await get_conversation_turn_snapshot(
            db,
            agent_id=agent_id,
            conversation_id=str(run.id),
        )
    assert fresh.status == "running"
    assert first.message_meta["subagent_input_state"] == "done"
    assert first.message_meta["turn_status"] == "failed"
    assert current_turn.anchor_id == next_anchor.id
    assert current_turn.status == "running"
    assert current_turn.generation > int(first.message_meta["turn_generation"])


async def test_subagent_finish_waits_for_reserved_stop_before_mutating_anchor():
    from app.services.active_turns import (
        ensure_active_turn,
        finalize_active_turn_stop,
        list_active_turns,
        reserve_active_turn_stop,
        reset_active_turns_for_testing,
    )
    from app.services.chat_history import mark_turn_cancelled

    await reset_active_turns_for_testing()
    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-stop-finish-race",
        task="must stop cleanly",
        mode="async",
        turn_anchor_id=anchor_id,
    )
    assert await runtime._claim_subagent(run.id) == run.id
    claimed = await runtime._load_or_start_input(run.id)
    assert claimed is not None
    turn_anchor, _ = claimed
    registered = asyncio.Event()
    finish_now = asyncio.Event()

    async def finishing_turn():
        await ensure_active_turn(
            owner_user_id=user_id,
            agent_id=agent_id,
            session_id=str(run.id),
            turn_type="subagent",
            turn_anchor_id=turn_anchor.id,
        )
        registered.set()
        await finish_now.wait()
        await runtime._finish_subagent_turn(
            run_id=run.id,
            anchor_id=turn_anchor.id,
            reply="must not be committed",
            failed=False,
        )

    worker = asyncio.create_task(finishing_turn())
    await registered.wait()
    record = (await list_active_turns(owner_user_id=user_id))[0]
    _, stop_token = await reserve_active_turn_stop(
        record.turn_id,
        owner_user_id=user_id,
    )
    assert stop_token is not None

    finish_now.set()
    await asyncio.sleep(0)
    assert not worker.done()
    async with async_session() as db:
        assert (
            await mark_turn_cancelled(
                db,
                agent_id=agent_id,
                conversation_id=str(run.id),
                turn_anchor_id=turn_anchor.id,
                reason="test control-plane stop",
            )
            == turn_anchor.id
        )
        await db.commit()
    await finalize_active_turn_stop(record, stop_token)
    with pytest.raises(asyncio.CancelledError):
        await worker

    async with async_session() as db:
        fresh_anchor = await db.get(ChatMessage, turn_anchor.id)
        terminal_reply = await db.scalar(
            select(ChatMessage.id).where(
                ChatMessage.conversation_id == str(run.id),
                ChatMessage.role == "assistant",
                ChatMessage.message_meta["turn_anchor_id"].as_string() == str(turn_anchor.id),
                ChatMessage.message_meta["turn_status"].as_string() == "completed",
            )
        )
    assert fresh_anchor.message_meta["turn_status"] == "cancelled"
    assert fresh_anchor.message_meta["subagent_input_state"] == "processing"
    assert terminal_reply is None
    await reset_active_turns_for_testing()


async def test_unexpected_failure_requeues_pending_input_without_lease_delay(
    monkeypatch,
):
    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-exception-pending",
        task="first",
        mode="async",
        turn_anchor_id=anchor_id,
    )
    assert await runtime._claim_subagent(run.id) == run.id
    await runtime.append_subagent_message(
        agent_id=agent_id,
        parent_session_id=str(parent_id),
        subagent_id=str(run.id),
        message="must run immediately after exception",
        execution_user_id=user_id,
        origin_tool_call_id="append-after-exception",
    )

    async def fake_tools(_agent_id, _session_id=None, execution_user_id=None):
        del execution_user_id
        return []

    async def failing_llm(*_args, **_kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(runtime, "prepare_subagent_tools", fake_tools)
    monkeypatch.setattr("app.services.channel_llm._call_agent_llm", failing_llm)

    await runtime.execute_claimed_subagent(run.id)

    async with async_session() as db:
        fresh = await db.get(SubagentRun, run.id)
        pending = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id == str(run.id),
                    ChatMessage.content == "must run immediately after exception",
                )
            )
        ).scalar_one()
    assert fresh.status == "queued"
    assert fresh.lease_owner is None
    assert fresh.lease_expires_at is None
    assert pending.message_meta["subagent_input_state"] == "pending"


async def test_parent_wake_remains_busy_after_it_emits_tool_rows():
    agent_id, user_id, parent_id, _anchor_id = await _make_context()
    wake_id = uuid.uuid4()
    now = datetime.now(UTC)
    async with async_session() as db:
        db.add(
            ChatMessage(
                id=wake_id,
                agent_id=agent_id,
                user_id=user_id,
                sender_agent_id=agent_id,
                role="user",
                content="durable subagent wake",
                conversation_id=str(parent_id),
                external_event_key=f"subagent-parent:{uuid.uuid4()}",
                message_meta={
                    "kind": "subagent_event",
                    "turn_status": "running",
                    "attachments": [],
                },
                created_at=now,
            )
        )
        db.add(
            ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                sender_agent_id=agent_id,
                role="tool_call",
                content='{"name":"example","status":"running"}',
                conversation_id=str(parent_id),
                message_meta={
                    "turn_anchor_id": str(wake_id),
                    "attachments": [],
                },
                created_at=now + timedelta(microseconds=1),
            )
        )
        await db.commit()

    async with async_session() as db:
        assert await _has_active_subagent_event_turn(db, str(parent_id))
        db.add(
            ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                sender_agent_id=agent_id,
                role="assistant",
                content="wake completed",
                conversation_id=str(parent_id),
                message_meta={
                    "turn_anchor_id": str(wake_id),
                    "turn_status": "completed",
                    "attachments": [],
                },
                created_at=now + timedelta(microseconds=2),
            )
        )
        await db.commit()
        assert not await _has_active_subagent_event_turn(db, str(parent_id))


async def test_processing_reclaim_continues_durable_done_tool_tail(monkeypatch):
    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-reclaim",
        task="recover me",
        mode="async",
        turn_anchor_id=anchor_id,
    )
    assert await runtime._claim_subagent(run.id) == run.id
    claimed = await runtime._load_or_start_input(run.id)
    assert claimed is not None
    turn_anchor, recovering = claimed
    assert recovering is False
    async with async_session() as db:
        db.add(
            ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                role="tool_call",
                content=json.dumps(
                    {
                        "name": "write_file",
                        "call_id": "stable-call-id",
                        "args": {"path": "marker.txt"},
                        "status": "done",
                        "result": "already written",
                    }
                ),
                conversation_id=str(run.id),
                message_meta={"turn_anchor_id": str(turn_anchor.id)},
            )
        )
        await db.commit()

    captured = {}

    async def fake_tools(_agent_id, _session_id=None, execution_user_id=None):
        del execution_user_id
        return []

    async def fake_llm(_db, _agent_id, user_text, **kwargs):
        captured["user_text"] = user_text
        captured.update(kwargs)
        return "recovered result"

    monkeypatch.setattr(runtime, "prepare_subagent_tools", fake_tools)
    monkeypatch.setattr("app.services.channel_llm._call_agent_llm", fake_llm)

    await runtime.execute_claimed_subagent(run.id)

    assert captured["user_text"] == ""
    assert captured["continue_turn"] is True
    assert captured["recovery_mode"] is True
    assert any(row.get("role") == "tool" for row in captured["history"])
    async with async_session() as db:
        fresh = await db.get(SubagentRun, run.id)
    assert fresh.status == "completed"


async def test_async_parent_event_is_deduplicated_and_keeps_execution_agent(monkeypatch):
    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-wake",
        task="child",
        mode="async",
        turn_anchor_id=anchor_id,
    )
    await runtime.send_subagent_message_to_parent(
        agent_id=agent_id,
        execution_user_id=user_id,
        origin_tool_call_id="child-message-one",
        subagent_session_id=str(run.id),
        message="interim",
    )
    await runtime.send_subagent_message_to_parent(
        agent_id=agent_id,
        execution_user_id=user_id,
        origin_tool_call_id="child-message-one",
        subagent_session_id=str(run.id),
        message="interim replay",
    )
    async with async_session() as db:
        db.add(
            ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                role="assistant",
                content="parent idle",
                conversation_id=str(parent_id),
                message_meta={"attachments": []},
            )
        )
        await db.commit()
        child_event = (
            await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.conversation_id == str(run.id),
                    ChatMessage.message_meta["kind"].as_string() == runtime.SUBAGENT_PARENT_MESSAGE,
                )
                .order_by(ChatMessage.created_at.desc())
                .limit(1)
            )
        ).scalar_one()

    captured = []
    dispatch_kwargs = []

    async def fake_resume(anchor):
        captured.append(anchor)
        return True

    async def fake_run_channel_message(_lock_key, *, work, **kwargs):
        dispatch_kwargs.append(kwargs)
        return await work()

    monkeypatch.setattr("app.services.turn_recovery.resume_turn", fake_resume)
    monkeypatch.setattr(
        "app.services.channel_dispatch.run_channel_message",
        fake_run_channel_message,
    )
    assert await runtime._dispatch_parent_event(child_event.id)
    assert await runtime._dispatch_parent_event(child_event.id)

    async with async_session() as db:
        anchors = (
            (
                await db.execute(
                    select(ChatMessage).where(ChatMessage.external_event_key == f"subagent-parent:{child_event.id}")
                )
            )
            .scalars()
            .all()
        )
    assert len(anchors) == 1
    assert anchors[0].agent_id == agent_id
    assert anchors[0].sender_agent_id == agent_id
    assert anchors[0].message_meta["execution_agent_id"] == str(agent_id)
    assert captured
    assert dispatch_kwargs
    assert all(kwargs["workload_kind"] is WorkloadKind.PROJECT for kwargs in dispatch_kwargs)
    assert all(kwargs["tenant_id"] != f"web:{parent_id}" for kwargs in dispatch_kwargs)


async def test_idle_parent_batches_multiple_subagent_events_into_one_resume(monkeypatch):
    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-batched-parent-wake",
        task="child",
        mode="async",
        turn_anchor_id=anchor_id,
    )
    for index in range(3):
        await runtime.send_subagent_message_to_parent(
            agent_id=agent_id,
            execution_user_id=user_id,
            origin_tool_call_id=f"batched-child-message-{index}",
            subagent_session_id=str(run.id),
            message=f"result {index}",
        )
    async with async_session() as db:
        db.add(
            ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                role="assistant",
                content="parent idle",
                conversation_id=str(parent_id),
                message_meta={"attachments": []},
            )
        )
        await db.commit()
        events = (
            (
                await db.execute(
                    select(ChatMessage)
                    .where(
                        ChatMessage.conversation_id == str(run.id),
                        ChatMessage.message_meta["kind"].as_string()
                        == runtime.SUBAGENT_PARENT_MESSAGE,
                    )
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                )
            )
            .scalars()
            .all()
        )

    resumed = []

    async def fake_resume(batch_anchor):
        resumed.append(batch_anchor.id)
        async with async_session() as db:
            db.add(
                ChatMessage(
                    agent_id=agent_id,
                    user_id=user_id,
                    role="assistant",
                    content="one merged parent response",
                    conversation_id=str(parent_id),
                    message_meta={
                        "turn_anchor_id": str(batch_anchor.id),
                        "turn_status": "completed",
                        "attachments": [],
                    },
                )
            )
            await db.commit()
        return True

    async def fake_run_channel_message(_lock_key, *, work, **_kwargs):
        return await work()

    monkeypatch.setattr("app.services.turn_recovery.resume_turn", fake_resume)
    monkeypatch.setattr(
        "app.services.channel_dispatch.run_channel_message",
        fake_run_channel_message,
    )
    batches, special_events = await runtime._pending_parent_event_groups(
        [event.id for event in events]
    )
    assert special_events == []
    assert batches == [[event.id for event in events]]
    assert await runtime._dispatch_parent_event_batch(batches[0])
    assert len(resumed) == 1

    async with async_session() as db:
        projections = (
            (
                await db.execute(
                    select(ChatMessage)
                    .where(
                        ChatMessage.external_event_key.in_(
                            [f"subagent-parent:{event.id}" for event in events]
                        )
                    )
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                )
            )
            .scalars()
            .all()
        )
    assert len(projections) == 3
    assert projections[0].id == resumed[0]
    assert projections[0].message_meta["subagent_event_batch"] is True
    assert all(
        row.message_meta["subagent_turn_anchor_id"] == str(resumed[0])
        for row in projections[1:]
    )
    pending = await runtime._pending_parent_events(debounce_seconds=0)
    assert not {event.id for event in events}.intersection(pending)

    async with async_session() as db:
        stored_events = (
            (
                await db.execute(
                    select(ChatMessage).where(
                        ChatMessage.id.in_([event.id for event in events])
                    )
                )
            )
            .scalars()
            .all()
        )
    assert {
        row.message_meta["subagent_dispatch_state"] for row in stored_events
    } == {runtime.SUBAGENT_DISPATCH_DELIVERED}


async def test_parent_event_retries_without_duplicate_projection_when_resume_is_busy(monkeypatch):
    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-parent-retry",
        task="child",
        mode="async",
        turn_anchor_id=anchor_id,
    )
    await runtime.send_subagent_message_to_parent(
        agent_id=agent_id,
        execution_user_id=user_id,
        origin_tool_call_id="parent-retry-message",
        subagent_session_id=str(run.id),
        message="retry me",
    )
    async with async_session() as db:
        db.add(
            ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                role="assistant",
                content="parent idle",
                conversation_id=str(parent_id),
                message_meta={"attachments": []},
            )
        )
        await db.commit()
        event = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id == str(run.id),
                    ChatMessage.message_meta["kind"].as_string()
                    == runtime.SUBAGENT_PARENT_MESSAGE,
                )
            )
        ).scalar_one()

    resume_calls = 0

    async def fake_resume(batch_anchor):
        nonlocal resume_calls
        resume_calls += 1
        if resume_calls == 1:
            return False
        async with async_session() as db:
            db.add(
                ChatMessage(
                    agent_id=agent_id,
                    user_id=user_id,
                    role="assistant",
                    content="recovered",
                    conversation_id=str(parent_id),
                    message_meta={
                        "turn_anchor_id": str(batch_anchor.id),
                        "turn_status": "completed",
                        "attachments": [],
                    },
                )
            )
            await db.commit()
        return True

    async def fake_run_channel_message(_lock_key, *, work, **_kwargs):
        return await work()

    monkeypatch.setattr("app.services.turn_recovery.resume_turn", fake_resume)
    monkeypatch.setattr(
        "app.services.channel_dispatch.run_channel_message",
        fake_run_channel_message,
    )

    assert await runtime._dispatch_parent_event_batch([event.id]) is False
    async with async_session() as db:
        pending_event = await db.get(ChatMessage, event.id)
        projection_count = await db.scalar(
            select(func.count(ChatMessage.id)).where(
                ChatMessage.external_event_key == f"subagent-parent:{event.id}"
            )
        )
    assert pending_event.message_meta["subagent_dispatch_state"] == runtime.SUBAGENT_DISPATCH_PENDING
    assert projection_count == 1

    assert await runtime._dispatch_parent_event_batch([event.id]) is True
    async with async_session() as db:
        delivered_event = await db.get(ChatMessage, event.id)
        projection_count = await db.scalar(
            select(func.count(ChatMessage.id)).where(
                ChatMessage.external_event_key == f"subagent-parent:{event.id}"
            )
        )
    assert delivered_event.message_meta["subagent_dispatch_state"] == runtime.SUBAGENT_DISPATCH_DELIVERED
    assert projection_count == 1
    assert resume_calls == 2


async def test_idle_dispatch_repairs_legacy_projection_without_durable_owner():
    from app.services.conversation_turn_lifecycle import (
        conversation_turn_snapshot_for_session,
    )

    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-repair-unowned-projection",
        task="child",
        mode="async",
        turn_anchor_id=anchor_id,
    )
    for index in range(2):
        await runtime.send_subagent_message_to_parent(
            agent_id=agent_id,
            execution_user_id=user_id,
            origin_tool_call_id=f"repair-unowned-projection-result-{index}",
            subagent_session_id=str(run.id),
            message=f"repair me {index}",
        )
    async with async_session() as db:
        db.add(
            ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                role="assistant",
                content="parent idle",
                conversation_id=str(parent_id),
                message_meta={"attachments": []},
            )
        )
        await db.flush()
        events = list(
            (
                await db.execute(
                    select(ChatMessage)
                    .where(
                    ChatMessage.conversation_id == str(run.id),
                    ChatMessage.message_meta["kind"].as_string()
                    == runtime.SUBAGENT_PARENT_MESSAGE,
                )
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                )
            ).scalars()
        )
        stale_root = ChatMessage(
            agent_id=agent_id,
            user_id=user_id,
            sender_agent_id=agent_id,
            role="user",
            content="legacy projection without admission",
            conversation_id=str(parent_id),
            external_event_key=f"subagent-parent:{events[0].id}",
            message_meta={
                "kind": runtime.SUBAGENT_PARENT_EVENT,
                "execution_agent_id": str(agent_id),
                "subagent_id": str(run.id),
                "child_message_id": str(events[0].id),
                "turn_status": "running",
                "subagent_event_batch": True,
                "attachments": [],
            },
        )
        db.add(stale_root)
        await db.flush()
        stale_child = ChatMessage(
            agent_id=agent_id,
            user_id=user_id,
            sender_agent_id=agent_id,
            role="user",
            content="legacy child projection without admission",
            conversation_id=str(parent_id),
            external_event_key=f"subagent-parent:{events[1].id}",
            message_meta={
                "kind": runtime.SUBAGENT_PARENT_EVENT,
                "execution_agent_id": str(agent_id),
                "subagent_id": str(run.id),
                "child_message_id": str(events[1].id),
                "subagent_turn_anchor_id": str(stale_root.id),
                "attachments": [],
            },
        )
        db.add(stale_child)
        await db.commit()
        stale_root_id = stale_root.id
        stale_child_id = stale_child.id

    repaired_root, injected, status = await runtime._materialize_parent_event_batch(
        parent_session_id=parent_id,
        execution_agent_id=agent_id,
        execution_user_id=user_id,
        candidate_ids=[events[0].id],
    )

    assert status == "materialized"
    assert repaired_root is not None and repaired_root.id != stale_root_id
    assert len(injected) == 1 and "repair me 0" in injected[0]["content"]
    async with async_session() as db:
        assert await db.get(ChatMessage, stale_root_id) is None
        assert await db.get(ChatMessage, stale_child_id) is None
        projections = list(
            (
                await db.execute(
                    select(ChatMessage).where(
                        ChatMessage.external_event_key
                        == f"subagent-parent:{events[0].id}"
                    )
                )
            ).scalars()
        )
        parent = await db.get(ChatSession, parent_id)
    assert [row.id for row in projections] == [repaired_root.id]
    snapshot = conversation_turn_snapshot_for_session(parent)
    assert snapshot.anchor_id == repaired_root.id
    assert snapshot.status == "running"
    await runtime._finish_parent_events_for_root(repaired_root.id, [events[0].id])
    async with async_session() as db:
        from app.services.conversation_turn_lifecycle import (
            transition_conversation_turn,
        )

        await transition_conversation_turn(
            db,
            agent_id=agent_id,
            conversation_id=str(parent_id),
            turn_anchor_id=repaired_root.id,
            status="completed",
        )
        db.add(
            ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                role="assistant",
                content="first repaired batch complete",
                conversation_id=str(parent_id),
                message_meta={
                    "turn_anchor_id": str(repaired_root.id),
                    "turn_status": "completed",
                    "attachments": [],
                },
            )
        )
        await db.commit()

    second_root, second_injected, second_status = (
        await runtime._materialize_parent_event_batch(
            parent_session_id=parent_id,
            execution_agent_id=agent_id,
            execution_user_id=user_id,
            candidate_ids=[events[1].id],
        )
    )
    assert second_status == "materialized"
    assert second_root is not None and second_root.id != repaired_root.id
    assert len(second_injected) == 1 and "repair me 1" in second_injected[0]["content"]
    await runtime._finish_parent_events_for_root(second_root.id, [events[1].id])
    async with async_session() as db:
        from app.services.conversation_turn_lifecycle import (
            transition_conversation_turn,
        )

        await transition_conversation_turn(
            db,
            agent_id=agent_id,
            conversation_id=str(parent_id),
            turn_anchor_id=second_root.id,
            status="completed",
        )
        await db.commit()


async def test_idle_dispatch_repairs_projection_whose_root_is_missing():
    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-repair-missing-root",
        task="child",
        mode="async",
        turn_anchor_id=anchor_id,
    )
    await runtime.send_subagent_message_to_parent(
        agent_id=agent_id,
        execution_user_id=user_id,
        origin_tool_call_id="repair-missing-root-result",
        subagent_session_id=str(run.id),
        message="repair missing root",
    )
    missing_root_id = uuid.uuid4()
    async with async_session() as db:
        event = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id == str(run.id),
                    ChatMessage.message_meta["kind"].as_string()
                    == runtime.SUBAGENT_PARENT_MESSAGE,
                )
            )
        ).scalar_one()
        broken_projection = ChatMessage(
            agent_id=agent_id,
            user_id=user_id,
            sender_agent_id=agent_id,
            role="user",
            content="projection with missing root",
            conversation_id=str(parent_id),
            external_event_key=f"subagent-parent:{event.id}",
            message_meta={
                "kind": runtime.SUBAGENT_PARENT_EVENT,
                "execution_agent_id": str(agent_id),
                "subagent_id": str(run.id),
                "child_message_id": str(event.id),
                "subagent_turn_anchor_id": str(missing_root_id),
                "attachments": [],
            },
        )
        db.add_all(
            [
                ChatMessage(
                    agent_id=agent_id,
                    user_id=user_id,
                    role="assistant",
                    content="parent idle",
                    conversation_id=str(parent_id),
                    message_meta={"attachments": []},
                ),
                broken_projection,
            ]
        )
        await db.commit()
        broken_projection_id = broken_projection.id

    repaired_root, injected, status = await runtime._materialize_parent_event_batch(
        parent_session_id=parent_id,
        execution_agent_id=agent_id,
        execution_user_id=user_id,
        candidate_ids=[event.id],
    )
    assert status == "materialized"
    assert repaired_root is not None and repaired_root.id != missing_root_id
    assert injected and "repair missing root" in injected[0]["content"]
    async with async_session() as db:
        assert await db.get(ChatMessage, broken_projection_id) is None
    await runtime._finish_parent_events_for_root(repaired_root.id, [event.id])
    async with async_session() as db:
        from app.services.conversation_turn_lifecycle import (
            transition_conversation_turn,
        )

        await transition_conversation_turn(
            db,
            agent_id=agent_id,
            conversation_id=str(parent_id),
            turn_anchor_id=repaired_root.id,
            status="completed",
        )
        await db.commit()


async def test_parent_event_terminal_crash_window_converges_without_second_resume(monkeypatch):
    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-parent-terminal-window",
        task="child",
        mode="async",
        turn_anchor_id=anchor_id,
    )
    await runtime.send_subagent_message_to_parent(
        agent_id=agent_id,
        execution_user_id=user_id,
        origin_tool_call_id="parent-terminal-window-message",
        subagent_session_id=str(run.id),
        message="finish before state update",
    )
    async with async_session() as db:
        db.add(
            ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                role="assistant",
                content="parent idle",
                conversation_id=str(parent_id),
                message_meta={"attachments": []},
            )
        )
        await db.commit()
        event = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id == str(run.id),
                    ChatMessage.message_meta["kind"].as_string()
                    == runtime.SUBAGENT_PARENT_MESSAGE,
                )
            )
        ).scalar_one()

    resume_calls = 0

    async def fake_resume(batch_anchor):
        nonlocal resume_calls
        resume_calls += 1
        async with async_session() as db:
            db.add(
                ChatMessage(
                    agent_id=agent_id,
                    user_id=user_id,
                    role="assistant",
                    content="already durable",
                    conversation_id=str(parent_id),
                    message_meta={
                        "turn_anchor_id": str(batch_anchor.id),
                        "turn_status": "completed",
                        "attachments": [],
                    },
                )
            )
            await db.commit()
        return False

    async def fake_run_channel_message(_lock_key, *, work, **_kwargs):
        return await work()

    monkeypatch.setattr("app.services.turn_recovery.resume_turn", fake_resume)
    monkeypatch.setattr(
        "app.services.channel_dispatch.run_channel_message",
        fake_run_channel_message,
    )

    assert await runtime._dispatch_parent_event_batch([event.id]) is False
    assert await runtime._dispatch_parent_event_batch([event.id]) is True
    async with async_session() as db:
        stored_event = await db.get(ChatMessage, event.id)
    assert stored_event.message_meta["subagent_dispatch_state"] == runtime.SUBAGENT_DISPATCH_DELIVERED
    assert resume_calls == 1


async def test_committed_parent_event_wakes_dispatcher_within_batch_window(monkeypatch):
    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-event-driven-parent",
        task="child",
        mode="async",
        turn_anchor_id=anchor_id,
    )
    async with async_session() as db:
        db.add(
            ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                role="assistant",
                content="parent idle",
                conversation_id=str(parent_id),
                message_meta={"attachments": []},
            )
        )
        await db.commit()

    resume_calls = 0

    async def fake_resume(batch_anchor):
        nonlocal resume_calls
        resume_calls += 1
        if resume_calls == 1:
            return False
        async with async_session() as db:
            db.add(
                ChatMessage(
                    agent_id=agent_id,
                    user_id=user_id,
                    role="assistant",
                    content="event-driven reply",
                    conversation_id=str(parent_id),
                    message_meta={
                        "turn_anchor_id": str(batch_anchor.id),
                        "turn_status": "completed",
                        "attachments": [],
                    },
                )
            )
            await db.commit()
        return True

    async def fake_run_channel_message(_lock_key, *, work, **_kwargs):
        return await work()

    startup_scan_complete = asyncio.Event()
    original_pending_parent_events = runtime._pending_parent_events

    async def observed_pending_parent_events(**kwargs):
        result = await original_pending_parent_events(**kwargs)
        if kwargs.get("project_scope") is False:
            startup_scan_complete.set()
        return result

    monkeypatch.setattr("app.services.turn_recovery.resume_turn", fake_resume)
    monkeypatch.setattr(
        "app.services.channel_dispatch.run_channel_message",
        fake_run_channel_message,
    )
    monkeypatch.setattr(runtime, "PARENT_EVENT_BATCH_DEBOUNCE_SECONDS", 0.01)
    monkeypatch.setattr(runtime, "DISPATCH_RETRY_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(runtime, "DISPATCH_RECOVERY_INTERVAL_SECONDS", 10.0)
    monkeypatch.setattr(runtime, "LEGACY_PARENT_RECOVERY_INTERVAL_SECONDS", 10.0)
    monkeypatch.setattr(runtime, "_pending_parent_events", observed_pending_parent_events)

    monkeypatch.setattr(runtime, "_dispatch_wakeup", asyncio.Event())
    daemon = asyncio.create_task(runtime._subagent_parent_dispatch_loop())
    try:
        await asyncio.wait_for(startup_scan_complete.wait(), timeout=1)
        await asyncio.sleep(0.02)
        started_at = asyncio.get_running_loop().time()
        await runtime.send_subagent_message_to_parent(
            agent_id=agent_id,
            execution_user_id=user_id,
            origin_tool_call_id="event-driven-parent-message",
            subagent_session_id=str(run.id),
            message="wake now",
        )
        projection = None
        source = None
        while asyncio.get_running_loop().time() - started_at < 1.5:
            async with async_session() as db:
                projection = (
                    await db.execute(
                        select(ChatMessage).where(
                            ChatMessage.external_event_key.like("subagent-parent:%"),
                            ChatMessage.conversation_id == str(parent_id),
                        )
                    )
                ).scalar_one_or_none()
                source = (
                    await db.execute(
                        select(ChatMessage).where(
                            ChatMessage.conversation_id == str(run.id),
                            ChatMessage.message_meta["kind"].as_string()
                            == runtime.SUBAGENT_PARENT_MESSAGE,
                        )
                    )
                ).scalar_one()
            if (
                projection is not None
                and source.message_meta.get("subagent_dispatch_state")
                == runtime.SUBAGENT_DISPATCH_DELIVERED
            ):
                break
            await asyncio.sleep(0.05)
        assert projection is not None
        assert asyncio.get_running_loop().time() - started_at < 1.5
        assert source.message_meta["subagent_dispatch_state"] == runtime.SUBAGENT_DISPATCH_DELIVERED
        assert resume_calls == 2
        async with async_session() as db:
            projection_count = await db.scalar(
                select(func.count(ChatMessage.id)).where(
                    ChatMessage.external_event_key.like("subagent-parent:%"),
                    ChatMessage.conversation_id == str(parent_id),
                )
            )
        assert projection_count == 1
    finally:
        daemon.cancel()
        with pytest.raises(asyncio.CancelledError):
            await daemon


async def test_idle_parent_dispatcher_does_not_poll_between_recovery_passes(monkeypatch):
    scan_scopes = []
    leader_scan_calls = 0
    recovery_calls = 0

    async def no_parent_events(**_kwargs):
        scan_scopes.append(_kwargs.get("project_scope"))
        return []

    async def no_parent_groups(_pending):
        return [], []

    async def no_leader_groups(**_kwargs):
        nonlocal leader_scan_calls
        leader_scan_calls += 1
        return []

    async def no_project_recovery():
        nonlocal recovery_calls
        recovery_calls += 1
        return False

    monkeypatch.setattr(runtime, "PARENT_EVENT_BATCH_DEBOUNCE_SECONDS", 0.01)
    monkeypatch.setattr(runtime, "DISPATCH_RECOVERY_INTERVAL_SECONDS", 10.0)
    monkeypatch.setattr(runtime, "LEGACY_PARENT_RECOVERY_INTERVAL_SECONDS", 10.0)
    monkeypatch.setattr(runtime, "_pending_parent_events", no_parent_events)
    monkeypatch.setattr(runtime, "_pending_parent_event_groups", no_parent_groups)
    monkeypatch.setattr(runtime, "_pending_project_leader_groups", no_leader_groups)
    monkeypatch.setattr(runtime, "_recover_project_dispatch_outbox_once", no_project_recovery)

    monkeypatch.setattr(runtime, "_dispatch_wakeup", asyncio.Event())
    monkeypatch.setattr(runtime, "_project_dispatch_wakeup", asyncio.Event())
    daemons = [
        asyncio.create_task(runtime._subagent_parent_dispatch_loop()),
        asyncio.create_task(runtime._project_dispatch_loop()),
    ]
    try:
        await asyncio.sleep(0.1)
        assert scan_scopes.count(False) == 1
        assert scan_scopes.count(True) == 1
        assert leader_scan_calls == 1
        assert recovery_calls == 1
    finally:
        for daemon in daemons:
            daemon.cancel()
        await asyncio.gather(*daemons, return_exceptions=True)


async def test_project_dispatch_signal_wakes_stable_leader_loop(monkeypatch):
    broken_group_id = uuid.uuid4()
    group_id = uuid.uuid4()
    startup_scan_complete = asyncio.Event()
    dispatched = asyncio.Event()
    leader_ready = False
    dispatch_calls = 0

    async def no_parent_events(**_kwargs):
        return []

    async def no_parent_groups(_pending):
        return [], []

    async def leader_groups(**_kwargs):
        startup_scan_complete.set()
        return [broken_group_id, group_id] if leader_ready else []

    async def dispatch_leader(candidate_group_id, **_kwargs):
        nonlocal dispatch_calls
        if candidate_group_id == broken_group_id:
            raise RuntimeError("isolated project failure")
        assert candidate_group_id == group_id
        dispatch_calls += 1
        dispatched.set()
        return True

    async def no_project_recovery():
        return False

    monkeypatch.setattr(runtime, "PARENT_EVENT_BATCH_DEBOUNCE_SECONDS", 0.01)
    monkeypatch.setattr(runtime, "DISPATCH_RECOVERY_INTERVAL_SECONDS", 10.0)
    monkeypatch.setattr(runtime, "LEGACY_PARENT_RECOVERY_INTERVAL_SECONDS", 10.0)
    monkeypatch.setattr(runtime, "_pending_parent_events", no_parent_events)
    monkeypatch.setattr(runtime, "_pending_parent_event_groups", no_parent_groups)
    monkeypatch.setattr(runtime, "_pending_project_leader_groups", leader_groups)
    monkeypatch.setattr(runtime, "_dispatch_project_leader_batch", dispatch_leader)
    monkeypatch.setattr(runtime, "_recover_project_dispatch_outbox_once", no_project_recovery)

    monkeypatch.setattr(runtime, "_project_dispatch_wakeup", asyncio.Event())
    daemon = asyncio.create_task(runtime._project_dispatch_loop())
    try:
        await asyncio.wait_for(startup_scan_complete.wait(), timeout=1)
        await asyncio.sleep(0.02)
        leader_ready = True
        runtime._signal_project_dispatch_work()
        await asyncio.wait_for(dispatched.wait(), timeout=1)
        assert dispatch_calls == 1
    finally:
        daemon.cancel()
        with pytest.raises(asyncio.CancelledError):
            await daemon


async def test_stalled_project_dispatch_does_not_block_ordinary_parent_wake(monkeypatch):
    ordinary_scans = 0
    startup_complete = asyncio.Event()
    project_dispatch_started = asyncio.Event()
    hold_project_dispatch = asyncio.Event()
    project_ready = False
    group_id = uuid.uuid4()

    async def no_parent_events(**kwargs):
        nonlocal ordinary_scans
        if kwargs.get("project_scope") is False:
            ordinary_scans += 1
        if ordinary_scans and kwargs.get("project_scope") is True:
            startup_complete.set()
        return []

    async def no_parent_groups(_pending):
        return [], []

    async def leader_groups(**_kwargs):
        return [group_id] if project_ready else []

    async def stalled_leader_dispatch(_group_id, **_kwargs):
        project_dispatch_started.set()
        await hold_project_dispatch.wait()
        return True

    async def no_project_recovery():
        return False

    monkeypatch.setattr(runtime, "PARENT_EVENT_BATCH_DEBOUNCE_SECONDS", 0.01)
    monkeypatch.setattr(runtime, "DISPATCH_RECOVERY_INTERVAL_SECONDS", 10.0)
    monkeypatch.setattr(runtime, "LEGACY_PARENT_RECOVERY_INTERVAL_SECONDS", 10.0)
    monkeypatch.setattr(runtime, "_pending_parent_events", no_parent_events)
    monkeypatch.setattr(runtime, "_pending_parent_event_groups", no_parent_groups)
    monkeypatch.setattr(runtime, "_pending_project_leader_groups", leader_groups)
    monkeypatch.setattr(runtime, "_dispatch_project_leader_batch", stalled_leader_dispatch)
    monkeypatch.setattr(runtime, "_recover_project_dispatch_outbox_once", no_project_recovery)
    monkeypatch.setattr(runtime, "_dispatch_wakeup", asyncio.Event())
    monkeypatch.setattr(runtime, "_project_dispatch_wakeup", asyncio.Event())

    daemons = [
        asyncio.create_task(runtime._subagent_parent_dispatch_loop()),
        asyncio.create_task(runtime._project_dispatch_loop()),
    ]
    try:
        await asyncio.wait_for(startup_complete.wait(), timeout=1)
        project_ready = True
        runtime._signal_project_dispatch_work()
        await asyncio.wait_for(project_dispatch_started.wait(), timeout=1)
        scans_before_wake = ordinary_scans
        runtime._signal_dispatch_work()
        while ordinary_scans == scans_before_wake:
            await asyncio.sleep(0.01)
        assert ordinary_scans == scans_before_wake + 1
    finally:
        for daemon in daemons:
            daemon.cancel()
        await asyncio.gather(*daemons, return_exceptions=True)


async def test_concurrent_parent_dispatchers_resume_one_batch_once(monkeypatch):
    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-concurrent-parent-wake",
        task="child",
        mode="async",
        turn_anchor_id=anchor_id,
    )
    for index in range(3):
        await runtime.send_subagent_message_to_parent(
            agent_id=agent_id,
            execution_user_id=user_id,
            origin_tool_call_id=f"concurrent-child-message-{index}",
            subagent_session_id=str(run.id),
            message=f"concurrent result {index}",
        )
    async with async_session() as db:
        db.add(
            ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                role="assistant",
                content="parent idle",
                conversation_id=str(parent_id),
                message_meta={"attachments": []},
            )
        )
        await db.commit()
        event_ids = list(
            (
                await db.execute(
                    select(ChatMessage.id)
                    .where(
                        ChatMessage.conversation_id == str(run.id),
                        ChatMessage.message_meta["kind"].as_string()
                        == runtime.SUBAGENT_PARENT_MESSAGE,
                    )
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                )
            ).scalars()
        )

    resumed: list[uuid.UUID] = []

    async def fake_resume(batch_anchor):
        resumed.append(batch_anchor.id)
        await asyncio.sleep(0.1)
        async with async_session() as db:
            db.add(
                ChatMessage(
                    agent_id=agent_id,
                    user_id=user_id,
                    role="assistant",
                    content="one concurrent parent response",
                    conversation_id=str(parent_id),
                    message_meta={
                        "turn_anchor_id": str(batch_anchor.id),
                        "turn_status": "completed",
                        "attachments": [],
                    },
                )
            )
            await db.commit()
        return True

    monkeypatch.setattr("app.services.turn_recovery.resume_turn", fake_resume)
    await asyncio.gather(
        runtime._dispatch_parent_event_batch(event_ids),
        runtime._dispatch_parent_event_batch(event_ids),
    )

    async with async_session() as db:
        projection_count = await db.scalar(
            select(func.count(ChatMessage.id)).where(
                ChatMessage.external_event_key.in_(
                    [f"subagent-parent:{event_id}" for event_id in event_ids]
                )
            )
        )
    assert len(resumed) == 1
    assert projection_count == 3


async def test_compat_single_event_dispatch_resolves_materialized_batch_root_once(monkeypatch):
    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-compat-parent-wake",
        task="child",
        mode="async",
        turn_anchor_id=anchor_id,
    )
    for index in range(3):
        await runtime.send_subagent_message_to_parent(
            agent_id=agent_id,
            execution_user_id=user_id,
            origin_tool_call_id=f"compat-child-message-{index}",
            subagent_session_id=str(run.id),
            message=f"compat result {index}",
        )
    async with async_session() as db:
        db.add(
            ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                role="assistant",
                content="parent idle",
                conversation_id=str(parent_id),
                message_meta={"attachments": []},
            )
        )
        await db.commit()
        event_ids = list(
            (
                await db.execute(
                    select(ChatMessage.id)
                    .where(
                        ChatMessage.conversation_id == str(run.id),
                        ChatMessage.message_meta["kind"].as_string()
                        == runtime.SUBAGENT_PARENT_MESSAGE,
                    )
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                )
            ).scalars()
        )

    root, _injected, status = await runtime._materialize_parent_event_batch(
        parent_session_id=parent_id,
        execution_agent_id=agent_id,
        execution_user_id=user_id,
        candidate_ids=event_ids,
    )
    assert status == "materialized"
    resumed: list[uuid.UUID] = []

    async def fake_resume(batch_root):
        resumed.append(batch_root.id)
        async with async_session() as db:
            db.add(
                ChatMessage(
                    agent_id=agent_id,
                    user_id=user_id,
                    role="assistant",
                    content="compat completed",
                    conversation_id=str(parent_id),
                    message_meta={
                        "turn_anchor_id": str(batch_root.id),
                        "turn_status": "completed",
                        "attachments": [],
                    },
                )
            )
            await db.commit()
        return True

    async def fake_run_channel_message(_lock_key, *, work, **_kwargs):
        return await work()

    monkeypatch.setattr("app.services.turn_recovery.resume_turn", fake_resume)
    monkeypatch.setattr(
        "app.services.channel_dispatch.run_channel_message",
        fake_run_channel_message,
    )
    assert await runtime._dispatch_parent_event(event_ids[1])
    assert await runtime._dispatch_parent_event(event_ids[2])
    assert resumed == [root.id]
    pending = await runtime._pending_parent_events(debounce_seconds=0)
    assert not set(event_ids).intersection(pending)


async def test_active_parent_turn_drains_subagent_events_once_and_tool_tail_recovery_keeps_root():
    from app.services.turn_recovery import _find_turn_anchor_for_latest

    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-active-parent-wake",
        task="child",
        mode="async",
        turn_anchor_id=anchor_id,
    )
    for index in range(2):
        await runtime.send_subagent_message_to_parent(
            agent_id=agent_id,
            execution_user_id=user_id,
            origin_tool_call_id=f"active-child-message-{index}",
            subagent_session_id=str(run.id),
            message=f"late result {index}",
        )

    first = await runtime.drain_parent_subagent_events(
        parent_session_id=str(parent_id),
        active_turn_anchor_id=anchor_id,
        execution_agent_id=agent_id,
        execution_user_id=user_id,
    )
    second = await runtime.drain_parent_subagent_events(
        parent_session_id=str(parent_id),
        active_turn_anchor_id=anchor_id,
        execution_agent_id=agent_id,
        execution_user_id=user_id,
    )
    assert len(first) == 2
    assert "late result 0" in first[0]["content"]
    assert "late result 1" in first[1]["content"]
    assert second == []

    async with async_session() as db:
        projections = (
            (
                await db.execute(
                    select(ChatMessage)
                    .where(
                        ChatMessage.conversation_id == str(parent_id),
                        ChatMessage.message_meta["subagent_turn_anchor_id"].as_string()
                        == str(anchor_id),
                    )
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                )
            )
            .scalars()
            .all()
        )
        tool_tail = ChatMessage(
            agent_id=agent_id,
            user_id=user_id,
            role="tool_call",
            content=json.dumps(
                {
                    "name": "read_file",
                    "call_id": "active-parent-tool-tail",
                    "args": {"path": "evidence.txt"},
                    "status": "done",
                    "result": "durable evidence",
                }
            ),
            conversation_id=str(parent_id),
            message_meta={
                "turn_anchor_id": str(anchor_id),
                "attachments": [],
            },
            created_at=projections[-1].created_at + timedelta(microseconds=1),
        )
        db.add(tool_tail)
        await db.commit()
        recovered_from_projection = await _find_turn_anchor_for_latest(db, projections[-1])
        recovered_from_tool_tail = await _find_turn_anchor_for_latest(db, tool_tail)
    assert len(projections) == 2
    assert recovered_from_projection.id == anchor_id
    assert recovered_from_tool_tail.id == anchor_id


async def test_channel_llm_composes_parent_event_drain_with_existing_round_hook(monkeypatch):
    agent_id, user_id, parent_id, anchor_id = await _make_context(parent_channel="dingtalk")
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-channel-parent-hook",
        task="child",
        mode="async",
        turn_anchor_id=anchor_id,
    )
    await runtime.send_subagent_message_to_parent(
        agent_id=agent_id,
        execution_user_id=user_id,
        origin_tool_call_id="channel-parent-hook-result",
        subagent_session_id=str(run.id),
        message="channel late result",
    )
    captured: list[dict] = []

    async def upstream(_round_i: int) -> list[dict]:
        return [{"role": "user", "content": "existing round input"}]

    async def fake_provider_dispatch(**kwargs):
        captured.extend(await kwargs["before_round"](0))
        return "merged channel answer"

    monkeypatch.setattr(
        "app.services.llm.call_llm_with_failover",
        fake_provider_dispatch,
    )
    async with async_session() as db:
        parent = await db.get(ChatSession, parent_id)
        reply = await channel_llm._call_agent_llm(
            db,
            agent_id,
            "parent request",
            session_id=str(parent_id),
            user_id=user_id,
            prepared_tools=[],
            include_soul=False,
            include_memory=False,
            broadcast_web=False,
            runtime_session=parent,
            turn_anchor_id=anchor_id,
            before_round=upstream,
        )
    assert reply == "merged channel answer"
    assert captured[0] == {"role": "user", "content": "existing round input"}
    assert "channel late result" in captured[1]["content"]


async def test_project_parent_event_stays_on_special_materialization_path(monkeypatch):
    agent_id, user_id, parent_id, anchor_id = await _make_context(
        parent_channel="project",
        project=True,
    )
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-project-special-parent-wake",
        task="project child",
        mode="async",
        turn_anchor_id=anchor_id,
    )
    await runtime.send_subagent_message_to_parent(
        agent_id=agent_id,
        execution_user_id=user_id,
        origin_tool_call_id="project-special-result",
        subagent_session_id=str(run.id),
        message="project result",
    )
    async with async_session() as db:
        event = await db.scalar(
            select(ChatMessage)
            .where(
                ChatMessage.conversation_id == str(run.id),
                ChatMessage.message_meta["kind"].as_string()
                == runtime.SUBAGENT_PARENT_MESSAGE,
            )
            .limit(1)
        )

    batches, special_events = await runtime._pending_parent_event_groups([event.id])
    assert batches == []
    assert special_events == [event.id]

    async def fail_batch(_message_ids):
        raise AssertionError("project parent event must not enter ordinary batching")

    monkeypatch.setattr(runtime, "_dispatch_parent_event_batch", fail_batch)
    assert await runtime._dispatch_parent_event(event.id)
    async with async_session() as db:
        projected = await db.scalar(
            select(ChatMessage).where(
                ChatMessage.external_event_key == f"project-subagent:{event.id}"
            )
        )
        ordinary = await db.scalar(
            select(ChatMessage.id).where(
                ChatMessage.external_event_key == f"subagent-parent:{event.id}"
            )
        )
    assert projected.role == "assistant"
    assert projected.message_meta["kind"] == "project_subagent_reply"
    assert ordinary is None


async def test_sync_execution_reuses_unified_llm_and_persists_terminal_result(monkeypatch):
    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-sync",
        task="do it",
        mode="sync",
        model="Readable-Test-Model",
        turn_anchor_id=anchor_id,
    )
    captured = {}

    async def fake_tools(_agent_id, _session_id=None, execution_user_id=None):
        del execution_user_id
        return [{"type": "function", "function": {"name": "safe", "parameters": {}}}]

    async def fake_llm(_db, _agent_id, user_text, **kwargs):
        captured.update(kwargs)
        captured["user_text"] = user_text
        assert await kwargs["before_round"](0) == []
        await kwargs["on_thinking"]("child hidden reasoning")
        await runtime.send_subagent_message_to_parent(
            agent_id=_agent_id,
            execution_user_id=user_id,
            origin_tool_call_id="sync-message-one",
            subagent_session_id=kwargs["session_id"],
            message="sync interim",
        )
        return "child result"

    monkeypatch.setattr(runtime, "prepare_subagent_tools", fake_tools)
    monkeypatch.setattr("app.services.channel_llm._call_agent_llm", fake_llm)
    status, result, parent_messages = await runtime.run_subagent_sync(run.id)

    assert (status, result) == ("completed", "child result")
    assert parent_messages == ["sync interim"]
    assert captured["model_name"] == "Readable-Test-Model"
    assert captured["broadcast_web"] is True
    assert captured["turn_anchor_id"] is not None
    assert captured["continue_turn"] is False
    assert captured["recovery_mode"] is False
    assert captured["prepared_tools"][0]["function"]["name"] == "safe"
    async with async_session() as db:
        final = (
            await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.conversation_id == str(run.id),
                    ChatMessage.message_meta["kind"].as_string() == runtime.SUBAGENT_COMPLETION,
                )
                .limit(1)
            )
        ).scalar_one()
    assert final.content == "child result"
    assert final.thinking == "child hidden reasoning"
    assert final.message_meta["subagent_wake"] is False


async def test_subagent_confirmation_suspends_and_resumes_durable_turn(monkeypatch):
    from app.services import confirmation_service
    from app.services.conversation_turn_lifecycle import (
        get_conversation_turn_snapshot,
    )

    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-confirmation",
        task="confirm before continuing",
        mode="async",
        model="Readable-Test-Model",
        turn_anchor_id=anchor_id,
    )
    assert await runtime._claim_subagent(run.id) == run.id
    invocations = []
    pending_call_id = None

    async def fake_tools(_agent_id, _session_id=None, execution_user_id=None):
        del _agent_id, _session_id, execution_user_id
        return []

    async def fake_llm(_db, _agent_id, user_text, **kwargs):
        nonlocal pending_call_id
        invocations.append(dict(kwargs, user_text=user_text))
        if len(invocations) == 1:
            pending_call_id = await confirmation_service.suspend_for_confirmation(
                agent_id=_agent_id,
                conversation_id=kwargs["session_id"],
                chat_session_id=uuid.UUID(kwargs["session_id"]),
                source_channel="subagent",
                user_id=user_id,
                intro_text="需要确认",
                title="继续执行",
                summary="确认后继续当前项目任务",
                action={"tool": "project_write_file", "args": {"path": "ok.txt"}},
                risk_level="medium",
                buttons=[{"label": "继续", "value": "continue"}],
                force_confirmation=True,
                turn_anchor_id=kwargs["turn_anchor_id"],
            )
            return ""
        assert kwargs["continue_turn"] is True
        assert kwargs["recovery_mode"] is True
        assert any(row.get("role") == "tool" for row in kwargs["history"])
        await kwargs["on_thinking"]("resumed hidden reasoning")
        return "confirmed child result"

    async def no_origin_card(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime, "prepare_subagent_tools", fake_tools)
    monkeypatch.setattr("app.services.channel_llm._call_agent_llm", fake_llm)
    monkeypatch.setattr(confirmation_service, "_update_origin_card", no_origin_card)

    await runtime.execute_claimed_subagent(run.id)
    assert pending_call_id is not None
    async with async_session() as db:
        parked = await db.get(SubagentRun, run.id)
        processing = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id == str(run.id),
                    ChatMessage.message_meta["kind"].as_string() == runtime.SUBAGENT_INPUT,
                )
            )
        ).scalar_one()
        assert parked.status == runtime.RUN_WAITING
        assert processing.message_meta["subagent_input_state"] == runtime.INPUT_PROCESSING
        parked_current = await get_conversation_turn_snapshot(
            db,
            agent_id=agent_id,
            conversation_id=str(run.id),
        )
        parked_exact = await get_conversation_turn_snapshot(
            db,
            agent_id=agent_id,
            conversation_id=str(run.id),
            turn_anchor_id=processing.id,
        )
        assert parked_current == parked_exact
        assert parked_current.status == "suspended"

    resolved = await confirmation_service.resolve_confirmation(
        agent_id=agent_id,
        call_id=pending_call_id,
        button_value="continue",
        button_label="继续",
        resolving_user_id=user_id,
    )
    assert resolved and "继续" in resolved
    assert len(invocations) == 2
    async with async_session() as db:
        completed = await db.get(SubagentRun, run.id)
        confirmation = await db.get(ChatMessage, pending_call_id)
        final = (
            await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.conversation_id == str(run.id),
                    ChatMessage.message_meta["kind"].as_string() == runtime.SUBAGENT_COMPLETION,
                )
                .limit(1)
            )
        ).scalar_one()
        completed_current = await get_conversation_turn_snapshot(
            db,
            agent_id=agent_id,
            conversation_id=str(run.id),
        )
        completed_exact = await get_conversation_turn_snapshot(
            db,
            agent_id=agent_id,
            conversation_id=str(run.id),
            turn_anchor_id=processing.id,
        )
    assert completed.status == runtime.RUN_COMPLETED
    assert json.loads(confirmation.content)["status"] == "done"
    assert final.content == "confirmed child result"
    assert final.thinking == "resumed hidden reasoning"
    assert completed_current == completed_exact
    assert completed_current.status == "completed"


async def test_revoked_execution_user_fails_before_llm_or_tool_side_effect(monkeypatch):
    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-revoked-before-run",
        task="must not execute",
        mode="async",
        turn_anchor_id=anchor_id,
    )
    assert await runtime._claim_subagent(run.id) == run.id

    async with async_session() as db:
        user = await db.get(User, user_id)
        user.is_active = False
        await db.commit()

    async def fail_llm(*_args, **_kwargs):
        raise AssertionError("revoked execution identity must fail before LLM dispatch")

    monkeypatch.setattr("app.services.channel_llm._call_agent_llm", fail_llm)
    await runtime.execute_claimed_subagent(run.id)

    async with async_session() as db:
        fresh = await db.get(SubagentRun, run.id)
        failure = await db.scalar(
            select(ChatMessage)
            .where(
                ChatMessage.conversation_id == str(run.id),
                ChatMessage.message_meta["kind"].as_string() == runtime.SUBAGENT_FAILURE,
            )
            .limit(1)
        )
    assert fresh.status == "failed"
    assert "ExecutionIdentityError" in failure.content


async def test_child_is_hidden_from_lists_but_direct_web_detail_is_accessible():
    from app.api.chat_sessions import (
        _build_session_detail_out,
        _get_session_messages_page,
        _load_accessible_session,
    )
    from app.services.tools.session_introspection import handle_list_sessions

    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-session-visibility",
        task="visible to session tools",
        mode="async",
        turn_anchor_id=anchor_id,
    )
    raw = await handle_list_sessions(
        agent_id,
        user_id,
        str(parent_id),
        {"raw": True, "limit": 50},
    )
    assert str(run.id) in raw

    async with async_session() as db:
        user = await db.get(User, user_id)
        _agent, direct_child, view_scope = await _load_accessible_session(db, user, agent_id, run.id)
        detail = await _build_session_detail_out(db, direct_child, view_scope)
        child_rows = await _get_session_messages_page(
            agent_id=agent_id,
            session_id=run.id,
            limit=100,
            turn_limit=None,
            before=None,
            current_user=user,
            db=db,
            response=None,
        )
        db.add(
            ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                sender_agent_id=agent_id,
                role="user",
                content="internal wake anchor",
                conversation_id=str(parent_id),
                message_meta={"kind": runtime.SUBAGENT_PARENT_EVENT},
            )
        )
        await db.commit()
        web_rows = await _get_session_messages_page(
            agent_id=agent_id,
            session_id=parent_id,
            limit=100,
            turn_limit=None,
            before=None,
            current_user=user,
            db=db,
            response=None,
        )
    assert direct_child.id == run.id
    assert view_scope == "mine"
    assert detail.runtime is not None
    assert detail.runtime.kind == "subagent"
    assert detail.runtime.status == "queued"
    assert detail.runtime.execution_agent_id == str(agent_id)
    assert any(row.get("content") == "visible to session tools" for row in child_rows)
    assert all(row.get("content") != "internal wake anchor" for row in web_rows)


async def test_nonhuman_parent_child_uses_execution_owner_for_standard_access():
    from app.api.chat_sessions import (
        PatchSessionIn,
        _load_accessible_session,
        delete_session,
        rename_session,
    )
    from app.services.tools.session_introspection import handle_list_sessions

    agent_id, user_id, parent_id, anchor_id = await _make_context(parent_channel="trigger")
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-trigger-owner",
        task="trigger-owned child",
        mode="async",
        turn_anchor_id=anchor_id,
    )

    raw = await handle_list_sessions(
        agent_id,
        user_id,
        str(parent_id),
        {"raw": True, "limit": 50},
    )
    assert str(run.id) in raw

    async with async_session() as db:
        user = await db.get(User, user_id)
        child = await db.get(ChatSession, run.id)
        assert child is not None and child.user_id is None
        _agent, direct_child, view_scope = await _load_accessible_session(db, user, agent_id, run.id)
        assert direct_child.id == run.id
        assert view_scope == "mine"

        with pytest.raises(HTTPException) as rename_error:
            await rename_session(
                agent_id,
                run.id,
                PatchSessionIn(title="must not change"),
                current_user=user,
                db=db,
            )
        assert rename_error.value.status_code == 409

        with pytest.raises(HTTPException) as delete_error:
            await delete_session(
                agent_id,
                run.id,
                current_user=user,
                db=db,
            )
        assert delete_error.value.status_code == 409


async def test_parent_with_subagent_audit_record_cannot_be_deleted():
    from app.api.chat_sessions import delete_session

    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-parent-delete-guard",
        task="retain child audit",
        mode="async",
        turn_anchor_id=anchor_id,
    )

    async with async_session() as db:
        user = await db.get(User, user_id)
        with pytest.raises(HTTPException) as delete_error:
            await delete_session(
                agent_id,
                parent_id,
                current_user=user,
                db=db,
            )
        assert delete_error.value.status_code == 409
        assert await db.get(ChatSession, parent_id) is not None
        assert await db.get(ChatSession, run.id) is not None
        assert await db.get(SubagentRun, run.id) is not None


async def test_agent_with_subagent_audit_record_cannot_be_deleted():
    from app.api.agents import delete_agent

    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-agent-delete-guard",
        task="retain agent audit",
        mode="async",
        turn_anchor_id=anchor_id,
    )

    async with async_session() as db:
        user = await db.get(User, user_id)
        with pytest.raises(HTTPException) as delete_error:
            await delete_agent(
                agent_id,
                current_user=user,
                db=db,
            )
        assert delete_error.value.status_code == 409
        assert await db.get(Agent, agent_id) is not None
        assert await db.get(ChatSession, run.id) is not None
        assert await db.get(SubagentRun, run.id) is not None


async def test_company_delete_helper_releases_subagent_session_tree():
    from app.api.admin import _delete_company_subagent_runs

    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-company-delete",
        task="company purge child",
        mode="async",
        turn_anchor_id=anchor_id,
    )

    async with async_session() as db:
        await _delete_company_subagent_runs(db, [agent_id])
        await db.execute(delete(ChatMessage).where(ChatMessage.conversation_id.in_([str(run.id), str(parent_id)])))
        await db.execute(delete(ChatSession).where(ChatSession.id.in_([run.id, parent_id])))
        await db.commit()
        assert await db.get(SubagentRun, run.id) is None
        assert await db.get(ChatSession, run.id) is None
        assert await db.get(ChatSession, parent_id) is None


async def test_round_inbox_survives_first_dispatch_context_recovery(monkeypatch):
    from app.services.llm import caller

    captured = {}

    async def fake_turn_context(**_kwargs):
        return "system", "dynamic"

    async def fake_call_llm(_model, _messages, _name, _role, **kwargs):
        assert await kwargs["before_round"](0) == [{"role": "user", "content": "late message"}]
        captured["recovered"] = await kwargs["context_recovery"](_model, None)
        return "ok"

    async def recover(_model, _budget):
        return [{"role": "user", "content": "compacted current"}]

    delivered = False

    async def before_round(_round):
        nonlocal delivered
        if delivered:
            return []
        delivered = True
        return [{"role": "user", "content": "late message"}]

    monkeypatch.setattr(caller, "_build_turn_context", fake_turn_context)
    monkeypatch.setattr(caller, "call_llm", fake_call_llm)
    model = SimpleNamespace(id=uuid.uuid4(), provider="test", model="primary")
    result = await caller.call_llm_with_failover(
        primary_model=model,
        fallback_model=None,
        messages=[{"role": "user", "content": "current"}],
        agent_name="Agent",
        role_description="",
        prepared_tools=[],
        context_recovery=recover,
        before_round=before_round,
    )
    assert result == "ok"
    assert captured["recovered"] == [
        {"role": "user", "content": "compacted current"},
        {"role": "user", "content": "late message"},
    ]


async def test_round_inbox_recovery_deduplicates_empty_attachment_shape_by_count(
    monkeypatch,
):
    from app.services.llm import caller

    captured = {}

    async def fake_turn_context(**_kwargs):
        return "system", "dynamic"

    async def fake_call_llm(_model, _messages, _name, _role, **kwargs):
        live = await kwargs["before_round"](0)
        assert live == [
            {"role": "user", "content": "same late message"},
            {"role": "user", "content": "same late message"},
        ]
        captured["recovered"] = await kwargs["context_recovery"](_model, None)
        return "ok"

    async def recover(_model, _budget):
        return [
            {"role": "user", "content": "compacted current", "attachments": []},
            {"role": "user", "content": "same late message", "attachments": []},
            {"role": "user", "content": "same late message", "attachments": []},
        ]

    async def before_round(_round):
        return [
            {"role": "user", "content": "same late message"},
            {"role": "user", "content": "same late message"},
        ]

    monkeypatch.setattr(caller, "_build_turn_context", fake_turn_context)
    monkeypatch.setattr(caller, "call_llm", fake_call_llm)
    model = SimpleNamespace(id=uuid.uuid4(), provider="test", model="primary")

    result = await caller.call_llm_with_failover(
        primary_model=model,
        fallback_model=None,
        messages=[{"role": "user", "content": "current"}],
        agent_name="Agent",
        role_description="",
        prepared_tools=[],
        context_recovery=recover,
        before_round=before_round,
    )

    assert result == "ok"
    assert captured["recovered"] == [
        {"role": "user", "content": "compacted current", "attachments": []},
        {"role": "user", "content": "same late message", "attachments": []},
        {"role": "user", "content": "same late message", "attachments": []},
    ]


async def test_round_inbox_recovery_does_not_match_injection_against_same_text_root(
    monkeypatch,
):
    from app.services.llm import caller

    captured = {}

    async def fake_turn_context(**_kwargs):
        return "system", "dynamic"

    async def fake_call_llm(_model, _messages, _name, _role, **kwargs):
        assert await kwargs["before_round"](0) == [
            {"role": "user", "content": "continue"}
        ]
        captured["recovered"] = await kwargs["context_recovery"](_model, None)
        return "ok"

    async def recover(_model, _budget):
        # The durable snapshot contains the pre-existing root but a read race
        # has not exposed the trailing, identically-worded injection yet.
        return [{"role": "user", "content": "continue", "attachments": []}]

    async def before_round(_round):
        return [{"role": "user", "content": "continue"}]

    monkeypatch.setattr(caller, "_build_turn_context", fake_turn_context)
    monkeypatch.setattr(caller, "call_llm", fake_call_llm)
    model = SimpleNamespace(id=uuid.uuid4(), provider="test", model="primary")

    result = await caller.call_llm_with_failover(
        primary_model=model,
        fallback_model=None,
        messages=[{"role": "user", "content": "continue"}],
        agent_name="Agent",
        role_description="",
        prepared_tools=[],
        context_recovery=recover,
        before_round=before_round,
    )

    assert result == "ok"
    assert captured["recovered"] == [
        {"role": "user", "content": "continue", "attachments": []},
        {"role": "user", "content": "continue"},
    ]


async def test_round_inbox_recovery_does_not_reserve_compacted_old_same_text(
    monkeypatch,
):
    from app.services.llm import caller

    captured = {}

    async def fake_turn_context(**_kwargs):
        return "system", "dynamic"

    async def fake_call_llm(_model, _messages, _name, _role, **kwargs):
        await kwargs["before_round"](0)
        captured["recovered"] = await kwargs["context_recovery"](_model, None)
        return "ok"

    async def recover(_model, _budget):
        return [
            {"role": "system", "content": "compacted old history"},
            {"role": "user", "content": "different current root", "attachments": []},
            {"role": "user", "content": "continue", "attachments": []},
        ]

    async def before_round(_round):
        return [{"role": "user", "content": "continue"}]

    monkeypatch.setattr(caller, "_build_turn_context", fake_turn_context)
    monkeypatch.setattr(caller, "call_llm", fake_call_llm)
    model = SimpleNamespace(id=uuid.uuid4(), provider="test", model="primary")

    result = await caller.call_llm_with_failover(
        primary_model=model,
        fallback_model=None,
        messages=[
            {"role": "user", "content": "continue"},
            {"role": "assistant", "content": "old reply"},
            {"role": "user", "content": "different current root"},
        ],
        agent_name="Agent",
        role_description="",
        prepared_tools=[],
        context_recovery=recover,
        before_round=before_round,
    )

    assert result == "ok"
    assert captured["recovered"] == [
        {"role": "system", "content": "compacted old history"},
        {"role": "user", "content": "different current root", "attachments": []},
        {"role": "user", "content": "continue", "attachments": []},
    ]


async def test_round_inbox_recovery_does_not_reserve_protected_old_same_text(
    monkeypatch,
):
    from app.services.llm import caller

    captured = {}

    async def fake_turn_context(**_kwargs):
        return "system", "dynamic"

    async def fake_call_llm(_model, _messages, _name, _role, **kwargs):
        await kwargs["before_round"](0)
        captured["recovered"] = await kwargs["context_recovery"](_model, None)
        return "ok"

    async def recover(_model, _budget):
        # The protected baseline suffix survives intact, but the racing read
        # does not yet contain the identically-worded trailing injection.
        return [
            {"role": "user", "content": "continue", "attachments": []},
            {"role": "assistant", "content": "older reply"},
            {"role": "user", "content": "current root", "attachments": []},
        ]

    async def before_round(_round):
        return [{"role": "user", "content": "continue"}]

    monkeypatch.setattr(caller, "_build_turn_context", fake_turn_context)
    monkeypatch.setattr(caller, "call_llm", fake_call_llm)
    model = SimpleNamespace(id=uuid.uuid4(), provider="test", model="primary")

    result = await caller.call_llm_with_failover(
        primary_model=model,
        fallback_model=None,
        messages=[
            {"role": "user", "content": "continue"},
            {"role": "assistant", "content": "older reply"},
            {"role": "user", "content": "current root"},
        ],
        agent_name="Agent",
        role_description="",
        prepared_tools=[],
        context_recovery=recover,
        before_round=before_round,
    )

    assert result == "ok"
    assert captured["recovered"] == [
        {"role": "user", "content": "continue", "attachments": []},
        {"role": "assistant", "content": "older reply"},
        {"role": "user", "content": "current root", "attachments": []},
        {"role": "user", "content": "continue"},
    ]


async def test_a2a_parent_wake_distinguishes_execution_and_storage_agent(monkeypatch):
    first_agent_id, user_id, _parent_id, _anchor_id = await _make_context()
    second_agent_id = uuid.uuid4()
    storage_agent_id, execution_agent_id = sorted(
        [first_agent_id, second_agent_id],
        key=str,
    )
    async with async_session() as db:
        first_agent = await db.get(Agent, first_agent_id)
        if second_agent_id != first_agent_id:
            db.add(
                Agent(
                    id=second_agent_id,
                    name="A2A peer",
                    creator_id=user_id,
                    tenant_id=first_agent.tenant_id,
                    primary_model_id=first_agent.primary_model_id,
                    status="idle",
                )
            )
        parent = ChatSession(
            agent_id=storage_agent_id,
            peer_agent_id=execution_agent_id,
            user_id=None,
            title="A2A parent",
            source_channel="agent",
            is_primary=False,
            is_group=False,
        )
        db.add(parent)
        await db.flush()
        db.add(
            ChatMessage(
                agent_id=storage_agent_id,
                user_id=user_id,
                sender_agent_id=execution_agent_id,
                role="assistant",
                content="idle",
                conversation_id=str(parent.id),
                message_meta={"attachments": []},
            )
        )
        await db.commit()
        parent_id = parent.id

    run, _ = await runtime.create_subagent(
        agent_id=execution_agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-a2a-wake",
        task="A2A child",
        mode="async",
    )
    await runtime.send_subagent_message_to_parent(
        agent_id=execution_agent_id,
        execution_user_id=user_id,
        origin_tool_call_id="a2a-message-one",
        subagent_session_id=str(run.id),
        message="A2A interim",
    )
    async with async_session() as db:
        event = (
            await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.conversation_id == str(run.id),
                    ChatMessage.message_meta["kind"].as_string() == runtime.SUBAGENT_PARENT_MESSAGE,
                )
                .limit(1)
            )
        ).scalar_one()

    captured = []

    async def fake_resume(anchor):
        captured.append(anchor)
        return True

    monkeypatch.setattr("app.services.turn_recovery.resume_turn", fake_resume)
    assert await runtime._dispatch_parent_event(event.id)
    assert captured[0].agent_id == storage_agent_id
    assert captured[0].sender_agent_id == execution_agent_id
    assert captured[0].message_meta["execution_agent_id"] == str(execution_agent_id)

    from app.api.chat_sessions import _build_session_detail_out, _load_accessible_session

    async with async_session() as db:
        user = await db.get(User, user_id)
        _route_agent, child_session, scope = await _load_accessible_session(
            db,
            user,
            storage_agent_id,
            run.id,
        )
        detail = await _build_session_detail_out(db, child_session, scope)
    assert detail.agent_id == str(execution_agent_id)
    assert detail.runtime is not None
    assert detail.runtime.execution_agent_id == str(execution_agent_id)


async def test_parent_wake_does_not_resume_with_revoked_execution_user(monkeypatch):
    from app.services.conversation_turn_lifecycle import (
        get_conversation_turn_snapshot,
    )

    first_agent_id, user_id, _parent_id, _anchor_id = await _make_context()
    second_agent_id = uuid.uuid4()
    storage_agent_id, execution_agent_id = sorted(
        [first_agent_id, second_agent_id],
        key=str,
    )
    async with async_session() as db:
        first_agent = await db.get(Agent, first_agent_id)
        db.add(
            Agent(
                id=second_agent_id,
                name="Revoked wake peer",
                creator_id=user_id,
                tenant_id=first_agent.tenant_id,
                primary_model_id=first_agent.primary_model_id,
                status="idle",
            )
        )
        parent = ChatSession(
            agent_id=storage_agent_id,
            peer_agent_id=execution_agent_id,
            user_id=None,
            title="Revoked A2A parent",
            source_channel="agent",
            is_primary=False,
            is_group=False,
        )
        db.add(parent)
        await db.flush()
        db.add(
            ChatMessage(
                agent_id=storage_agent_id,
                user_id=user_id,
                sender_agent_id=execution_agent_id,
                role="assistant",
                content="parent turn completed",
                conversation_id=str(parent.id),
                message_meta={"attachments": []},
            )
        )
        await db.commit()
        parent_id = parent.id
    run, _ = await runtime.create_subagent(
        agent_id=execution_agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-revoked-parent-wake",
        task="revoked wake",
        mode="async",
    )
    await runtime.send_subagent_message_to_parent(
        agent_id=execution_agent_id,
        execution_user_id=user_id,
        origin_tool_call_id="revoked-parent-message",
        subagent_session_id=str(run.id),
        message="must not resume",
    )
    async with async_session() as db:
        event = await db.scalar(
            select(ChatMessage)
            .where(
                ChatMessage.conversation_id == str(run.id),
                ChatMessage.message_meta["kind"].as_string() == runtime.SUBAGENT_PARENT_MESSAGE,
            )
            .limit(1)
        )
        user = await db.get(User, user_id)
        user.is_active = False
        await db.commit()

    async def fail_resume(_anchor):
        raise AssertionError("revoked execution identity must not resume parent LLM")

    broadcasts: list[tuple[str, str, dict]] = []

    async def capture_broadcast(owner_agent_id, conversation_id, payload):
        broadcasts.append((str(owner_agent_id), str(conversation_id), payload))

    monkeypatch.setattr("app.services.turn_recovery.resume_turn", fail_resume)
    monkeypatch.setattr("app.api.websocket.manager.send_to_session", capture_broadcast)
    assert await runtime._dispatch_parent_event(event.id)

    async with async_session() as db:
        anchor = await db.scalar(
            select(ChatMessage).where(ChatMessage.external_event_key == f"subagent-parent:{event.id}")
        )
        final = await db.scalar(
            select(ChatMessage)
            .where(
                ChatMessage.conversation_id == str(parent_id),
                ChatMessage.role == "assistant",
                ChatMessage.message_meta["turn_anchor_id"].as_string() == str(anchor.id),
            )
            .limit(1)
        )
        current_turn = await get_conversation_turn_snapshot(
            db,
            agent_id=storage_agent_id,
            conversation_id=str(parent_id),
        )
    assert anchor.message_meta["turn_status"] == "failed"
    assert "执行身份已失效" in final.content
    assert final.agent_id == storage_agent_id
    assert final.sender_agent_id == execution_agent_id
    assert current_turn.anchor_id == anchor.id
    assert current_turn.status == "failed"
    assert current_turn.revision >= 1
    assert broadcasts
    assert {row[0] for row in broadcasts} == {str(storage_agent_id)}
    assert {row[1] for row in broadcasts} == {str(parent_id)}
