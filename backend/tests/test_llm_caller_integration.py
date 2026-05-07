"""Integration tests for caller._process_tool_call normalization.

集成契约：`_process_tool_call` 调用 `finalize_tool_output` 产出 `llm_view`
字符串，三方（DB 回调、LLM api_messages、前端 on_tool_call 回调）一致。
本文件只 mock `execute_tool` 这类外部依赖；`finalize_tool_output` 真实运行，
通过 `AGENT_DATA_DIR` 指向 tmp_path 做文件系统隔离。
"""
import json
import uuid
from pathlib import Path

import pytest
from unittest.mock import patch

from app.services.llm.caller import _process_tool_call
from app.services.llm import tool_output_store as tos


@pytest.fixture
def tmp_workspace(tmp_path, monkeypatch):
    """把 AGENT_DATA_DIR 指向临时目录，隔离 finalize_tool_output 的落盘副作用。"""
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    from app.config import get_settings

    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_process_tool_call_canonicalizes_malformed_arguments():
    """Malformed arguments (trailing comma) must be rewritten to valid JSON
    on tc['function']['arguments'] so later LLM rounds get clean history."""
    tc = {
        "id": "call_1",
        "function": {
            "name": "read_file",
            # Trailing comma — Qwen streaming produces this sometimes
            "arguments": '{"path": "foo.md",}',
        },
    }
    api_messages: list = []

    async def fake_execute_tool(name, args, **kwargs):
        assert name == "read_file"
        assert args == {"path": "foo.md"}
        return "file contents here"

    with patch("app.services.llm.caller.execute_tool", side_effect=fake_execute_tool):
        await _process_tool_call(
            tc=tc,
            api_messages=api_messages,
            agent_id="agent-1",
            user_id="user-1",
            session_id="sess-1",
            supports_vision=False,
            on_tool_call=None,
            full_reasoning_content="",
            allowed_tool_names={"read_file", "write_file"},
        )

    # CRITICAL: arguments on the tc object must now be valid JSON
    repaired = tc["function"]["arguments"]
    parsed = json.loads(repaired)
    assert parsed == {"path": "foo.md"}
    # And it must not have a trailing comma
    assert ",}" not in repaired.replace(" ", "")


@pytest.mark.asyncio
async def test_process_tool_call_materializes_oversized_result(tmp_workspace):
    """超过 per-tool 阈值的 tool 输出必须被 materialize，三方一致：
      1) on_tool_call 回调收到的 data["result"] 是 <persisted-output> 块
      2) api_messages 里 role="tool" 的 content 是同一个 <persisted-output> 块
      3) workspace 下 .tool_results/{session_id}/ 存有完整原文
    这样 DB 视图、LLM 历史回放视图、前端显示值全部等价。
    """
    agent_id = str(uuid.uuid4())
    session_id = "sess-oversized"
    tool_call_id = "call_huge"
    # grep 的 per-tool budget 是 20_000；用 60k 字符触发 materialize
    huge_result = "A" * 60_000

    tc = {
        "id": tool_call_id,
        "function": {"name": "grep", "arguments": '{"pattern": "foo"}'},
    }
    api_messages: list = []
    received: list[dict] = []

    async def fake_execute_tool(name, args, **kwargs):
        return huge_result

    async def fake_on_tool_call(data):
        # 只关心 done 事件（running 事件发生在 execute_tool 之前，没有 result 字段）
        if data.get("status") == "done":
            received.append(data)

    with patch("app.services.llm.caller.execute_tool", side_effect=fake_execute_tool):
        await _process_tool_call(
            tc=tc,
            api_messages=api_messages,
            agent_id=agent_id,
            user_id="user-1",
            session_id=session_id,
            supports_vision=False,
            on_tool_call=fake_on_tool_call,
            full_reasoning_content="",
            allowed_tool_names={"grep", "read_file", "write_file"},
        )

    # ------ 断言 1：on_tool_call 收到的 result 是 persisted-output 块 ------
    assert len(received) == 1, "expected exactly one done event"
    callback_result = received[0]["result"]
    assert isinstance(callback_result, str)
    assert tos.PERSISTED_OPEN in callback_result
    assert tos.PERSISTED_CLOSE in callback_result
    # 不是原始 60k 裸文本
    assert callback_result != huge_result
    assert len(callback_result) < len(huge_result)

    # ------ 断言 2：api_messages 里的 tool content 与回调完全一致 ------
    tool_msg = api_messages[-1]
    assert tool_msg.role == "tool"
    assert tool_msg.tool_call_id == tool_call_id
    assert tool_msg.content == callback_result  # 三方一致的核心断言

    # ------ 断言 3：workspace 下完整原文落盘 ------
    persisted_dir = tmp_workspace / agent_id / ".tool_results" / session_id
    assert persisted_dir.exists(), f"expected {persisted_dir} to exist"
    files = list(persisted_dir.iterdir())
    assert len(files) == 1
    persisted_file = files[0]
    # grep 原文是纯文本非 JSON → .txt
    assert persisted_file.name == f"grep_{tool_call_id}.txt"
    assert persisted_file.read_text() == huge_result
    # 文件路径引用也出现在 llm_view 里，LLM 可以凭此调 read_file 取全文
    assert persisted_file.name in callback_result


@pytest.mark.asyncio
async def test_process_tool_call_clean_arguments_pass_through_unchanged_semantic():
    """Clean JSON must still work exactly as before (backwards compat)."""
    tc = {
        "id": "call_1",
        "function": {
            "name": "read_file",
            "arguments": '{"path": "foo.md"}',
        },
    }
    api_messages: list = []

    async def fake_execute_tool(name, args, **kwargs):
        assert args == {"path": "foo.md"}
        return "ok"

    with patch("app.services.llm.caller.execute_tool", side_effect=fake_execute_tool):
        await _process_tool_call(
            tc=tc, api_messages=api_messages,
            agent_id="agent-1", user_id="user-1", session_id="sess-1",
            supports_vision=False, on_tool_call=None, full_reasoning_content="",
            allowed_tool_names={"read_file"},
        )

    # Semantic equivalence (key order / spacing may differ)
    assert json.loads(tc["function"]["arguments"]) == {"path": "foo.md"}


def test_canonicalize_tc_arguments_helper_rewrites_tc_inplace():
    """Unit test the helper directly — exercised by both _process_tool_call
    and call_agent_llm_with_tools._try_model."""
    from app.services.llm.caller import _canonicalize_tc_arguments
    tc = {
        "id": "call_1",
        "function": {"name": "read_file", "arguments": '{"path": "foo.md",}'},
    }
    args = _canonicalize_tc_arguments(tc, session_id="sess-x")
    assert args == {"path": "foo.md"}
    # In-place mutation: tc now carries canonical JSON
    import json
    parsed = json.loads(tc["function"]["arguments"])
    assert parsed == {"path": "foo.md"}
