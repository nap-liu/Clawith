"""Stable group access through PostgreSQL, HTTP, and real IM ingress."""

import uuid
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from app.api.group_policy import router
from app.core.security import get_current_user
from app.database import async_session, engine
from app.models.agent import Agent
from app.models.agent_group import AgentGroup, AgentGroupMember
from app.models.audit import AuditLog, ChatMessage
from app.models.channel_config import ChannelConfig
from app.models.chat_session import ChatSession
from app.models.tenant import Tenant
from app.models.user import Identity, User
from app.services.channel_session import find_or_create_channel_session
from app.services.channel_commands import handle_channel_command
from app.services.group_policy import group_ingress_allowed, session_group_allowed
import app.models.registry  # noqa: F401

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def isolated_engine():
    await engine.dispose()
    yield
    await engine.dispose()


async def seed(channel="feishu"):
    suffix = uuid.uuid4().hex
    async with async_session() as db:
        tenant = Tenant(id=uuid.uuid4(), name="Group policy test", slug=f"groups-{suffix}", im_provider="web_only")
        identity = Identity(id=uuid.uuid4(), email=f"groups-{suffix}@example.com", username=f"groups-{suffix}")
        user = User(id=uuid.uuid4(), identity=identity, tenant_id=tenant.id, display_name="Group manager", is_active=True)
        agent = Agent(id=uuid.uuid4(), name="Group assistant", creator_id=user.id, tenant_id=tenant.id, status="idle")
        config = ChannelConfig(id=uuid.uuid4(), agent_id=agent.id,
                               channel_type="microsoft_teams" if channel == "teams" else channel,
                               app_id=f"app-{suffix}", is_configured=True)
        db.add_all([tenant, identity, user, agent, config])
        await db.commit()
        return agent.id, user.id, config.id


async def bind_test_sender(db, agent_id, user_id, subject, kind="open_id", scope=None):
    from app.models.org import ChannelUserBinding
    from app.services.channel_user_service import channel_user_service
    agent = await db.get(Agent, agent_id)
    provider, info = await channel_user_service.resolve_channel_provider(db, agent, "feishu")
    scope = scope or info["_installation_scope"]
    binding = await db.scalar(select(ChannelUserBinding).where(
        ChannelUserBinding.provider_id == provider.id, ChannelUserBinding.installation_scope == scope,
        ChannelUserBinding.id_type == kind, ChannelUserBinding.subject == subject))
    if binding:
        binding.user_id = user_id
    else:
        db.add(ChannelUserBinding(tenant_id=agent.tenant_id, provider_id=provider.id,
            installation_scope=scope, channel_type="feishu", id_type=kind, subject=subject, user_id=user_id))
    await db.flush()


async def client_for(user_id):
    app = FastAPI()
    app.include_router(router, prefix="/api")

    async def current_user():
        async with async_session() as db:
            return await db.get(User, user_id)

    app.dependency_overrides[get_current_user] = current_user
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def rule(effect, members=(), *, enabled=True, all_members=False):
    return {"id": str(uuid.uuid4()), "name": "Member rule", "effect": effect,
            "enabled": enabled, "all_members": all_members, "member_ids": [str(value) for value in members]}


async def discover(agent_id, room="room", sender="sender", channel="feishu", name="Member"):
    await group_ingress_allowed(agent_id, channel, room, is_group=True, name="Support",
                                sender_id=sender, sender_name=name)
    async with async_session() as db:
        group = await db.scalar(select(AgentGroup).where(AgentGroup.agent_id == agent_id, AgentGroup.external_group_id == room))
        member = await db.scalar(select(AgentGroupMember).where(AgentGroupMember.group_id == group.id, AgentGroupMember.subject == sender))
        return group.id, member.id


async def deny_group(agent_id, room, channel="feishu"):
    group_id, _ = await discover(agent_id, room, channel=channel)
    async with async_session() as db:
        group = await db.get(AgentGroup, group_id)
        group.rules = [rule("deny", all_members=True)]
        await db.commit()


async def test_group_member_rules_api_conflict_audit_and_tenant_isolation():
    agent_id, user_id, _ = await seed()
    other_agent, other_user, _ = await seed()
    group_id, alice = await discover(agent_id, sender="alice", name="Alice")
    _, bob = await discover(agent_id, sender="bob", name="Bob")
    another_group, carol = await discover(agent_id, room="other", sender="carol", name="Carol")
    async with async_session() as db:
        for room in ["room", "other"]:
            db.add(ChatSession(agent_id=agent_id, source_channel="feishu", is_group=True,
                               external_conv_id=f"feishu_group_{room}", group_name="Support"))
        await db.commit()
    base = f"/api/agents/{agent_id}/group-policy/groups"
    rules = [rule("allow", [alice, bob]), rule("deny", [bob])]
    async with await client_for(user_id) as client:
        listing = (await client.get(base, params={"q": "Support", "channel": "feishu"})).json()
        assert listing["total"] == 2
        assert all(item["channel"] == "feishu" and "external_group_id" not in item for item in listing["items"])
        assert (await client.post(base, json={"channel": "feishu", "external_group_id": "manual"})).status_code == 405
        payload = {"rules": rules, "expected_revision": 0}
        saved = await client.put(f"{base}/{group_id}", json=payload)
        assert saved.status_code == 200, saved.text
        assert saved.json()["revision"] == 1 and len(saved.json()["rules"]) == 2
        assert {member["name"] for member in saved.json()["members"]} == {"Alice", "Bob"}
        assert (await client.put(f"{base}/{group_id}", json=payload)).status_code == 409
        assert (await client.put(f"{base}/{group_id}", json={"rules": [rule("deny", [carol])], "expected_revision": 1})).status_code == 422
        assert (await client.put(f"{base}/{group_id}", json={"rules": [rule("deny")], "expected_revision": 1})).status_code == 422
        assert (await client.get(f"{base}/{another_group}")).json()["rules"] == []
        assert (await client.get(f"/api/agents/{other_agent}/group-policy/groups")).status_code in {403, 404}
        assert (await client.get(f"{base}/{group_id}/members", params={"q": "Alice"})).json()["items"] == [{"id": str(alice), "name": "Alice"}]
    async with await client_for(other_user) as client:
        assert (await client.put(f"/api/agents/{other_agent}/group-policy/groups/{group_id}", json=payload)).status_code == 404
    assert await group_ingress_allowed(agent_id, "feishu", "room", is_group=True, sender_id="alice")
    assert not await group_ingress_allowed(agent_id, "feishu", "room", is_group=True, sender_id="bob")
    assert not await group_ingress_allowed(agent_id, "feishu", "room", is_group=True, sender_id="newcomer", sender_name="New member")
    assert await group_ingress_allowed(agent_id, "feishu", "other", is_group=True, sender_id="bob")
    assert await group_ingress_allowed(agent_id, "feishu", "room", is_group=False, sender_id="bob")
    async with await client_for(user_id) as client:
        state = (await client.get(f"{base}/{group_id}")).json()
        state["rules"][1]["enabled"] = False
        assert (await client.put(f"{base}/{group_id}", json={"rules": state["rules"], "expected_revision": 1})).status_code == 200
    assert await group_ingress_allowed(agent_id, "feishu", "room", is_group=True, sender_id="bob")
    async with async_session() as db:
        assert await db.scalar(select(func.count()).select_from(ChatSession).where(ChatSession.agent_id == agent_id)) == 2
        logs = list(await db.scalars(select(AuditLog).where(AuditLog.agent_id == agent_id, AuditLog.action == "group_policy_updated")))
        assert len(logs) == 2 and logs[-1].details["group_id"] == str(group_id)
        member = await db.scalar(select(AgentGroupMember).where(AgentGroupMember.group_id == group_id, AgentGroupMember.subject == "newcomer"))
        assert member.name == "New member"


@pytest.mark.parametrize("channel", ["feishu", "dingtalk", "wecom", "slack", "discord", "teams"])
async def test_member_rules_share_all_transports(channel):
    agent_id, _, _ = await seed(channel)
    group_id, alice = await discover(agent_id, channel=channel, sender="alice")
    async with async_session() as db:
        group = await db.get(AgentGroup, group_id)
        group.rules = [rule("deny", [alice])]
        await db.commit()
    assert not await group_ingress_allowed(agent_id, channel, "room", is_group=True, sender_id="alice")
    assert await group_ingress_allowed(agent_id, channel, "room", is_group=True, sender_id="bob")
    assert not await group_ingress_allowed(agent_id, channel, "room", is_group=True)


async def test_reset_and_secret_rotation_preserve_member_rules():
    agent_id, user_id, config_id = await seed()
    group_id, alice = await discover(agent_id, sender="alice")
    async with async_session() as db:
        group = await db.get(AgentGroup, group_id)
        group.rules = [rule("allow", [alice])]
        first = await find_or_create_channel_session(db, agent_id, user_id, "feishu_group_room", "feishu", "hello", is_group=True)
        first_id = first.id
        await db.commit()
    async with async_session() as db:
        result = await handle_channel_command(db, "/new", agent_id, user_id, "feishu_group_room", "feishu", is_group=True)
        assert result["action"] == "new_session"
        await db.commit()
    assert await group_ingress_allowed(agent_id, "feishu", "room", is_group=True, sender_id="alice")
    async with async_session() as db:
        second = await find_or_create_channel_session(db, agent_id, user_id, "feishu_group_room", "feishu", "again", is_group=True)
        assert second.id != first_id and second.im_config["group_target_id"] == str(group_id)
        config = await db.get(ChannelConfig, config_id)
        config.app_secret = "rotated-test-secret"
        await db.commit()
    assert not await group_ingress_allowed(agent_id, "feishu", "room", is_group=True, sender_id="bob")
    async with async_session() as db:
        config = await db.get(ChannelConfig, config_id)
        config.app_id = "new-installation"
        await db.commit()
    assert await group_ingress_allowed(agent_id, "feishu", "room", is_group=True, sender_id="bob")
    async with async_session() as db:
        assert await db.scalar(select(func.count()).select_from(AgentGroup).where(AgentGroup.agent_id == agent_id)) == 2


async def test_use_grant_cannot_manage_member_rules():
    from app.models.agent import AgentPermission
    agent_id, _, _ = await seed()
    group_id, _ = await discover(agent_id)
    _, reader_id, _ = await seed()
    async with async_session() as db:
        agent = await db.get(Agent, agent_id)
        reader = await db.get(User, reader_id)
        reader.tenant_id = agent.tenant_id
        db.add(AgentPermission(agent_id=agent_id, scope_type="user", scope_id=reader_id, access_level="use"))
        await db.commit()
    async with await client_for(reader_id) as client:
        base = f"/api/agents/{agent_id}/group-policy/groups"
        assert (await client.get(base)).status_code == 403
        assert (await client.put(f"{base}/{group_id}", json={"rules": [], "expected_revision": 0})).status_code == 403
        assert (await client.get(f"{base}/{group_id}/members")).status_code == 403


async def test_confirmation_rechecks_clicking_member_and_preserves_pending_state():
    import json
    from app.services.confirmation_core import resolve_confirmation
    agent_id, user_id, _ = await seed()
    group_id, alice = await discover(agent_id, sender="alice")
    async with async_session() as db:
        group = await db.get(AgentGroup, group_id)
        await bind_test_sender(db, agent_id, user_id, "alice")
        group.rules = [rule("allow", [alice])]
        session = await find_or_create_channel_session(db, agent_id, user_id, "feishu_group_room", "feishu", "hello", is_group=True)
        await db.flush()
        assert await session_group_allowed(db, session, user_id)
        assert not await session_group_allowed(db, session, uuid.uuid4())
        group.rules = [rule("allow", [alice]), rule("deny", [alice])]
        content = json.dumps({"name": "request_confirmation", "status": "pending", "args": {"title": "Review"}})
        call = ChatMessage(agent_id=agent_id, user_id=user_id, role="tool_call", conversation_id=str(session.id), content=content)
        db.add(call)
        await db.commit()
        call_id = call.id
    assert await resolve_confirmation(agent_id=agent_id, call_id=call_id, button_value="confirm", button_label="Confirm", resolving_user_id=user_id) is None
    async with async_session() as db:
        assert (await db.get(ChatMessage, call_id)).content == content


@pytest.mark.parametrize("message_type", ["text", "image", "file", "post"])
async def test_feishu_denied_ingress_never_downloads_or_creates_conversation(monkeypatch, message_type):
    from app.api.feishu_events import process_feishu_event
    from app.api.feishu_events import feishu_service

    agent_id, _, _ = await seed()
    await deny_group(agent_id, "blocked-group")
    download = AsyncMock(side_effect=AssertionError("Rejected attachments must not be downloaded"))
    monkeypatch.setattr(feishu_service, "download_message_resource", download)
    body = {"header": {"event_id": str(uuid.uuid4()), "event_type": "im.message.receive_v1"},
            "event": {"sender": {"sender_id": {"open_id": "sender"}},
                      "message": {"message_type": message_type, "chat_type": "group", "chat_id": "blocked-group",
                                  "content": '{"text":"/new","image_key":"image","content":[[{"tag":"img","image_key":"image"}]]}'}}}
    async with async_session() as db:
        assert await process_feishu_event(agent_id, body, db) == {"code": 0, "msg": "ok"}
        assert await db.scalar(select(func.count()).select_from(ChatSession).where(ChatSession.agent_id == agent_id)) == 0
        assert await db.scalar(select(func.count()).select_from(ChatMessage).where(ChatMessage.agent_id == agent_id)) == 0
    download.assert_not_awaited()


async def test_dingtalk_denied_before_identity_provider_lookup(monkeypatch):
    from app.api.dingtalk_message_processing import process_dingtalk_message
    from app.services.channel_user_service import channel_user_service

    agent_id, _, _ = await seed("dingtalk")
    await deny_group(agent_id, "blocked-group", "dingtalk")
    lookup = AsyncMock(side_effect=AssertionError("No sender lookup for a rejected group"))
    monkeypatch.setattr(channel_user_service, "resolve_channel_provider", lookup)
    await process_dingtalk_message(agent_id, "sender", "create task", "blocked-group", "2")
    lookup.assert_not_awaited()


@pytest.mark.parametrize("channel", ["teams", "discord"])
async def test_thread_messages_use_parent_group_identity(monkeypatch, channel):
    import json
    from types import SimpleNamespace
    from app.api.teams import teams_event_webhook
    from app.api.discord_bot import discord_interaction_webhook

    agent_id, _, _ = await seed(channel)
    await deny_group(agent_id, "parent-room", channel)
    monkeypatch.setattr("app.services.webhook_security.verify_teams_webhook", AsyncMock(return_value={}))
    monkeypatch.setattr("app.services.webhook_security.verify_teams_service_url", lambda *_: "")
    for thread in ["thread-a", "thread-b"]:
        if channel == "teams":
            body = {"type": "message", "id": thread, "text": "hello", "channelId": "msteams",
                    "conversation": {"id": thread, "conversationType": "channel"},
                    "channelData": {"channel": {"id": "parent-room"}},
                    "from": {"id": "sender", "name": "Sender"}}
            endpoint = teams_event_webhook
        else:
            body = {"type": 2, "guild_id": "guild", "channel_id": thread,
                    "channel": {"type": 11, "parent_id": "parent-room"},
                    "member": {"user": {"id": "sender"}},
                    "data": {"name": "ask", "options": [{"name": "message", "value": "hello"}]}}
            endpoint = discord_interaction_webhook
        request = SimpleNamespace(headers={}, body=AsyncMock(return_value=json.dumps(body).encode()))
        async with async_session() as db:
            result = await endpoint(agent_id, request, db)
            assert result == {"ok": True} if channel == "teams" else result.status_code == 403
    async with async_session() as db:
        groups = list(await db.scalars(select(AgentGroup).where(AgentGroup.agent_id == agent_id)))
        assert len(groups) == 1 and groups[0].external_group_id == "parent-room"
        assert await db.scalar(select(func.count()).select_from(ChatMessage).where(ChatMessage.agent_id == agent_id)) == 0


async def test_verified_sender_aliases_cannot_bypass_member_denial():
    agent_id, user_id, _ = await seed()
    group_id, primary = await discover(agent_id, sender="primary", name="")
    await discover(agent_id, sender="alias", name="")
    async with async_session() as db:
        group = await db.get(AgentGroup, group_id)
        for subject in ["primary", "alias"]:
            await bind_test_sender(db, agent_id, user_id, subject)
        group.rules = [rule("deny", [primary])]
        await db.commit()
    assert not await group_ingress_allowed(agent_id, "feishu", "room", is_group=True, sender_id="alias")
    async with await client_for(user_id) as client:
        response = await client.get(f"/api/agents/{agent_id}/group-policy/groups/{group_id}/members", params={"q": "Group manager"})
        assert response.status_code == 200, response.text
        assert len(response.json()["items"]) == 2
        assert {item["name"] for item in response.json()["items"]} == {"Group manager"}


async def test_existing_group_choices_share_scene_source_without_discovery_or_backfill():
    from app.services.group_policy import installation_scope
    from app.services.scene_targets import conversation_options
    agent_id, user_id, _ = await seed()
    async with async_session() as db:
        agent = await db.get(Agent, agent_id)
        scope = await installation_scope(db, agent, "feishu")
        for route in ["feishu_group_existing", "feishu_group_existing__archived_20260910_120000",
                      "feishu_group_existing__control_abcdefgh", "feishu_p2p_person"]:
            session = ChatSession(agent_id=agent_id, source_channel="feishu", is_group="group_" in route,
                                  external_conv_id=route, group_name="Existing support", user_id=user_id)
            db.add(session)
            await db.flush()
            if route == "feishu_group_existing":
                current_id = session.id
                db.add(ChatMessage(agent_id=agent_id, user_id=user_id, role="user",
                                   conversation_id=str(session.id), content="Existing group message"))
        await bind_test_sender(db, agent_id, user_id, "existing-sender", scope=scope)
        await db.commit()
        scenes = await conversation_options(db, agent, await db.get(User, user_id), kind="group")
        assert len(scenes["items"]) == 1
    base = f"/api/agents/{agent_id}/group-policy/groups"
    async with await client_for(user_id) as client:
        groups = (await client.get(base, params={"q": "Existing", "channel": "feishu"})).json()
        assert len(groups["items"]) == 1, groups
        group = groups["items"][0]
        assert group["target_ref"] == scenes["items"][0]["target_ref"]
        assert group["name"] == "Existing support" and group["available"]
        params = {"target_ref": group["target_ref"]}
        path = f"{base}/{group['id']}"
        policy = (await client.get(path, params=params)).json()
        assert policy["rules"] == [] and policy["revision"] == 0
        members = (await client.get(path + "/members", params=params)).json()["items"]
        assert len(members) == 1 and members[0]["name"] == "Group manager"
        async with async_session() as db:
            assert await db.scalar(select(func.count()).select_from(AgentGroup).where(AgentGroup.agent_id == agent_id)) == 0
            assert (await db.get(ChatSession, current_id)).im_config == {}
        payload = {**params, "rules": [rule("allow", [members[0]["id"]])], "expected_revision": 0}
        saved = await client.put(path, json=payload)
        assert saved.status_code == 200, saved.text
        assert saved.json()["group"]["id"] == group["id"]
        async with async_session() as db:
            legacy = await db.get(ChatSession, current_id)
            assert legacy.im_config == {}
            assert await session_group_allowed(db, legacy, user_id)
            assert not await session_group_allowed(db, legacy, uuid.uuid4())
    assert await group_ingress_allowed(agent_id, "feishu", "existing", is_group=True, sender_id="existing-sender")
    assert not await group_ingress_allowed(agent_id, "feishu", "existing", is_group=True, sender_id="outsider")
    async with async_session() as db:
        reset = await handle_channel_command(db, "/new", agent_id, user_id, "feishu_group_existing", "feishu",
                                             is_group=True, group_name="Existing support")
        assert reset["action"] == "new_session"
        await db.commit()
    assert await group_ingress_allowed(agent_id, "feishu", "existing", is_group=True, sender_id="existing-sender")
    async with async_session() as db:
        await find_or_create_channel_session(db, agent_id, user_id, "feishu_group_existing", "feishu",
                                            "Next message", is_group=True, group_name="Existing support")
        await db.commit()
    async with await client_for(user_id) as client:
        groups = (await client.get(base)).json()["items"]
        assert len(groups) == 1 and groups[0]["id"] == group["id"] and groups[0]["rule_count"] == 1
        assert (await client.get(path, params=params)).status_code == 200


async def test_organization_member_rules_before_participation_and_bound_aliases():
    agent_id, user_id, _ = await seed()
    _, foreign_user, _ = await seed()
    group_id, _ = await discover(agent_id, sender="unrelated")
    selected_rule = {**rule("allow"), "user_ids": [str(user_id)]}
    base = f"/api/agents/{agent_id}/group-policy/groups/{group_id}"
    async with await client_for(user_id) as client:
        response = await client.put(base, json={"rules": [selected_rule], "expected_revision": 0})
        assert response.status_code == 200, response.text
        assert response.json()["rules"][0]["user_ids"] == [str(user_id)]
        assert response.json()["users"][0]["name"] == "Group manager"
        assert response.json()["members"] == []
        rejected = await client.put(base, json={"rules": [{**selected_rule, "user_ids": [str(foreign_user)]}], "expected_revision": 1})
        assert rejected.status_code == 422
        assert (await client.get(base)).json()["revision"] == 1
    # A directory member need not already have appeared in this group.
    # Provider subjects must match a verified binding in this installation.
    assert not await group_ingress_allowed(agent_id, "feishu", "room", is_group=True, sender_id="future-member")
    async with async_session() as db:
        await bind_test_sender(db, agent_id, user_id, "future-member", scope="unrelated-installation")
        session = ChatSession(agent_id=agent_id, user_id=user_id, source_channel="feishu", is_group=True,
                              external_conv_id="feishu_group_room")
        db.add(session)
        await db.flush()
        assert await session_group_allowed(db, session, user_id)
        assert not await session_group_allowed(db, session, foreign_user)
        await db.commit()
    assert not await group_ingress_allowed(agent_id, "feishu", "room", is_group=True, sender_id="future-member")
    async with async_session() as db:
        for kind, subject in [("open_id", "future-member"), ("union_id", "future-alias")]:
            await bind_test_sender(db, agent_id, user_id, subject, kind)
        await db.commit()
    assert await group_ingress_allowed(agent_id, "feishu", "room", is_group=True, sender_id="future-member")
    assert await group_ingress_allowed(agent_id, "feishu", "room", is_group=True, sender_id="future-alias", sender_type="union_id")
    async with await client_for(user_id) as client:
        deny = {**rule("deny"), "user_ids": [str(user_id)]}
        response = await client.put(base, json={"rules": [selected_rule, deny], "expected_revision": 1})
        assert response.status_code == 200
        assert len((await client.get(base)).json()["rules"]) == 2
    assert not await group_ingress_allowed(agent_id, "feishu", "room", is_group=True, sender_id="future-alias", sender_type="union_id")
    assert await group_ingress_allowed(agent_id, "feishu", "room", is_group=False, sender_id="future-member")


async def test_legacy_bound_members_open_in_organization_picker_without_read_writes():
    agent_id, user_id, _ = await seed()
    group_id, legacy = await discover(agent_id, sender="existing")
    async with async_session() as db:
        group = await db.get(AgentGroup, group_id)
        group.rules = [rule("deny", [legacy])]
        await bind_test_sender(db, agent_id, user_id, "existing")
        await db.commit()
    base = f"/api/agents/{agent_id}/group-policy/groups/{group_id}"
    async with await client_for(user_id) as client:
        response = (await client.get(base)).json()
        assert response["rules"][0]["user_ids"] == [str(user_id)]
        assert response["rules"][0]["member_ids"] == []
        async with async_session() as db:
            persisted = await db.get(AgentGroup, group_id)
            assert persisted.rules[0]["member_ids"] == [str(legacy)]
            assert persisted.revision == 0
        assert (await client.put(base, json={"rules": response["rules"], "expected_revision": 0})).status_code == 200
    assert not await group_ingress_allowed(agent_id, "feishu", "room", is_group=True, sender_id="existing")
