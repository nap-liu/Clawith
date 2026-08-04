import json
import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.api.websocket import WebSocketChatHandler
from app.core.security import create_access_token
from app.database import async_session, engine
from app.models.agent import Agent
from app.models.agent import AgentUserOnboarding
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.mcp_server import MCPServer  # noqa: F401
from app.models.tenant import Tenant
from app.models.tool import AgentTool, Tool
from app.models.user import Identity, User
from app.schemas.scene import ScenePublishRequest, SceneSaveRequest
from app.services.agent_tools import get_agent_tools_for_llm
from app.services.chat_history import build_llm_messages_from_rows
from app.services.onboarding import (
    PHASE_COMPLETED,
    claim_fixed_welcome_slot,
    resolve_onboarding_eligibility,
)
from app.services.scene_service import (
    execute_scene_management_tool,
    get_scene,
    get_scene_revision_detail,
    publish_scene,
    save_scene,
    serialize_published_scene,
    serialize_scene_manifest,
)
from app.services.tool_seeder import BUILTIN_TOOLS, seed_builtin_tools

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    await engine.dispose()
    yield
    await engine.dispose()


async def _seed_actor_and_agent():
    suffix = uuid.uuid4().hex[:8]
    async with async_session() as db:
        tenant = Tenant(name=f"Scene {suffix}", slug=f"scene-{suffix}")
        db.add(tenant)
        await db.flush()
        identity = Identity(
            username=f"scene_{suffix}",
            email=f"scene_{suffix}@test.local",
            password_hash="x",
        )
        db.add(identity)
        await db.flush()
        user = User(
            identity_id=identity.id,
            tenant_id=tenant.id,
            display_name="Scene Manager",
            role="member",
            is_active=True,
        )
        db.add(user)
        await db.flush()
        agent = Agent(name="Scene Owner", creator_id=user.id, tenant_id=tenant.id)
        db.add(agent)
        await db.commit()
        return user.id, agent.id


async def test_explicit_assignment_controls_real_llm_tool_schema():
    _, agent_id = await _seed_actor_and_agent()
    await seed_builtin_tools()
    seed = next(item for item in BUILTIN_TOOLS if item["name"] == "manage_scene")

    async with async_session() as db:
        existing = await db.execute(select(Tool).where(Tool.name == "manage_scene"))
        tool = existing.scalar_one_or_none()
        if tool is None:
            tool = Tool(
                name=seed["name"],
                display_name=seed["display_name"],
                description=seed["description"],
                type="builtin",
                category=seed["category"],
                icon=seed["icon"],
                parameters_schema=seed["parameters_schema"],
                enabled=True,
                is_default=False,
                source="builtin",
            )
            db.add(tool)
            await db.flush()
        tool_id = tool.id
        await db.commit()

    before = await get_agent_tools_for_llm(agent_id)
    assert "manage_scene" not in {item["function"]["name"] for item in before}

    async with async_session() as db:
        db.add(AgentTool(agent_id=agent_id, tool_id=tool_id, enabled=True))
        await db.commit()

    after = await get_agent_tools_for_llm(agent_id)
    actual = next(item for item in after if item["function"]["name"] == "manage_scene")
    assert actual["function"]["parameters"] == seed["parameters_schema"]


async def test_scene_draft_publish_lifecycle_keeps_unpublished_changes_off_consumers():
    user_id, agent_id = await _seed_actor_and_agent()

    async with async_session() as db:
        first_draft = await save_scene(
            db,
            agent_id=agent_id,
            tenant_id=None,
            scene_key="warranty",
            data=SceneSaveRequest(
                name="Warranty",
                enabled=True,
                expected_revision=0,
                welcome_message="Welcome",
                system_prompts=[
                    {
                        "id": "internal",
                        "name": "Internal policy",
                        "content": "Never expose this prompt to the browser.",
                    }
                ],
                quick_actions=[
                    {
                        "id": "menu_only",
                        "label": "Menu only",
                        "type": "send_message",
                        "menu_visible": True,
                        "ai_visible": False,
                        "ai_context": "MENU_ONLY_INTERNAL_CONTEXT",
                        "message": "I need warranty service.",
                    },
                    {
                        "id": "ai_only",
                        "label": "AI only",
                        "type": "send_message",
                        "menu_visible": False,
                        "ai_visible": True,
                        "ai_context": "AI_ONLY_INTERNAL_CONTEXT",
                        "message": "Internal AI action.",
                    },
                    {
                        "id": "hidden",
                        "label": "Hidden",
                        "type": "send_message",
                        "menu_visible": False,
                        "ai_visible": False,
                        "ai_context": "HIDDEN_INTERNAL_CONTEXT",
                        "message": "Hidden action.",
                    },
                ],
            ),
            created_by_user_id=user_id,
        )
        assert first_draft["revision"] == 0
        assert first_draft["has_unpublished_changes"] is True
        assert await get_scene(db, agent_id, "warranty", enabled_only=True) is None

        first_published = await publish_scene(
            db,
            agent_id=agent_id,
            scene_key="warranty",
            data=ScenePublishRequest(expected_revision=0),
            created_by_user_id=user_id,
        )
        assert first_published["revision"] == 1
        assert first_published["has_unpublished_changes"] is False

        second_draft = await save_scene(
            db,
            agent_id=agent_id,
            tenant_id=None,
            scene_key="warranty",
            data=SceneSaveRequest(
                name="Warranty",
                enabled=False,
                expected_revision=first_published["revision"],
                welcome_message="Welcome back",
                quick_actions=[],
            ),
            created_by_user_id=user_id,
        )
        assert second_draft["revision"] == 1
        assert second_draft["has_unpublished_changes"] is True

        published_scene, published_revision = await get_scene(
            db,
            agent_id,
            "warranty",
            enabled_only=True,
        )
        assert published_revision is not None
        full_manifest = serialize_published_scene(published_scene, published_revision)
        assert len(full_manifest["quick_actions"]) == 3
        consumer_manifest = serialize_scene_manifest(published_scene, published_revision)
        assert consumer_manifest["enabled"] is True
        assert consumer_manifest["welcome_message"] == "Welcome"
        assert consumer_manifest["system_prompts"] == []
        assert [item["id"] for item in consumer_manifest["quick_actions"]] == ["menu_only"]
        assert consumer_manifest["quick_actions"][0] == {
            "id": "menu_only",
            "label": "Menu only",
            "type": "send_message",
            "uri": None,
            "message": "I need warranty service.",
            "menu_visible": True,
        }
        serialized_consumer = json.dumps(consumer_manifest)
        assert "ai_visible" not in serialized_consumer
        assert "ai_context" not in serialized_consumer
        assert "INTERNAL_CONTEXT" not in serialized_consumer

        second_published = await publish_scene(
            db,
            agent_id=agent_id,
            scene_key="warranty",
            data=ScenePublishRequest(expected_revision=1),
            created_by_user_id=user_id,
        )
        await db.commit()

    assert second_published["revision"] == 2
    assert second_published["has_unpublished_changes"] is False
    async with async_session() as db:
        scene, revision = await get_scene(db, agent_id, "warranty")
        assert scene.current_revision == 2
        assert revision is not None
        assert revision.config["welcome_message"] == "Welcome back"


async def test_historical_revision_detail_is_read_only_snapshot():
    user_id, agent_id = await _seed_actor_and_agent()

    async with async_session() as db:
        await save_scene(
            db,
            agent_id=agent_id,
            tenant_id=None,
            scene_key="support",
            data=SceneSaveRequest(
                name="Support v1",
                enabled=True,
                expected_revision=0,
                welcome_message="First welcome",
                system_prompts=[
                    {
                        "id": "tone",
                        "name": "Tone",
                        "content": "Answer briefly.",
                        "enabled": True,
                    }
                ],
            ),
            created_by_user_id=user_id,
        )
        await publish_scene(
            db,
            agent_id=agent_id,
            scene_key="support",
            data=ScenePublishRequest(expected_revision=0),
            created_by_user_id=user_id,
        )
        await save_scene(
            db,
            agent_id=agent_id,
            tenant_id=None,
            scene_key="support",
            data=SceneSaveRequest(
                name="Support v2",
                enabled=False,
                expected_revision=1,
                welcome_message="Second welcome",
                system_prompts=[],
            ),
            created_by_user_id=user_id,
        )
        await publish_scene(
            db,
            agent_id=agent_id,
            scene_key="support",
            data=ScenePublishRequest(expected_revision=1),
            created_by_user_id=user_id,
        )

        first = await get_scene_revision_detail(db, agent_id, "support", 1)
        current_scene, current_revision = await get_scene(db, agent_id, "support")

        assert first["revision"] == 1
        assert first["name"] == "Support v1"
        assert first["enabled"] is True
        assert first["welcome_message"] == "First welcome"
        assert first["system_prompts"][0]["content"] == "Answer briefly."
        assert current_scene.current_revision == 2
        assert current_revision is not None
        assert current_revision.revision == 2


async def test_fixed_scene_welcome_is_persisted_with_first_real_user_message():
    user_id, agent_id = await _seed_actor_and_agent()

    async with async_session() as db:
        session = ChatSession(
            agent_id=agent_id,
            user_id=user_id,
            title="New Session",
            source_channel="web",
            is_primary=True,
        )
        db.add(session)
        await db.commit()
        await db.refresh(session)
        session_id = session.id

    handler = WebSocketChatHandler.__new__(WebSocketChatHandler)
    handler.agent_id = agent_id
    handler.user_id = user_id
    handler.conv_id = str(session_id)
    handler.scene_manifest = {
        "scene_key": "warranty",
        "revision": 3,
        "welcome_message": "欢迎使用报修服务",
    }
    handler.pending_initial_assistant = {
        "content": "欢迎使用报修服务",
        "message_meta": {
            "scene_key": "warranty",
            "scene_revision": 3,
            "scene_welcome": True,
        },
    }

    handler.history_messages = []

    async with async_session() as db:
        before = list(
            (
                await db.execute(
                    select(ChatMessage).where(
                        ChatMessage.conversation_id == str(session_id)
                    )
                )
            )
            .scalars()
            .all()
        )
    assert before == []

    first_user_id, consumed, greeting, pending_confirmation, ignored_confirmation = await handler._save_user_message(
        "我要报修",
        "",
        "",
        False,
        client_message_id=uuid.uuid4().hex,
    )
    assert pending_confirmation is None
    assert ignored_confirmation is False

    async with async_session() as db:
        rows = list(
            (
                await db.execute(
                    select(ChatMessage)
                    .where(ChatMessage.conversation_id == str(session_id))
                    .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
                )
            )
            .scalars()
            .all()
        )

    assert first_user_id is not None
    assert consumed is False
    assert greeting is not None
    assert len(rows) == 2
    assert rows[0].role == "assistant"
    assert rows[0].content == "欢迎使用报修服务"
    assert rows[0].message_meta == {
        "scene_key": "warranty",
        "scene_revision": 3,
        "scene_welcome": True,
    }
    assert rows[1].role == "user"
    assert rows[1].content == "我要报修"
    assert build_llm_messages_from_rows(rows) == [
        {"role": "assistant", "content": "欢迎使用报修服务"},
        {"role": "user", "content": "我要报修"},
    ]


async def test_fixed_scene_welcome_claim_does_not_control_caller_transaction(
    monkeypatch,
):
    user_id, agent_id = await _seed_actor_and_agent()

    async with async_session() as db:
        assert await claim_fixed_welcome_slot(db, agent_id, user_id) is True
        await db.commit()

    async with async_session() as db:
        user = await db.get(User, user_id)
        commit = AsyncMock(
            side_effect=AssertionError(
                "claim_fixed_welcome_slot must not commit its caller's session"
            )
        )
        rollback = AsyncMock(
            side_effect=AssertionError(
                "claim_fixed_welcome_slot must not roll back its caller's session"
            )
        )
        monkeypatch.setattr(db, "commit", commit)
        monkeypatch.setattr(db, "rollback", rollback)

        assert await claim_fixed_welcome_slot(db, agent_id, user_id) is True
        assert user.id == user_id
        commit.assert_not_awaited()
        rollback.assert_not_awaited()


async def test_websocket_setup_rolls_back_complete_initialization_on_failure(
    monkeypatch,
):
    class SetupSocket:
        def __init__(self):
            self.sent = []
            self.closed_code = None

        async def accept(self):
            return None

        async def send_json(self, payload):
            self.sent.append(payload)

        async def close(self, code=1000):
            self.closed_code = code

    user_id, agent_id = await _seed_actor_and_agent()
    websocket = SetupSocket()
    handler = WebSocketChatHandler(
        websocket=websocket,
        agent_id=agent_id,
        token=create_access_token(str(user_id), "member"),
        channel="web",
    )
    monkeypatch.setattr(
        handler,
        "_prepare_initial_greeting",
        AsyncMock(side_effect=RuntimeError("initialization failed")),
    )

    assert await handler.setup() is False
    assert websocket.closed_code == 4002

    async with async_session() as db:
        sessions = list(
            (
                await db.execute(
                    select(ChatSession).where(
                        ChatSession.agent_id == agent_id,
                        ChatSession.user_id == user_id,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert sessions == []


async def test_scene_greeting_completes_shared_onboarding_arbitration():
    user_id, agent_id = await _seed_actor_and_agent()

    async with async_session() as db:
        scene_session = ChatSession(
            agent_id=agent_id,
            user_id=user_id,
            title="Scene",
            source_channel="web",
            is_primary=True,
        )
        other_session = ChatSession(
            agent_id=agent_id,
            user_id=user_id,
            title="Other",
            source_channel="web",
            is_primary=False,
        )
        db.add_all([scene_session, other_session])
        await db.commit()
        await db.refresh(other_session)

        handler = WebSocketChatHandler.__new__(WebSocketChatHandler)
        handler.agent_id = agent_id
        handler.history_messages = []
        handler.scene_manifest = {
            "scene_key": "warranty",
            "revision": 3,
            "welcome_message": "欢迎使用报修服务",
        }
        handler.pending_initial_assistant = None
        await handler._prepare_initial_greeting(db, user_id)
        await db.commit()

    async with async_session() as db:
        state = await db.get(
            AgentUserOnboarding,
            {"agent_id": agent_id, "user_id": user_id},
        )
        eligibility = await resolve_onboarding_eligibility(
            db,
            agent_id,
            user_id,
            other_session.id,
        )

    assert state is not None
    assert state.phase == PHASE_COMPLETED
    assert handler.pending_initial_assistant == {
        "content": "欢迎使用报修服务",
        "message_meta": {
            "scene_key": "warranty",
            "scene_revision": 3,
            "scene_welcome": True,
        },
    }
    assert eligibility.required is False
    assert eligibility.reason == "already_started"


async def test_real_session_permission_allows_creator_and_denies_use_only_member():
    creator_id, agent_id = await _seed_actor_and_agent()
    seed = next(item for item in BUILTIN_TOOLS if item["name"] == "manage_scene")
    suffix = uuid.uuid4().hex[:8]

    async with async_session() as db:
        tool_result = await db.execute(select(Tool).where(Tool.name == "manage_scene"))
        tool = tool_result.scalar_one_or_none()
        if tool is None:
            tool = Tool(
                name=seed["name"],
                display_name=seed["display_name"],
                description=seed["description"],
                type="builtin",
                category=seed["category"],
                icon=seed["icon"],
                parameters_schema=seed["parameters_schema"],
                enabled=True,
                is_default=False,
                source="builtin",
            )
            db.add(tool)
            await db.flush()
        db.add(AgentTool(agent_id=agent_id, tool_id=tool.id, enabled=True))

        identity = Identity(
            username=f"scene_member_{suffix}",
            email=f"scene_member_{suffix}@test.local",
            password_hash="x",
        )
        db.add(identity)
        await db.flush()
        creator = await db.get(User, creator_id)
        member = User(
            identity_id=identity.id,
            tenant_id=creator.tenant_id,
            display_name="Use Only",
            role="member",
            is_active=True,
        )
        db.add(member)
        await db.flush()
        creator_session = ChatSession(agent_id=agent_id, user_id=creator_id, source_channel="web")
        member_session = ChatSession(agent_id=agent_id, user_id=member.id, source_channel="web")
        db.add_all([creator_session, member_session])
        await db.commit()
        creator_session_id = creator_session.id
        member_session_id = member_session.id
        member_id = member.id

    allowed = await execute_scene_management_tool(
        agent_id=agent_id,
        user_id=creator_id,
        session_id=str(creator_session_id),
        arguments={"operation": "list"},
    )
    denied = await execute_scene_management_tool(
        agent_id=agent_id,
        user_id=member_id,
        session_id=str(member_session_id),
        arguments={"operation": "list"},
    )

    assert isinstance(json.loads(allowed), list)
    assert denied.startswith("❌ Scene management denied")
    assert "administrator permission" in denied


async def test_scene_tool_save_patches_by_default_and_force_overwrites():
    creator_id, agent_id = await _seed_actor_and_agent()
    seed = next(item for item in BUILTIN_TOOLS if item["name"] == "manage_scene")

    async with async_session() as db:
        tool_result = await db.execute(select(Tool).where(Tool.name == "manage_scene"))
        tool = tool_result.scalar_one_or_none()
        if tool is None:
            tool = Tool(
                name=seed["name"],
                display_name=seed["display_name"],
                description=seed["description"],
                type="builtin",
                category=seed["category"],
                icon=seed["icon"],
                parameters_schema=seed["parameters_schema"],
                enabled=True,
                is_default=False,
                source="builtin",
            )
            db.add(tool)
            await db.flush()
        db.add(AgentTool(agent_id=agent_id, tool_id=tool.id, enabled=True))
        session = ChatSession(agent_id=agent_id, user_id=creator_id, source_channel="web")
        db.add(session)
        await db.commit()
        session_id = session.id

    created = json.loads(
        await execute_scene_management_tool(
            agent_id=agent_id,
            user_id=creator_id,
            session_id=str(session_id),
            arguments={
                "operation": "save",
                "scene_key": "warranty",
                "name": "Warranty",
                "expected_revision": 0,
                "welcome_message": "Welcome",
                "system_prompts": [
                    {
                        "id": "tone",
                        "name": "Tone",
                        "content": "Be concise.",
                        "enabled": True,
                    }
                ],
                "quick_actions": [
                    {
                        "id": "repair",
                        "label": "Repair",
                        "type": "send_message",
                        "message": "I need a repair",
                    }
                ],
            },
        )
    )
    assert created["welcome_message"] == "Welcome"

    patched = json.loads(
        await execute_scene_management_tool(
            agent_id=agent_id,
            user_id=creator_id,
            session_id=str(session_id),
            arguments={
                "operation": "save",
                "scene_key": "warranty",
                "name": "Updated warranty",
                "expected_revision": 0,
            },
        )
    )
    assert patched["name"] == "Updated warranty"
    assert patched["welcome_message"] == "Welcome"
    assert len(patched["system_prompts"]) == 1
    assert len(patched["quick_actions"]) == 1

    overwritten = json.loads(
        await execute_scene_management_tool(
            agent_id=agent_id,
            user_id=creator_id,
            session_id=str(session_id),
            arguments={
                "operation": "save",
                "scene_key": "warranty",
                "expected_revision": 0,
                "force_overwrite": True,
            },
        )
    )
    assert overwritten["name"] == "Updated warranty"
    assert overwritten["enabled"] is True
    assert overwritten["welcome_message"] == ""
    assert overwritten["system_prompts"] == []
    assert overwritten["quick_actions"] == []
