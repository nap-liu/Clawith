"""Agent-visible identity contracts use canonical, typed execution IDs only."""

import uuid

import pytest
from pydantic import ValidationError

from app.schemas.schemas import (
    GatewayMessageOut,
    GatewayRelationshipItem,
    GatewaySendMessageRequest,
)
from app.services.agent_tools import AGENT_TOOLS
from app.services.tool_seeder import BUILTIN_TOOLS


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


def _agent_schema(name: str) -> dict:
    for tool in AGENT_TOOLS:
        function = tool.get("function") or {}
        if function.get("name") == name:
            return function["parameters"]
    raise AssertionError(f"Agent tool {name!r} is missing")


def _seed_schema(name: str) -> dict:
    for tool in BUILTIN_TOOLS:
        if tool.get("name") == name:
            return tool["parameters_schema"]
    raise AssertionError(f"Seeded tool {name!r} is missing")


def test_seeded_tool_strings_fit_persisted_column_contracts():
    for tool in BUILTIN_TOOLS:
        assert len(tool.get("icon") or "") <= 10, tool.get("name")


@pytest.mark.parametrize(
    ("name", "canonical_field"),
    [
        ("send_channel_message", "user_id"),
        ("send_platform_message", "user_id"),
        ("send_feishu_message", "user_id"),
        ("send_message_to_agent", "agent_id"),
        ("send_file_to_agent", "agent_id"),
    ],
)
def test_message_tool_schemas_use_only_canonical_recipient_ids(name, canonical_field):
    for schema in (_agent_schema(name), _seed_schema(name)):
        properties = schema["properties"]
        assert canonical_field in properties
        assert canonical_field in schema["required"]
        assert LEGACY_EXECUTION_FIELDS.isdisjoint(properties)


@pytest.mark.parametrize("name", ["add_contact", "remove_contact"])
def test_contact_mutation_schema_requires_exactly_one_canonical_id(name):
    for schema in (_agent_schema(name), _seed_schema(name)):
        assert LEGACY_EXECUTION_FIELDS.isdisjoint(schema["properties"])
        assert schema["oneOf"] == [
            {"required": ["user_id"], "not": {"required": ["agent_id"]}},
            {"required": ["agent_id"], "not": {"required": ["user_id"]}},
        ]


def test_okr_seed_schemas_use_canonical_owner_and_member_ids():
    objective = _seed_schema("create_objective")
    daily = _seed_schema("upsert_member_daily_report")

    assert LEGACY_EXECUTION_FIELDS.isdisjoint(objective["properties"])
    assert objective["not"] == {"required": ["user_id", "agent_id"]}
    assert {"user_id", "agent_id"}.issubset(objective["properties"])

    assert LEGACY_EXECUTION_FIELDS.isdisjoint(daily["properties"])
    assert daily["oneOf"] == [
        {"required": ["user_id"], "not": {"required": ["agent_id"]}},
        {"required": ["agent_id"], "not": {"required": ["user_id"]}},
    ]


@pytest.mark.parametrize(
    "name",
    [
        "feishu_user_search",
        "feishu_calendar_list",
        "feishu_calendar_create",
        "feishu_calendar_update",
        "feishu_calendar_delete",
        "feishu_approval_create",
    ],
)
def test_feishu_tool_contract_never_exposes_provider_or_name_execution_ids(name):
    for schema in (_agent_schema(name), _seed_schema(name)):
        assert LEGACY_EXECUTION_FIELDS.isdisjoint(schema.get("properties", {}))

    runtime_description = next(
        tool["function"]["description"]
        for tool in AGENT_TOOLS
        if tool.get("function", {}).get("name") == name
    )
    seeded_description = next(
        tool["description"] for tool in BUILTIN_TOOLS if tool.get("name") == name
    )
    for description in (runtime_description, seeded_description):
        assert "open_id" not in description


def test_feishu_execution_tools_use_canonical_user_ids():
    for getter in (_agent_schema, _seed_schema):
        assert "user_id" in getter("feishu_calendar_list")["properties"]
        assert "attendee_user_ids" in getter("feishu_calendar_create")["properties"]
        approval = getter("feishu_approval_create")
        assert "user_id" in approval["properties"]
        assert "user_id" in approval["required"]


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
