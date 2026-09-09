"""Media defaults reuse the existing tenant tool configuration."""

import uuid

from fastapi import Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.enterprise_api_shared import router, _assert_tenant_scope
from app.core.security import get_current_admin, get_current_user
from app.database import get_db
from app.models.llm import LLMModel
from app.models.tenant import Tenant
from app.models.user import User
from app.services.agent_tools_config_runtime import invalidate_tool_config_cache
from app.services.model_capabilities import MEDIA_MODEL_DEFAULTS, supports_purpose
from app.services.llm.failure_outcome import render_message
from app.services.tool_config import get_tenant_tool_config, set_tenant_tool_config


class MediaModelDefaults(BaseModel):
    understanding_model_id: uuid.UUID | None = None
    image_model_id: uuid.UUID | None = None
    audio_model_id: uuid.UUID | None = None
    video_model_id: uuid.UUID | None = None
    speech_model_id: uuid.UUID | None = None


async def clear_media_model_defaults(db: AsyncSession, model: LLMModel) -> None:
    """Deleting a pool entry also removes its company default references."""
    if model.tenant_id is None:
        return
    await db.get(Tenant, model.tenant_id, with_for_update=True)
    config = await get_tenant_tool_config(db, model.tenant_id, "media_ai")
    fields = [key for key in MEDIA_MODEL_DEFAULTS if config.get(key) == str(model.id)]
    if fields:
        config.update({key: None for key in fields})
        await set_tenant_tool_config(db, model.tenant_id, "media_ai", config)
        invalidate_tool_config_cache(None, "generate_media")


def _tenant(current_user, tenant_id):
    target = tenant_id or current_user.tenant_id
    if target is None:
        raise HTTPException(status_code=422, detail=render_message("modelPool.tenantRequired"))
    _assert_tenant_scope(current_user, target)
    return target


@router.get("/media-model-defaults", response_model=MediaModelDefaults)
async def get_media_model_defaults(
    tenant_id: uuid.UUID | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    target = _tenant(current_user, tenant_id)
    from app.services.speech_model_selection import migrate_speech_config

    await migrate_speech_config(db, target)
    await db.commit()
    config = await get_tenant_tool_config(db, target, "media_ai")
    return {key: config.get(key) for key in MEDIA_MODEL_DEFAULTS}


@router.put("/media-model-defaults", response_model=MediaModelDefaults)
async def update_media_model_defaults(
    data: MediaModelDefaults,
    tenant_id: uuid.UUID | None = None,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    target = _tenant(current_user, tenant_id)
    tenant = await db.get(Tenant, target, with_for_update=True)
    if tenant is None:
        raise HTTPException(status_code=404, detail=render_message("modelPool.tenantNotFound"))
    changes = data.model_dump(exclude_unset=True)
    for key, model_id in changes.items():
        if model_id is None:
            continue
        model = await db.get(LLMModel, model_id)
        if model is None or model.tenant_id != target or not model.enabled or not supports_purpose(model, MEDIA_MODEL_DEFAULTS[key]):
            raise HTTPException(status_code=422, detail=render_message("mediaAI.modelUnavailable"))
    config = await get_tenant_tool_config(db, target, "media_ai")
    if "speech_model_id" in changes:
        # Explicit selection/clearing wins even before the first legacy migration.
        # Generic tool config storage omits null values, so retain that intent.
        from app.services.speech_model_selection import MIGRATED_KEY

        config[MIGRATED_KEY] = True
    config.update({key: str(value) if value else None for key, value in changes.items()})
    await set_tenant_tool_config(db, target, "media_ai", config)
    await db.commit()
    invalidate_tool_config_cache(None, "generate_media")
    return {key: config.get(key) for key in MEDIA_MODEL_DEFAULTS}
