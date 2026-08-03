"""Shared temporary chat-model selection for Web and external IM channels."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.llm import LLMModel
from app.services.llm.runtime_model import RuntimeLLMModel

MODEL_SESSION_CONFIG_KEY = "model_id"

MODEL_STATUS_OK = "ok"
MODEL_STATUS_NOT_FOUND = "not_found"
MODEL_STATUS_DISABLED = "disabled"
MODEL_STATUS_AMBIGUOUS = "ambiguous"

MODEL_OVERRIDE_NONE = "none"
MODEL_OVERRIDE_OK = "ok"
MODEL_OVERRIDE_INVALID = "invalid"
MODEL_OVERRIDE_UNAVAILABLE = "unavailable"
MODEL_OVERRIDE_DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class ModelNameResolution:
    status: str
    model: LLMModel | None = None


@dataclass(frozen=True, slots=True)
class RuntimeModelResolution:
    primary_model: RuntimeLLMModel | None
    fallback_model: RuntimeLLMModel | None
    override_status: str = MODEL_OVERRIDE_NONE


def _normalized_model_name(value: str) -> str:
    return str(value or "").strip().casefold()


async def list_enabled_tenant_models(
    db: AsyncSession,
    tenant_id: uuid.UUID,
) -> list[LLMModel]:
    """Return the saved, enabled model catalog visible to one tenant."""
    result = await db.execute(
        select(LLMModel).where(
            LLMModel.tenant_id == tenant_id,
            LLMModel.enabled.is_(True),
        )
    )
    return sorted(
        result.scalars().all(),
        key=lambda model: (_normalized_model_name(model.model), str(model.id)),
    )


async def resolve_tenant_model_by_name(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    model_name: str,
) -> ModelNameResolution:
    """Resolve an exact saved model name without exposing internal IDs."""
    normalized = _normalized_model_name(model_name)
    if not normalized:
        return ModelNameResolution(MODEL_STATUS_NOT_FOUND)

    result = await db.execute(select(LLMModel).where(LLMModel.tenant_id == tenant_id))
    matches = [
        model
        for model in result.scalars().all()
        if _normalized_model_name(model.model) == normalized
    ]
    if not matches:
        return ModelNameResolution(MODEL_STATUS_NOT_FOUND)
    if len(matches) > 1:
        return ModelNameResolution(MODEL_STATUS_AMBIGUOUS)
    if not matches[0].enabled:
        return ModelNameResolution(MODEL_STATUS_DISABLED, matches[0])
    return ModelNameResolution(MODEL_STATUS_OK, matches[0])


async def resolve_runtime_models(
    db: AsyncSession,
    *,
    agent: Agent,
    override_model_id: str | uuid.UUID | None = None,
) -> RuntimeModelResolution:
    """Resolve the effective primary/fallback pair used by both Web and IM."""

    async def _load_enabled(model_id: uuid.UUID | None) -> LLMModel | None:
        if model_id is None:
            return None
        model = (await db.execute(select(LLMModel).where(LLMModel.id == model_id))).scalar_one_or_none()
        return model if model is not None and model.enabled else None

    primary_orm = await _load_enabled(agent.primary_model_id)
    fallback_orm = await _load_enabled(agent.fallback_model_id)
    if primary_orm is None and fallback_orm is not None:
        primary_orm = fallback_orm
        fallback_orm = None

    primary = RuntimeLLMModel.from_orm(primary_orm) if primary_orm is not None else None
    fallback = RuntimeLLMModel.from_orm(fallback_orm) if fallback_orm is not None else None

    if not override_model_id:
        return RuntimeModelResolution(primary, fallback)

    try:
        requested_id = (
            override_model_id if isinstance(override_model_id, uuid.UUID) else uuid.UUID(str(override_model_id))
        )
    except (TypeError, ValueError):
        return RuntimeModelResolution(primary, fallback, MODEL_OVERRIDE_INVALID)

    override = (await db.execute(select(LLMModel).where(LLMModel.id == requested_id))).scalar_one_or_none()
    agent_tenant_id = getattr(agent, "tenant_id", None)
    if override is None or agent_tenant_id is None or override.tenant_id != agent_tenant_id:
        return RuntimeModelResolution(primary, fallback, MODEL_OVERRIDE_UNAVAILABLE)
    if not override.enabled:
        return RuntimeModelResolution(primary, fallback, MODEL_OVERRIDE_DISABLED)

    effective = RuntimeLLMModel.from_orm(override)
    if fallback is not None and fallback.id == effective.id:
        fallback = None
    return RuntimeModelResolution(effective, fallback, MODEL_OVERRIDE_OK)


async def load_turn_model_id(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    session_id: str,
    turn_anchor_id: uuid.UUID | None,
) -> str | None:
    """Read the exact model selection snapshotted on a durable user turn."""
    if turn_anchor_id is None:
        return None
    get_row = getattr(db, "get", None)
    if not callable(get_row):
        return None
    anchor = await get_row(ChatMessage, turn_anchor_id)
    if anchor is None or anchor.agent_id != agent_id or anchor.conversation_id != str(session_id):
        return None
    meta = anchor.message_meta if isinstance(anchor.message_meta, dict) else {}
    model_id = str(meta.get("model_id") or "")
    try:
        return str(uuid.UUID(model_id)) if model_id else None
    except ValueError:
        return None
