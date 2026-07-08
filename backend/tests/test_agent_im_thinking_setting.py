from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.schemas.schemas import AgentOut, AgentUpdate


def test_agent_model_has_im_thinking_output_enabled_default_false():
    agent = Agent(
        name="测试数字员工",
        creator_id=uuid.uuid4(),
        im_thinking_output_enabled=False,
    )

    assert agent.im_thinking_output_enabled is False


def test_chat_session_model_has_im_config_default_dict():
    session = ChatSession(
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        title="IM",
        source_channel="dingtalk",
        external_conv_id="dingtalk_p2p_1",
        im_config={},
    )

    assert session.im_config == {}


def test_agent_update_accepts_im_thinking_output_enabled():
    update = AgentUpdate(im_thinking_output_enabled=True)

    assert update.im_thinking_output_enabled is True


def test_agent_out_serializes_im_thinking_output_enabled():
    out = AgentOut(
        id=uuid.uuid4(),
        name="测试数字员工",
        role_description="",
        status="running",
        creator_id=uuid.uuid4(),
        autonomy_policy={},
        tokens_used_today=0,
        tokens_used_month=0,
        context_window_size=100,
        max_tool_rounds=50,
        created_at=datetime.now(timezone.utc),
        im_thinking_output_enabled=True,
    )

    assert out.im_thinking_output_enabled is True
