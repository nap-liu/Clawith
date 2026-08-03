"""Channel command handler for external channels (DingTalk, Feishu, etc.)

Supports slash commands like /new to reset session context.
"""

import uuid
from datetime import UTC, datetime

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

COMMANDS = {"/new", "/reset", "/help", "/stop", "/thinking", "/think", "/scene"}


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
    # Invalid scene syntax must still stay on the control plane so it receives
    # an explicit usage error instead of being sent to the LLM as dialogue.
    return command == "/scene"


def _lock_key(source_channel: str, external_conv_id: str) -> str:
    return f"{source_channel}:{external_conv_id}"


async def _load_channel_session(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    external_conv_id: str,
    source_channel: str,
    for_update: bool = False,
) -> ChatSession | None:
    query = select(ChatSession).where(
        ChatSession.agent_id == agent_id,
        ChatSession.external_conv_id == external_conv_id,
        ChatSession.source_channel == source_channel,
    )
    if for_update:
        query = query.with_for_update()
    result = await db.execute(query)
    return result.scalar_one_or_none()


async def _load_agent(db: AsyncSession, *, agent_id: uuid.UUID) -> Agent | None:
    result = await db.execute(select(Agent).where(Agent.id == agent_id))
    return result.scalar_one_or_none()


def _help_message() -> str:
    return (
        "可用指令：\n"
        "/new 或 /reset：开启新对话，清除当前上下文\n"
        "/thinking on：开启数字员工的 IM 思考输出\n"
        "/thinking off：关闭数字员工的 IM 思考输出\n"
        "/thinking status：查看数字员工的 IM 思考输出状态（/think 可作为简写）\n"
        "/scene <场景标识>：从下一条消息起激活指定场景\n"
        "/scene status：查看当前场景；/scene off：退出当前场景\n"
        "/stop：停止当前这轮正在执行的工作\n"
        "/help：查看帮助"
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
    is_group: bool = False,
    group_name: str | None = None,
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

    if parsed_cmd == "/scene":
        from app.schemas.scene import validate_scene_key
        from app.services.channel_session import find_or_create_channel_session
        from app.services.scene_service import (
            SCENE_SESSION_CONFIG_KEY,
            SCENE_STATUS_CAPABILITY_DISABLED,
            SCENE_STATUS_DISABLED,
            SCENE_STATUS_NOT_FOUND,
            SCENE_STATUS_OK,
            SCENE_STATUS_UNPUBLISHED,
            resolve_scene_for_activation,
        )

        usage = "用法：/scene <场景标识>；查看当前场景：/scene status；退出场景：/scene off。"
        if arg in {None, "__invalid__"}:
            return {"action": "scene_invalid", "message": f"❌ {usage}"}

        session = await _load_channel_session(
            db,
            agent_id=agent_id,
            external_conv_id=external_conv_id,
            source_channel=source_channel,
            # Serialize config mutations with inbound-message ingestion so a
            # turn snapshots either the old scene or the new scene, never a
            # partially updated session preference.
            for_update=arg != "status",
        )

        if arg == "status":
            active_key = str((session.im_config or {}).get(SCENE_SESSION_CONFIG_KEY) or "") if session else ""
            if not active_key:
                return {"action": "scene_status", "message": "当前会话未激活场景。"}
            resolved = await resolve_scene_for_activation(db, agent_id, active_key)
            if resolved.status == SCENE_STATUS_OK and resolved.manifest:
                manifest = resolved.manifest
                return {
                    "action": "scene_status",
                    "message": (
                        f"当前场景：{manifest['name']}"
                        f"（{manifest['scene_key']}，v{manifest['revision']}）。"
                    ),
                }
            return {
                "action": "scene_status_unavailable",
                "message": f"⚠️ 当前记录的场景 {active_key} 已不可用，请切换场景或发送 /scene off。",
            }

        if arg == "off":
            active_key = str((session.im_config or {}).get(SCENE_SESSION_CONFIG_KEY) or "") if session else ""
            if not active_key:
                return {"action": "scene_off", "message": "当前会话未激活场景。"}
            config = dict(session.im_config or {})
            config.pop(SCENE_SESSION_CONFIG_KEY, None)
            session.im_config = config
            await db.flush()
            return {
                "action": "scene_off",
                "message": f"✅ 已退出场景 {active_key}，从下一条消息起恢复默认对话模式。",
            }

        try:
            scene_key = validate_scene_key(arg)
        except ValueError:
            return {
                "action": "scene_invalid",
                "message": f"❌ 场景标识 {arg} 无效。场景标识需以字母开头，且只能包含小写字母、数字、_ 或 -。",
            }

        resolved = await resolve_scene_for_activation(db, agent_id, scene_key)
        if resolved.status == SCENE_STATUS_CAPABILITY_DISABLED:
            return {"action": "scene_capability_disabled", "message": "❌ 该数字员工未启用场景能力。"}
        if resolved.status == SCENE_STATUS_NOT_FOUND:
            return {"action": "scene_not_found", "message": f"❌ 未找到场景 {scene_key}。"}
        if resolved.status == SCENE_STATUS_UNPUBLISHED:
            return {"action": "scene_unpublished", "message": f"❌ 场景 {scene_key} 尚未发布，无法激活。"}
        if resolved.status == SCENE_STATUS_DISABLED:
            return {"action": "scene_disabled", "message": f"❌ 场景 {scene_key} 已停用，无法激活。"}
        if resolved.status != SCENE_STATUS_OK or not resolved.manifest:
            return {"action": "scene_failed", "message": "❌ 场景激活失败，请稍后重试。"}

        if session is None:
            session = await find_or_create_channel_session(
                db=db,
                agent_id=agent_id,
                user_id=user_id,
                external_conv_id=external_conv_id,
                source_channel=source_channel,
                first_message_title="New Session",
                is_group=is_group,
                group_name=group_name,
                allow_unresolved_user=True,
            )
            session = await _load_channel_session(
                db,
                agent_id=agent_id,
                external_conv_id=external_conv_id,
                source_channel=source_channel,
                for_update=True,
            )
        if session is None:
            return {"action": "scene_failed", "message": "❌ 场景激活失败，请稍后重试。"}

        config = dict(session.im_config or {})
        config[SCENE_SESSION_CONFIG_KEY] = scene_key
        session.im_config = config
        await db.flush()
        manifest = resolved.manifest
        return {
            "action": "scene_activated",
            "message": (
                f"✅ 已激活场景「{manifest['name']}」"
                f"（{manifest['scene_key']}，v{manifest['revision']}），从下一条消息起生效。"
            ),
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

        cleared_scene_key = ""
        if old_session:
            from app.services.scene_service import SCENE_SESSION_CONFIG_KEY

            cleared_scene_key = str(
                (getattr(old_session, "im_config", None) or {}).get(
                    SCENE_SESSION_CONFIG_KEY
                )
                or ""
            )
            # Rename old external_conv_id so find_or_create will make a new one
            now = datetime.now(UTC)
            old_session.external_conv_id = (
                f"{external_conv_id}__archived_{now.strftime('%Y%m%d_%H%M%S')}"
            )
            await db.flush()

        # Defer session creation to the user's next message so its title
        # auto-names from that message (via find_or_create_channel_session)
        # instead of being locked to a hard-coded placeholder.
        return {
            "action": "new_session",
            "message": (
                "当前对话已重置。"
                + (f"已退出场景 {cleared_scene_key}。" if cleared_scene_key else "")
                + "你的下一条消息将开启新对话。"
                + "请重新发送刚才的需求；如有附件，请一并重新发送。"
            ),
        }

    return {"action": "unknown", "message": f"未知命令: {cmd}"}
