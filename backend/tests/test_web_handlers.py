"""Unit tests for the web_* dispatch handlers (backend mocked)."""
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.services import agent_tools


def _patches(fake_backend):
    return (
        patch("app.services.sandbox.registry.get_sandbox_backend", return_value=fake_backend),
        patch.object(agent_tools, "_get_tool_config", AsyncMock(return_value=None)),
    )


async def test_web_eval_handler_passes_anchor_and_formats_result():
    fake = AsyncMock()
    fake.web_eval = AsyncMock(return_value={"success": True, "error": None, "result": "Example Domain"})
    p1, p2 = _patches(fake)
    with p1, p2:
        out = await agent_tools._web_eval(
            uuid.uuid4(), Path("/tmp"), {"expression": "document.title"}, session_id="conv-7"
        )
    kwargs = fake.web_eval.call_args.kwargs
    assert kwargs["conversation_id"] == "conv-7"
    assert kwargs["expression"] == "document.title"
    assert "Example Domain" in out


async def test_web_eval_handler_requires_expression():
    out = await agent_tools._web_eval(uuid.uuid4(), Path("/tmp"), {}, session_id="c")
    assert "expression" in out


async def test_web_cdp_handler_passes_method_and_params():
    fake = AsyncMock()
    fake.web_cdp = AsyncMock(return_value={"success": True, "error": None, "result": {"ok": 1}})
    p1, p2 = _patches(fake)
    with p1, p2:
        out = await agent_tools._web_cdp(
            uuid.uuid4(), Path("/tmp"),
            {"method": "Input.dispatchMouseEvent", "params": {"type": "mouseMoved", "x": 1, "y": 2}},
            session_id="c",
        )
    kwargs = fake.web_cdp.call_args.kwargs
    assert kwargs["method"] == "Input.dispatchMouseEvent"
    assert kwargs["params"] == {"type": "mouseMoved", "x": 1, "y": 2}
    assert "ok" in out


async def test_web_open_handler_reports_title():
    fake = AsyncMock()
    fake.web_open = AsyncMock(return_value={"success": True, "error": None, "url": "https://e.com", "title": "E"})
    p1, p2 = _patches(fake)
    with p1, p2:
        out = await agent_tools._web_open(uuid.uuid4(), Path("/tmp"), {"url": "https://e.com"}, session_id="c")
    assert "https://e.com" in out
    assert "E" in out


async def test_web_screenshot_handler_writes_png(tmp_path: Path):
    png_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    fake = AsyncMock()
    fake.web_screenshot = AsyncMock(return_value={"success": True, "error": None, "screenshot_b64": png_b64})
    p1, p2 = _patches(fake)
    with p1, p2:
        out = await agent_tools._web_screenshot(uuid.uuid4(), tmp_path, {}, session_id="c")
    pngs = list(tmp_path.glob("*.png"))
    assert len(pngs) == 1
    assert pngs[0].name in out


async def test_web_eval_handler_reports_failure():
    fake = AsyncMock()
    fake.web_eval = AsyncMock(return_value={"success": False, "error": "web_eval failed: boom", "result": None})
    p1, p2 = _patches(fake)
    with p1, p2:
        out = await agent_tools._web_eval(uuid.uuid4(), Path("/tmp"), {"expression": "x"}, session_id="c")
    assert "boom" in out
