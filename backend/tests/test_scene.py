import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft7Validator
from pydantic import ValidationError

from app.schemas.scene import (
    SceneConfig,
    SceneQuickAction,
    SceneSaveRequest,
    SceneToolSaveRequest,
    validate_scene_key,
)
from app.services import scene_service
from app.services.llm.client import GeminiClient
from app.services.tool_seeder import BUILTIN_TOOLS
from app.utils.mini_program_uri import parse_mini_program_uri


MINI_PROGRAM_URI_CONTRACT = json.loads(
    (
        Path(__file__).resolve().parents[2]
        / "frontend"
        / "scripts"
        / "fixtures"
        / "mini_program_uri.json"
    ).read_text(encoding="utf-8")
)


def test_scene_manifest_allows_unbounded_ordered_actions():
    actions = [
        {
            "id": f"action_{index}",
            "label": f"Action {index}",
            "type": "send_message",
            "message": f"message {index}",
        }
        for index in range(250)
    ]

    config = SceneConfig.model_validate({"quick_actions": actions})

    assert len(config.quick_actions) == 250
    assert config.quick_actions[249].message == "message 249"


def test_scene_only_requires_name_and_key():
    request = SceneSaveRequest(name="Basic scene")

    assert validate_scene_key("basic") == "basic"
    assert request.welcome_message == ""
    assert request.system_prompts == []
    assert request.quick_actions == []

    with pytest.raises(ValidationError):
        SceneSaveRequest()


def test_quick_action_preserves_inactive_type_content():
    action = SceneQuickAction(
        id="warranty",
        label="Warranty",
        type="open_uri",
        uri="/work-orders",
        message="我要报修",
    )

    assert action.uri == "/work-orders"
    assert action.message == "我要报修"
    assert action.menu_visible is True
    assert action.ai_visible is True
    assert action.ai_context == ""


def test_quick_action_accepts_bounded_horizontal_button_style():
    action = SceneQuickAction(
        id="warranty",
        label="Warranty",
        type="send_message",
        message="I need warranty service",
        style={
            "bold": True,
            "italic": True,
            "color": "#7c3aed",
            "font": "serif",
        },
    )

    assert action.style is not None
    assert action.style.model_dump() == {
        "bold": True,
        "italic": True,
        "color": "#7C3AED",
        "font": "serif",
    }


@pytest.mark.parametrize(
    "style",
    [
        {"color": "red"},
        {"color": "#12345G"},
        {"font": "Comic Sans MS"},
        {"css": "display:none"},
    ],
)
def test_quick_action_rejects_unbounded_horizontal_button_style(style):
    with pytest.raises(ValidationError):
        SceneQuickAction(
            id="warranty",
            label="Warranty",
            type="send_message",
            message="I need warranty service",
            style=style,
        )


@pytest.mark.parametrize("legacy_enabled", [True, False])
def test_quick_action_maps_legacy_enabled_to_both_visibility_flags(legacy_enabled):
    action = SceneQuickAction.model_validate(
        {
            "id": "warranty",
            "label": "Warranty",
            "type": "send_message",
            "enabled": legacy_enabled,
            "message": "我要报修",
        }
    )

    assert action.menu_visible is legacy_enabled
    assert action.ai_visible is legacy_enabled
    assert action.message == "我要报修"


def test_quick_action_visibility_is_independent_and_new_fields_override_legacy():
    action = SceneQuickAction.model_validate(
        {
            "id": "warranty",
            "label": "Warranty",
            "type": "send_message",
            "enabled": False,
            "menu_visible": True,
            "ai_visible": False,
            "ai_context": "Use when the device is covered by warranty.",
            "message": "我要报修",
        }
    )

    assert action.menu_visible is True
    assert action.ai_visible is False
    assert action.ai_context == "Use when the device is covered by warranty."
    assert "enabled" not in action.model_dump()


@pytest.mark.parametrize(
    "uri",
    [
        "https://example.com/warranty",
        "/orders/current",
        "miniprogram://navigate-to/pages/order/detail?id=1",
    ],
)
def test_scene_open_uri_accepts_supported_protocols(uri):
    action = SceneQuickAction(
        id="warranty",
        label="Warranty",
        type="open_uri",
        uri=uri,
    )
    assert action.uri == uri


@pytest.mark.parametrize(
    "uri",
    [
        "javascript:alert(1)",
        "data:text/html,unsafe",
        "//evil.example/path",
        "miniprogram://launch/pages/home",
    ],
)
def test_scene_open_uri_rejects_unsafe_or_unknown_protocols(uri):
    with pytest.raises(ValidationError):
        SceneQuickAction(
            id="unsafe",
            label="Unsafe",
            type="open_uri",
            uri=uri,
        )


def test_mini_program_uri_contract_matches_scene_validation():
    for expected in MINI_PROGRAM_URI_CONTRACT["valid"]:
        parsed = parse_mini_program_uri(expected["value"])
        assert parsed is not None
        assert parsed.action == expected["action"]
        assert parsed.route == expected["route"]
        assert SceneQuickAction(
            id="valid",
            label="Valid",
            type="open_uri",
            uri=expected["value"],
        ).uri == expected["value"]

    for value in MINI_PROGRAM_URI_CONTRACT["invalid"]:
        assert parse_mini_program_uri(value) is None
        if value.lower().startswith("miniprogram"):
            with pytest.raises(ValidationError):
                SceneQuickAction(
                    id="invalid",
                    label="Invalid",
                    type="open_uri",
                    uri=value,
                )


def test_scene_tool_is_global_builtin_and_opt_in():
    seed = next(item for item in BUILTIN_TOOLS if item["name"] == "manage_scene")

    assert seed["is_default"] is False
    assert "H5" not in seed["description"]
    assert "Agent" not in seed["description"]
    assert seed["parameters_schema"]["properties"]["operation"]["enum"] == [
        "list",
        "get",
        "save",
        "publish",
        "delete",
        "rollback",
    ]
    overwrite = seed["parameters_schema"]["properties"]["force_overwrite"]
    assert overwrite["type"] == "boolean"
    assert overwrite["default"] is False
    description = seed["description"]
    assert "Omitted fields preserve" in description
    assert "explicit empty string or empty array clears" in description
    assert "force_overwrite=true" in description
    assert "omitted enabled to true" in description
    assert "name may be omitted when updating" in description
    assert "required when creating" in description
    assert "array field is provided, it replaces that entire ordered array" in description
    assert "Every save requires expected_revision" in description
    schema = seed["parameters_schema"]
    action_properties = schema["properties"]["quick_actions"]["items"]["properties"]
    assert action_properties["id"] == {
        "type": "string",
        "minLength": 1,
        "maxLength": 64,
        "pattern": "^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$",
    }
    assert action_properties["menu_visible"]["default"] is True
    assert action_properties["ai_visible"]["default"] is True
    assert action_properties["ai_context"]["maxLength"] == 4000
    assert action_properties["style"]["additionalProperties"] is False
    assert action_properties["style"]["properties"]["font"]["enum"] == [
        "default",
        "sans",
        "serif",
        "monospace",
    ]
    assert "enabled" not in action_properties
    assert "examples" not in schema
    Draft7Validator.check_schema(schema)
    validator = Draft7Validator(schema)
    for valid in (
        {"operation": "list"},
        {"operation": "get", "scene_key": "warranty"},
        {
            "operation": "save",
            "scene_key": "warranty",
            "name": "Warranty service",
            "expected_revision": 0,
        },
        {
            "operation": "save",
            "scene_key": "warranty",
            "expected_revision": 2,
            "welcome_message": "",
        },
        {
            "operation": "save",
            "scene_key": "warranty",
            "expected_revision": 2,
            "force_overwrite": True,
        },
        {
            "operation": "publish",
            "scene_key": "warranty",
            "expected_revision": 2,
        },
        {
            "operation": "rollback",
            "scene_key": "warranty",
            "expected_revision": 2,
            "target_revision": 1,
        },
        {
            "operation": "save",
            "scene_key": "warranty",
            "expected_revision": 2,
            "quick_actions": [
                {
                    "id": "repair",
                    "label": "Repair",
                    "type": "send_message",
                    "message": "I need a repair",
                    "style": {
                        "bold": True,
                        "italic": False,
                        "color": "#7C3AED",
                        "font": "serif",
                    },
                },
                {
                    "id": "orders",
                    "label": "Orders",
                    "type": "open_uri",
                    "uri": "/orders",
                },
            ],
        },
    ):
        assert list(validator.iter_errors(valid)) == []

    for invalid in (
        {"operation": "get"},
        {"operation": "publish", "scene_key": "warranty"},
        {
            "operation": "rollback",
            "scene_key": "warranty",
            "expected_revision": 2,
        },
        {
            "operation": "save",
            "scene_key": "warranty",
        },
        {
            "operation": "save",
            "scene_key": "Warranty",
            "expected_revision": 0,
        },
        {
            "operation": "save",
            "scene_key": "warranty",
            "quick_actions": [
                {"id": "repair", "label": "Repair", "type": "send_message"}
            ],
        },
        {
            "operation": "save",
            "scene_key": "warranty",
            "expected_revision": 2,
            "quick_actions": [
                {
                    "id": "bad id",
                    "label": "Repair",
                    "type": "send_message",
                    "message": "Repair",
                }
            ],
        },
        {
            "operation": "save",
            "scene_key": "warranty",
            "expected_revision": 2,
            "quick_actions": [
                {
                    "id": "a" * 65,
                    "label": "Repair",
                    "type": "send_message",
                    "message": "Repair",
                }
            ],
        },
    ):
        assert list(validator.iter_errors(invalid))

    declarations, _ = GeminiClient(
        api_key="test",
        model="gemini-test",
    )._convert_tools(
        [
            {
                "type": "function",
                "function": {
                    "name": seed["name"],
                    "description": seed["description"],
                    "parameters": schema,
                },
            }
        ]
    )
    assert declarations == [
        {
            "functionDeclarations": [
                {
                    "name": "manage_scene",
                    "description": seed["description"],
                    "parameters": schema,
                }
            ]
        }
    ]


def test_scene_tool_save_is_incremental_by_default():
    current = {
        "name": "Warranty",
        "enabled": False,
        "welcome_message": "Welcome",
        "system_prompts": [
            {"id": "tone", "name": "Tone", "content": "Be concise.", "enabled": True}
        ],
        "quick_actions": [
            {
                "id": "repair",
                "label": "Repair",
                "type": "send_message",
                "message": "I need a repair",
            }
        ],
    }

    merged = scene_service._merge_tool_save_request(
        current,
        SceneToolSaveRequest(name="Updated warranty"),
    )

    assert merged.name == "Updated warranty"
    assert merged.enabled is False
    assert merged.welcome_message == "Welcome"
    assert merged.system_prompts[0].content == "Be concise."
    assert merged.quick_actions[0].message == "I need a repair"


def test_scene_tool_save_can_clear_one_field_without_clearing_others():
    current = {
        "name": "Warranty",
        "enabled": True,
        "welcome_message": "Welcome",
        "system_prompts": [
            {"id": "tone", "name": "Tone", "content": "Be concise.", "enabled": True}
        ],
        "quick_actions": [
            {
                "id": "repair",
                "label": "Repair",
                "type": "send_message",
                "message": "I need a repair",
            }
        ],
    }

    merged = scene_service._merge_tool_save_request(
        current,
        SceneToolSaveRequest(welcome_message=""),
    )

    assert merged.welcome_message == ""
    assert len(merged.system_prompts) == 1
    assert len(merged.quick_actions) == 1


def test_scene_tool_force_overwrite_resets_omitted_optional_fields():
    current = {
        "name": "Warranty",
        "enabled": False,
        "welcome_message": "Welcome",
        "system_prompts": [
            {"id": "tone", "name": "Tone", "content": "Be concise.", "enabled": True}
        ],
        "quick_actions": [
            {
                "id": "repair",
                "label": "Repair",
                "type": "send_message",
                "message": "I need a repair",
            }
        ],
    }

    merged = scene_service._merge_tool_save_request(
        current,
        SceneToolSaveRequest(force_overwrite=True),
    )

    assert merged.name == "Warranty"
    assert merged.enabled is True
    assert merged.welcome_message == ""
    assert merged.system_prompts == []
    assert merged.quick_actions == []


def test_scene_tool_save_requires_name_only_when_creating():
    with pytest.raises(ValueError, match="name is required"):
        scene_service._merge_tool_save_request(None, SceneToolSaveRequest())

    created = scene_service._merge_tool_save_request(
        None,
        SceneToolSaveRequest(name="Warranty"),
    )

    assert created.name == "Warranty"
    assert created.welcome_message == ""


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _FakeDb:
    def __init__(self, results):
        self.results = list(results)
        self.added = []
        self.committed = False

    async def execute(self, _query):
        return _ScalarResult(self.results.pop(0))

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.committed = True


class _SessionFactory:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, *_args):
        return False


async def test_scene_tool_denies_non_manager_in_current_direct_session(monkeypatch):
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    session = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        user_id=user_id,
        is_group=False,
        source_channel="miniprogram",
    )
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)
    user = SimpleNamespace(id=user_id, tenant_id=tenant_id, is_active=True, role="member")
    db = _FakeDb([session, agent, user])

    monkeypatch.setattr(scene_service, "async_session", lambda: _SessionFactory(db))
    monkeypatch.setattr(scene_service, "scene_tool_enabled", lambda *_args: _async_value(True))
    monkeypatch.setattr(scene_service, "user_can_manage_agent_id", lambda *_args: _async_value(False))

    result = await scene_service.execute_scene_management_tool(
        agent_id=agent_id,
        user_id=user_id,
        session_id=str(session.id),
        arguments={"operation": "list"},
    )

    assert result.startswith("❌ Scene management denied")
    assert "administrator permission" in result
    assert db.committed is True
    assert db.added[-1].action == "scene_tool_denied"


@pytest.mark.parametrize("source_channel", ["web", "miniprogram", "feishu"])
async def test_scene_tool_allows_verified_manager_from_any_direct_human_channel(monkeypatch, source_channel):
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    session = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        user_id=user_id,
        is_group=False,
        source_channel=source_channel,
    )
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)
    user = SimpleNamespace(id=user_id, tenant_id=tenant_id, is_active=True, role="member")
    db = _FakeDb([session, agent, user])

    monkeypatch.setattr(scene_service, "async_session", lambda: _SessionFactory(db))
    monkeypatch.setattr(scene_service, "scene_tool_enabled", lambda *_args: _async_value(True))
    monkeypatch.setattr(scene_service, "user_can_manage_agent_id", lambda *_args: _async_value(True))
    monkeypatch.setattr(
        scene_service,
        "list_scenes",
        lambda *_args: _async_value([{"scene_key": "default", "revision": 2}]),
    )

    result = await scene_service.execute_scene_management_tool(
        agent_id=agent_id,
        user_id=user_id,
        session_id=str(session.id),
        arguments={"operation": "list"},
    )

    assert json.loads(result) == [{"scene_key": "default", "revision": 2}]
    assert db.added[-1].action == "scene_tool_allowed"


async def _async_value(value):
    return value
