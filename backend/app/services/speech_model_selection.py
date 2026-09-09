"""Speech input uses the enterprise model pool and its tenant default."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.config import get_settings
from app.models.llm import LLMModel
from app.models.speech_recognition_config import SpeechRecognitionConfig
from app.models.tenant import Tenant
from app.services.llm.failure_outcome import render_message
from app.services.model_capabilities import supports_purpose
from app.services.tool_config import get_tenant_tool_config, set_tenant_tool_config

MIGRATED_KEY = "legacy_speech_config_migrated"


class SpeechCredentialUnavailable(RuntimeError):
    """The selected enterprise speech model cannot be used by this tenant."""


async def migrate_speech_config(db, tenant_id: uuid.UUID) -> bool:
    config = await get_tenant_tool_config(db, tenant_id, "media_ai")
    if config.get(MIGRATED_KEY):
        return False
    tenant = await db.get(Tenant, tenant_id, with_for_update=True)
    if tenant is None:
        return False
    # Re-read after locking: concurrent startup and first-use must not duplicate.
    config = await get_tenant_tool_config(db, tenant_id, "media_ai")
    if config.get(MIGRATED_KEY):
        return False
    legacy = await db.scalar(select(SpeechRecognitionConfig).where(
        SpeechRecognitionConfig.tenant_id == tenant_id,
    ))
    if legacy is not None and "speech_model_id" not in config:
        model_id = uuid.uuid5(legacy.id, "enterprise-speech-model")
        model = await db.get(LLMModel, model_id)
        if model is None:
            model = LLMModel(
                id=model_id, tenant_id=tenant_id,
                provider="qwen" if legacy.provider == "aliyun_dashscope" else legacy.provider,
                model=legacy.model, label=legacy.model,
                api_key_encrypted=legacy.api_key_encrypted,
                base_url=get_settings().ASR_WEBSOCKET_URL,
                api_protocol="openai_compatible", purposes=["speech_recognition"],
                input_modalities=["audio"], enabled=legacy.enabled,
            )
            db.add(model)
            await db.flush()
        config["speech_model_id"] = str(model.id)
    config[MIGRATED_KEY] = True
    await set_tenant_tool_config(db, tenant_id, "media_ai", config)
    return True


async def migrate_legacy_speech_configs(db) -> int:
    tenants = (await db.scalars(select(SpeechRecognitionConfig.tenant_id))).all()
    count = 0
    for tenant_id in tenants:
        count += int(await migrate_speech_config(db, tenant_id))
    return count


async def resolve_speech_model(db, tenant_id, model_id=None, *, require_enabled=True):
    if tenant_id is None:
        raise SpeechCredentialUnavailable(render_message("speech.tenantRequired"))
    await migrate_speech_config(db, tenant_id)
    if model_id is None:
        config = await get_tenant_tool_config(db, tenant_id, "media_ai")
        model_id = config.get("speech_model_id")
    if not model_id:
        raise SpeechCredentialUnavailable(render_message("speech.notConfigured"))
    try:
        model = await db.get(LLMModel, uuid.UUID(str(model_id)))
    except (TypeError, ValueError) as exc:
        raise SpeechCredentialUnavailable(render_message("speech.modelUnavailable")) from exc
    if (model is None or model.tenant_id != tenant_id
            or not supports_purpose(model, "speech_recognition")
            or (require_enabled and not model.enabled)):
        raise SpeechCredentialUnavailable(render_message("speech.modelUnavailable"))
    return model
