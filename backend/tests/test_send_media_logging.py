import json
import uuid

import pytest
from loguru import logger

from app.services.llm import caller


@pytest.mark.asyncio
async def test_send_media_signed_url_never_enters_tool_logs(monkeypatch):
    secret = "do-not-log-signature-123"
    signed_url = f"https://media.example/demo.mp4?token={secret}"
    result = json.dumps({
        "type": "platform_media_delivery",
        "version": 1,
        "status": "sent",
        "media_kind": "video",
        "source_mode": "external_url",
        "url": signed_url,
    })

    async def fake_execute_tool(*_args, **_kwargs):
        return result

    persisted_events = []
    callback_events = []

    async def fake_persist(events, *_args, **_kwargs):
        persisted_events.extend(events)
        return True

    async def on_tool_call(event):
        callback_events.append(event)

    monkeypatch.setattr(caller, "execute_tool", fake_execute_tool)
    monkeypatch.setattr(caller, "_persist_tool_call_events_strict", fake_persist)

    log_lines = []
    sink_id = logger.add(lambda message: log_lines.append(str(message)), level="DEBUG")
    try:
        api_messages = []
        await caller._process_tool_call(
            {
                "id": "call-media-log",
                "function": {
                    "name": "send_media",
                    "arguments": json.dumps({
                        "media_type": "video",
                        "url": signed_url,
                        "url_mode": "external",
                    }),
                },
            },
            api_messages,
            uuid.uuid4(),
            uuid.uuid4(),
            str(uuid.uuid4()),
            False,
            on_tool_call,
            "",
            {"send_media"},
        )
    finally:
        logger.remove(sink_id)

    combined = "".join(log_lines)
    assert secret not in combined
    assert signed_url not in combined
    assert "send_media" in combined
    assert '"source": "url"' in combined
    persisted_done = [event for event in persisted_events if event["status"] == "done"]
    callback_done = [event for event in callback_events if event["status"] == "done"]
    assert json.loads(persisted_done[-1]["result"])["url"] == signed_url
    assert json.loads(callback_done[-1]["result"])["url"] == signed_url
    assert json.loads(api_messages[-1].content)["url"] == signed_url
