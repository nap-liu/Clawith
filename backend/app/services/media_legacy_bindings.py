"""Tenant-local migration receipts, never a second model configuration store."""

import hashlib
import json

from sqlalchemy import select

from app.models.tenant import Tenant
from app.models.tenant_setting import TenantSetting
from app.models.llm import LLMModel
from app.models.tool import Tool

BINDINGS_KEY = "media_legacy_model_bindings"


def config_reference(config: dict) -> str:
    """Hash the original reference, excluding any later inherited configuration."""
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def locked_bindings(db, tenant_id):
    await db.scalar(select(Tenant.id).where(Tenant.id == tenant_id).with_for_update())
    row = await db.get(TenantSetting, (tenant_id, BINDINGS_KEY), populate_existing=True)
    if row is None:
        company = await db.get(TenantSetting, (tenant_id, "tool_config:media_ai"))
        config = ((company.value or {}).get("config") or {}) if company else {}
        converted = any(config.get(f"{kind}_model_id") for kind in ("understanding", "image", "audio", "video"))
        retired = await db.scalar(select(Tool.id).where(Tool.name == "read_image", Tool.source == "legacy"))
        existing = await db.scalar(select(LLMModel.id).where(LLMModel.tenant_id == tenant_id).limit(1))
        row = TenantSetting(tenant_id=tenant_id, key=BINDINGS_KEY, value={
            "connections_unrecorded": bool(converted or (retired and existing)),
        })
        db.add(row)
        await db.flush()
    return row
