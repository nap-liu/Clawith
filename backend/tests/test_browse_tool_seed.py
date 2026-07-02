"""The `browse` builtin tool definition is well-formed and non-default."""
from app.services.tool_seeder import BUILTIN_TOOLS


def _tool(name):
    return next((t for t in BUILTIN_TOOLS if t["name"] == name), None)


def test_browse_tool_is_registered_and_non_default():
    t = _tool("browse")
    assert t is not None
    assert t["is_default"] is False
    assert t["category"] == "browser"


def test_browse_tool_targets_aio_sandbox():
    t = _tool("browse")
    assert t["config"]["sandbox_type"] == "aio_sandbox"
    assert t["config"]["api_url"] == "http://aio-sandbox:8080"


def test_browse_tool_params():
    props = _tool("browse")["parameters_schema"]["properties"]
    assert props["url"]["type"] == "string"
    assert props["extract"]["type"] == "boolean"
    assert props["screenshot"]["type"] == "boolean"
    assert _tool("browse")["parameters_schema"]["required"] == ["url"]
