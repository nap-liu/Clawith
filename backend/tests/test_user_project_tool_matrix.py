"""PostgreSQL behavior matrix for Human-delegated standard Agent project tools."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

import pytest
from sqlalchemy import delete, func, select
from test_project_actions import ProjectApiEnv, _create_project, _user

from app.models.activity_log import AgentActivityLog
from app.models.agent import AgentPermission
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.project import Project, ProjectAccessGrant, ProjectEvent, ProjectRun, ProjectWorkItem
from app.models.tenant import Tenant
from app.models.tool import AgentTool, Tool
from app.models.user import User
from app.services.agent_tools import execute_tool
from app.services.user_project_tools import USER_PROJECT_TOOL_SEEDS

pytestmark = pytest.mark.asyncio
pytest_plugins = ("test_project_actions",)


@dataclass(frozen=True)
class HumanTurn:
    user_id: uuid.UUID
    session_id: uuid.UUID
    anchor_id: uuid.UUID
    tool_row_id: uuid.UUID


@dataclass
class ToolMatrixEnv:
    project_api: ProjectApiEnv
    project_id: uuid.UUID
    standard_agent_id: uuid.UUID
    owner: User
    editor: User
    viewer: User
    removed: User

    @property
    def db(self):
        return self.project_api.db

    async def turn(
        self,
        actor: User,
        tool_name: str,
        *,
        source_channel: str = "web",
        message_meta: dict | None = None,
    ) -> HumanTurn:
        session = ChatSession(
            agent_id=self.standard_agent_id,
            user_id=actor.id,
            title=f"{source_channel} project assistance",
            source_channel=source_channel,
            external_conv_id=(f"{source_channel}-{uuid.uuid4()}" if source_channel != "web" else None),
            is_group=False,
        )
        self.db.add(session)
        await self.db.flush()
        anchor = ChatMessage(
            agent_id=self.standard_agent_id,
            user_id=actor.id,
            sender_user_id=actor.id,
            role="user",
            content="Please assist with the selected project.",
            conversation_id=str(session.id),
            message_meta=message_meta or {},
        )
        self.db.add(anchor)
        await self.db.flush()
        tool_row = ChatMessage(
            agent_id=self.standard_agent_id,
            user_id=actor.id,
            role="tool_call",
            content=json.dumps(
                {"name": tool_name, "args": {}, "status": "running"},
                separators=(",", ":"),
            ),
            conversation_id=str(session.id),
        )
        self.db.add(tool_row)
        await self.db.commit()
        return HumanTurn(actor.id, session.id, anchor.id, tool_row.id)

    async def call(
        self,
        turn: HumanTurn,
        tool_name: str,
        arguments: dict,
        *,
        resumed_after_confirmation: bool = False,
    ) -> str:
        return await execute_tool(
            tool_name,
            arguments,
            agent_id=self.standard_agent_id,
            user_id=turn.user_id,
            session_id=str(turn.session_id),
            tool_call_id=str(turn.tool_row_id),
            turn_anchor_id=turn.anchor_id,
            skip_autonomy=resumed_after_confirmation,
        )


@pytest.fixture
async def tool_matrix_env(
    project_api: ProjectApiEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> ToolMatrixEnv:
    env = project_api
    engine = env.session_factory.kw["bind"]
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: AgentActivityLog.__table__.create(
                sync_connection,
                checkfirst=True,
            )
        )

    monkeypatch.setattr("app.services.user_project_tools.async_session", env.session_factory)
    monkeypatch.setattr("app.services.activity_logger.async_session", env.session_factory)
    monkeypatch.setattr("app.services.agent_tools.async_session", env.session_factory)

    project_payload = await _create_project(env, name="Standard Agent tool matrix")
    project_id = uuid.UUID(project_payload["id"])
    project = await env.db.get(Project, project_id)
    assert project is not None
    project.status = "running"
    project.visibility = "shared"

    tenant = await env.db.get(Tenant, env.tenant_id)
    assert tenant is not None
    editor = await _user(env.db, tenant, "Editor")
    removed = await _user(env.db, tenant, "Removed")
    viewer = env.viewer
    standard_agent_id = env.source_leader_id
    env.leader.access_mode = "custom"

    for actor in (editor, viewer, removed):
        env.db.add(
            AgentPermission(
                agent_id=standard_agent_id,
                scope_type="user",
                scope_id=actor.id,
                access_level="use",
            )
        )
    env.db.add_all(
        [
            ProjectAccessGrant(
                tenant_id=env.tenant_id,
                project_id=project_id,
                user_id=editor.id,
                role="edit",
                created_by_user_id=env.owner_id,
            ),
            ProjectAccessGrant(
                tenant_id=env.tenant_id,
                project_id=project_id,
                user_id=viewer.id,
                role="view",
                created_by_user_id=env.owner_id,
            ),
            ProjectAccessGrant(
                tenant_id=env.tenant_id,
                project_id=project_id,
                user_id=removed.id,
                role="view",
                created_by_user_id=env.owner_id,
            ),
        ]
    )
    for seed in USER_PROJECT_TOOL_SEEDS:
        tool = Tool(
            name=seed["name"],
            display_name=seed["display_name"],
            description=seed["description"],
            type="builtin",
            category=seed["category"],
            icon=seed["icon"],
            parameters_schema=seed["parameters_schema"],
            config=seed["config"],
            config_schema=seed["config_schema"],
            enabled=True,
            is_default=False,
            source="builtin",
        )
        env.db.add(tool)
        await env.db.flush()
        env.db.add(
            AgentTool(
                agent_id=standard_agent_id,
                tool_id=tool.id,
                enabled=True,
                source="system",
            )
        )
    await env.db.commit()

    return ToolMatrixEnv(
        project_api=env,
        project_id=project_id,
        standard_agent_id=standard_agent_id,
        owner=env.owner,
        editor=editor,
        viewer=viewer,
        removed=removed,
    )


def _payload(result: str) -> dict:
    assert not result.startswith("❌"), result
    return json.loads(result)


async def test_web_and_mapped_im_human_turns_use_the_real_conversation_actor(
    tool_matrix_env: ToolMatrixEnv,
):
    env = tool_matrix_env
    web_turn = await env.turn(env.owner, "user_project_get", source_channel="web")
    im_turn = await env.turn(
        env.owner,
        "user_project_get",
        source_channel="dingtalk",
        message_meta={"provider_sender_ref": "mapped-human"},
    )

    web_project = _payload(await env.call(web_turn, "user_project_get", {"project_id": str(env.project_id)}))
    im_project = _payload(await env.call(im_turn, "user_project_get", {"project_id": str(env.project_id)}))

    assert web_project["id"] == im_project["id"] == str(env.project_id)
    assert web_project["access_role"] == im_project["access_role"] == "owner"


async def test_non_human_turn_with_user_id_cannot_unlock_project_tools(
    tool_matrix_env: ToolMatrixEnv,
):
    env = tool_matrix_env
    subagent_turn = await env.turn(
        env.owner,
        "user_project_get",
        source_channel="subagent",
        message_meta={"subagent_run_id": str(uuid.uuid4())},
    )

    result = await env.call(
        subagent_turn,
        "user_project_get",
        {"project_id": str(env.project_id)},
    )

    assert result == "❌ This capability requires an active user conversation"


async def test_owner_editor_viewer_removed_acl_is_rechecked_for_each_call(
    tool_matrix_env: ToolMatrixEnv,
):
    env = tool_matrix_env
    for actor, expected_role in (
        (env.owner, "owner"),
        (env.editor, "edit"),
        (env.viewer, "view"),
    ):
        turn = await env.turn(actor, "user_project_get")
        project = _payload(await env.call(turn, "user_project_get", {"project_id": str(env.project_id)}))
        assert project["access_role"] == expected_role

    owner_write = await env.turn(env.owner, "user_project_work_item_create")
    editor_write = await env.turn(env.editor, "user_project_work_item_create")
    viewer_write = await env.turn(env.viewer, "user_project_work_item_create")
    assert (
        _payload(
            await env.call(
                owner_write,
                "user_project_work_item_create",
                {"project_id": str(env.project_id), "title": "Owner delivery"},
            )
        )["title"]
        == "Owner delivery"
    )
    assert (
        _payload(
            await env.call(
                editor_write,
                "user_project_work_item_create",
                {"project_id": str(env.project_id), "title": "Editor delivery"},
            )
        )["title"]
        == "Editor delivery"
    )
    assert (
        await env.call(
            viewer_write,
            "user_project_work_item_create",
            {"project_id": str(env.project_id), "title": "Viewer must not write"},
        )
        == "❌ Project not found"
    )

    editor_owner_action = await env.turn(env.editor, "user_project_status_update")
    assert (
        await env.call(
            editor_owner_action,
            "user_project_status_update",
            {"project_id": str(env.project_id), "status": "paused"},
        )
        == "❌ Project not found"
    )

    await env.db.execute(
        delete(ProjectAccessGrant).where(
            ProjectAccessGrant.project_id == env.project_id,
            ProjectAccessGrant.user_id == env.removed.id,
        )
    )
    await env.db.commit()
    removed_read = await env.turn(env.removed, "user_project_get")
    assert (
        await env.call(
            removed_read,
            "user_project_get",
            {"project_id": str(env.project_id)},
        )
        == "❌ Project not found"
    )


async def test_confirmation_resume_rechecks_acl_instead_of_reusing_prior_access(
    tool_matrix_env: ToolMatrixEnv,
):
    env = tool_matrix_env
    pre_confirmation = await env.turn(env.editor, "user_project_get")
    assert (
        _payload(
            await env.call(
                pre_confirmation,
                "user_project_get",
                {"project_id": str(env.project_id)},
            )
        )["access_role"]
        == "edit"
    )

    resumed_turn = await env.turn(env.editor, "user_project_work_item_create")
    await env.db.execute(
        delete(ProjectAccessGrant).where(
            ProjectAccessGrant.project_id == env.project_id,
            ProjectAccessGrant.user_id == env.editor.id,
        )
    )
    await env.db.commit()

    result = await env.call(
        resumed_turn,
        "user_project_work_item_create",
        {"project_id": str(env.project_id), "title": "Stale approval must fail"},
        resumed_after_confirmation=True,
    )

    assert result == "❌ Project not found"
    assert (
        await env.db.scalar(
            select(func.count()).select_from(ProjectWorkItem).where(ProjectWorkItem.title == "Stale approval must fail")
        )
        == 0
    )


async def test_work_item_pagination_and_cross_project_ids_are_bounded(
    tool_matrix_env: ToolMatrixEnv,
):
    env = tool_matrix_env
    for index in range(5):
        env.db.add(
            ProjectWorkItem(
                tenant_id=env.project_api.tenant_id,
                project_id=env.project_id,
                created_by_user_id=env.owner.id,
                title=f"Paged item {index}",
                status="backlog",
                priority="medium",
            )
        )
    other_project = Project(
        tenant_id=env.project_api.tenant_id,
        owner_user_id=env.owner.id,
        execution_user_id=env.owner.id,
        name="Other project",
        goal="Keep identifiers isolated",
        status="running",
    )
    env.db.add(other_project)
    await env.db.flush()
    other_item = ProjectWorkItem(
        tenant_id=env.project_api.tenant_id,
        project_id=other_project.id,
        created_by_user_id=env.owner.id,
        title="Other project item",
        status="backlog",
        priority="medium",
    )
    env.db.add(other_item)
    await env.db.commit()

    first_turn = await env.turn(env.owner, "user_project_work_item_list")
    second_turn = await env.turn(env.owner, "user_project_work_item_list")
    first_page = _payload(
        await env.call(
            first_turn,
            "user_project_work_item_list",
            {"project_id": str(env.project_id), "offset": 0, "limit": 2},
        )
    )
    second_page = _payload(
        await env.call(
            second_turn,
            "user_project_work_item_list",
            {
                "project_id": str(env.project_id),
                "offset": first_page["next_offset"],
                "limit": 2,
            },
        )
    )
    assert first_page["has_more"] is True
    assert first_page["next_offset"] == 2
    assert second_page["offset"] == 2
    assert {item["id"] for item in first_page["items"]}.isdisjoint(item["id"] for item in second_page["items"])
    assert all(item["project_id"] == str(env.project_id) for item in first_page["items"])

    cross_turn = await env.turn(env.owner, "user_project_work_item_get")
    result = await env.call(
        cross_turn,
        "user_project_work_item_get",
        {"project_id": str(env.project_id), "work_item_id": str(other_item.id)},
    )
    assert result == "❌ Work item not found"


async def test_paused_project_blocks_new_run_but_keeps_authorized_reads(
    tool_matrix_env: ToolMatrixEnv,
):
    env = tool_matrix_env
    project = await env.db.get(Project, env.project_id)
    assert project is not None
    project.status = "paused"
    await env.db.commit()

    read_turn = await env.turn(env.viewer, "user_project_get")
    project_payload = _payload(
        await env.call(
            read_turn,
            "user_project_get",
            {"project_id": str(env.project_id)},
        )
    )
    assert project_payload["status"] == "paused"

    run_turn = await env.turn(env.owner, "user_project_run_start")
    result = await env.call(
        run_turn,
        "user_project_run_start",
        {"project_id": str(env.project_id), "instruction": "Must wait for resume"},
    )
    assert "Project runtime is paused" in result
    assert (
        await env.db.scalar(select(func.count()).select_from(ProjectRun).where(ProjectRun.project_id == env.project_id))
        == 0
    )


async def test_successful_mutation_persists_complete_separate_evidence_anchors(
    tool_matrix_env: ToolMatrixEnv,
):
    env = tool_matrix_env
    turn = await env.turn(env.owner, "user_project_work_item_create")
    result = _payload(
        await env.call(
            turn,
            "user_project_work_item_create",
            {"project_id": str(env.project_id), "title": "Anchored delivery"},
        )
    )
    work_item_id = uuid.UUID(result["id"])

    await env.db.rollback()
    completion_event = (
        await env.db.execute(
            select(ProjectEvent).where(
                ProjectEvent.project_id == env.project_id,
                ProjectEvent.work_item_id == work_item_id,
                ProjectEvent.event_type == "project.management.action.completed",
            )
        )
    ).scalar_one()
    activity_rows = (
        (
            await env.db.execute(
                select(AgentActivityLog).where(
                    AgentActivityLog.agent_id == env.standard_agent_id,
                    AgentActivityLog.action_type == "tool_call",
                )
            )
        )
        .scalars()
        .all()
    )
    activity = next(
        row for row in activity_rows if (row.detail_json or {}).get("tool_call_id") == str(turn.tool_row_id)
    )

    assert completion_event.actor_user_id == env.owner.id
    assert completion_event.actor_agent_id == env.standard_agent_id
    assert completion_event.run_id is None
    assert completion_event.event_metadata == {
        "session_id": str(turn.session_id),
        "anchor_message_id": str(turn.anchor_id),
        "turn_anchor_id": str(turn.anchor_id),
        "tool_call_id": str(turn.tool_row_id),
        "tool_name": "user_project_work_item_create",
        "outcome": "completed",
    }
    assert activity.related_id == env.project_id
    assert activity.detail_json == {
        "capability": "project_management",
        "tool": "user_project_work_item_create",
        "outcome": "completed",
        "actor_user_id": str(env.owner.id),
        "source_session_id": str(turn.session_id),
        "source_message_id": str(turn.anchor_id),
        "tool_call_id": str(turn.tool_row_id),
        "project_id": str(env.project_id),
    }
    assert await env.db.get(ChatSession, turn.session_id) is not None
    assert (await env.db.get(ChatMessage, turn.anchor_id)).sender_user_id == env.owner.id
    assert (await env.db.get(ChatMessage, turn.tool_row_id)).sender_agent_id == env.standard_agent_id
