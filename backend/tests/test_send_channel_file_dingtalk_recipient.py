"""Canonical user-to-DingTalk file delivery behavior."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

from app.services import agent_tools_file_delivery_runtime as runtime
from app.services.im_delivery import IMDeliveryResult


async def test_canonical_user_dingtalk_route_reuses_exact_session(monkeypatch, tmp_path):
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    session_id = uuid.uuid4()
    report = tmp_path / "report.pdf"
    report.write_bytes(b"%PDF")
    commits = 0

    class Result:
        def scalar_one_or_none(self):
            return SimpleNamespace(is_configured=True)

    class FakeDb:
        async def execute(self, _statement):
            return Result()

        async def commit(self):
            nonlocal commits
            commits += 1

    @asynccontextmanager
    async def fake_session():
        yield FakeDb()

    async def resolve_route(_db, _agent_id, canonical_user_id, *, channel):
        assert canonical_user_id == str(user_id)
        assert channel == "dingtalk"
        return SimpleNamespace(
            channel="dingtalk",
            user=SimpleNamespace(id=user_id, display_name="User"),
            member=SimpleNamespace(external_id="staff-42"),
        )

    async def find_session(**kwargs):
        assert kwargs["external_conv_id"] == "dingtalk_p2p_staff-42"
        assert kwargs["source_channel"] == "dingtalk"
        return SimpleNamespace(id=session_id)

    async def send_exact(actual_agent_id, actual_path, actual_session_id, message):
        assert (actual_agent_id, actual_path, actual_session_id, message) == (
            agent_id,
            report,
            str(session_id),
            "请查收",
        )
        return "sent", IMDeliveryResult.sent("dingtalk")

    monkeypatch.setattr(runtime, "async_session", fake_session)
    monkeypatch.setattr(runtime, "resolve_human_channel_recipient", resolve_route)
    monkeypatch.setattr(runtime, "find_or_create_channel_session", find_session)
    monkeypatch.setattr(runtime, "_send_file_to_session", send_exact)

    message, result = await runtime._send_file_to_recipient(
        agent_id,
        report,
        str(user_id),
        "请查收",
        channel="dingtalk",
    )

    assert message == "sent"
    assert result.ok and result.channel == "dingtalk"
    assert commits == 1
