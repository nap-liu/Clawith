from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.database import async_session, engine
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.participant import Participant  # noqa: F401 - register FK metadata
from app.models.tenant import Tenant
from app.models.user import Identity, User
from app.services.chat_history import (
    build_llm_message_from_row,
    ingest_incoming_chat_message,
)
from app.services.chat_message_serializer import serialize_chat_message_for_client
from app.services.quoted_message import normalize_quoted_message

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    await engine.dispose()
    yield
    await engine.dispose()


async def test_dingtalk_quote_survives_jsonb_persistence_and_history_replay():
    suffix = uuid.uuid4().hex[:10]
    quote = normalize_quoted_message(
        {
            "message_type": "rich_text",
            "provider_message_type": "richText",
            "provider_message_id": f"quote-{suffix}",
            "sender_name": "张三",
            "content_status": "available",
            "text": "持久化的引用内容",
            "attachments": [
                {
                    "display_name": "quoted.png",
                    "path": f"workspace/uploads/{suffix}-quoted.png",
                    "kind": "image",
                    "mime_type": "image/png",
                    "size_bytes": 128,
                }
            ],
        }
    )
    assert quote is not None

    async with async_session() as db:
        tenant = Tenant(name=f"quote-{suffix}", slug=f"quote-{suffix}")
        identity = Identity(
            username=f"quote_{suffix}",
            email=f"quote_{suffix}@test.local",
            password_hash="x",
        )
        db.add_all([tenant, identity])
        await db.flush()
        user = User(
            identity_id=identity.id,
            tenant_id=tenant.id,
            display_name="Quote Test User",
            role="member",
            is_active=True,
        )
        db.add(user)
        await db.flush()
        agent = Agent(
            name=f"Quote Agent {suffix}",
            creator_id=user.id,
            tenant_id=tenant.id,
            status="idle",
        )
        db.add(agent)
        await db.flush()
        session = ChatSession(
            agent_id=agent.id,
            user_id=user.id,
            title="DingTalk quote persistence",
            source_channel="dingtalk",
            external_conv_id=f"dingtalk_p2p_{suffix}",
        )
        db.add(session)
        await db.flush()

        ingested = await ingest_incoming_chat_message(
            db,
            session=session,
            agent_id=agent.id,
            user_id=user.id,
            content="请分析这条引用",
            source_channel="dingtalk",
            provider_event_id=f"event-{suffix}",
            channel_config_id=f"bot-{suffix}",
            actor_ref=f"staff-{suffix}",
            message_meta={
                "attachments": quote["attachments"],
                "quoted_message": quote,
            },
        )
        await db.commit()
        assert ingested.created is True

        stored = (await db.execute(select(ChatMessage).where(ChatMessage.id == ingested.message.id))).scalar_one()
        assert stored.message_meta["quoted_message"] == quote

        client_message = serialize_chat_message_for_client(stored)
        assert client_message["quoted_message"] == quote
        assert client_message["display_content"] == "请分析这条引用"
        llm_message = build_llm_message_from_row(stored)
        assert llm_message is not None
        assert llm_message["role"] == "user"
        assert llm_message["attachments"] == quote["attachments"]
        assert "引用消息上下文" in llm_message["content"]
        assert "持久化的引用内容" in str(llm_message["content"])
        assert "请分析这条引用" in str(llm_message["content"])
