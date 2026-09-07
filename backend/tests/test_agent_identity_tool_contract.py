"""Seeded LLM tools and Gateway requests use canonical, typed execution IDs."""

import uuid

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from sqlalchemy import select

from app.database import async_session
from app.models.channel_config import ChannelConfig
from app.models.tool import AgentTool, Tool
from app.schemas.schemas import (
    GatewayMessageOut,
    GatewayRelationshipItem,
    GatewaySendMessageRequest,
)
from app.services.agent_tools import get_agent_tools_for_llm
from app.services.tool_seeder import seed_builtin_tools
from tests.test_agent_mcp_lifecycle import _isolate, _make_agents  # noqa: F401 - autouse fixture


LEGACY_EXECUTION_FIELDS = {
    "agent_name",
    "member_id",
    "member_name",
    "member_type",
    "owner_id",
    "owner_name",
    "owner_type",
    "target",
    "target_agent_id",
    "open_id",
    "user_open_id",
    "user_email",
    "attendee_names",
    "attendee_open_ids",
    "attendee_emails",
}


@pytest.mark.asyncio
async def test_seeded_identity_contract_reaches_llm_with_canonical_parameters():
    recipients = {
        "send_channel_message": "user_id", "send_platform_message": "user_id",
        "send_feishu_message": "user_id", "send_message_to_agent": "agent_id",
        "send_file_to_agent": "agent_id",
    }
    sessions = {"send_session_message", "send_group_session_message"}
    feishu = {
        "feishu_user_search", "feishu_calendar_list", "feishu_calendar_create",
        "feishu_calendar_update", "feishu_calendar_delete", "feishu_approval_create",
    }
    contacts = {"add_contact", "remove_contact"}
    names = set(recipients) | sessions | feishu | contacts
    await seed_builtin_tools()
    _, (agent_id,) = await _make_agents(1)
    async with async_session() as db:
        db.add(ChannelConfig(agent_id=agent_id, channel_type="feishu", is_configured=True))
        rows = (await db.scalars(select(Tool).where(Tool.name.in_(names)))).all()
        assert {row.name for row in rows} == names
        db.add_all(AgentTool(agent_id=agent_id, tool_id=row.id, enabled=True) for row in rows)
        await db.commit()
    runtime = {
        item["function"]["name"]: item["function"]
        for item in await get_agent_tools_for_llm(agent_id)
    }
    for row in rows:
        schema = runtime[row.name]["parameters"]
        assert LEGACY_EXECUTION_FIELDS.isdisjoint(schema["properties"])
        # A2A runtime legitimately removes msg_type when async A2A is disabled.
        for name, prop in schema["properties"].items():
            assert prop == row.parameters_schema["properties"][name]
        if row.name in recipients:
            field = recipients[row.name]
            assert field in schema["properties"] and field in schema["required"]
        if row.name in feishu:
            assert "open_id" not in runtime[row.name]["description"]
        if row.name in contacts:
            validator = Draft202012Validator(schema)
            assert validator.is_valid({"user_id": str(uuid.uuid4())})
            assert validator.is_valid({"agent_id": str(uuid.uuid4())})
            assert not validator.is_valid({})
            assert not validator.is_valid({"user_id": "u", "agent_id": "a"})
        if row.name in sessions:
            assert set(schema["properties"]) == {
                "session_id", "message", "mention_user_ids", "mention_all",
            }
            assert schema["required"] == ["session_id", "message"]
            assert schema["additionalProperties"] is False
            assert schema["properties"]["mention_user_ids"]["maxItems"] == 20
            assert schema["properties"]["mention_all"]["type"] == "boolean"
            description = runtime[row.name]["description"]
            assert "MUST call" in description and "mention_all=true" in description
            mention_copy = description + " ".join(
                prop.get("description", "") for prop in schema["properties"].values()
            )
            assert "dingtalk" not in mention_copy.lower() and "钉钉" not in mention_copy
    assert "user_id" in runtime["feishu_calendar_list"]["parameters"]["properties"]
    assert "attendee_user_ids" in runtime["feishu_calendar_create"]["parameters"]["properties"]
    assert "user_id" in runtime["feishu_approval_create"]["parameters"]["required"]


@pytest.mark.asyncio
async def test_persisted_okr_schema_retains_canonical_owner_contract():
    await seed_builtin_tools()
    async with async_session() as db:
        rows = (await db.scalars(select(Tool).where(Tool.name.in_(
            {"create_objective", "upsert_member_daily_report"}
        )))).all()
    assert len(rows) == 2
    for row in rows:
        schema = row.parameters_schema
        assert LEGACY_EXECUTION_FIELDS.isdisjoint(schema["properties"])
        if row.name == "create_objective":
            assert schema["not"] == {"required": ["user_id", "agent_id"]}
            assert {"user_id", "agent_id"}.issubset(schema["properties"])
        else:
            assert schema["oneOf"] == [
                {"required": ["user_id"], "not": {"required": ["agent_id"]}},
                {"required": ["agent_id"], "not": {"required": ["user_id"]}},
            ]


def test_gateway_recipient_is_exactly_one_typed_id():
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()

    assert GatewaySendMessageRequest(user_id=user_id, content="hello").user_id == user_id
    assert GatewaySendMessageRequest(agent_id=agent_id, content="hello").agent_id == agent_id
    with pytest.raises(ValidationError):
        GatewaySendMessageRequest(content="missing")
    with pytest.raises(ValidationError):
        GatewaySendMessageRequest(user_id=user_id, agent_id=agent_id, content="ambiguous")
    with pytest.raises(ValidationError):
        GatewaySendMessageRequest(agent_id=agent_id, channel="feishu", content="wrong route")
    with pytest.raises(ValidationError):
        GatewaySendMessageRequest.model_validate({"target": "Alice", "content": "legacy"})


def test_gateway_relationship_item_has_display_name_and_one_typed_id():
    user_id = uuid.uuid4()
    item = GatewayRelationshipItem(
        user_id=user_id,
        display_name="Alice",
        role="collaborator",
        channels=["feishu"],
    )
    assert item.user_id == user_id
    assert "name" not in GatewayRelationshipItem.model_fields
    assert "type" not in GatewayRelationshipItem.model_fields
    with pytest.raises(ValidationError):
        GatewayRelationshipItem(display_name="Missing ID", role="collaborator")


def test_gateway_message_has_exactly_one_canonical_sender_id():
    payload = {
        "id": uuid.uuid4(),
        "content": "hello",
        "created_at": "2026-07-15T00:00:00Z",
    }
    assert GatewayMessageOut(**payload, sender_agent_id=uuid.uuid4()).sender_user_id is None
    assert GatewayMessageOut(**payload, sender_user_id=str(uuid.uuid4())).sender_agent_id is None
    with pytest.raises(ValidationError):
        GatewayMessageOut(**payload)
    with pytest.raises(ValidationError):
        GatewayMessageOut(
            **payload,
            sender_agent_id=uuid.uuid4(),
            sender_user_id=str(uuid.uuid4()),
        )
