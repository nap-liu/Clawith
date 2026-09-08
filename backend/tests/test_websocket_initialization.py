"""Initial Web/H5 output and hidden onboarding admission regressions."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from starlette.websockets import WebSocketDisconnect

from app.api import websocket as api
from app.database import async_session, engine
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.services.chat_history import HIDDEN_ONBOARDING_ANCHOR_KIND, load_messages_for_session
from app.services.conversation_turn_lifecycle import conversation_turn_snapshot_for_session
from app.services.scene_targets import encode_target, session_target_identity
from tests.test_scene_auto_activation import _seed_scene_runtime, publish, save_auto

pytestmark = pytest.mark.asyncio
CHANNELS = ["web", "miniprogram", "wechat_miniprogram"]


@pytest.fixture(autouse=True)
async def isolate_engine():
    await engine.dispose()
    yield
    await engine.dispose()


async def pristine_handler(channel, *, selection="none"):
    agent_id, user_id = await _seed_scene_runtime()
    async with async_session() as db:
        agent = await db.get(Agent, agent_id)
        session = ChatSession(agent_id=agent_id, user_id=user_id, source_channel=channel)
        db.add(session)
        await db.flush()
        target = {"target_ref": encode_target(await session_target_identity(db, agent, session))}
        if selection != "none":
            await save_auto(db, agent, user_id, target)
            await publish(db, agent, user_id)
        handler = api.WebSocketChatHandler(
            AsyncMock(), agent_id, "unused", channel=channel,
            scene="warranty" if selection == "explicit" else None,
        )
        handler.conv_id = str(session.id)
        handler.user_id = user_id
        handler.tenant_id = agent.tenant_id
        handler.history_messages = []
        handler.conversation = []
        handler.welcome_message = "Agent default greeting"
        await handler._load_scene_manifest(db)
        await handler._prepare_initial_greeting(db, user_id)
        await db.commit()
    handler.websocket.receive_json.side_effect = WebSocketDisconnect(1000)
    return handler


@pytest.mark.parametrize("channel", CHANNELS)
async def test_hidden_onboarding_reaches_turn_execution(channel):
    handler = await pristine_handler(channel)
    handler.onboarding_required = True
    handler.websocket.receive_json.side_effect = [
        {"kind": "onboarding_trigger"}, WebSocketDisconnect(1000),
    ]
    handler._safe_send = AsyncMock()
    handler._check_quotas = AsyncMock(return_value=True)
    handler._resolve_effective_model = AsyncMock(return_value=SimpleNamespace(id=handler.agent_id, reasoning_effort=None))
    handler._execute_web_turn = AsyncMock(return_value="disconnect")

    try:
        await handler.message_loop()
    except WebSocketDisconnect:
        pass

    handler._execute_web_turn.assert_awaited_once()
    execution = handler._execute_web_turn.call_args.kwargs
    assert execution["is_onboarding_trigger"] is True
    assert execution["onboarding_claim"].acquired
    assert handler.conversation == [{"role": "user", "content": "Please begin the onboarding."}]
    assert not any(call.args[0].get("type") == "error" for call in handler._safe_send.call_args_list)
    async with async_session() as db:
        anchor = await db.get(ChatMessage, execution["turn_anchor_id"])
        session = await db.get(ChatSession, UUID(handler.conv_id))
        assert anchor.role == "system"
        assert anchor.message_meta["kind"] == HIDDEN_ONBOARDING_ANCHOR_KIND
        assert conversation_turn_snapshot_for_session(session).status == "running"
        assert await load_messages_for_session(db, agent_id=handler.agent_id,
            conversation_id=handler.conv_id, ctx_size=128000) == []


@pytest.mark.parametrize("channel", CHANNELS)
@pytest.mark.parametrize("selection, expected", [
    ("automatic", []),
    ("explicit", ["Must never be sent automatically"]),
    ("none", ["Agent default greeting"]),
])
async def test_initial_welcome_preserves_explicit_and_default_but_silences_auto(channel, selection, expected):
    handler = await pristine_handler(channel, selection=selection)
    handler.onboarding_required = handler._resolve_onboarding_required(selection == "automatic")
    if selection == "automatic":
        assert handler.scene_manifest["activation_source"] == "automatic"
        assert handler.onboarding_required is False
    with pytest.raises(WebSocketDisconnect):
        await handler.message_loop()
    frames = [call.args[0] for call in handler.websocket.send_json.call_args_list]
    assert [frame["content"] for frame in frames] == expected
    assert all(frame["type"] == "done" for frame in frames)
