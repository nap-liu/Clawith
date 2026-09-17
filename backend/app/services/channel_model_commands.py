"""Human-friendly model selection for external channel commands."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.llm.failure_outcome import render_message


_CODE_SELECTOR = re.compile(r"^(?P<name>.+?)\s+@(?P<code>[0-9a-fA-F]{6,32})$")
_RESERVED_NAMES = {"list", "status", "default"}


def _normalized(value: object) -> str:
    return str(value or "").strip().casefold()


def _message(key: str, **values: object) -> str:
    return render_message(key).format(**values)


def _matching_label_or_name(models: list[Any], reference: str) -> list[Any]:
    """Resolve readable labels first, then provider model keys."""
    normalized = _normalized(reference)
    label_matches = [model for model in models if _normalized(model.label) == normalized]
    if label_matches:
        return label_matches
    return [model for model in models if _normalized(model.model) == normalized]


def _all_readable_matches(models: list[Any], reference: str) -> list[Any]:
    """Return every model that a human-readable selector could denote."""
    normalized = _normalized(reference)
    return [
        model
        for model in models
        if _normalized(model.label) == normalized
        or _normalized(model.model) == normalized
    ]


def _friendly_selector(model: Any, models: list[Any]) -> tuple[str, str]:
    """Prefer a unique readable name; suffix only truly duplicated entries."""
    label = str(getattr(model, "label", "") or "").strip()
    selector_base = label or str(model.model)
    duplicates = _all_readable_matches(models, selector_base)
    if len(duplicates) == 1:
        needs_literal = bool(
            _normalized(selector_base) in _RESERVED_NAMES
            or _normalized(selector_base).startswith(("use ", "pick "))
            or _CODE_SELECTOR.fullmatch(selector_base)
        )
        return selector_base, "literal" if needs_literal else "direct"
    compact_ids = [str(item.id).replace("-", "").casefold() for item in duplicates]
    model_id = str(model.id).replace("-", "").casefold()
    code = model_id
    for length in (6, 8, 12, 32):
        candidate = model_id[:length]
        if sum(value.startswith(candidate) for value in compact_ids) == 1:
            code = candidate
            break
    return f"{selector_base} @{code}", "coded"


def _selection_command(selector: str, *, mode: str) -> str:
    prefix = {
        "literal": "/model use",
        "coded": "/model pick",
    }.get(mode, "/model")
    return f"{prefix} {selector}"


async def _resolve_model(db, *, tenant_id, reference: str, literal: bool = False):
    from app.services.chat_model_selection import (
        MODEL_STATUS_NOT_FOUND,
        MODEL_STATUS_OK,
        ModelNameResolution,
        list_enabled_tenant_models,
        resolve_tenant_model_reference,
    )

    models = await list_enabled_tenant_models(db, tenant_id)
    code_match = None if literal else _CODE_SELECTOR.fullmatch(reference.strip())
    selector_base = (
        code_match.group("name") if code_match is not None else reference
    )
    matches = (
        _all_readable_matches(models, selector_base)
        if code_match is not None
        else _matching_label_or_name(models, selector_base)
    )
    if code_match is not None:
        code = code_match.group("code").casefold()
        coded_matches = [
            model
            for model in matches
            if str(model.id).replace("-", "").casefold().startswith(code)
        ]
        if len(coded_matches) == 1:
            return ModelNameResolution(MODEL_STATUS_OK, coded_matches[0])
        return ModelNameResolution(MODEL_STATUS_NOT_FOUND)
    if len(matches) == 1:
        return ModelNameResolution(MODEL_STATUS_OK, matches[0])
    if len(matches) > 1:
        from app.services.chat_model_selection import MODEL_STATUS_AMBIGUOUS

        return ModelNameResolution(MODEL_STATUS_AMBIGUOUS)
    return await resolve_tenant_model_reference(
        db,
        tenant_id=tenant_id,
        reference=reference,
    )


async def handle_model_command(
    db: AsyncSession,
    *,
    arg: str | None,
    agent_id,
    user_id,
    external_conv_id: str,
    source_channel: str,
    is_group: bool,
    group_name: str | None,
    load_agent: Callable,
    load_session: Callable,
) -> dict:
    """Handle one /model command and persist only the canonical model UUID."""
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
    )

    agent = await load_agent(db, agent_id=agent_id)
    if agent is None or agent.tenant_id is None:
        return {"action": "model_failed", "message": _message("commands.model.readFailed")}

    normalized_arg = str(arg or "status").strip()
    explicit_use = normalized_arg.casefold().startswith("use ")
    explicit_pick = normalized_arg.casefold().startswith("pick ")
    if explicit_use or explicit_pick:
        normalized_arg = normalized_arg[5 if explicit_pick else 4:].strip()
        if not normalized_arg:
            return {
                "action": "model_usage",
                "message": _message("commands.model.usage"),
            }
    control_arg = "" if explicit_use or explicit_pick else normalized_arg.casefold()
    if control_arg == "list":
        models = await list_enabled_tenant_models(db, agent.tenant_id)
        if not models:
            return {"action": "model_list", "message": _message("commands.model.empty")}
        items = []
        for model in models:
            label = str(getattr(model, "label", "") or "").strip()
            selector, mode = _friendly_selector(model, models)
            items.append(_message(
                "commands.model.listItem",
                display=label or model.model,
                provider=model.provider,
                model=model.model,
                command=_selection_command(selector, mode=mode),
            ))
        return {
            "action": "model_list",
            "message": _message("commands.model.list", items="\n".join(items)),
        }

    session = await load_session(
        db,
        agent_id=agent_id,
        external_conv_id=external_conv_id,
        source_channel=source_channel,
        for_update=control_arg != "status",
    )
    current_model_id = (
        str((session.im_config or {}).get(MODEL_SESSION_CONFIG_KEY) or "")
        if session
        else ""
    )

    if control_arg == "status":
        resolved = await resolve_runtime_models(
            db,
            agent=agent,
            override_model_id=current_model_id or None,
        )
        if current_model_id and resolved.override_status != MODEL_OVERRIDE_OK:
            return {
                "action": "model_status_unavailable",
                "message": _message("commands.model.statusUnavailable"),
            }
        if resolved.primary_model is None:
            return {"action": "model_status", "message": _message("commands.model.none")}
        source_key = "sessionSource" if current_model_id else "defaultSource"
        return {
            "action": "model_status",
            "message": _message(
                "commands.model.status",
                model=resolved.primary_model.model,
                source=_message(f"commands.model.{source_key}"),
            ),
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
                "message": _message("commands.model.defaultNone"),
            }
        return {
            "action": "model_default",
            "message": _message(
                "commands.model.default",
                model=resolved.primary_model.model,
            ),
        }

    matched = await _resolve_model(
        db,
        tenant_id=agent.tenant_id,
        reference=normalized_arg,
        literal=explicit_use,
    )
    if matched.status == MODEL_STATUS_NOT_FOUND:
        return {
            "action": "model_not_found",
            "message": _message("commands.model.notFound", reference=normalized_arg),
        }
    if matched.status == MODEL_STATUS_DISABLED:
        return {
            "action": "model_disabled",
            "message": _message("commands.model.disabled", model=matched.model.model),
        }
    if matched.status == MODEL_STATUS_AMBIGUOUS:
        return {
            "action": "model_ambiguous",
            "message": _message("commands.model.ambiguous", reference=normalized_arg),
        }
    if matched.status != MODEL_STATUS_OK or matched.model is None:
        return {"action": "model_failed", "message": _message("commands.model.switchFailed")}

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
        session = await load_session(
            db,
            agent_id=agent_id,
            external_conv_id=external_conv_id,
            source_channel=source_channel,
            for_update=True,
        )
    if session is None:
        return {"action": "model_failed", "message": _message("commands.model.switchFailed")}

    config = dict(session.im_config or {})
    config[MODEL_SESSION_CONFIG_KEY] = str(matched.model.id)
    session.im_config = config
    await db.flush()
    return {
        "action": "model_switched",
        "message": _message("commands.model.switched", model=matched.model.model),
    }
