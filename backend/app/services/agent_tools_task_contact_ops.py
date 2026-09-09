from __future__ import annotations

import json
from pathlib import Path
import uuid

from loguru import logger
from sqlalchemy import select

from app.database import async_session as _database_async_session
from app.models.task import Task
from app.services.recipient_resolver import RecipientResolutionError
from app.services.task_time_projection import serialize_tasks_for_agent
from app.services.timezone_utils import (
    format_datetime_for_agent,
    get_agent_timezone_in_session,
    parse_datetime_for_agent,
)


class _AsyncSessionProxy:
    def __call__(self):
        from app.services import agent_tools

        bound = getattr(agent_tools, "async_session", None)
        if bound is self or bound is None:
            bound = _database_async_session
        return bound()


async_session = _AsyncSessionProxy()


def _render_tool_time(value: object, timezone_name: str) -> str:
    """Project a canonical service timestamp without changing its source value."""
    try:
        parsed = parse_datetime_for_agent(value, "UTC")
    except (TypeError, ValueError):
        return str(value)
    return format_datetime_for_agent(parsed, timezone_name) or str(value)


async def _sync_tasks_to_file(agent_id: uuid.UUID, ws: Path):
    """Sync tasks from DB to legacy tasks.json, if the file already exists."""
    tasks_path = ws / "tasks.json"
    if not tasks_path.exists():
        return

    try:
        async with async_session() as db:
            result = await db.execute(select(Task).where(Task.agent_id == agent_id).order_by(Task.created_at.desc()))
            tasks = result.scalars().all()

        task_list = await serialize_tasks_for_agent(agent_id, tasks)

        tasks_path.write_text(
            json.dumps(task_list, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.error(f"[AgentTools] Failed to sync tasks: {e}")


async def _manage_tasks(
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    ws: Path,
    args: dict,
) -> str:
    """Create / update / delete tasks in DB and sync to workspace."""
    from app.models.task import TaskLog
    from datetime import datetime, timezone

    action = args["action"]
    title = args["title"]

    async with async_session() as db:
        if action == "create":
            task_type = args.get("task_type", "todo")
            resolved_target = None
            target_user_id = None
            target_agent_id = None
            if task_type == "supervision":
                if args.get("supervision_target_name"):
                    return (
                        "❌ supervision_target_name is display-only; provide exactly "
                        "one canonical supervision_target_user_id or "
                        "supervision_target_agent_id"
                    )
                try:
                    target_user_id = (
                        uuid.UUID(str(args["supervision_target_user_id"]))
                        if args.get("supervision_target_user_id")
                        else None
                    )
                    target_agent_id = (
                        uuid.UUID(str(args["supervision_target_agent_id"]))
                        if args.get("supervision_target_agent_id")
                        else None
                    )
                except (TypeError, ValueError):
                    return "❌ supervision target IDs must be complete platform UUIDs"
                from app.services.supervision_targets import resolve_supervision_target

                try:
                    resolved_target = await resolve_supervision_target(
                        db,
                        agent_id,
                        target_user_id=target_user_id,
                        target_agent_id=target_agent_id,
                        channel=args.get("supervision_channel"),
                    )
                except (RecipientResolutionError, ValueError) as exc:
                    return exc.as_json() if isinstance(exc, RecipientResolutionError) else f"❌ {exc}"
            task = Task(
                agent_id=agent_id,
                title=title,
                description=args.get("description"),
                type=task_type,
                priority=args.get("priority", "medium"),
                created_by=user_id,
                execution_user_id=user_id,
                status="pending",
                supervision_target_user_id=target_user_id,
                supervision_target_agent_id=target_agent_id,
                supervision_target_name=(resolved_target.display_name if resolved_target else None),
                supervision_channel=(resolved_target.channel if resolved_target else None),
                remind_schedule=args.get("remind_schedule"),
            )
            if task_type == "todo":
                from app.services.agent_execution.bridge import dispatch_background
                from app.services.task_executor import prepare_created_task_turn

                anchor = await prepare_created_task_turn(db, task)
                await db.commit()
                if anchor is not None:
                    anchor_id = anchor.id
                    await dispatch_background("app.services.background_turns:run_background_turn", anchor_id)
                await _sync_tasks_to_file(agent_id, ws)
                if anchor is None:
                    from app.services.llm.failure_outcome import render_message

                    return f"✅ Task created: {title} — {render_message('background.unavailable')}"
                return f"✅ Task created: {title} — auto-execution started"
            else:
                # Supervision task — reminder engine will pick it up
                db.add(task)
                await db.commit()
                target = resolved_target.display_name if resolved_target else "unknown"
                schedule = args.get("remind_schedule", "not set")
                await _sync_tasks_to_file(agent_id, ws)
                return f"✅ Supervision task created: '{title}' — will remind {target} on schedule ({schedule})"

        elif action == "update_status":
            result = await db.execute(select(Task).where(Task.agent_id == agent_id, Task.title.ilike(f"%{title}%")))
            task = result.scalars().first()
            if not task:
                return f"No task found matching '{title}'"
            # Persisted background work always follows the last conversation
            # user who changed it. ``created_by`` remains immutable audit data.
            from app.services.execution_identity import align_background_execution_user

            await align_background_execution_user(
                db,
                agent_id=agent_id,
                resource_type="task",
                resource_id=task.id,
                execution_user_id=user_id,
            )
            old = task.status
            task.status = args["status"]
            if args["status"] == "done":
                task.completed_at = datetime.now(timezone.utc)
            await db.commit()
            await _sync_tasks_to_file(agent_id, ws)
            return f"✅ Updated '{task.title}' from {old} to {args['status']}"

        elif action == "delete":
            from sqlalchemy import delete as sa_delete

            result = await db.execute(select(Task).where(Task.agent_id == agent_id, Task.title.ilike(f"%{title}%")))
            task = result.scalars().first()
            if not task:
                return f"No task found matching '{title}'"
            task_title = task.title
            await db.execute(sa_delete(TaskLog).where(TaskLog.task_id == task.id))
            await db.delete(task)
            await db.commit()
            await _sync_tasks_to_file(agent_id, ws)
            return f"✅ Task deleted: {task_title}"

        return f"Unknown action: {action}"


def _format_contact_search_results(rows: list[dict]) -> str:
    if not rows:
        return "No contacts found."

    lines = [f"Found {len(rows)} contact(s):"]
    for item in rows:
        if item.get("agent_id"):
            role = f" — {item.get('role_description')}" if item.get("role_description") else ""
            lines.append(
                f"- agent_id={item['agent_id']} | display_name={item.get('name')}{role} | "
                f"relationship={item.get('relationship_status')}"
            )
        else:
            title = f" — {item.get('title')}" if item.get("title") else ""
            dept = f" | dept={item.get('department_path')}" if item.get("department_path") else ""
            phone = f" | phone={item.get('phone')}" if item.get("phone") else ""
            lines.append(
                f"- user_id={item['user_id']} | display_name={item.get('name')}{title} | "
                f"channel={item.get('channel')}{dept}{phone} | relationship={item.get('relationship_status')}"
            )
    return "\n".join(lines)


async def _search_contacts_tool(agent_id: uuid.UUID, args: dict, user_id: uuid.UUID | None) -> str:
    query = (args.get("query") or args.get("q") or "").strip()
    if not query:
        return "❌ Please provide query"

    contact_type = (args.get("type") or args.get("contact_type") or "all").strip().lower()
    limit = args.get("limit", 20)

    from app.services.contact_relationships import search_contacts_for_agent

    async with async_session() as db:
        rows = await search_contacts_for_agent(
            db,
            agent_id,
            query=query,
            contact_type=contact_type,
            current_user_id=user_id,
            limit=limit,
        )
        normalized: list[dict] = []
        seen_users: set[str] = set()
        for item in rows:
            public = dict(item)
            raw_id = public.pop("id", None)
            item_type = public.pop("type", None)
            if item_type == "agent":
                public["agent_id"] = str(public.get("agent_id") or raw_id)
                normalized.append(public)
                continue
            canonical_user_id = public.get("user_id") or raw_id
            if not canonical_user_id:
                # Workstream 3 must provision an external-only User before this
                # person can enter the public Agent identity contract.
                continue
            canonical = str(canonical_user_id)
            if canonical in seen_users:
                continue
            seen_users.add(canonical)
            public["user_id"] = canonical
            normalized.append(public)
    return _format_contact_search_results(normalized)


async def _add_contact_tool(agent_id: uuid.UUID, args: dict, user_id: uuid.UUID | None) -> str:
    human_id = str(args.get("user_id") or "").strip()
    digital_id = str(args.get("agent_id") or "").strip()
    if bool(human_id) == bool(digital_id):
        return "❌ Provide exactly one of user_id or agent_id from search_contacts"

    from app.services.contact_relationships import (
        add_agent_contact_for_agent,
        add_user_contact_for_agent,
    )

    async with async_session() as db:
        try:
            target_uuid = uuid.UUID(digital_id or human_id)
        except ValueError:
            return "❌ user_id/agent_id must be a complete platform UUID"
        common = {
            "relation": args.get("relation") or "collaborator",
            "description": args.get("description") or "",
            "current_user_id": user_id,
        }
        if digital_id:
            result = await add_agent_contact_for_agent(db, agent_id, target_agent_id=target_uuid, **common)
        else:
            result = await add_user_contact_for_agent(db, agent_id, user_id=target_uuid, **common)
        if result.get("status") in {"added", "already_added"}:
            await db.commit()
        else:
            await db.rollback()

    if result.get("status") == "added":
        public_id = digital_id if digital_id else human_id
        public_field = "agent_id" if digital_id else "user_id"
        return f"✅ Added {result.get('name')} ({public_field}={public_id}) as a contact."
    if result.get("status") == "already_added":
        public_id = digital_id if digital_id else human_id
        public_field = "agent_id" if digital_id else "user_id"
        return (
            f"ℹ️ {result.get('name')} ({public_field}={public_id}) is already in your relationship network. "
            "Updated relation details."
        )
    return f"❌ Unable to add contact: {result.get('reason', 'unknown_error')}"


async def _remove_contact_tool(agent_id: uuid.UUID, args: dict, user_id: uuid.UUID | None) -> str:
    human_id = str(args.get("user_id") or "").strip()
    digital_id = str(args.get("agent_id") or "").strip()
    if bool(human_id) == bool(digital_id):
        return "❌ Provide exactly one of user_id or agent_id from search_contacts"

    from app.services.contact_relationships import (
        remove_agent_contact_for_agent,
        remove_user_contact_for_agent,
    )

    async with async_session() as db:
        try:
            target_uuid = uuid.UUID(digital_id or human_id)
        except ValueError:
            return "❌ user_id/agent_id must be a complete platform UUID"
        if digital_id:
            result = await remove_agent_contact_for_agent(
                db,
                agent_id,
                target_agent_id=target_uuid,
                current_user_id=user_id,
            )
        else:
            result = await remove_user_contact_for_agent(
                db,
                agent_id,
                user_id=target_uuid,
                current_user_id=user_id,
            )
        if result.get("status") == "removed":
            await db.commit()
        else:
            await db.rollback()

    if result.get("status") == "removed":
        public_id = digital_id if digital_id else human_id
        public_field = "agent_id" if digital_id else "user_id"
        return f"✅ Removed {result.get('name')} ({public_field}={public_id}) from your relationship network."
    if result.get("status") == "not_found":
        public_id = digital_id if digital_id else human_id
        public_field = "agent_id" if digital_id else "user_id"
        return f"ℹ️ {result.get('name')} ({public_field}={public_id}) is not in your relationship network."
    return f"❌ Unable to remove contact: {result.get('reason', 'unknown_error')}"


def _parse_uuid(value) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except Exception:
        return None


async def _start_dingtalk_channel_provisioning_tool(
    agent_id: uuid.UUID,
    user_id: uuid.UUID | None,
    args: dict,
) -> str:
    """Start DingTalk robot provisioning for the current digital employee."""
    if not user_id:
        return "❌ 需要登录用户上下文才能为数字员工配置钉钉通道。请在用户会话中重新发起。"

    force_reconfigure = (args or {}).get("force_reconfigure", False)
    if not isinstance(force_reconfigure, bool):
        return "❌ force_reconfigure 必须是布尔值。普通配置请求请保持 false。"

    from app.core.permissions import user_can_manage_agent_id
    from app.models.agent import Agent as AgentModel
    from app.services.dingtalk_provisioning import start_dingtalk_channel_provisioning

    async with async_session() as db:
        result = await db.execute(select(AgentModel).where(AgentModel.id == agent_id))
        agent = result.scalar_one_or_none()
        if not agent:
            return "❌ 未找到要配置钉钉通道的数字员工。"
        if not await user_can_manage_agent_id(db, user_id, agent):
            return "❌ 只有数字员工创建者或管理员可以发起钉钉机器人授权配置。"

        response = await start_dingtalk_channel_provisioning(
            db,
            agent=agent,
            requested_by_user_id=user_id,
            force_reconfigure=force_reconfigure,
        )
        timezone_name = await get_agent_timezone_in_session(db, agent)
        await db.commit()

    flow_action = response.get("flow_action")
    if flow_action == "already_configured":
        return (
            "当前数字员工的钉钉通道已经配置完成，可以直接使用，无需重复配置。\n"
            "只有在用户明确要求强制重配时才重新授权；强制重配会创建新的钉钉机器人应用，"
            "原应用需要用户在钉钉后台自行清理。"
        )
    if flow_action == "configured_existing":
        return f"原钉钉授权已经成功，数字员工通道配置已完成。\n配置编号: {response['provisioning_id']}"
    action_message = {
        "reused": "当前授权流程仍有效，已复用原钉钉授权链接。",
        "replaced": "已同步并替换原授权流程，请只使用下面的新链接。",
        "created": "已创建钉钉数字员工机器人授权流程。",
    }.get(flow_action, "钉钉数字员工机器人授权流程已就绪。")
    force_warning = (
        "\n这是强制重配流程，会创建新的钉钉机器人应用；切换完成后请在钉钉后台清理原应用。" if force_reconfigure else ""
    )
    return (
        f"{action_message}\n"
        f"授权链接: {response['authorization_url']}\n"
        f"配置编号: {response['provisioning_id']}\n"
        f"有效期至: {_render_tool_time(response['expires_at'], timezone_name)}\n"
        "用户完成授权后，平台会自动配置钉钉通道，并在钉钉中发送配置完成通知，无需手动回复确认。"
        f"{force_warning}"
    )


async def _get_dingtalk_channel_provisioning_status_tool(
    agent_id: uuid.UUID,
    user_id: uuid.UUID | None,
    args: dict,
) -> str:
    """Return DingTalk robot provisioning status for the current digital employee."""
    if not user_id:
        return "❌ 需要登录用户上下文才能查询数字员工钉钉通道配置状态。"

    provisioning_id = _parse_uuid((args or {}).get("provisioning_id"))
    if not provisioning_id:
        return "❌ 缺少有效的配置编号 provisioning_id。"

    from app.core.permissions import user_can_manage_agent_id
    from app.models.agent import Agent as AgentModel
    from app.models.dingtalk_provisioning import DingTalkChannelProvisioningSession
    from app.services.dingtalk_provisioning import get_dingtalk_provisioning_status_response

    async with async_session() as db:
        agent_result = await db.execute(select(AgentModel).where(AgentModel.id == agent_id))
        agent = agent_result.scalar_one_or_none()
        if not agent:
            return "❌ 未找到要查询的数字员工。"
        if not await user_can_manage_agent_id(db, user_id, agent):
            return "❌ 只有数字员工创建者或管理员可以查询钉钉机器人授权配置状态。"

        session_result = await db.execute(
            select(DingTalkChannelProvisioningSession).where(
                DingTalkChannelProvisioningSession.id == provisioning_id,
                DingTalkChannelProvisioningSession.agent_id == agent_id,
            )
        )
        session = session_result.scalar_one_or_none()
        if not session:
            return "❌ 未找到该钉钉数字员工通道配置流程。"

        response = get_dingtalk_provisioning_status_response(session)
        timezone_name = await get_agent_timezone_in_session(db, agent)

    lines = [
        "钉钉数字员工通道配置状态:",
        f"状态: {response['status']}",
        f"配置编号: {response['provisioning_id']}",
        f"有效期至: {_render_tool_time(response['expires_at'], timezone_name)}",
    ]
    if response.get("authorization_url") and response["status"] in {"waiting_for_authorization", "polling"}:
        lines.append(f"授权链接: {response['authorization_url']}")
    if response.get("message"):
        lines.append(response["message"])
    if response.get("last_error"):
        lines.append(f"错误: {response['last_error']}")
    return "\n".join(lines)
