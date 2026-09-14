"""Admission regressions from the independent review, using real identities and APIs."""

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.database import async_session
from app.models.agent import Agent
from app.models.agent_group import AgentGroup
from app.models.audit import ChatMessage
from app.models.channel_config import ChannelConfig
from app.models.chat_session import ChatSession
from app.models.identity import IdentityProvider
from app.models.org import ChannelUserBinding, OrgMember
from app.models.user import User
from app.services.group_policy import group_ingress_allowed, session_group_allowed
from app.services.group_policy_sender import current_sender, resolve_ingress_user
from tests.test_group_policy import seed, discover, rule, client_for, bind_test_sender
from tests.test_group_policy import isolated_engine  # noqa: F401

pytestmark = pytest.mark.asyncio


async def directory_user(agent_id, user_id, config_id, *, channel="feishu", linked_user=None):
    async with async_session() as db:
        agent = await db.get(Agent, agent_id)
        provider = IdentityProvider(tenant_id=agent.tenant_id, provider_type=channel,
                                    name="Admission directory", is_active=True)
        db.add(provider)
        await db.flush()
        db.add(OrgMember(tenant_id=agent.tenant_id, provider_id=provider.id, external_id="staff",
                         open_id="open", user_id=linked_user or user_id, name="Directory member", status="active"))
        (await db.get(ChannelConfig, config_id)).extra_config = {"identity_provider_id": str(provider.id)}
        await db.commit()
        return provider.id


@pytest.mark.parametrize("effect", ["allow", "deny"])
async def test_first_directory_sender_policy_precedes_file_dispatch(monkeypatch, effect):
    from app.api import feishu_events
    agent_id, user_id, config_id = await seed()
    group_id, _ = await discover(agent_id)
    await directory_user(agent_id, user_id, config_id)
    async with await client_for(user_id) as client:
        response = await client.put(f"/api/agents/{agent_id}/group-policy/groups/{group_id}",
            json={"rules": [{**rule(effect), "user_ids": [str(user_id)]}], "expected_revision": 0})
        assert response.status_code == 200
    dispatched = []
    async def file_dispatch(*args):
        actor = current_sender()
        async with async_session() as db:
            user = await resolve_ingress_user(db, await db.get(Agent, agent_id), "feishu", "staff")
        dispatched.append(user.id)
        assert actor.user_id == user_id
    monkeypatch.setattr(feishu_events, "_handle_feishu_file", file_dispatch)
    body = {"header": {"event_type": "im.message.receive_v1"}, "event": {
        "sender": {"sender_id": {"open_id": "open", "user_id": "staff"}},
        "message": {"message_type": "image", "chat_type": "group", "chat_id": "room", "content": '{}'}}}
    for _ in range(2):
        async with async_session() as db:
            assert await feishu_events.process_feishu_event(agent_id, body, db) == {"code": 0, "msg": "ok"}
    assert dispatched == ([user_id, user_id] if effect == "allow" else [])
    async with async_session() as db:
        assert await db.scalar(select(ChannelUserBinding.user_id).where(
            ChannelUserBinding.subject == "open", ChannelUserBinding.user_id == user_id)) == user_id
        assert await db.scalar(select(func.count()).select_from(ChatMessage).where(ChatMessage.agent_id == agent_id)) == 0


async def test_fresh_stronger_feishu_identity_denies_before_media(monkeypatch):
    from app.api import feishu_events
    from app.services import feishu_sender
    agent_id, selected, _ = await seed()
    group_id, _ = await discover(agent_id, sender="weak")
    async with async_session() as db:
        await bind_test_sender(db, agent_id, selected, "strong", "union_id")
        (await db.get(AgentGroup, group_id)).rules = [{**rule("deny"), "user_ids": [str(selected)]}]
        await db.commit()
    profile = AsyncMock(return_value={"open_id": "weak", "unionid": "strong", "external_id": "staff"})
    monkeypatch.setattr(feishu_sender, "feishu_sender_info", profile)
    file_dispatch = AsyncMock()
    monkeypatch.setattr(feishu_events, "_handle_feishu_file", file_dispatch)
    async with async_session() as db:
        await feishu_events.process_feishu_event(agent_id, {"header": {"event_type": "im.message.receive_v1"},
            "event": {"sender": {"sender_id": {"open_id": "weak"}}, "message": {
                "message_type": "image", "chat_type": "group", "chat_id": "room", "content": '{}'}}}, db)
    profile.assert_awaited_once()
    file_dispatch.assert_not_awaited()


async def test_policy_save_during_identity_wait_is_not_blocked_and_wins(monkeypatch):
    from app.services import feishu_sender
    agent_id, user_id, _ = await seed()
    group_id, _ = await discover(agent_id)
    async with async_session() as db:
        (await db.get(AgentGroup, group_id)).rules = [rule("allow", all_members=True)]
        await db.commit()
    started, release = asyncio.Event(), asyncio.Event()
    async def profile(*args):
        started.set()
        await release.wait()
        return {"open_id": "sender"}
    monkeypatch.setattr(feishu_sender, "feishu_sender_info", profile)
    incoming = asyncio.create_task(group_ingress_allowed(agent_id, "feishu", "room", is_group=True, sender_id="sender"))
    await asyncio.wait_for(started.wait(), 2)
    try:
        async with await client_for(user_id) as client:
            response = await asyncio.wait_for(client.put(f"/api/agents/{agent_id}/group-policy/groups/{group_id}",
                json={"rules": [rule("deny", all_members=True)], "expected_revision": 0}), 2)
            assert response.status_code == 200
    finally:
        release.set()
    assert not await incoming


async def test_discord_gateway_upgrades_group_and_confirmation_boundary(monkeypatch):
    from app.services import channel_session
    from app.services.discord_gateway import DiscordGatewayManager
    agent_id, user_id, _ = await seed("discord")
    async with async_session() as db:
        legacy = ChatSession(agent_id=agent_id, user_id=user_id, source_channel="discord", is_group=False,
                             external_conv_id="discord_thread_sender")
        db.add(legacy)
        await db.commit()
        legacy_id = legacy.id
    assert await group_ingress_allowed(agent_id, "discord", "parent", is_group=True,
        conversation_ref="discord_thread_sender", sender_id="sender")
    captured = {}
    class StopAfterSession(BaseException):
        pass
    original = channel_session.find_or_create_channel_session
    async def capture(**kwargs):
        session = await original(**kwargs)
        await kwargs["db"].commit()
        captured.update(id=session.id, is_group=session.is_group, im_config=session.im_config)
        raise StopAfterSession()
    monkeypatch.setattr(channel_session, "find_or_create_channel_session", capture)
    message = SimpleNamespace(author=SimpleNamespace(id="sender", display_name="Sender", name="Sender"),
        channel=SimpleNamespace(id="thread", parent=SimpleNamespace(name="Parent group")), guild=object())
    with pytest.raises(StopAfterSession):
        await DiscordGatewayManager()._handle_message(agent_id, message, "hello")
    assert captured["id"] == legacy_id and captured["is_group"]
    assert captured["im_config"]["group_target_id"]
    async with await client_for(user_id) as client:
        groups = (await client.get(f"/api/agents/{agent_id}/group-policy/groups")).json()
        assert len(groups["items"]) == groups["total"] == 1
        assert groups["items"][0]["name"] == "Parent group"
    async with async_session() as db:
        group = await db.get(AgentGroup, uuid.UUID(captured["im_config"]["group_target_id"]))
        group.rules = [rule("deny", all_members=True)]
        await db.flush()
        session = await db.get(ChatSession, legacy_id)
        assert not await session_group_allowed(db, session, user_id)
        # Existing guild sessions without corrected metadata must not become P2P bypasses.
        session.is_group, session.im_config, session.user_id = False, {}, user_id
        assert not await session_group_allowed(db, session, user_id)


async def test_thread_group_identity_precedes_pagination_and_count():
    agent_id, user_id, _ = await seed("teams")
    group_id, _ = await discover(agent_id, room="parent", channel="teams")
    async with async_session() as db:
        for index in range(51):
            db.add(ChatSession(agent_id=agent_id, user_id=user_id, source_channel="teams", is_group=True,
                external_conv_id=f"thread-{index}", group_name="One group",
                im_config={"group_target_id": str(group_id)}))
        db.add(ChatSession(agent_id=agent_id, user_id=user_id, source_channel="teams", is_group=True,
                          external_conv_id="legacy-unknown-parent", group_name="Unknown"))
        await db.commit()
    async with await client_for(user_id) as client:
        base = f"/api/agents/{agent_id}/group-policy/groups"
        first = (await client.get(base)).json()
        assert first["total"] == len(first["items"]) == 1
        assert first["next_offset"] is None
        assert (await client.get(base, params={"offset": 50})).json()["items"] == []
        assert (await client.get(base, params={"q": "Unknown"})).json()["total"] == 0
        assert (await client.get(base, params={"q": "One", "channel": "teams"})).json()["total"] == 1


async def test_wecom_private_text_reaches_normal_dispatch(monkeypatch):
    from app.api.wecom import _process_wecom_text
    from app.services import channel_dispatch
    agent_id, _, config_id = await seed("wecom")
    dispatched = AsyncMock(return_value="")
    monkeypatch.setattr(channel_dispatch, "run_channel_message", dispatched)
    async with async_session() as db:
        config = await db.get(ChannelConfig, config_id)
    await _process_wecom_text(agent_id, config, "sender", "hello")
    dispatched.assert_awaited_once()
    assert dispatched.await_args.kwargs["is_command"] is False


async def test_dingtalk_complete_claims_resolve_before_member_denial(monkeypatch):
    from app.api import dingtalk
    from app.services import dingtalk_sender
    agent_id, selected, config_id = await seed("dingtalk")
    group_id, _ = await discover(agent_id, channel="dingtalk")
    async with async_session() as db:
        agent = await db.get(Agent, agent_id)
        weak = User(tenant_id=agent.tenant_id, display_name="Old directory shell", source="dingtalk", is_active=True)
        db.add(weak)
        await db.flush()
        weak_id = weak.id
        email = (await db.get(User, selected)).email
        group = await db.get(AgentGroup, group_id)
        group.rules = [{**rule("deny"), "user_ids": [str(selected)]}]
        await db.commit()
    await directory_user(agent_id, selected, config_id, channel="dingtalk", linked_user=weak_id)
    monkeypatch.setattr(dingtalk, "_resolve_dingtalk_directory_credentials", lambda *args: [("key", "secret", "directory")])
    detail = AsyncMock(return_value={"name": "Verified member", "email": email, "userid": "staff", "unionid": "union"})
    monkeypatch.setattr(dingtalk, "_get_dingtalk_user_detail_with_fallback", detail)
    original = dingtalk_sender.resolve_dingtalk_sender
    resolved = []
    async def capture(*args):
        result = await original(*args)
        resolved.append(result[0].id)
        return result
    monkeypatch.setattr(dingtalk_sender, "resolve_dingtalk_sender", capture)
    assert not await group_ingress_allowed(agent_id, "dingtalk", "room", is_group=True,
        sender_id="opaque", sender_info={"staff_id": "staff"})
    assert resolved == [selected]
    assert detail.await_count >= 1
    async with async_session() as db:
        assert await db.scalar(select(func.count()).select_from(ChatMessage).where(ChatMessage.agent_id == agent_id)) == 0


async def test_dingtalk_configuration_change_cannot_write_stale_identity(monkeypatch):
    from app.api import dingtalk
    agent_id, selected, config_id = await seed("dingtalk")
    provider_id = await directory_user(agent_id, selected, config_id, channel="dingtalk")
    group_id, _ = await discover(agent_id, channel="dingtalk")
    async with async_session() as db:
        (await db.get(AgentGroup, group_id)).rules = [rule("allow", all_members=True)]
        await db.commit()
    monkeypatch.setattr(dingtalk, "_resolve_dingtalk_directory_credentials", lambda *args: [("key", "secret", "directory")])
    async def changed(*args):
        async with async_session() as db:
            agent = await db.get(Agent, agent_id)
            provider = IdentityProvider(tenant_id=agent.tenant_id, provider_type="dingtalk", name="Replacement", is_active=True)
            db.add(provider)
            await db.flush()
            (await db.get(ChannelConfig, config_id)).extra_config = {"identity_provider_id": str(provider.id)}
            await db.commit()
        return {"name": "Verified", "mobile": "13800138000", "userid": "staff"}
    monkeypatch.setattr(dingtalk, "_get_dingtalk_user_detail_with_fallback", changed)
    assert not await group_ingress_allowed(agent_id, "dingtalk", "room", is_group=True,
        sender_id="opaque", sender_info={"staff_id": "staff"})
    async with async_session() as db:
        assert await db.scalar(select(func.count()).select_from(ChannelUserBinding).where(ChannelUserBinding.provider_id == provider_id)) == 0
        assert await db.scalar(select(OrgMember.user_id).where(OrgMember.provider_id == provider_id)) == selected


@pytest.mark.parametrize("contact", [{"name": "Sparse profile"}, {"user_id": "conflicting-staff"}])
async def test_feishu_sparse_or_conflicting_profile_cannot_bypass_event_identity(monkeypatch, contact):
    from app.services import feishu_sender
    agent_id, selected, config_id = await seed()
    group_id, _ = await discover(agent_id)
    await directory_user(agent_id, selected, config_id)
    async with async_session() as db:
        (await db.get(ChannelConfig, config_id)).app_secret = "test-secret"
        (await db.get(AgentGroup, group_id)).rules = [{**rule("deny"), "user_ids": [str(selected)]}]
        await db.commit()
    http = AsyncMock()
    http.__aenter__.return_value = http
    http.post.return_value = SimpleNamespace(json=lambda: {"app_access_token": "test-token"})
    http.get.return_value = SimpleNamespace(json=lambda: {"code": 0, "data": {"user": contact}})
    monkeypatch.setattr(feishu_sender._httpx, "AsyncClient", lambda: http)
    assert not await group_ingress_allowed(agent_id, "feishu", "room", is_group=True,
        sender_id="new-open", sender_info={"external_id": "staff"})
    http.get.assert_awaited_once()
