"""Shared, lossless trigger patch contract. Callers own authorization and transactions."""

from copy import deepcopy
from datetime import datetime
import re

from app.services.trigger_time_contract import normalize_trigger_time_config

NULLABLE = {"focus_ref", "model_id", "temperature", "reasoning_effort", "max_fires", "expires_at"}
WRITABLE = NULLABLE | {"reason", "config", "is_enabled", "cooldown_seconds", "soul", "memory"}


def pointer(value):
    if not isinstance(value, str) or not value.startswith("/"):
        raise ValueError("clear_fields entries must be JSON Pointer paths")
    parts = value[1:].split("/")
    if any(re.search(r"~(?![01])", part) for part in parts):
        raise ValueError("Invalid JSON Pointer escape")
    return tuple(part.replace("~1", "/").replace("~0", "~") for part in parts)


def leaves(value, path=()):
    if isinstance(value, dict) and value:
        for key, child in value.items():
            yield from leaves(child, (*path, key))
    elif path:
        yield path


def merge_object(old, patch):
    result = deepcopy(old)
    for key, value in patch.items():
        result[key] = (
            merge_object(result.get(key, {}), value)
            if isinstance(value, dict) and isinstance(result.get(key, {}), dict)
            else deepcopy(value)
        )
    return result


def public_config(config):
    return {key: deepcopy(value) for key, value in config.items() if not key.startswith("_")}


def reject_reserved(value, private_key=None):
    if isinstance(value, dict):
        for key, child in value.items():
            if key.startswith("_") or key == "at_local" or (private_key and private_key(key)):
                raise ValueError("Trigger config contains reserved internal or display fields")
            reject_reserved(child, private_key)
    elif isinstance(value, list):
        for child in value:
            reject_reserved(child, private_key)


def protected_config(value, private_key):
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            if key.startswith("_") or (private_key and private_key(key)):
                result[key] = deepcopy(child)
            else:
                nested = protected_config(child, private_key)
                if nested:
                    result[key] = nested
        return result
    if isinstance(value, list):
        return {
            str(index): nested for index, child in enumerate(value) if (nested := protected_config(child, private_key))
        }
    return {}


def prepare_patch(trigger, patch, timezone_name, *, private_key=None):
    """Build a complete candidate without changing the row or execution identity."""
    patch = deepcopy(patch)
    clear = patch.pop("clear_fields", [])
    if not isinstance(clear, list):
        raise ValueError("clear_fields must be an array")
    unknown = set(patch) - WRITABLE
    if unknown:
        raise ValueError(f"Unsupported trigger fields: {', '.join(sorted(unknown))}")
    paths = [pointer(item) for item in clear]
    writes = list(leaves(patch))
    for path in paths:
        for other in writes:
            if path[: len(other)] == other or other[: len(path)] == path:
                raise ValueError("Cannot write and clear overlapping fields")
        if path[0] not in NULLABLE | {"reason", "config"}:
            raise ValueError("This trigger field cannot be cleared")
        if path[0] != "config" and len(path) != 1:
            raise ValueError("Invalid trigger field path")
        if path[0] == "config" and len(path) < 2:
            raise ValueError("Clear specific optional config fields")
    changed = set(patch) | {path[0] for path in paths}
    if trigger.is_system and changed - {"is_enabled", "model_id", "temperature", "reasoning_effort", "soul", "memory"}:
        raise ValueError("System trigger configuration cannot be changed")
    candidate = {key: deepcopy(getattr(trigger, key)) for key in WRITABLE}
    if "config" in patch:
        config = patch["config"]
        if not isinstance(config, dict):
            raise ValueError("Trigger config must be an object")
        reject_reserved(config, private_key)
        if "token" in config and config["token"] != (trigger.config or {}).get("token"):
            raise ValueError("Webhook callback token cannot be changed")
        patch["config"] = merge_object(trigger.config or {}, config)
    candidate.update(patch)
    for path in paths:
        if path[0] != "config":
            candidate[path[0]] = "" if path[0] == "reason" else None
            continue
        if (
            any(key.startswith("_") or key == "at_local" or (private_key and private_key(key)) for key in path[1:])
            or path[1] == "token"
        ):
            raise ValueError("Reserved trigger configuration cannot be cleared")
        parent = candidate["config"]
        for key in path[1:-1]:
            if not isinstance(parent, dict):
                raise ValueError("Cannot clear array elements or descend into scalar values")
            parent = parent.get(key, {})
        if not isinstance(parent, dict):
            raise ValueError("Cannot clear array elements or descend into scalar values")
        parent.pop(path[-1], None)
    if protected_config(candidate["config"], private_key) != protected_config(trigger.config or {}, private_key):
        raise ValueError("Cannot replace or clear a parent containing reserved configuration")
    if "config" in changed:
        cfg = candidate["config"]
        if trigger.type == "cron":
            from croniter import croniter

            if not isinstance(cfg.get("expr"), str) or not croniter.is_valid(cfg["expr"]):
                raise ValueError("cron requires a valid config.expr")
        elif trigger.type == "once" and not cfg.get("at"):
            raise ValueError("once requires config.at")
        elif trigger.type == "interval":
            minutes = cfg.get("minutes")
            if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or minutes <= 0:
                raise ValueError("interval requires positive config.minutes")
        elif trigger.type == "poll" and not cfg.get("url"):
            raise ValueError("poll requires config.url")
        elif trigger.type == "on_message" and bool(cfg.get("from_agent_id")) == bool(cfg.get("from_user_id")):
            raise ValueError("on_message requires exactly one of from_agent_id or from_user_id")
        if cfg.get("webhook_mode", "legacy") not in {"legacy", "queue", "merge"}:
            raise ValueError("Invalid webhook_mode")
        if trigger.type == "webhook":
            if cfg.get("webhook_mode") == "legacy":
                cfg.pop("webhook_mode", None)
            elif cfg.get("webhook_mode") in {"queue", "merge"}:
                cfg.setdefault("_webhook_queue", [])
        candidate["config"] = normalize_trigger_time_config(trigger.type, cfg, timezone_name)
    for field in {"is_enabled", "soul", "memory"} & changed:
        if not isinstance(candidate[field], bool):
            raise ValueError(f"{field} must be boolean")
    for field in {"max_fires", "cooldown_seconds"} & changed:
        value = candidate[field]
        if value is None and field == "max_fires":
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{field} must be a nonnegative integer")
    if "focus_ref" in changed and candidate["focus_ref"] is not None:
        if not isinstance(candidate["focus_ref"], str) or len(candidate["focus_ref"]) > 200:
            raise ValueError("focus_ref must be at most 200 characters")
    if "reason" in changed and not isinstance(candidate["reason"], str):
        raise ValueError("reason must be a string; use clear_fields to clear it")
    if "expires_at" in changed and isinstance(candidate["expires_at"], str):
        candidate["expires_at"] = datetime.fromisoformat(candidate["expires_at"].replace("Z", "+00:00"))
        if candidate["expires_at"].tzinfo is None:
            raise ValueError("expires_at must include a timezone")
    if "temperature" in changed:
        from app.services.chat_model_selection import validate_temperature

        candidate["temperature"] = validate_temperature(candidate["temperature"])
    if "reasoning_effort" in changed:
        from app.services.llm.reasoning import validate_reasoning_effort

        candidate["reasoning_effort"] = validate_reasoning_effort(candidate["reasoning_effort"])
    return {key: candidate[key] for key in changed}


async def resolve_patch_recipient(db, agent_id, trigger, values):
    if trigger.type != "on_message" or "config" not in values:
        return
    from app.services.recipient_resolver import resolve_agent_recipient, resolve_platform_user_recipient

    cfg = values["config"]
    if cfg.get("from_agent_id"):
        recipient = await resolve_agent_recipient(db, agent_id, str(cfg["from_agent_id"]))
        cfg["from_agent_id"] = str(recipient.target_agent.id)
    else:
        recipient = await resolve_platform_user_recipient(db, agent_id, str(cfg["from_user_id"]))
        cfg["from_user_id"] = str(recipient.user.id)
    old = trigger.config or {}
    if (old.get("from_agent_id"), old.get("from_user_id")) != (cfg.get("from_agent_id"), cfg.get("from_user_id")):
        values["config"] = {
            key: value
            for key, value in cfg.items()
            if not key.startswith("_") or key.startswith("_origin_") or key == "_set_trigger_context"
        }


async def check_enable_limit(db, trigger, values):
    if not values.get("is_enabled") or trigger.is_enabled:
        return
    from sqlalchemy import select, func
    from app.models.agent import Agent
    from app.models.trigger import AgentTrigger

    agent = (await db.execute(select(Agent).where(Agent.id == trigger.agent_id).with_for_update())).scalar_one()
    count = await db.scalar(
        select(func.count())
        .select_from(AgentTrigger)
        .where(
            AgentTrigger.agent_id == trigger.agent_id,
            AgentTrigger.is_enabled.is_(True),
        )
    )
    limit = agent.max_triggers or 20
    if count >= limit:
        raise ValueError(f"Maximum trigger limit reached ({limit})")


def apply_patch(trigger, values):
    for key, value in values.items():
        setattr(trigger, key, value)
    if trigger.type == "on_message":
        cfg = dict(trigger.config or {})
        cfg["_set_trigger_context"] = {
            "name": trigger.name,
            "type": trigger.type,
            "reason": trigger.reason,
            "focus_ref": trigger.focus_ref or "",
            "config": public_config(cfg),
        }
        trigger.config = cfg


async def recover_enabled_trigger(trigger, values):
    if not values.get("is_enabled") or trigger.type != "on_message" or not (trigger.config or {}).get("_watch_session_id"):
        return
    from app.services.trigger_runtime.evaluator import recover_exact_on_message_events
    from loguru import logger
    try:
        await recover_exact_on_message_events(trigger)
    except Exception as exc:
        logger.warning("Trigger reply-before-arm recovery failed for {}: {}", trigger.id, exc)


def serialize_trigger(trigger, *, config_projector=public_config, base_url="", timezone_name=None):
    fields = WRITABLE | {
        "id",
        "name",
        "type",
        "is_system",
        "fire_count",
        "last_fired_at",
        "created_at",
        "created_by_user_id",
        "execution_user_id",
    }
    result = {key: getattr(trigger, key) for key in fields}
    result["config"] = config_projector(trigger.config or {})
    if timezone_name and result["config"].get("at"):
        from app.services.trigger_time_contract import project_trigger_config
        result["at_local"] = project_trigger_config(result["config"], timezone_name).get("at_local")
    if base_url and trigger.type == "webhook" and result["config"].get("token"):
        result["webhook_url"] = f"{base_url}/api/webhooks/t/{result['config']['token']}"
    return result
