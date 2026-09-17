"""Trigger management tool adapters over the shared patch contract."""

import json
import uuid

from sqlalchemy import select, func
from app.database import async_session
from app.models.trigger import AgentTrigger
from app.services.timezone_utils import get_agent_timezone
from app.services.trigger_patch import (
    prepare_patch,
    resolve_patch_recipient,
    apply_patch,
    serialize_trigger,
    check_enable_limit,
    recover_enabled_trigger,
)


def result_json(value):
    return json.dumps(value, ensure_ascii=False, default=str)


def selector(agent_id, arguments):
    query = select(AgentTrigger).where(AgentTrigger.agent_id == agent_id)
    if arguments.get("id"):
        query = query.where(AgentTrigger.id == uuid.UUID(arguments["id"]))
        if arguments.get("name"):
            query = query.where(AgentTrigger.name == arguments["name"])
    elif arguments.get("name"):
        query = query.where(AgentTrigger.name == arguments["name"])
    else:
        raise ValueError("Provide trigger id or name")
    return query


async def update_locked(db, trigger, arguments, user_id=None):
    from app.services.agent_tools_trigger_ops import _resolve_trigger_model_id

    if "type" in arguments and arguments["type"] != trigger.type:
        raise ValueError("Cannot change an existing trigger type")
    patch = {key: value for key, value in arguments.items() if key not in {"id", "name", "type"}}
    if "model" in patch:
        patch["model_id"] = await _resolve_trigger_model_id(db, trigger.agent_id, patch.pop("model"))
    if "clear_fields" in patch:
        patch["clear_fields"] = ["/model_id" if path == "/model" else path for path in patch["clear_fields"]]
    if "webhook_mode" in patch:
        if trigger.type != "webhook":
            raise ValueError("webhook_mode only applies to webhook triggers")
        config = dict(patch.get("config", {}))
        if "webhook_mode" in config:
            raise ValueError("Provide webhook_mode only once")
        config["webhook_mode"] = patch.pop("webhook_mode")
        patch["config"] = config
    values = prepare_patch(trigger, patch, await get_agent_timezone(trigger.agent_id))
    await resolve_patch_recipient(db, trigger.agent_id, trigger, values)
    await check_enable_limit(db, trigger, values)
    if user_id is not None and values:
        from app.services.execution_identity import align_background_execution_user

        await align_background_execution_user(
            db, agent_id=trigger.agent_id, resource_type="trigger", resource_id=trigger.id, execution_user_id=user_id
        )
    apply_patch(trigger, values)
    return values


async def _handle_update_trigger(agent_id, arguments, *, user_id=None, _audit_action="trigger_updated"):
    try:
        async with async_session() as db:
            trigger = (await db.execute(selector(agent_id, arguments).with_for_update())).scalar_one_or_none()
            if not trigger:
                return "❌ Trigger not found"
            values = await update_locked(db, trigger, arguments, user_id)
            if not values:
                return "❌ Provide at least one field to update"
            await db.commit()
            await recover_enabled_trigger(trigger, values)
            result = serialize_trigger(trigger)
        from app.services.audit_logger import write_audit_log

        try:
            await write_audit_log(_audit_action, {"name": trigger.name, "fields": sorted(values)}, agent_id=agent_id)
        except Exception:
            pass
        return result_json({"ok": True, "message": f"✅ Trigger '{trigger.name}' updated", "trigger": result})
    except Exception as exc:
        return f"❌ Failed to update trigger: {exc}"


async def _handle_cancel_trigger(agent_id, arguments, *, user_id=None):
    return await _handle_update_trigger(
        agent_id, {**arguments, "is_enabled": False}, user_id=user_id, _audit_action="trigger_cancelled"
    )


async def _handle_delete_trigger(agent_id, arguments, *, user_id=None):
    from app.services.trigger_deletion import TriggerDeletionError, delete_trigger_definition

    try:
        raw_id = arguments.get("id")
        trigger_id = uuid.UUID(str(raw_id)) if raw_id else None
        async with async_session() as db:
            deleted = await delete_trigger_definition(
                db,
                agent_id=agent_id,
                trigger_id=trigger_id,
                name=str(arguments.get("name") or "").strip() or None,
            )
            await db.commit()
        from app.services.audit_logger import write_audit_log

        try:
            await write_audit_log(
                "trigger_deleted",
                {"id": str(deleted.id), "name": deleted.name},
                agent_id=agent_id,
                user_id=user_id,
            )
        except Exception:
            pass
        return result_json(
            {
                "ok": True,
                "message": f"✅ Trigger '{deleted.name}' deleted",
                "deleted_trigger": {"id": str(deleted.id), "name": deleted.name},
            }
        )
    except TriggerDeletionError as exc:
        return f"❌ Failed to delete trigger: {exc}"
    except (TypeError, ValueError) as exc:
        return f"❌ Failed to delete trigger: {exc}"


async def _handle_list_triggers(agent_id, arguments=None):
    from app.models.agent import Agent
    from app.core.domain import resolve_base_url

    arguments = arguments or {}
    try:
        limit = arguments.get("limit", 100)
        offset = arguments.get("offset", 0)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
            or isinstance(offset, bool)
            or not isinstance(offset, int)
            or offset < 0
        ):
            raise ValueError("limit must be 1–100 and offset must be nonnegative")
        async with async_session() as db:
            query = (
                selector(agent_id, arguments)
                if arguments.get("id") or arguments.get("name")
                else select(AgentTrigger).where(AgentTrigger.agent_id == agent_id)
            )
            if "is_enabled" in arguments:
                query = query.where(AgentTrigger.is_enabled == arguments["is_enabled"])
            total = await db.scalar(select(func.count()).select_from(query.subquery()))
            rows = (
                (
                    await db.execute(
                        query.order_by(AgentTrigger.created_at.desc(), AgentTrigger.id).offset(offset).limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            agent = await db.get(Agent, agent_id)
            timezone_name = await get_agent_timezone(agent_id)
            base_url = (
                await resolve_base_url(
                    db, request=None, tenant_id=str(agent.tenant_id) if agent and agent.tenant_id else None
                )
            ).rstrip("/")
            return result_json(
                {
                    "triggers": [serialize_trigger(row, base_url=base_url, timezone_name=timezone_name) for row in rows],
                    "total": total,
                    "next_offset": offset + len(rows) if offset + len(rows) < total else None,
                }
            )
    except Exception as exc:
        return f"❌ Failed to list triggers: {exc}"
