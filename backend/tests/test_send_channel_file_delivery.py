import json
import uuid

import pytest

from app.services import agent_tools


@pytest.mark.asyncio
async def test_send_channel_file_web_fallback_returns_platform_delivery_json(tmp_path, monkeypatch):
    agent_id = uuid.uuid4()
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", tmp_path)

    workspace = tmp_path / str(agent_id)
    report = workspace / "workspace" / "report.pdf"
    report.parent.mkdir(parents=True)
    report.write_bytes(b"%PDF-1.4 test")

    result = await agent_tools._send_channel_file(
        agent_id,
        workspace,
        {
            "file_path": "workspace/report.pdf",
            "message": "这是报告",
        },
    )

    payload = json.loads(result)
    assert payload == {
        "type": "platform_file_delivery",
        "path": "workspace/report.pdf",
        "filename": "report.pdf",
        "message": "这是报告",
        "mime_type": "application/pdf",
        "size": len(b"%PDF-1.4 test"),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "file_path",
    [
        "../secret.txt",
        "/etc/passwd",
        "https://evil.example/file.txt",
        "C:/Users/secret.txt",
        "workspace\\..\\secret.txt",
    ],
)
async def test_send_channel_file_rejects_unsafe_relative_path(tmp_path, monkeypatch, file_path):
    agent_id = uuid.uuid4()
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", tmp_path)

    workspace = tmp_path / str(agent_id)
    workspace.mkdir(parents=True)

    result = await agent_tools._send_channel_file(
        agent_id,
        workspace,
        {"file_path": file_path},
    )

    assert result == "Error: Invalid file_path"
