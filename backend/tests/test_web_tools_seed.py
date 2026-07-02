"""The web_* RPA builtin tool definitions are well-formed and non-default."""
import pytest

from app.services.tool_seeder import BUILTIN_TOOLS

WEB_TOOLS = ["web_open", "web_eval", "web_cdp", "web_screenshot"]


def _tool(name):
    return next((t for t in BUILTIN_TOOLS if t["name"] == name), None)


@pytest.mark.parametrize("name", WEB_TOOLS)
def test_tool_registered_non_default_browser(name):
    t = _tool(name)
    assert t is not None, f"{name} not seeded"
    assert t["is_default"] is False
    assert t["category"] == "browser"


@pytest.mark.parametrize("name", WEB_TOOLS)
def test_tool_targets_aio_sandbox(name):
    t = _tool(name)
    assert t["config"]["sandbox_type"] == "aio_sandbox"
    assert t["config"]["api_url"] == "http://aio-sandbox:8080"


def test_required_params():
    assert _tool("web_open")["parameters_schema"]["required"] == ["url"]
    assert _tool("web_eval")["parameters_schema"]["required"] == ["expression"]
    assert _tool("web_cdp")["parameters_schema"]["required"] == ["method"]
    # web_screenshot takes no required params
    assert _tool("web_screenshot")["parameters_schema"].get("required", []) == []
