"""Idempotent conversion of legacy media connections into enterprise models."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.config import get_settings
from app.core.security import encrypt_data
from app.models.agent import Agent
from app.models.llm import LLMModel
from app.models.tenant import Tenant
from app.models.tenant_setting import TenantSetting
from app.models.tool import AgentTool, Tool
from app.services.media_ai_contract import MEDIA_AI_DEFAULTS, MEDIA_AI_NAMES
from app.services.model_capabilities import MEDIA_MODEL_DEFAULTS
from app.services.llm.utils import get_model_api_key
from app.services.tool_config import decrypt_sensitive_fields, tenant_tool_config_key

LEGACY_KEYS = frozenset({"api_key", "base_url", *MEDIA_AI_DEFAULTS})


def legacy_connection(config: dict) -> dict:
    from app.services.media_ai_provider import connection

    # The old format is specifically a DashScope connection. Its normalizer
    # must never be applied to ordinary enterprise endpoints with path prefixes.
    return connection({**MEDIA_AI_DEFAULTS, **decrypt_sensitive_fields(config)})


async def materialize_legacy_config(db, tenant_id, config: dict) -> dict:
    """Keep immutable scene revisions usable without executing inline secrets."""
    if not config.get("api_key"):
        return dict(config)
    resolved = legacy_connection(config)
    # Existing tenant lock serializes startup and legacy scene admission without
    # adding a registry, fingerprint column, or another configuration resource.
    await db.scalar(select(Tenant.id).where(Tenant.id == tenant_id).with_for_update())
    converted = {key: value for key, value in config.items() if key not in LEGACY_KEYS}
    candidates = list((await db.scalars(select(LLMModel).where(
        LLMModel.tenant_id == tenant_id, LLMModel.provider == "qwen",
    ))).all())
    settings = get_settings()
    endpoint = resolved["base_url"] + "/compatible-mode/v1"
    for slot, purpose in MEDIA_MODEL_DEFAULTS.items():
        if slot == "speech_model_id":
            continue  # Speech has its own historical source, not media tool credentials.
        kind = slot.removesuffix("_model_id")
        name = resolved[f"{kind}_model"]
        selected = None
        for model in candidates:
            if (model.model != name or model.base_url != endpoint
                    or purpose not in (model.purposes or [])
                    or model.api_protocol != "openai_compatible"
                    or model.context_window != resolved["context_window"]
                    or model.context_usage_ratio != resolved["context_usage_ratio"]
                    or model.max_output_tokens != resolved["max_output_tokens"]):
                continue
            if get_model_api_key(model) == resolved["api_key"]:
                selected = model
                break
        if selected is None:
            modalities = (["text", "image", "audio", "video"] if kind in {"understanding", "video"}
                          else ["text", "image"] if kind == "image" else ["text"])
            selected = LLMModel(
                tenant_id=tenant_id, provider="qwen", model=name, label=name,
                api_key_encrypted=encrypt_data(resolved["api_key"], settings.SECRET_KEY),
                base_url=endpoint, api_protocol="openai_compatible",
                purposes=[purpose], input_modalities=modalities,
                supports_vision="image" in modalities, enabled=True,
                context_window=resolved["context_window"],
                context_usage_ratio=resolved["context_usage_ratio"],
                max_output_tokens=resolved["max_output_tokens"], request_timeout=180,
            )
            db.add(selected)
            await db.flush()
            candidates.append(selected)
        converted[slot] = str(selected.id)
    return converted


async def restore_legacy_override(db, tenant_id, config: dict, slot: str) -> dict:
    """Resolve a published old override against an already migrated default."""
    if config.get("api_key") or not any(key in config for key in LEGACY_KEYS):
        return config
    reference = config.get(slot)
    if not reference:
        return config
    try:
        model = await db.get(LLMModel, uuid.UUID(str(reference)))
    except ValueError:
        return config
    if model is None or model.tenant_id != tenant_id or model.provider != "qwen":
        return config
    inherited = {
        "api_key": get_model_api_key(model),
        "base_url": model.base_url, "context_window": model.context_window,
        "context_usage_ratio": model.context_usage_ratio,
        "max_output_tokens": model.max_output_tokens,
        slot.removesuffix("_id"): model.model,
    }
    return {**inherited, **config}


async def migrate_legacy_media_configs(db) -> int:
    """Migrate mutable tenant/Agent settings; leave accepted turns and scenes intact."""
    settings = list((await db.scalars(select(TenantSetting).where(
        TenantSetting.key == tenant_tool_config_key("read_media"),
    ))).all())
    count = 0
    for setting in settings:
        original = decrypt_sensitive_fields((setting.value or {}).get("config") or {})
        # Agent overrides must be resolved against the old company connection
        # before removing its key from the mutable enterprise setting.
        assignments = (await db.execute(select(AgentTool, Tool).join(
            Tool, Tool.id == AgentTool.tool_id,
        ).join(Agent, Agent.id == AgentTool.agent_id).where(
            Agent.tenant_id == setting.tenant_id, Tool.name.in_(MEDIA_AI_NAMES),
        ))).all()
        for assignment, tool in assignments:
            override = decrypt_sensitive_fields(assignment.config or {})
            if not any(key in override for key in LEGACY_KEYS):
                continue
            effective = {**original, **override}
            if not effective.get("api_key"):
                continue
            assignment.config = await materialize_legacy_config(db, setting.tenant_id, effective)
            count += 1
        if original.get("api_key"):
            setting.value = {**dict(setting.value or {}), "config": await materialize_legacy_config(
                db, setting.tenant_id, original,
            )}
            count += 1
        elif any(key in original for key in LEGACY_KEYS):
            setting.value = {**dict(setting.value or {}), "config": {
                key: value for key, value in original.items() if key not in LEGACY_KEYS
            }}
            count += 1
    return count
