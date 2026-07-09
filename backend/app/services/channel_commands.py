"""Channel command handler for external channels (DingTalk, Feishu, etc.)

Supports slash commands like /new to reset session context.
"""

import uuid
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import user_can_manage_agent_id
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.services.channel_dispatch import cancel_running_turn
from app.services.im_thinking_output import (
    THINKING_OFF,
    THINKING_ON,
)


COMMANDS = {"/new", "/reset", "/help", "/stop", "/thinking", "/think"}


def _parse_command(text: str) -> tuple[str, str | None]:
    parts = text.strip().lower().split()
    if not parts:
        return "", None
    command = parts[0]
    arg = parts[1] if len(parts) == 2 else None
    if len(parts) > 2:
        return command, "__invalid__"
    return command, arg


def is_channel_command(text: str) -> bool:
    """Check if the message is a recognized channel command."""
    command, arg = _parse_command(text)
    if command in {"/new", "/reset", "/help", "/stop"}:
        return arg is None
    if command in {"/thinking", "/think"}:
        return arg in {"on", "off", "status"}
    return False


def _lock_key(source_channel: str, external_conv_id: str) -> str:
    return f"{source_channel}:{external_conv_id}"


async def _load_channel_session(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    external_conv_id: str,
    source_channel: str,
) -> ChatSession | None:
    result = await db.execute(
        select(ChatSession).where(
            ChatSession.agent_id == agent_id,
            ChatSession.external_conv_id == external_conv_id,
            ChatSession.source_channel == source_channel,
        )
    )
    return result.scalar_one_or_none()


async def _load_agent(db: AsyncSession, *, agent_id: uuid.UUID) -> Agent | None:
    result = await db.execute(select(Agent).where(Agent.id == agent_id))
    return result.scalar_one_or_none()


def _help_message() -> str:
    return "\n".join(
        [
            "可用指令：",
            "/new 或 /reset：开启新对话，清除当前上下文",
            "/thinking on：开启数字员工的 IM 思考输出",
            "/thinking off：关闭数字员工的 IM 思考输出",
            "/thinking status：查看数字员工的 IM 思考输出状态（/think 可作为简写）",
            "/stop：停止当前这轮正在执行的工作",
            "/help：查看帮助",
        ]
    )


def _thinking_status_label(enabled: bool) -> str:
    return "开启" if enabled else "关闭"


async def handle_channel_command(
    db: AsyncSession,
    command: str,
    agent_id: uuid.UUID,
    user_id: uuid.UUID | None,
    external_conv_id: str,
    source_channel: str,
) -> dict:
    """Handle a channel command and return response info.

    Returns:
        {"action": "new_session", "message": "..."}
    """
    cmd = command.strip().lower()
    parsed_cmd, arg = _parse_command(command)

    if parsed_cmd == "/help":
        return {"action": "help", "message": _help_message()}

    if parsed_cmd == "/stop":
        cancelled = await cancel_running_turn(_lock_key(source_channel, external_conv_id))
        return {
            "action": "stop_turn",
            "message": "已请求停止当前工作。" if cancelled else "当前没有正在执行的工作。",
        }

    if parsed_cmd in {"/thinking", "/think"}:
        agent = await _load_agent(db, agent_id=agent_id)
        if agent is None:
            return {
                "action": "thinking_output_missing_agent",
                "message": "数字员工不存在，无法切换思考输出。",
            }
        if arg == "status":
            current_value = bool(getattr(agent, "im_thinking_output_enabled", False))
            return {
                "action": "thinking_output_status",
                "message": f"数字员工 IM 思考输出当前为：{_thinking_status_label(current_value)}。",
            }
        if not await user_can_manage_agent_id(db, user_id, agent):
            return {
                "action": "thinking_output_denied",
                "message": "没有权限切换该数字员工的 IM 思考输出。",
            }
        if arg in {THINKING_ON, THINKING_OFF}:
            agent.im_thinking_output_enabled = arg == THINKING_ON
            await db.flush()
            return {
                "action": "thinking_output",
                "message": f"已{_thinking_status_label(agent.im_thinking_output_enabled)}数字员工 IM 思考输出。",
            }

    if cmd in ("/new", "/reset"):
        # Find current session. Scope by source_channel as well so we never
        # accidentally archive a session from a different channel that happens
        # to share the same external_conv_id (defensive against future changes
        # to the per-channel ID prefix scheme).
        old_session = await _load_channel_session(
            db,
            agent_id=agent_id,
            external_conv_id=external_conv_id,
            source_channel=source_channel,
        )

        if old_session:
            # Rename old external_conv_id so find_or_create will make a new one
            now = datetime.now(timezone.utc)
            old_session.external_conv_id = (
                f"{external_conv_id}__archived_{now.strftime('%Y%m%d_%H%M%S')}"
            )
            await db.flush()

        # Defer session creation to the user's next message so its title
        # auto-names from that message (via find_or_create_channel_session)
        # instead of being locked to a hard-coded placeholder.
        return {
            "action": "new_session",
            "message": "已开启新对话，之前的上下文已清除。",
        }

    return {"action": "unknown", "message": f"未知命令: {cmd}"}
