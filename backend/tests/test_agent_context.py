"""Tests for build_agent_context's is_group / Current Conversation logic."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

# Import the full model graph so FK references resolve at table-mapping time.
from app.models.user import User, Identity  # noqa: F401
from app.models.tenant import Tenant  # noqa: F401
from app.models.identity import IdentityProvider, SSOScanSession  # noqa: F401
from app.models.participant import Participant  # noqa: F401
from app.models.agent import Agent
from app.database import async_session, engine
from app.services.agent_context import (
    SCENE_QUICK_ACTION_CONTEXT_MAX_CHARS,
    SCENE_QUICK_ACTION_CONTEXT_TRUNCATION_NOTICE,
    _markdown_table_cell,
    _render_scene_quick_actions,
    build_agent_context,
)


pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _isolate_async_engine_between_tests():
    """Dispose the global async engine before each test."""
    await engine.dispose()
    yield
    await engine.dispose()


async def _seed_basic_agent() -> uuid.UUID:
    """Seed a minimal Identity → User → Agent chain so build_agent_context's
    internal DB queries succeed; return the agent id.

    Agent.creator_id is a NOT NULL FK to users — a random UUID will FK-violate.
    """
    suffix = uuid.uuid4().hex[:6]
    async with async_session() as db:
        identity = Identity(
            username=f"creator_{suffix}",
            email=f"creator_{suffix}@test.local",
            password_hash="x",
        )
        db.add(identity)
        await db.flush()
        user = User(
            identity_id=identity.id,
            display_name="Creator",
            role="member",
            is_active=True,
        )
        db.add(user)
        await db.flush()
        agent = Agent(
            name="Test Agent",
            creator_id=user.id,
        )
        db.add(agent)
        await db.commit()
        await db.refresh(agent)
        return agent.id


async def test_p2p_includes_current_conversation():
    """P2P 场景 (is_group=False): 必须含 ## Current Conversation 段."""
    agent_id = await _seed_basic_agent()
    current_user_id = uuid.uuid4()
    static_p, dynamic_p = await build_agent_context(
        agent_id,
        "Test Agent",
        "role",
        current_user_name="Alice",
        current_user_id=current_user_id,
        is_group=False,
    )
    assert "## Current Conversation" in dynamic_p
    assert "Alice" in dynamic_p
    assert str(current_user_id) in dynamic_p
    # The Message Sender Tag rules block is unconditional — it must be in
    # static_parts even for P2P (regression guard against someone making
    # the rules-block injection conditional on is_group).
    assert "## Message Sender Tag (Group Chat)" in static_p
    assert "<sender id=" in static_p
    assert "VERY BEGINNING" in static_p
    assert "stable user identifier" in static_p
    assert "across sessions" in static_p
    assert "not a session-scoped" in static_p.lower()


async def test_agent_daily_memory_zero_reaches_unified_memory_loader():
    agent_id = await _seed_basic_agent()
    async with async_session() as db:
        agent = await db.get(Agent, agent_id)
        agent.daily_memory_load_days = 0
        await db.commit()

    loader = AsyncMock(return_value=SimpleNamespace(render=lambda: ""))
    with patch("app.services.agent_context.load_agent_memory_snapshot", loader):
        await build_agent_context(agent_id, "Test Agent", "role")

    assert loader.await_count == 1
    assert loader.await_args.kwargs["daily_limit"] == 0


async def test_group_excludes_current_conversation():
    """群聊场景 (is_group=True): 不含 ## Current Conversation 段."""
    agent_id = await _seed_basic_agent()
    static_p, dynamic_p = await build_agent_context(
        agent_id,
        "Test Agent",
        "role",
        current_user_name="Alice",
        is_group=True,
    )
    assert "## Current Conversation" not in dynamic_p


async def test_static_guides_agent_relative_markdown_images():
    agent_id = await _seed_basic_agent()
    static_p, _ = await build_agent_context(agent_id, "Test Agent", "role")

    assert "![clear description](workspace/path/to/image.png)" in static_p
    assert "Never construct a platform domain" in static_p
    assert "The platform resolves the relative image path" in static_p


async def test_no_user_name_no_current_conversation_either_mode():
    """current_user_name=None 时, P2P 和 group 都不应注入 Current Conversation."""
    agent_id = await _seed_basic_agent()
    for is_group in (False, True):
        _, dynamic_p = await build_agent_context(
            agent_id,
            "Test Agent",
            "role",
            current_user_name=None,
            is_group=is_group,
        )
        assert "## Current Conversation" not in dynamic_p


async def test_channel_context_is_dynamic_not_static():
    agent_id = await _seed_basic_agent()
    static_p, dynamic_p = await build_agent_context(
        agent_id,
        "Test Agent",
        "role",
        current_user_name="Alice",
        is_group=False,
        channel_context={
            "source_channel": "miniprogram",
            "display_name": "小程序",
            "client_surface": "mini-program web-view",
        },
    )

    assert "## Current Channel" in dynamic_p
    assert "source_channel: miniprogram" in dynamic_p
    assert "display_name: 小程序" in dynamic_p
    assert "client_surface: mini-program web-view" in dynamic_p
    assert "## Current Channel" not in static_p


async def test_scene_quick_actions_follow_scene_prompts_in_dynamic_context():
    agent_id = await _seed_basic_agent()
    static_p, dynamic_p = await build_agent_context(
        agent_id,
        "Test Agent",
        "role",
        current_user_name="Alice",
        is_group=False,
        channel_context={
            "scene_key": "warranty",
            "scene_revision": 3,
            "scene_system_prompts": [
                {
                    "id": "tone",
                    "name": "Response style",
                    "content": "Keep answers concise.",
                    "enabled": True,
                }
            ],
            "scene_quick_actions": [
                {
                    "id": "action_repair_id",
                    "label": "我要报修",
                    "type": "send_message",
                    "ai_visible": True,
                    "ai_context": "仅适用于仍在保修期内的设备。\n提交前先确认设备编号。",
                    "message": "我要申请设备保修",
                },
                {
                    "id": "action_orders_id",
                    "label": "查看工单",
                    "type": "open_uri",
                    "ai_visible": True,
                    "uri": "/orders",
                },
                {
                    "id": "disabled",
                    "label": "暂停入口",
                    "type": "send_message",
                    "ai_visible": False,
                    "message": "不应进入上下文",
                },
            ],
        },
    )

    prompt_position = dynamic_p.index("### Response style")
    actions_position = dynamic_p.index("### Available Quick Actions")
    assert prompt_position < actions_position
    assert "| Title | Type | Content | AI Context |" in dynamic_p
    assert (
        "| 我要报修 | send_message | 我要申请设备保修 | "
        "仅适用于仍在保修期内的设备。<br>提交前先确认设备编号。 |"
    ) in dynamic_p
    assert "| 查看工单 | open_uri | /orders |  |" in dynamic_p
    assert "action_repair_id" not in dynamic_p
    assert "暂停入口" not in dynamic_p
    assert "不应进入上下文" not in dynamic_p
    assert "Available Quick Actions" not in static_p


async def test_scene_quick_actions_use_stable_bounded_context_projection():
    actions = [
        {
            "id": "first",
            "label": "First action",
            "type": "send_message",
            "ai_visible": True,
            "message": "m" * 12000,
            "ai_context": "c" * 4000,
        },
        {
            "id": "second",
            "label": "Second action",
            "type": "send_message",
            "ai_visible": True,
            "message": "n" * 12000,
            "ai_context": "d" * 4000,
        },
    ]

    rendered = _render_scene_quick_actions(actions)

    assert len(rendered) <= SCENE_QUICK_ACTION_CONTEXT_MAX_CHARS
    assert "| First action | send_message |" in rendered
    assert "| Second action | send_message |" not in rendered
    assert rendered.endswith(SCENE_QUICK_ACTION_CONTEXT_TRUNCATION_NOTICE)


async def test_scene_quick_action_table_cells_escape_markdown_compactly():
    assert _markdown_table_cell("a\\b|c\r\nd\ne\rf") == "a\\\\b\\|c<br>d<br>e<br>f"


async def test_scene_quick_action_keeps_single_row_after_escape_expansion():
    rendered = _render_scene_quick_actions(
        [
            {
                "id": "escaped",
                "label": "Escaped action",
                "type": "send_message",
                "ai_visible": True,
                "message": "|" * 12_000,
                "ai_context": "\\" * 4_000,
            }
        ]
    )

    assert len(rendered) <= SCENE_QUICK_ACTION_CONTEXT_MAX_CHARS
    assert "| Escaped action | send_message |" in rendered
    assert "\\|" in rendered
    assert "…" in rendered
    assert not rendered.endswith(SCENE_QUICK_ACTION_CONTEXT_TRUNCATION_NOTICE)


async def test_build_agent_context_does_not_inject_focus_block():
    """Focus is no longer injected into the dynamic context.

    Injecting completed/stale focus items into the system prompt was reinforcing
    old workflow patterns over updated soul.md instructions, so the ## Focus
    block injection is disabled (agents query focus via list_focus_items, and
    focus is DB-backed, not read from focus.md). This guards against anyone
    re-introducing focus.md → dynamic-context leakage.
    """
    agent_id = uuid.uuid4()

    async def fake_read_file(key, _max_chars=3000):
        # Even if focus.md existed in storage, it must NOT surface in context.
        if key == f"{agent_id}/focus.md":
            return "# Focus\n\n- [ ] follow_up: Check the deployment"
        return ""

    with (
        patch("app.services.agent_context._read_file_safe", side_effect=fake_read_file),
        patch("app.services.agent_context._load_skills_index", new_callable=AsyncMock, return_value=""),
        patch("app.services.timezone_utils.get_agent_timezone", new_callable=AsyncMock, return_value="UTC"),
    ):
        _static, dynamic = await build_agent_context(agent_id, "TestAgent")

    assert "## Focus" not in dynamic
    assert "follow_up: Check the deployment" not in dynamic
