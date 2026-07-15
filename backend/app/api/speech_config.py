"""Independent tenant configuration API for speech-recognition services."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.security import decrypt_data, encrypt_data, get_current_admin
from app.database import get_db
from app.models.speech_recognition_config import SpeechRecognitionConfig
from app.models.tenant import Tenant
from app.models.user import User
from app.services.speech_recognition import resolve_speech_credentials, verify_speech_credentials

router = APIRouter(prefix="/api/speech-config", tags=["speech-config"])
settings = get_settings()

SUPPORTED_PROVIDER = "aliyun_dashscope"
SUPPORTED_MODEL = "fun-asr-realtime"


class SpeechConfigUpdate(BaseModel):
    provider: str = SUPPORTED_PROVIDER
    model: str = SUPPORTED_MODEL
    api_key: str | None = None
    enabled: bool = True


def _target_tenant_id(tenant_id: str | None, current_user: User) -> uuid.UUID:
    try:
        target = uuid.UUID(tenant_id) if tenant_id else current_user.tenant_id
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid tenant ID") from exc
    if target is None:
        raise HTTPException(status_code=400, detail="Tenant is required")
    is_platform_admin = current_user.role == "platform_admin" or bool(
        getattr(getattr(current_user, "identity", None), "is_platform_admin", False)
    )
    if not is_platform_admin and current_user.tenant_id != target:
        raise HTTPException(status_code=403, detail="Cannot configure another tenant")
    return target


def _masked_key(config: SpeechRecognitionConfig) -> str:
    try:
        key = decrypt_data(config.api_key_encrypted, settings.SECRET_KEY)
    except ValueError:
        key = config.api_key_encrypted
    return f"****{key[-4:]}" if len(key) > 4 else "****"


def _config_response(config: SpeechRecognitionConfig | None) -> dict:
    if config is None:
        return {
            "configured": False,
            "provider": SUPPORTED_PROVIDER,
            "model": SUPPORTED_MODEL,
            "api_key_masked": "",
            "enabled": False,
        }
    return {
        "configured": True,
        "provider": config.provider,
        "model": config.model,
        "api_key_masked": _masked_key(config),
        "enabled": config.enabled,
    }


async def _ensure_tenant_exists(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    if await db.scalar(select(Tenant.id).where(Tenant.id == tenant_id)) is None:
        raise HTTPException(status_code=404, detail="Tenant not found")


@router.get("")
async def get_speech_config(
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    target = _target_tenant_id(tenant_id, current_user)
    await _ensure_tenant_exists(db, target)
    config = (
        await db.execute(select(SpeechRecognitionConfig).where(SpeechRecognitionConfig.tenant_id == target))
    ).scalar_one_or_none()
    return _config_response(config)


@router.put("")
async def update_speech_config(
    data: SpeechConfigUpdate,
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    target = _target_tenant_id(tenant_id, current_user)
    if data.provider != SUPPORTED_PROVIDER or data.model != SUPPORTED_MODEL:
        raise HTTPException(status_code=400, detail="当前仅支持阿里云百炼 fun-asr-realtime")
    await _ensure_tenant_exists(db, target)

    config = (
        await db.execute(select(SpeechRecognitionConfig).where(SpeechRecognitionConfig.tenant_id == target))
    ).scalar_one_or_none()
    api_key = (data.api_key or "").strip()
    if config is None:
        if not api_key:
            raise HTTPException(status_code=400, detail="首次配置必须填写语音识别 API Key")
        config = SpeechRecognitionConfig(
            tenant_id=target,
            provider=data.provider,
            model=data.model,
            api_key_encrypted=encrypt_data(api_key, settings.SECRET_KEY),
            enabled=data.enabled,
        )
        db.add(config)
    else:
        config.provider = data.provider
        config.model = data.model
        config.enabled = data.enabled
        if api_key and not api_key.startswith("****"):
            config.api_key_encrypted = encrypt_data(api_key, settings.SECRET_KEY)
    await db.flush()
    return _config_response(config)


@router.post("/test")
async def test_speech_config(
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    target = _target_tenant_id(tenant_id, current_user)
    try:
        credentials = await resolve_speech_credentials(db, target)
        await verify_speech_credentials(credentials)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"语音识别连接测试失败：{str(exc)[:240]}") from exc
    return {"success": True}
