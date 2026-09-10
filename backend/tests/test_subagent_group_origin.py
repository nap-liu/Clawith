"""An admitted IM group turn delegates only its own execution authority."""

import uuid

import pytest
from sqlalchemy import func, select

from app.database import async_session, engine
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.subagent_run import SubagentRun
from app.models.tenant import Tenant
from app.models.user import Identity
from app.services import subagent_runtime as runtime
from app.services.conversation_turn_lifecycle import transition_conversation_turn
from app.services.execution_identity import ExecutionIdentityError, resolve_execution_user_id
from tests.execution_identity_support import _user
from tests.test_subagent_runtime import _make_context

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def dispose_engine():
    await engine.dispose()
    yield
    await engine.dispose()


async def group_context(variation="valid"):
    aid, owner, sid, anchor_id = await _make_context(parent_channel="dingtalk")
    async with async_session() as db:
        agent = await db.get(Agent, aid)
        agent.access_mode = "private"
        tenant_id = agent.tenant_id
        if variation == "cross_tenant":
            tenant = Tenant(name="Other tenant", slug=f"other-{uuid.uuid4().hex}")
            db.add(tenant)
            await db.flush()
            tenant_id = tenant.id
        member = await _user(db, tenant_id, "member", uuid.uuid4().hex)
        parent = await db.get(ChatSession, sid)
        parent.is_group = variation not in {"other_p2p", "own_p2p"}
        if variation == "own_p2p":
            parent.user_id = member.id
        parent.source_channel = "web" if variation == "non_im" else "dingtalk"
        anchor = await db.get(ChatMessage, anchor_id)
        anchor.user_id = owner if variation == "cross_tenant" else member.id
        anchor.sender_user_id = owner if variation in {"wrong_actor", "cross_tenant"} else member.id
        if variation == "synthetic":
            anchor.message_meta = {"kind": "subagent_completed"}
        await transition_conversation_turn(
            db, agent_id=aid, conversation_id=str(sid),
            turn_anchor_id=anchor_id, status="running",
        )
        if variation == "inactive_identity":
            identity = await db.get(Identity, member.identity_id)
            identity.is_active = False
        await db.commit()
        return aid, member.id, sid, anchor_id


@pytest.mark.parametrize("executor", ["agent", "media"])
async def test_group_member_child_keeps_actor_and_revalidates_origin(executor):
    aid, uid, sid, anchor_id = await group_context()
    async with async_session() as db:
        agent = await db.get(Agent, aid)
        with pytest.raises(ExecutionIdentityError):
            await resolve_execution_user_id(db, agent, uid)
    run, created = await runtime.create_subagent(
        agent_id=aid, execution_user_id=uid, parent_session_id=str(sid),
        origin_tool_call_id="group-child", task="Analyze supplied media",
        turn_anchor_id=anchor_id, executor=executor,
    )
    assert created and run.execution_user_id == uid
    async with async_session() as db:
        child = await db.get(ChatSession, run.id)
        assert child.im_config["execution_origin"] == {
            "session_id": str(sid), "anchor_id": str(anchor_id),
        }
        await transition_conversation_turn(
            db, agent_id=aid, conversation_id=str(sid),
            turn_anchor_id=anchor_id, status="completed",
        )
        await db.commit()
        assert (await runtime._validate_execution_identity(db, run, child)).id == aid
        original = await db.get(ChatMessage, anchor_id)
        # A synthetic sender cannot retain a human origin's authority.
        original.sender_user_id = None
        await db.flush()
        with pytest.raises(ExecutionIdentityError):
            await runtime._validate_execution_identity(db, run, child)
        await db.rollback()


async def test_group_child_can_delegate_media_with_same_durable_origin():
    aid, uid, sid, anchor_id = await group_context()
    parent_run, _ = await runtime.create_subagent(
        agent_id=aid, execution_user_id=uid, parent_session_id=str(sid),
        origin_tool_call_id="group-agent", task="Review media", turn_anchor_id=anchor_id,
    )
    async with async_session() as db:
        child_input = await db.scalar(select(ChatMessage).where(
            ChatMessage.conversation_id == str(parent_run.id), ChatMessage.role == "user",
        ))
        await transition_conversation_turn(
            db, agent_id=aid, conversation_id=str(parent_run.id),
            turn_anchor_id=child_input.id, status="running",
        )
        await db.commit()
    media_run, _ = await runtime.create_subagent(
        agent_id=aid, execution_user_id=uid, parent_session_id=str(parent_run.id),
        origin_tool_call_id="nested-media", task="Understand media",
        turn_anchor_id=child_input.id, executor="media",
    )
    async with async_session() as db:
        child = await db.get(ChatSession, media_run.id)
        assert media_run.execution_user_id == uid
        assert child.im_config["execution_origin"]["session_id"] == str(sid)
        assert (await runtime._validate_execution_identity(db, media_run, child)).id == aid


async def test_admitted_im_p2p_sender_can_create_own_child():
    aid, uid, sid, anchor_id = await group_context("own_p2p")
    run, created = await runtime.create_subagent(
        agent_id=aid, execution_user_id=uid, parent_session_id=str(sid),
        origin_tool_call_id="p2p-child", task="Understand media", turn_anchor_id=anchor_id,
        executor="media",
    )
    assert created and run.execution_user_id == uid
    async with async_session() as db:
        child = await db.get(ChatSession, run.id)
        assert (await runtime._validate_execution_identity(db, run, child)).id == aid


@pytest.mark.parametrize("variation", [
    "cross_tenant", "wrong_actor", "other_p2p", "non_im", "synthetic", "inactive_identity", "wrong_anchor",
])
async def test_untrusted_origin_cannot_create_child(variation):
    aid, uid, sid, anchor_id = await group_context(variation)
    with pytest.raises(ExecutionIdentityError):
        await runtime.create_subagent(
            agent_id=aid, execution_user_id=uid, parent_session_id=str(sid),
            origin_tool_call_id="rejected-child", task="Must not execute",
            turn_anchor_id=uuid.uuid4() if variation == "wrong_anchor" else anchor_id,
            executor="media",
        )
    async with async_session() as db:
        assert await db.scalar(select(func.count()).select_from(SubagentRun).where(
            SubagentRun.parent_session_id == sid,
        )) == 0
