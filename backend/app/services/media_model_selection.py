"""Resolve enterprise models once, before accepting a media task."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict

from sqlalchemy import select

from app.core.security import decrypt_data
from app.config import get_settings
from app.database import async_session
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.llm import LLMModel
from app.models.subagent_run import SubagentRun
from app.models.tool import AgentTool, Tool
from app.services.llm.client_registry import get_provider_spec, resolve_api_protocol
from app.services.llm.runtime_model import RuntimeLLMModel
from app.services.llm.utils import get_model_api_key
from app.services.media_ai_io import MediaAIError
from app.services.media_model_inputs import matching_understanding_model, source_modalities
from app.services.tool_config import get_tenant_tool_config
from app.services.model_capabilities import MEDIA_MODEL_DEFAULTS, model_modalities, model_purposes
from app.services.turn_tool_settings import current_tool_settings
from app.services.model_headers import encrypt_model_headers, resolve_model_headers


def media_model_slot(tool: str, args: dict) -> tuple[str, str]:
    kind = "understanding" if tool == "read_media" else args["output_type"]
    slot = f"{kind}_model_id"
    return slot, MEDIA_MODEL_DEFAULTS[slot]


async def _previous_selection(db, state) -> dict:
    session_id = state.arguments.get("session_id")
    if not session_id:
        return {}
    child = await db.get(ChatSession, uuid.UUID(session_id))
    run = await db.get(SubagentRun, uuid.UUID(session_id))
    if (child is None or run is None or child.agent_id != state.agent_id
            or str(run.parent_session_id) != str(state.session_id)
            or run.execution_user_id != state.user_id
            or dict(child.im_config or {}).get("executor") != "media"):
        raise MediaAIError("contextRequired")
    rows = (await db.scalars(select(ChatMessage).where(
        ChatMessage.conversation_id == str(child.id), ChatMessage.role == "user",
        ChatMessage.message_meta["media_request"].is_not(None),
    ).order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc()))).all()
    slot, _ = media_model_slot(state.tool_name, state.arguments)
    required = set()
    for row in rows:
        meta = row.message_meta or {}
        required.update(source_modalities((meta.get("media_request") or {}).get("arguments", {}).get("files", [])))
        required.update((meta.get("media_context") or {}).get("input_modalities", []))
    for row in rows:
        previous = (row.message_meta or {}).get("media_request") or {}
        previous_args = previous.get("arguments") or {}
        if previous.get("tool") != state.tool_name:
            continue
        if media_model_slot(previous["tool"], previous_args)[0] != slot:
            continue
        return {**previous, "required_input_modalities": sorted(required)}
    return {}


def model_connection(model: LLMModel) -> dict:
    runtime = RuntimeLLMModel.from_orm(model)
    snapshot = asdict(runtime)
    snapshot["id"] = str(runtime.id)
    snapshot["tenant_id"] = str(runtime.tenant_id) if runtime.tenant_id else None
    spec = get_provider_spec(runtime.provider)
    protocol = resolve_api_protocol(runtime.provider, getattr(runtime, "api_protocol", None))
    base_url = runtime.base_url or (spec.default_base_url if spec else None)
    if not base_url:
        raise MediaAIError("invalidEndpoint")
    snapshot["api_protocol"] = protocol
    snapshot["base_url"] = base_url
    headers = resolve_model_headers(runtime)
    snapshot["extra_headers_encrypted"] = encrypt_model_headers(headers)
    return {
        "model_id": str(runtime.id), "provider": runtime.provider,
        "api_protocol": protocol, "model": runtime.model, "base_url": base_url,
        "api_key": get_model_api_key(runtime),
        "extra_headers": headers,
        "runtime_model": snapshot,
        "input_modalities": model_modalities(model),
        "purposes": model_purposes(model),
        "context_window": runtime.context_window,
        "context_usage_ratio": runtime.context_usage_ratio,
        "max_output_tokens": runtime.max_output_tokens,
        "request_timeout": runtime.request_timeout,
        "temperature": runtime.temperature,
        "reasoning_effort": runtime.reasoning_effort,
        **{f"{kind}_model": runtime.model for kind in ("understanding", "image", "audio", "video")},
    }


async def resolve_media_model(state, effective_config: dict) -> dict:
    """Explicit > same-purpose session selection > resolved tool/company default."""
    from app.services.media_model_migration import LEGACY_KEYS, materialize_legacy_config, restore_legacy_override

    slot, purpose = media_model_slot(state.tool_name, state.arguments)
    async with async_session() as db:
        agent = await db.get(Agent, state.agent_id)
        if agent is None or agent.tenant_id is None:
            raise MediaAIError("contextRequired")
        previous = await _previous_selection(db, state)
        required = source_modalities(state.arguments.get("files", []))
        required.update(previous.get("required_input_modalities", []))
        model_id = state.arguments.get("model_id")
        if not model_id:
            model_id = (previous.get("config") or {}).get("model_id")
        config = dict(effective_config)
        scope = current_tool_settings(state.agent_id)
        reference = scope.legacy_media_references.get(state.tool_name) if scope else None
        if scope and not reference and any(key in config for key in LEGACY_KEYS):
            reference = "company"
        if not scope and any(key in config for key in LEGACY_KEYS):
            assignment = await db.scalar(select(AgentTool).join(Tool, Tool.id == AgentTool.tool_id).where(
                AgentTool.agent_id == agent.id, Tool.name == state.tool_name,
            ))
            reference = (f"assignment:{assignment.id}" if assignment and any(
                key in (assignment.config or {}) for key in LEGACY_KEYS
            ) else "company")
        if not model_id and previous.get("connection_ref"):
            config = json.loads(decrypt_data(previous["connection_ref"], get_settings().SECRET_KEY))
        if not model_id:
            config = await restore_legacy_override(db, agent.tenant_id, config, slot)
        # Published legacy scenes remain immutable. Their old effective connection
        # is materialized into a tenant model, rather than overriding its secret.
        if not model_id and (config.get("api_key") or reference):
            config = await materialize_legacy_config(
                db, agent.tenant_id, config, reference=reference, required_slot=slot,
            )
        if not model_id:
            model_id = config.get(slot)
        if not model_id:
            defaults = await get_tenant_tool_config(db, agent.tenant_id, "read_media")
            if defaults.get("api_key"):
                defaults = await materialize_legacy_config(
                    db, agent.tenant_id, defaults, reference="company", required_slot=slot,
                )
            model_id = defaults.get(slot)
        if not model_id and state.tool_name != "read_media":
            raise MediaAIError("notConfigured")
        try:
            model = await db.get(LLMModel, uuid.UUID(str(model_id))) if model_id else None
        except (TypeError, ValueError) as exc:
            raise MediaAIError("modelUnavailable") from exc
        if model_id and (model is None or model.tenant_id != agent.tenant_id or not model.enabled
                or purpose not in (model.purposes or [])):
            raise MediaAIError("modelUnavailable")
        if state.tool_name == "read_media":
            model = await matching_understanding_model(
                db, agent.tenant_id, model, required, explicit=bool(state.arguments.get("model_id")),
            )
        resolved = model_connection(model)
        if state.tool_name == "read_media":
            resolved["retained_input_modalities"] = previous.get("required_input_modalities", [])
        if state.tool_name == "read_media" and config.get("fallback_model_id"):
            try:
                fallback = await db.get(LLMModel, uuid.UUID(str(config["fallback_model_id"])))
            except (ValueError, TypeError):
                fallback = None
            if (fallback is not None and fallback.id != model.id and fallback.tenant_id == agent.tenant_id
                    and fallback.enabled and purpose in (fallback.purposes or [])):
                resolved["fallback_connection"] = model_connection(fallback)
        await db.commit()
    return resolved
