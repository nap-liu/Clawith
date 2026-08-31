"""Authorization boundary for background execution reassignment."""

import uuid

import pytest

import app.models.chat_compaction  # noqa: F401 - registers ChatMessage FK target
import app.models.participant  # noqa: F401 - registers ChatSession FK target
from app.database import async_session
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.tenant import Tenant
from app.models.trigger import AgentTrigger
from execution_identity_support import _dispose_engine_between_cases, _user

pytestmark = pytest.mark.asyncio


async def test_member_creator_and_unattended_context_cannot_use_reassignment_tool():
    from app.services.execution_identity import handle_reassign_background_execution_user

    suffix = uuid.uuid4().hex[:10]
    async with async_session() as db:
        tenant = Tenant(name=f"Locked {suffix}", slug=f"locked-{suffix}")
        db.add(tenant)
        await db.flush()
        creator = await _user(db, tenant.id, "member", f"creator_locked_{suffix}")
        admin = await _user(db, tenant.id, "org_admin", f"admin_locked_{suffix}")
        agent = Agent(
            tenant_id=tenant.id,
            creator_id=creator.id,
            name=f"Locked Agent {suffix}",
            access_mode="company",
            status="idle",
        )
        db.add(agent)
        await db.flush()
        trigger = AgentTrigger(
            agent_id=agent.id,
            created_by_user_id=creator.id,
            execution_user_id=creator.id,
            name=f"locked-trigger-{suffix}",
            type="cron",
            config={"expr": "0 9 * * *"},
            reason="daily",
        )
        member_ctx = ChatSession(
            agent_id=agent.id,
            user_id=creator.id,
            source_channel="web",
            title="member",
        )
        trigger_ctx = ChatSession(
            agent_id=agent.id,
            user_id=None,
            source_channel="trigger",
            title="background",
        )
        forged_human_ctx = ChatSession(
            agent_id=agent.id,
            user_id=admin.id,
            source_channel="web",
            title="background event in human session",
        )
        db.add_all([trigger, member_ctx, trigger_ctx, forged_human_ctx])
        await db.flush()
        member_anchor = ChatMessage(
            agent_id=agent.id,
            user_id=creator.id,
            role="user",
            content="please hand over",
            conversation_id=str(member_ctx.id),
        )
        trigger_anchor = ChatMessage(
            agent_id=agent.id,
            user_id=admin.id,
            role="user",
            content="background event",
            conversation_id=str(trigger_ctx.id),
            message_meta={"kind": "on_message_event", "trigger_execution_id": str(uuid.uuid4())},
        )
        forged_human_anchor = ChatMessage(
            agent_id=agent.id,
            user_id=admin.id,
            role="user",
            content="background event in web history",
            conversation_id=str(forged_human_ctx.id),
            message_meta={"kind": "on_message_event", "trigger_execution_id": str(uuid.uuid4())},
        )
        db.add_all([member_anchor, trigger_anchor, forged_human_anchor])
        await db.commit()

    member_args = {
        "resource_type": "trigger",
        "resource_id": str(trigger.id),
        "execution_user_id": str(creator.id),
        "expected_execution_user_id": str(creator.id),
        "reason": "handover",
    }
    member_result = await handle_reassign_background_execution_user(
        agent.id, creator.id, str(member_ctx.id), member_anchor.id, member_args
    )
    assert "Only platform administrators and organization administrators" in member_result

    admin_args = {
        **member_args,
        "execution_user_id": str(admin.id),
    }
    unattended_result = await handle_reassign_background_execution_user(
        agent.id, admin.id, str(trigger_ctx.id), trigger_anchor.id, admin_args
    )
    assert "only run in a human interactive session" in unattended_result

    forged_human_result = await handle_reassign_background_execution_user(
        agent.id,
        admin.id,
        str(forged_human_ctx.id),
        forged_human_anchor.id,
        admin_args,
    )
    assert "only run in a human interactive session" in forged_human_result
