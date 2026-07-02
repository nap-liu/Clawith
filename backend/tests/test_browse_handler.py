"""Unit test for the _browse dispatch handler (backend mocked)."""
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.services import agent_tools


async def test_browse_handler_passes_anchor_and_writes_screenshot(tmp_path: Path):
    agent_id = uuid.uuid4()
    fake_backend = AsyncMock()
    fake_backend.browse = AsyncMock(return_value={
        "success": True, "error": None, "url": "https://example.com",
        "title": "Example", "text": "Body here",
        "screenshot_b64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==",
        "truncated": False,
    })
    with patch("app.services.sandbox.registry.get_sandbox_backend", return_value=fake_backend), \
         patch.object(agent_tools, "_get_tool_config", AsyncMock(return_value=None)):
        out = await agent_tools._browse(
            agent_id, tmp_path,
            {"url": "https://example.com", "screenshot": True},
            session_id="conv-9",
        )
    # backend.browse received the conversation as conversation_id
    kwargs = fake_backend.browse.call_args.kwargs
    assert kwargs["conversation_id"] == "conv-9"
    assert kwargs["url"] == "https://example.com"
    # screenshot was materialized into the workspace
    pngs = list(tmp_path.glob("*.png"))
    assert len(pngs) == 1
    assert "Example" in out
    assert pngs[0].name in out


async def test_browse_handler_reports_failure(tmp_path: Path):
    fake_backend = AsyncMock()
    fake_backend.browse = AsyncMock(return_value={
        "success": False, "error": "browse failed: boom", "url": "x",
        "title": "", "text": "", "screenshot_b64": None, "truncated": False,
    })
    with patch("app.services.sandbox.registry.get_sandbox_backend", return_value=fake_backend), \
         patch.object(agent_tools, "_get_tool_config", AsyncMock(return_value=None)):
        out = await agent_tools._browse(
            uuid.uuid4(), tmp_path, {"url": "x"}, session_id="c",
        )
    assert "boom" in out
