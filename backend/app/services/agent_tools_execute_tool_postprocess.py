from __future__ import annotations

import json
import uuid

from loguru import logger

from app.database import async_session


async def execute_tool_postprocess(
    tool_name: str,
    arguments: dict,
    agent_id: uuid.UUID,
    user_id: uuid.UUID | None,
    session_id: str,
    result: str,
) -> str:
    # Log tool call activity (skip noisy read operations). Keep the result
    # shape intact for diagnostics and mask only explicit credential values.
    if tool_name not in ("list_files", "read_file", "read_document"):
        from app.services.activity_logger import log_activity
        from app.utils.sanitize import sanitize_sensitive_values, sanitize_tool_args

        _log_args = sanitize_tool_args(arguments) or {}
        _log_result = result
        if tool_name == "list_installed_mcp_servers":
            try:
                _log_result = json.dumps(
                    sanitize_sensitive_values(json.loads(result)),
                    ensure_ascii=False,
                    default=str,
                )
            except Exception:
                pass
        _summary = f"Called tool {tool_name}: {_log_result[:80]}"
        _detail = {
            "tool": tool_name,
            "args": {k: str(v)[:100] for k, v in _log_args.items()},
            "result": _log_result[:300],
        }
        await log_activity(
            agent_id,
            "tool_call",
            _summary,
            detail=_detail,
        )
    # Save error message to current session if a messaging tool fails, so the user is notified
    if (
        session_id
        and tool_name
        in (
            "send_channel_message",
            "send_group_session_message",
            "send_session_message",
            "send_feishu_message",
            "send_platform_message",
            "send_message_to_agent",
        )
        and isinstance(result, str)
        and result.startswith("❌")
    ):
        try:
            async with async_session() as _err_db:
                from app.models.audit import ChatMessage as _CM

                _err_db.add(
                    _CM(
                        agent_id=agent_id,
                        user_id=user_id,
                        role="assistant",
                        content=f"⚠️ [系统提示] 数字员工工具调用失败！\n工具名: `{tool_name}`\n参数: `{json.dumps(arguments, ensure_ascii=False)}`\n错误信息: {result}",
                        conversation_id=session_id,
                    )
                )
                await _err_db.commit()
        except Exception as _e:
            logger.warning(f"Failed to save tool error message to session: {_e}")

    return result
