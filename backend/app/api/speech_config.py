"""Compatibility read/test endpoints backed exclusively by the enterprise pool."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.enterprise_routes_model_defaults import _tenant
from app.core.security import get_current_admin
from app.database import get_db
from app.models.user import User
from app.services.llm.failure_outcome import render_message
from app.services.llm.utils import get_model_api_key
from app.services.speech_model_selection import SpeechCredentialUnavailable, resolve_speech_model
from app.services.speech_recognition import resolve_speech_credentials, verify_speech_credentials

router = APIRouter(prefix="/api/speech-config", tags=["speech-config"])


@router.get("")
async def get_speech_config(
    tenant_id: uuid.UUID | None = None,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    target = _tenant(current_user, tenant_id)
    try:
        model = await resolve_speech_model(db, target, require_enabled=False)
    except SpeechCredentialUnavailable:
        await db.commit()
        return {"configured": False, "provider": "", "model": "", "model_id": None,
                "api_key_masked": "", "enabled": False}
    key = get_model_api_key(model)
    result = {"configured": True, "provider": model.provider, "model": model.model,
              "model_id": str(model.id), "enabled": model.enabled,
              "api_key_masked": f"****{key[-4:]}" if len(key) > 4 else "****"}
    await db.commit()
    return result


@router.put("")
async def update_speech_config(current_user: User = Depends(get_current_admin)):
    del current_user
    raise HTTPException(410, detail=render_message("speech.configureInModelPool"))


@router.post("/test")
async def test_speech_config(
    tenant_id: uuid.UUID | None = None,
    model_id: uuid.UUID | None = None,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    target = _tenant(current_user, tenant_id)
    try:
        credentials = await resolve_speech_credentials(db, target, model_id)
        await db.commit()
        await verify_speech_credentials(credentials)
    except SpeechCredentialUnavailable as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(400, detail=render_message("speech.testFailed")) from exc
    return {"success": True}
