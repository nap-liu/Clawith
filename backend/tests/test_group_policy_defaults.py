"""Unconfigured policies leave transport identity and processing in their normal owner."""

import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.database import async_session
from app.models.agent_group import AgentGroup
from app.models.channel_config import ChannelConfig
from app.models.chat_session import ChatSession
from app.models.org import ChannelUserBinding
from app.services.channel_user_errors import ChannelUserResolutionError
from app.services.group_policy import group_ingress_allowed, session_group_allowed
from app.services.group_policy_sender import current_sender
from tests.test_group_policy import seed, discover, rule
from tests.test_group_policy import isolated_engine  # noqa: F401

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("channel", ["feishu", "dingtalk", "wecom", "slack", "discord", "teams"])
@pytest.mark.parametrize("mode", ["never", "empty", "disabled", "other_group"])
async def test_unrestricted_groups_do_not_prepare_identity(monkeypatch, channel, mode):
    from app.services import group_policy
    agent_id, user_id, _ = await seed(channel)
    if mode != "never":
        group_id, _ = await discover(agent_id, room="other" if mode == "other_group" else "room", channel=channel)
        async with async_session() as db:
            group = await db.get(AgentGroup, group_id)
            group.rules = [] if mode == "empty" else [rule("deny", all_members=True, enabled=mode != "disabled")]
            await db.commit()
    preparation = AsyncMock(side_effect=ChannelUserResolutionError("Directory unavailable"))
    monkeypatch.setattr(group_policy, "prepare_sender", preparation)
    assert await group_ingress_allowed(agent_id, channel, "room", is_group=True, sender_id="first-sender")
    assert current_sender() is None
    preparation.assert_not_awaited()
    async with async_session() as db:
        group = await db.scalar(select(AgentGroup).where(AgentGroup.agent_id == agent_id,
            AgentGroup.external_group_id == "room"))
        assert group is not None  # Stable parent group selection remains available.
        session = ChatSession(agent_id=agent_id, user_id=user_id, source_channel=channel, is_group=True,
            external_conv_id="thread", im_config={"group_target_id": str(group.id)})
        assert await session_group_allowed(db, session, user_id)
        assert await db.scalar(select(func.count()).select_from(ChannelUserBinding).where(
            ChannelUserBinding.subject == "first-sender")) == 0


@pytest.mark.parametrize("disabled", [False, True])
async def test_unrestricted_confirmation_survives_configuration_removal(disabled):
    agent_id, user_id, config_id = await seed()
    group_id, _ = await discover(agent_id)
    async with async_session() as db:
        group = await db.get(AgentGroup, group_id)
        group.rules = [rule("deny", all_members=True, enabled=False)] if disabled else []
        await db.delete(await db.get(ChannelConfig, config_id))
        await db.flush()
        session = ChatSession(agent_id=agent_id, user_id=user_id, source_channel="feishu", is_group=True,
            external_conv_id="feishu_group_room", im_config={"group_target_id": str(group.id)})
        assert await session_group_allowed(db, session, user_id)


@pytest.mark.parametrize("chat_type", ["group", "p2p"])
async def test_unrestricted_feishu_media_reaches_original_dispatch(monkeypatch, chat_type):
    from app.api import feishu_events
    from app.services import group_policy
    agent_id, _, _ = await seed()
    prepare = AsyncMock(side_effect=ChannelUserResolutionError("Directory unavailable"))
    dispatch = AsyncMock()
    monkeypatch.setattr(group_policy, "prepare_sender", prepare)
    monkeypatch.setattr(feishu_events, "_handle_feishu_file", dispatch)
    async with async_session() as db:
        result = await feishu_events.process_feishu_event(agent_id, {
            "header": {"event_type": "im.message.receive_v1"}, "event": {
                "sender": {"sender_id": {"open_id": "sender"}}, "message": {
                    "message_type": "image", "chat_type": chat_type, "chat_id": "room", "content": '{}'}}}, db)
    assert result == {"code": 0, "msg": "ok"}
    prepare.assert_not_awaited()
    dispatch.assert_awaited_once()
    assert current_sender() is None


async def test_feishu_private_identity_conflict_uses_existing_error_reply(monkeypatch):
    from app.api import feishu_events
    from app.services import feishu_sender
    agent_id, _, _ = await seed()
    monkeypatch.setattr(feishu_sender, "feishu_sender_info", AsyncMock(
        side_effect=ChannelUserResolutionError("Conflicting provider identity")))
    reply = AsyncMock()
    monkeypatch.setattr(feishu_events, "_persist_feishu_control_reply", reply)
    async with async_session() as db:
        result = await feishu_events.process_feishu_event(agent_id, {
            "header": {"event_type": "im.message.receive_v1"}, "event": {
                "sender": {"sender_id": {"open_id": "sender"}}, "message": {
                    "message_type": "text", "chat_type": "p2p", "chat_id": "private",
                    "content": json.dumps({"text": "Hello"})}}}, db)
    assert result == {"code": 0, "msg": "user_resolution_skipped"}
    assert reply.await_args.kwargs["artifact_role"] == "identity_error_ack"
