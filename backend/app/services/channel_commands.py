"""Channel command handler for external channels (DingTalk, Feishu, etc.)

Supports slash commands like /new to reset session context.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import user_can_manage_agent_id
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.services.channel_dispatch import cancel_running_turn, has_running_turn
from app.services.im_thinking_output import (
    THINKING_OFF,
    THINKING_ON,
)

COMMANDS = {
    "/new",
    "/reset",
    "/help",
    "/stop",
    "/status",
    "/thinking",
    "/think",
    "/scene",
    "/model",
}


def _parse_command(text: str) -> tuple[str, str | None]:
    raw_parts = text.strip().split(maxsplit=1)
    if raw_parts and raw_parts[0].lower() == "/model":
        return "/model", raw_parts[1].strip() if len(raw_parts) == 2 else None
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
    if command in {"/new", "/reset", "/help", "/stop", "/status"}:
        return arg is None
    if command in {"/thinking", "/think"}:
        return arg in {"on", "off", "status"}
    # Invalid scene/model syntax must still stay on the control plane so it receives
    # an explicit usage error instead of being sent to the LLM as dialogue.
    return command in {"/scene", "/model"}


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
        "/model list：查看可用模型；/model <模型名>：切换当前会话模型\n"
        "/model use <模型名>：切换名称为 list、status、default 的模型\n"
        "/model status：查看当前模型；/model default：恢复默认模型\n"
        "/stop：停止当前这轮正在执行的工作\n"
        "/status：查看当前数字员工和会话状态\n"
        "/help：查看帮助"
    )


def _thinking_status_label(enabled: bool) -> str:
    return "开启" if enabled else "关闭"


async def _count_session_messages(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
) -> int:
    from app.models.audit import ChatMessage

    result = await db.execute(
        select(func.count(ChatMessage.id)).where(
            ChatMessage.agent_id == agent_id,
            ChatMessage.conversation_id == str(session_id),
        )
    )
    return int(result.scalar_one() or 0)


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

    if parsed_cmd == "/status":
        from app.services.chat_model_selection import (
            MODEL_OVERRIDE_OK,
            MODEL_SESSION_CONFIG_KEY,
            resolve_runtime_models,
        )
        from app.services.scene_service import SCENE_SESSION_CONFIG_KEY
        from app.services.session_token_usage import load_session_token_usage
        from app.services.token_tracker import TokenUsage

        agent = await _load_agent(db, agent_id=agent_id)
        if agent is None:
            return {"action": "status_failed", "message": "❌ 无法读取数字员工状态。"}

        session = await _load_channel_session(
            db,
            agent_id=agent_id,
            external_conv_id=external_conv_id,
            source_channel=source_channel,
        )
        config = dict((session.im_config if session else None) or {})
        override_model_id = str(config.get(MODEL_SESSION_CONFIG_KEY) or "")
        resolved = await resolve_runtime_models(
            db,
            agent=agent,
            override_model_id=override_model_id or None,
        )

        if resolved.primary_model is None:
            model_status = "未配置"
        elif override_model_id and resolved.override_status == MODEL_OVERRIDE_OK:
            model_status = f"{resolved.primary_model.model}（会话临时模型）"
        elif override_model_id:
            model_status = f"{resolved.primary_model.model}（会话模型已失效，已回退默认）"
        else:
            model_status = f"{resolved.primary_model.model}（默认模型）"

        turn_running = await has_running_turn(_lock_key(source_channel, external_conv_id))
        runtime_labels = {
            "creating": "创建中",
            "running": "运行中",
            "idle": "空闲",
            "stopped": "已停止",
            "error": "异常",
        }
        runtime_status = "处理中" if turn_running else runtime_labels.get(
            str(agent.status or ""),
            str(agent.status or "未知"),
        )

        if session is None:
            session_status = "尚未建立"
            conversation_type = "群聊" if is_group else "单聊"
            message_count = 0
            context_status = "正常"
            session_usage = TokenUsage()
            tracked_turns = 0
        else:
            conversation_type = "群聊" if session.is_group else "单聊"
            message_count = await _count_session_messages(
                db,
                agent_id=agent_id,
                session_id=session.id,
            )
            session_status = f"{conversation_type} · {message_count:,} 条消息"
            context_status = "已终止，请使用 /new" if session.context_terminated_reason else "正常"
            session_usage, tracked_turns = await load_session_token_usage(
                db,
                agent_id=agent_id,
                session_id=session.id,
            )

        cache_denominator = session_usage.cache_eligible_input_tokens
        if cache_denominator > 0:
            cache_hit_rate = min(
                100.0,
                session_usage.cache_read_tokens / cache_denominator * 100,
            )
            cache_status = (
                f"{cache_hit_rate:.1f}%（命中 {session_usage.cache_read_tokens:,} / "
                f"可缓存输入 {cache_denominator:,}）"
            )
        else:
            cache_status = "暂无可用统计"
        estimated_suffix = (
            f"，其中估算 {session_usage.estimated_tokens:,}"
            if session_usage.estimated_tokens > 0
            else ""
        )

        scene_key = str(config.get(SCENE_SESSION_CONFIG_KEY) or "")
        scene_status = scene_key or "未激活"
        return {
            "action": "status",
            "message": (
                f"数字员工：{agent.name}\n"
                f"运行状态：{runtime_status}\n"
                f"模型：{model_status}\n"
                f"场景：{scene_status}\n"
                f"会话：{session_status}\n"
                f"通道：{source_channel} · {conversation_type}\n"
                f"上下文：{context_status}\n"
                f"Session Token（已记录 {tracked_turns:,} 轮）："
                f"输入 {session_usage.input_tokens:,} / "
                f"输出 {session_usage.output_tokens:,} / "
                f"总计 {session_usage.total_tokens:,}{estimated_suffix}\n"
                f"缓存命中率：{cache_status}"
            ),
        }

    if parsed_cmd == "/model":
        from app.services.channel_session import find_or_create_channel_session
        from app.services.chat_model_selection import (
            MODEL_OVERRIDE_OK,
            MODEL_SESSION_CONFIG_KEY,
            MODEL_STATUS_AMBIGUOUS,
            MODEL_STATUS_DISABLED,
            MODEL_STATUS_NOT_FOUND,
            MODEL_STATUS_OK,
            list_enabled_tenant_models,
            resolve_runtime_models,
            resolve_tenant_model_by_name,
        )

        agent = await _load_agent(db, agent_id=agent_id)
        if agent is None or agent.tenant_id is None:
            return {"action": "model_failed", "message": "❌ 无法读取数字员工的模型配置。"}

        normalized_arg = str(arg or "status").strip()
        explicit_use = normalized_arg.casefold().startswith("use ")
        if explicit_use:
            normalized_arg = normalized_arg[4:].strip()
            if not normalized_arg:
                return {
                    "action": "model_usage",
                    "message": "❌ 用法：/model use <模型名>。",
                }
        control_arg = "" if explicit_use else normalized_arg.casefold()
        if control_arg == "list":
            models = await list_enabled_tenant_models(db, agent.tenant_id)
            if not models:
                return {"action": "model_list", "message": "当前企业没有已启用的模型。"}
            model_names = "\n".join(f"- {model.model}" for model in models)
            return {
                "action": "model_list",
                "message": (
                    f"可用模型：\n{model_names}\n\n切换方式：/model <模型名>"
                    "；若模型名为 list、status 或 default，请使用 /model use <模型名>。"
                ),
            }

        session = await _load_channel_session(
            db,
            agent_id=agent_id,
            external_conv_id=external_conv_id,
            source_channel=source_channel,
            for_update=control_arg != "status",
        )
        current_model_id = str((session.im_config or {}).get(MODEL_SESSION_CONFIG_KEY) or "") if session else ""

        if control_arg == "status":
            resolved = await resolve_runtime_models(
                db,
                agent=agent,
                override_model_id=current_model_id or None,
            )
            if current_model_id and resolved.override_status != MODEL_OVERRIDE_OK:
                return {
                    "action": "model_status_unavailable",
                    "message": "⚠️ 当前会话选择的模型已不可用，请发送 /model list 重新选择，或 /model default 恢复默认模型。",
                }
            if resolved.primary_model is None:
                return {"action": "model_status", "message": "当前数字员工未配置可用模型。"}
            source = "会话临时模型" if current_model_id else "数字员工默认模型"
            return {
                "action": "model_status",
                "message": f"当前模型：{resolved.primary_model.model}（{source}）。",
            }

        if control_arg == "default":
            if session is not None and current_model_id:
                config = dict(session.im_config or {})
                config.pop(MODEL_SESSION_CONFIG_KEY, None)
                session.im_config = config
                await db.flush()
            resolved = await resolve_runtime_models(db, agent=agent)
            if resolved.primary_model is None:
                return {
                    "action": "model_default",
                    "message": "✅ 已清除会话临时模型；数字员工当前没有可用的默认模型。",
                }
            return {
                "action": "model_default",
                "message": f"✅ 已恢复数字员工默认模型「{resolved.primary_model.model}」，从下一条消息起生效。",
            }

        matched = await resolve_tenant_model_by_name(
            db,
            tenant_id=agent.tenant_id,
            model_name=normalized_arg,
        )
        if matched.status == MODEL_STATUS_NOT_FOUND:
            return {
                "action": "model_not_found",
                "message": f"❌ 未找到模型「{normalized_arg}」，请发送 /model list 查看可用模型。",
            }
        if matched.status == MODEL_STATUS_DISABLED:
            return {
                "action": "model_disabled",
                "message": f"❌ 模型「{matched.model.model}」当前已停用，无法切换。",
            }
        if matched.status == MODEL_STATUS_AMBIGUOUS:
            return {
                "action": "model_ambiguous",
                "message": f"❌ 存在多个名为「{normalized_arg}」的模型，请管理员调整模型名称。",
            }
        if matched.status != MODEL_STATUS_OK or matched.model is None:
            return {"action": "model_failed", "message": "❌ 模型切换失败，请稍后重试。"}

        if session is None:
            await find_or_create_channel_session(
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
            return {"action": "model_failed", "message": "❌ 模型切换失败，请稍后重试。"}

        config = dict(session.im_config or {})
        config[MODEL_SESSION_CONFIG_KEY] = str(matched.model.id)
        session.im_config = config
        await db.flush()
        return {
            "action": "model_switched",
            "message": f"✅ 已切换到模型「{matched.model.model}」，从下一条消息起生效。",
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
                    "message": (f"当前场景：{manifest['name']}（{manifest['scene_key']}，v{manifest['revision']}）。"),
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

            cleared_scene_key = str((getattr(old_session, "im_config", None) or {}).get(SCENE_SESSION_CONFIG_KEY) or "")
            # Rename old external_conv_id so find_or_create will make a new one
            now = datetime.now(UTC)
            old_session.external_conv_id = f"{external_conv_id}__archived_{now.strftime('%Y%m%d_%H%M%S')}"
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
