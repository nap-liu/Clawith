from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from app.services.toolscall import capability
from app.services.toolscall.runtime import ToolscallRuntime

_SIGNING_SEED = b"t" * 32


@pytest.fixture(autouse=True)
def initialized_toolscall_runtime(monkeypatch):
    monkeypatch.setattr(
        capability,
        "initialize_toolscall_runtime",
        AsyncMock(
            return_value=ToolscallRuntime(
                signing_seed=_SIGNING_SEED,
                bridge_url="http://bridge.test/api/internal/toolscall/v1",
            )
        ),
    )
    monkeypatch.setattr(
        capability,
        "register_toolscall_scope",
        AsyncMock(return_value="scope-current-turn"),
    )


def _tool(name: str, properties=None):
    return {
        "type": "function",
        "function": {
            "name": name,
            "parameters": {
                "type": "object",
                "properties": properties or {},
            },
        },
    }


@pytest.mark.asyncio
async def test_scope_preserves_endpoint_type_and_excludes_recursive_controls(
    monkeypatch,
):
    tools = [
        _tool(
            "standard-tool",
            {
                "name": {"type": "string"},
                "count": {"type": "integer"},
                "filter": {"anyOf": [{"type": "object"}, {"type": "null"}]},
            },
        ),
        _tool("native-tool", {"command": {"type": "string"}}),
        _tool("request_confirmation"),
        _tool("execute_code"),
        _tool("execute_code_e2b"),
        _tool("execute_code_aio"),
    ]

    wrapper = await capability.build_toolscall_wrapper(
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        session_id="session-1",
        turn_anchor_id=None,
        tools_for_llm=tools,
        aio_base_url="http://aio-sandbox:8080",
        ttl_seconds=90,
        native_tool_names={"native-tool", "not-visible"},
    )

    assert wrapper is not None
    assert wrapper["native"] == ["native-tool"]
    assert wrapper["standard"] == {
        "standard-tool": {
            "name": "string",
            "count": "integer",
            "filter": ["object", "null"],
        }
    }
    assert wrapper["endpoint"] == "http://bridge.test/api/internal/toolscall/v1"
    assert wrapper["signing_seed"] == _SIGNING_SEED
    assert wrapper["scope"] == "scope-current-turn"
    capability.register_toolscall_scope.assert_awaited_once_with(
        standard_tool_names={"standard-tool"},
        ttl_seconds=90,
    )
