"""Observable message-lifecycle behavior for normalized IM delivery receipts."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete

from app.database import async_session, engine
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.channel_config import ChannelConfig
from app.models.chat_compaction import ChatCompaction
from app.models.chat_session import ChatSession
from app.models.mcp_server import MCPServer  # noqa: F401 - register Tool FK target
from app.models.participant import Participant  # noqa: F401 - register ChatMessage FK target
from app.models.tenant import Tenant
from app.models.user import Identity, User
from app.services import im_delivery
from app.services.chat_history import build_llm_messages_from_rows, load_messages_for_session
from app.services.chat_message_serializer import serialize_chat_message_for_client
from app.services.im_delivery import IMDeliveryPart, IMDeliveryResult, PartRecallResult
from app.services.llm.utils import convert_chat_messages_to_llm_format

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _isolate_messages_and_engine():
    async with async_session() as db:
        await db.execute(delete(ChatMessage))
        await db.commit()
    yield
    await engine.dispose()


async def _seed_agent() -> tuple[Agent, User]:
    suffix = uuid.uuid4().hex[:10]
    async with async_session() as db:
        tenant = Tenant(name=f"IM delivery {suffix}", slug=f"im-delivery-{suffix}")
        db.add(tenant)
        await db.flush()
        identity = Identity(
            username=f"im_delivery_{suffix}",
            email=f"im-delivery-{suffix}@test.local",
            password_hash="x",
        )
        db.add(identity)
        await db.flush()
        user = User(
            identity_id=identity.id,
            display_name="IM Delivery Tester",
            role="member",
            is_active=True,
            tenant_id=tenant.id,
        )
        db.add(user)
        await db.flush()
        agent = Agent(
            name=f"Delivery Agent {suffix}",
            creator_id=user.id,
            tenant_id=tenant.id,
            status="idle",
        )
        db.add(agent)
        await db.commit()
        await db.refresh(agent)
        await db.refresh(user)
        return agent, user


async def _seed_message(
    agent: Agent,
    user: User,
    result: IMDeliveryResult,
) -> ChatMessage:
    async with async_session() as db:
        session = ChatSession(
            agent_id=agent.id,
            user_id=user.id,
            title="IM delivery test",
            source_channel="web",
            external_conv_id=f"web_{uuid.uuid4().hex}",
        )
        db.add(session)
        await db.flush()
        row = ChatMessage(
            agent_id=agent.id,
            user_id=user.id,
            role="assistant",
            content="the original visible content",
            conversation_id=str(session.id),
            thinking="private chain-of-thought summary",
            message_meta={"delivery": result.to_meta()},
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row


async def _recall(agent: Agent, user: User, row: ChatMessage) -> dict:
    return await im_delivery.recall_message(
        agent_id=agent.id,
        message_id=row.id,
        user_id=user.id,
        current_session_id=row.conversation_id,
    )


async def test_unsupported_transport_recall_does_not_require_channel_config():
    agent, user = await _seed_agent()
    row = await _seed_message(
        agent,
        user,
        IMDeliveryResult.sent(
            "whatsapp",
            IMDeliveryPart(
                transport="whatsapp_cloud",
                provider_message_id="wamid-1",
                conversation_ref="15550001111",
                recallable=False,
            ),
        ),
    )

    result = await _recall(agent, user, row)

    assert result == {
        "status": "unsupported",
        "message_id": str(row.id),
        "parts": [
            {
                "part_id": "0",
                "transport": "whatsapp_cloud",
                "status": "unsupported",
            }
        ],
    }


async def test_pending_delivery_is_not_recalled_and_stale_pending_becomes_unknown():
    agent, user = await _seed_agent()
    row = await _seed_message(agent, user, IMDeliveryResult.pending("dingtalk"))

    pending = await _recall(agent, user, row)
    assert pending == {"status": "pending", "message_id": str(row.id)}

    async with async_session() as db:
        stored = await db.get(ChatMessage, row.id)
        delivery = dict(stored.message_meta["delivery"])
        delivery["updated_at"] = (
            datetime.now(UTC) - im_delivery.DELIVERY_LEASE - timedelta(seconds=1)
        ).isoformat()
        stored.message_meta = {**stored.message_meta, "delivery": delivery}
        await db.commit()

    unknown = await _recall(agent, user, row)
    assert unknown == {"status": "unknown", "message_id": str(row.id)}


async def test_successful_part_survives_later_delivery_failure():
    agent, user = await _seed_agent()
    row = await _seed_message(agent, user, IMDeliveryResult.pending("slack"))
    part = IMDeliveryPart(
        transport="slack",
        provider_message_id="remote-first-chunk",
        conversation_ref="channel-1",
        artifact_role="chunk",
    )

    assert await im_delivery.append_delivery_part(row.id, part) is True
    assert await im_delivery.register_delivery(
        row.id,
        IMDeliveryResult.failed("slack", "second_chunk_rejected"),
    ) is True

    async with async_session() as db:
        stored = await db.get(ChatMessage, row.id)
    delivery = stored.message_meta["delivery"]
    assert delivery["status"] == "partial"
    assert [item["provider_message_id"] for item in delivery["parts"]] == [
        "remote-first-chunk"
    ]
    assert delivery["recall"]["status"] == "available"


async def test_transport_timeout_maps_to_unknown_but_provider_rejection_maps_to_failed():
    unknown = IMDeliveryResult.from_exception("slack", TimeoutError())
    assert unknown.status == "unknown"
    assert unknown.to_meta()["status"] == "unknown"
    assert im_delivery.attach_delivery_to_meta({}, unknown)["delivery_status"] == "unknown"
    assert IMDeliveryResult.from_exception("slack", RuntimeError("rejected")).status == "failed"


async def test_uncertain_partial_delivery_never_becomes_fully_recalled(monkeypatch):
    agent, user = await _seed_agent()
    async with async_session() as db:
        db.add(
            ChannelConfig(
                agent_id=agent.id,
                channel_type="slack",
                app_id="app",
                app_secret="token",
                is_configured=True,
            )
        )
        await db.commit()
    row = await _seed_message(agent, user, IMDeliveryResult.pending("slack"))
    await im_delivery.append_delivery_part(
        row.id,
        IMDeliveryPart(
            transport="test_uncertain_recall",
            provider_message_id="known-part",
            conversation_ref="channel-1",
        ),
    )
    await im_delivery.register_delivery(
        row.id,
        IMDeliveryResult.unknown("slack", "second_chunk_timeout"),
    )

    async def fake_recall(_config, parts):
        return [PartRecallResult(part_id=str(part["part_id"]), status="recalled") for part in parts]

    monkeypatch.setitem(
        im_delivery.IM_RECALL_ADAPTERS,
        "test_uncertain_recall",
        fake_recall,
    )

    result = await _recall(agent, user, row)
    assert result["status"] == "partial"
    async with async_session() as db:
        stored = await db.get(ChatMessage, row.id)
        payload = serialize_chat_message_for_client(stored)
    assert payload["content"] == "the original visible content"
    assert payload["recall_status"] == "partial"


async def test_dingtalk_group_recall_uses_robot_api_and_retries_transient_failure(monkeypatch):
    config = ChannelConfig(
        agent_id=uuid.uuid4(),
        channel_type="dingtalk",
        app_id="robot-code",
        app_secret="secret",
        is_configured=True,
    )
    requests = []

    class FakeResponse:
        status_code = 200

        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, *, headers, json):
            requests.append((url, headers, json))
            if len(requests) == 1:
                return FakeResponse(
                    {"successResult": [], "failedResult": {"process-key": "not_ready"}}
                )
            return FakeResponse({"successResult": ["process-key"], "failedResult": {}})

    async def fake_token(_config):
        return "access-token"

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(im_delivery, "_dingtalk_token", fake_token)
    monkeypatch.setattr(im_delivery.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(im_delivery.asyncio, "sleep", no_sleep)

    result = await im_delivery._recall_dingtalk_group(
        config,
        [
            {
                "part_id": "0",
                "provider_message_id": "process-key",
                "conversation_ref": "open-conversation-id",
            }
        ],
    )

    assert result == [PartRecallResult(part_id="0", status="recalled")]
    assert len(requests) == 2
    assert requests[0][0].endswith("/v1.0/robot/groupMessages/recall")
    assert requests[0][2] == {
        "robotCode": "robot-code",
        "openConversationId": "open-conversation-id",
        "processQueryKeys": ["process-key"],
    }


async def test_recall_merges_per_part_provider_results_and_tombstones_only_full_success(monkeypatch):
    agent, user = await _seed_agent()
    async with async_session() as db:
        db.add(
            ChannelConfig(
                agent_id=agent.id,
                channel_type="slack",
                app_id="app",
                app_secret="token",
                is_configured=True,
            )
        )
        await db.commit()

    row = await _seed_message(
        agent,
        user,
        IMDeliveryResult.sent(
            "slack",
            IMDeliveryPart(
                transport="test_batch_recall",
                provider_message_id="remote-ok",
                conversation_ref="channel-1",
                artifact_role="chunk",
            ),
            IMDeliveryPart(
                transport="test_batch_recall",
                provider_message_id="remote-fail",
                conversation_ref="channel-1",
                artifact_role="chunk",
            ),
        ),
    )

    async def fake_batch_recall(_config, parts):
        return [
            PartRecallResult(
                part_id=str(part["part_id"]),
                status="recalled" if part["provider_message_id"] == "remote-ok" else "failed",
                error=None if part["provider_message_id"] == "remote-ok" else "provider_rejected",
            )
            for part in parts
        ]

    monkeypatch.setitem(im_delivery.IM_RECALL_ADAPTERS, "test_batch_recall", fake_batch_recall)

    result = await _recall(agent, user, row)

    assert result["status"] == "partial"
    assert [part["status"] for part in result["parts"]] == ["recalled", "failed"]
    async with async_session() as db:
        stored = await db.get(ChatMessage, row.id)
        client_payload = serialize_chat_message_for_client(stored)
    assert client_payload["content"] == "the original visible content"
    assert client_payload["recall_status"] == "partial"


async def test_full_recall_is_idempotent_and_hides_content_from_client_and_llm(monkeypatch):
    agent, user = await _seed_agent()
    async with async_session() as db:
        db.add(
            ChannelConfig(
                agent_id=agent.id,
                channel_type="slack",
                app_id="app",
                app_secret="token",
                is_configured=True,
            )
        )
        await db.commit()
    row = await _seed_message(
        agent,
        user,
        IMDeliveryResult.sent(
            "slack",
            IMDeliveryPart(
                transport="test_full_recall",
                provider_message_id="remote-1",
                conversation_ref="channel-1",
            ),
        ),
    )
    calls = 0

    async def fake_recall(_config, parts):
        nonlocal calls
        calls += 1
        return [PartRecallResult(part_id=str(part["part_id"]), status="recalled") for part in parts]

    monkeypatch.setitem(im_delivery.IM_RECALL_ADAPTERS, "test_full_recall", fake_recall)

    first = await _recall(agent, user, row)
    second = await _recall(agent, user, row)

    assert first["status"] == "recalled"
    assert second == {"status": "already_recalled", "message_id": str(row.id)}
    assert calls == 1
    async with async_session() as db:
        stored = await db.get(ChatMessage, row.id)
        client_payload = serialize_chat_message_for_client(stored)
        llm_payload = convert_chat_messages_to_llm_format([stored])
        authoritative_llm_payload = build_llm_messages_from_rows(
            [stored],
            include_thinking=True,
        )
    assert client_payload["content"] == "该消息已撤回"
    assert client_payload["display_content"] == "该消息已撤回"
    assert client_payload["recall_status"] == "recalled"
    assert "thinking" not in client_payload
    assert llm_payload == [
        {"role": "assistant", "content": "[该消息已撤回，不应视为仍对用户可见]"}
    ]
    assert authoritative_llm_payload == [
        {"role": "assistant", "content": "[该消息已撤回，不应视为仍对用户可见]"}
    ]


async def test_compacted_recall_injects_a_delivery_correction_after_summary():
    agent, user = await _seed_agent()
    row = await _seed_message(
        agent,
        user,
        IMDeliveryResult.sent(
            "slack",
            IMDeliveryPart(
                transport="slack",
                provider_message_id="remote-compacted",
                conversation_ref="channel-1",
            ),
        ),
    )
    async with async_session() as db:
        stored = await db.get(ChatMessage, row.id)
        delivery = dict(stored.message_meta["delivery"])
        delivery["recall"] = {
            **delivery["recall"],
            "status": "recalled",
            "completed_at": "2026-08-24T00:00:00+00:00",
        }
        stored.message_meta = {**stored.message_meta, "delivery": delivery}
        marker = ChatCompaction(
            session_id=stored.conversation_id,
            agent_id=agent.id,
            epoch=1,
            compacted_from_message_id=stored.id,
            compacted_to_message_id=stored.id,
            summary_text="The assistant sent a proposal.",
            summary_tokens=8,
            trigger_prompt_tokens=100,
            trigger_ratio=0.9,
            summary_validation_passed=True,
        )
        db.add(marker)
        await db.flush()
        stored.compacted_into = marker.id
        await db.commit()

    async with async_session() as db:
        rows = await load_messages_for_session(
            db,
            agent_id=agent.id,
            conversation_id=row.conversation_id,
            ctx_size=100,
        )

    assert len(rows) == 2
    assert "<conversation-summary" in rows[0].content
    assert "<delivery-corrections>" in rows[1].content
    assert str(row.id) in rows[1].content
    assert "was later withdrawn" in rows[1].content


async def test_concurrent_recall_returns_recalling_while_provider_call_is_in_flight(monkeypatch):
    agent, user = await _seed_agent()
    async with async_session() as db:
        db.add(
            ChannelConfig(
                agent_id=agent.id,
                channel_type="slack",
                app_id="app",
                app_secret="token",
                is_configured=True,
            )
        )
        await db.commit()
    row = await _seed_message(
        agent,
        user,
        IMDeliveryResult.sent(
            "slack",
            IMDeliveryPart(
                transport="test_slow_recall",
                provider_message_id="remote-1",
                conversation_ref="channel-1",
            ),
        ),
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_recall(_config, parts):
        entered.set()
        await release.wait()
        return [PartRecallResult(part_id=str(part["part_id"]), status="recalled") for part in parts]

    monkeypatch.setitem(im_delivery.IM_RECALL_ADAPTERS, "test_slow_recall", slow_recall)

    first_task = asyncio.create_task(
        _recall(agent, user, row)
    )
    await entered.wait()
    second = await _recall(agent, user, row)
    release.set()
    first = await first_task

    assert second == {"status": "recalling", "message_id": str(row.id)}
    assert first["status"] == "recalled"


async def test_agent_cannot_recall_another_agents_message():
    agent, user = await _seed_agent()
    other_agent, _ = await _seed_agent()
    row = await _seed_message(
        agent,
        user,
        IMDeliveryResult.unsupported_delivery("wechat", "wechat_ilink"),
    )

    result = await im_delivery.recall_message(
        agent_id=other_agent.id,
        message_id=row.id,
        user_id=user.id,
        current_session_id=row.conversation_id,
    )

    assert result == {"status": "not_found", "message_id": str(row.id)}


async def test_plain_member_cannot_recall_same_agents_other_users_message():
    agent, owner = await _seed_agent()
    row = await _seed_message(
        agent,
        owner,
        IMDeliveryResult.unsupported_delivery("wechat", "wechat_ilink"),
    )
    suffix = uuid.uuid4().hex[:10]
    async with async_session() as db:
        stored_agent = await db.get(Agent, agent.id)
        stored_agent.access_mode = "company"
        identity = Identity(
            username=f"im_delivery_viewer_{suffix}",
            email=f"im-delivery-viewer-{suffix}@test.local",
            password_hash="x",
        )
        db.add(identity)
        await db.flush()
        viewer = User(
            identity_id=identity.id,
            display_name="Limited IM Viewer",
            role="member",
            is_active=True,
            tenant_id=stored_agent.tenant_id,
        )
        db.add(viewer)
        await db.flush()
        viewer_session = ChatSession(
            agent_id=agent.id,
            user_id=viewer.id,
            title="Viewer current session",
            source_channel="web",
            external_conv_id=f"web_{uuid.uuid4().hex}",
        )
        db.add(viewer_session)
        await db.commit()
        viewer_id = viewer.id
        viewer_session_id = viewer_session.id

    result = await im_delivery.recall_message(
        agent_id=agent.id,
        message_id=row.id,
        user_id=viewer_id,
        current_session_id=viewer_session_id,
    )

    assert result == {"status": "not_found", "message_id": str(row.id)}
