"""Shared temporary chat-model selection for Web and external IM channels."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.llm import LLMModel
from app.models.tenant import Tenant
from app.services.llm.runtime_model import RuntimeLLMModel
from app.services.llm.reasoning import validate_reasoning_effort

MODEL_SESSION_CONFIG_KEY = "model_id"
REASONING_SESSION_CONFIG_KEY = "reasoning_effort"

MODEL_STATUS_OK = "ok"
MODEL_STATUS_NOT_FOUND = "not_found"
MODEL_STATUS_DISABLED = "disabled"
MODEL_STATUS_AMBIGUOUS = "ambiguous"

MODEL_OVERRIDE_NONE = "none"
MODEL_OVERRIDE_OK = "ok"
MODEL_OVERRIDE_INVALID = "invalid"
MODEL_OVERRIDE_UNAVAILABLE = "unavailable"
MODEL_OVERRIDE_DISABLED = "disabled"


class BackgroundModelUnavailableError(RuntimeError):
    """A persisted background model selection cannot execute safely."""


@dataclass(frozen=True, slots=True)
class ModelNameResolution:
    status: str
    model: LLMModel | None = None


@dataclass(frozen=True, slots=True)
class RuntimeModelResolution:
    primary_model: RuntimeLLMModel | None
    fallback_model: RuntimeLLMModel | None
    override_status: str = MODEL_OVERRIDE_NONE


def validate_temperature(value: float | None) -> float | None:
    if value is None:
        return None
    normalized = float(value)
    if normalized < 0 or normalized > 2:
        raise ValueError("temperature must be between 0 and 2")
    return normalized


def _stored_reasoning_effort(value: object | None) -> str | None:
    """Read nullable persisted effort without treating dynamic attributes as data."""
    if not isinstance(value, str):
        return None
    return validate_reasoning_effort(value)


def _runtime_snapshot(
    model: LLMModel | None,
    *,
    agent: Agent,
    override_temperature: float | None = None,
    override_reasoning_effort: str | None = None,
) -> RuntimeLLMModel | None:
    if model is None:
        return None
    snapshot = RuntimeLLMModel.from_orm(model)
    effective_temperature = (
        validate_temperature(override_temperature)
        if override_temperature is not None
        else validate_temperature(getattr(agent, "temperature", None))
    )
    effective_reasoning_effort = (
        validate_reasoning_effort(override_reasoning_effort)
        if override_reasoning_effort is not None
        else _stored_reasoning_effort(getattr(agent, "reasoning_effort", None))
    )
    changes = {}
    if effective_temperature is not None:
        changes["temperature"] = effective_temperature
    if effective_reasoning_effort is not None:
        changes["reasoning_effort"] = effective_reasoning_effort
    return replace(snapshot, **changes) if changes else snapshot


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


async def resolve_tenant_model_reference(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    reference: str | uuid.UUID,
) -> ModelNameResolution:
    """Resolve an enabled model by UUID, model key, or unique display label."""
    raw = str(reference or "").strip()
    if not raw:
        return ModelNameResolution(MODEL_STATUS_NOT_FOUND)
    visible = (
        (
            await db.execute(
                select(LLMModel).where(
                    or_(LLMModel.tenant_id == tenant_id, LLMModel.tenant_id.is_(None))
                )
            )
        )
        .scalars()
        .all()
    )
    try:
        requested_id = uuid.UUID(raw)
    except ValueError:
        requested_id = None
    if requested_id is not None:
        matches = [model for model in visible if model.id == requested_id]
    else:
        normalized = _normalized_model_name(raw)
        matches = [
            model
            for model in visible
            if normalized in {
                _normalized_model_name(model.model),
                _normalized_model_name(model.label),
            }
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
    override_temperature: float | None = None,
    override_reasoning_effort: str | None = None,
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

    primary = _runtime_snapshot(
        primary_orm,
        agent=agent,
        override_temperature=override_temperature,
        override_reasoning_effort=override_reasoning_effort,
    )
    fallback = _runtime_snapshot(
        fallback_orm,
        agent=agent,
        override_temperature=override_temperature,
        override_reasoning_effort=override_reasoning_effort,
    )

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
    if override is None or agent_tenant_id is None or override.tenant_id not in {agent_tenant_id, None}:
        return RuntimeModelResolution(primary, fallback, MODEL_OVERRIDE_UNAVAILABLE)
    if not override.enabled:
        return RuntimeModelResolution(primary, fallback, MODEL_OVERRIDE_DISABLED)

    effective = _runtime_snapshot(
        override,
        agent=agent,
        override_temperature=override_temperature,
        override_reasoning_effort=override_reasoning_effort,
    )
    if fallback is not None and fallback.id == effective.id:
        fallback = None
    return RuntimeModelResolution(effective, fallback, MODEL_OVERRIDE_OK)


async def validate_agent_model_override(
    db: AsyncSession,
    *,
    agent: Agent,
    model_id: uuid.UUID | None,
) -> None:
    if model_id is None:
        return
    resolved = await resolve_runtime_models(db, agent=agent, override_model_id=model_id)
    if resolved.override_status != MODEL_OVERRIDE_OK:
        raise ValueError("model override is unavailable for this Agent")


async def resolve_project_runtime_models(
    db: AsyncSession,
    *,
    agent: Agent,
    project_settings: dict | None,
    override_temperature: float | None = None,
    override_reasoning_effort: str | None = None,
) -> RuntimeModelResolution:
    """Resolve one project turn without crossing the tenant model boundary.

    Project members may intentionally omit an Agent-level model.  Native
    project execution then falls back to the project setting and finally the
    tenant catalog, while retaining the ordinary Agent primary/fallback
    precedence.  This returns runtime snapshots of existing tenant records; it
    never copies provider credentials into project state.
    """

    tenant_id = getattr(agent, "tenant_id", None)
    if tenant_id is None:
        return RuntimeModelResolution(None, None)

    enabled_models = await list_enabled_tenant_models(db, tenant_id)
    by_id = {model.id: model for model in enabled_models}

    primary_orm = by_id.get(agent.primary_model_id)
    fallback_orm = by_id.get(agent.fallback_model_id)
    if primary_orm is None and fallback_orm is not None:
        primary_orm, fallback_orm = fallback_orm, None
    if primary_orm is not None:
        return RuntimeModelResolution(
            _runtime_snapshot(primary_orm, agent=agent, override_temperature=override_temperature, override_reasoning_effort=override_reasoning_effort),
            _runtime_snapshot(fallback_orm, agent=agent, override_temperature=override_temperature, override_reasoning_effort=override_reasoning_effort),
        )

    runtime_settings = dict(dict(project_settings or {}).get("runtime") or {})
    configured = str(
        runtime_settings.get("model") or runtime_settings.get("default_model") or ""
    ).strip()
    project_model = None
    if configured and configured.casefold() != "default":
        try:
            project_model = by_id.get(uuid.UUID(configured))
        except (TypeError, ValueError):
            named = [
                model
                for model in enabled_models
                if _normalized_model_name(model.model) == _normalized_model_name(configured)
            ]
            if len(named) == 1:
                project_model = named[0]
    if project_model is not None:
        return RuntimeModelResolution(
            _runtime_snapshot(project_model, agent=agent, override_temperature=override_temperature, override_reasoning_effort=override_reasoning_effort),
            None,
        )

    tenant = await db.get(Tenant, tenant_id)
    tenant_default = by_id.get(tenant.default_model_id) if tenant is not None else None
    selected = tenant_default or (enabled_models[0] if enabled_models else None)
    return RuntimeModelResolution(
        _runtime_snapshot(selected, agent=agent, override_temperature=override_temperature, override_reasoning_effort=override_reasoning_effort),
        None,
    )


async def resolve_project_member_runtime_models(
    db: AsyncSession,
    *,
    agent: Agent,
    member_config: dict | None,
    project_settings: dict | None,
    override_temperature: float | None = None,
    override_reasoning_effort: str | None = None,
) -> RuntimeModelResolution:
    """Resolve models only from the frozen member config and project defaults."""

    tenant_id = getattr(agent, "tenant_id", None)
    if tenant_id is None:
        return RuntimeModelResolution(None, None)
    enabled_models = (
        (
            await db.execute(
                select(LLMModel).where(
                    or_(LLMModel.tenant_id == tenant_id, LLMModel.tenant_id.is_(None)),
                    LLMModel.enabled.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    by_id = {model.id: model for model in enabled_models}
    config = dict(member_config or {})

    def _configured_model(key: str) -> LLMModel | None:
        raw = config.get(key)
        if not raw:
            return None
        try:
            return by_id.get(uuid.UUID(str(raw)))
        except (TypeError, ValueError):
            return None

    primary_orm = _configured_model("primary_model_id")
    fallback_orm = _configured_model("fallback_model_id")
    if primary_orm is None and fallback_orm is not None:
        primary_orm, fallback_orm = fallback_orm, None
    if primary_orm is not None:
        return RuntimeModelResolution(
            _runtime_snapshot(primary_orm, agent=agent, override_temperature=override_temperature, override_reasoning_effort=override_reasoning_effort),
            _runtime_snapshot(fallback_orm, agent=agent, override_temperature=override_temperature, override_reasoning_effort=override_reasoning_effort),
        )

    # An intentionally empty member override uses project/tenant defaults, but
    # never re-reads the mutable source Agent model selection.
    runtime_settings = dict(dict(project_settings or {}).get("runtime") or {})
    configured = str(runtime_settings.get("model") or runtime_settings.get("default_model") or "").strip()
    project_model = None
    if configured and configured.casefold() != "default":
        try:
            project_model = by_id.get(uuid.UUID(configured))
        except (TypeError, ValueError):
            named = [model for model in enabled_models if _normalized_model_name(model.model) == configured.casefold()]
            if len(named) == 1:
                project_model = named[0]
    if project_model is not None:
        return RuntimeModelResolution(
            _runtime_snapshot(project_model, agent=agent, override_temperature=override_temperature, override_reasoning_effort=override_reasoning_effort),
            None,
        )

    tenant = await db.get(Tenant, tenant_id)
    tenant_default = by_id.get(tenant.default_model_id) if tenant is not None else None
    selected = tenant_default or (sorted(enabled_models, key=lambda model: str(model.id))[0] if enabled_models else None)
    return RuntimeModelResolution(
        _runtime_snapshot(selected, agent=agent, override_temperature=override_temperature, override_reasoning_effort=override_reasoning_effort),
        None,
    )


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


async def load_turn_reasoning_effort(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    session_id: str,
    turn_anchor_id: uuid.UUID | None,
) -> str | None:
    """Read the reasoning choice snapshotted on one accepted user turn."""
    if turn_anchor_id is None:
        return None
    get_row = getattr(db, "get", None)
    if not callable(get_row):
        return None
    anchor = await get_row(ChatMessage, turn_anchor_id)
    if anchor is None or anchor.agent_id != agent_id or anchor.conversation_id != str(session_id):
        return None
    meta = anchor.message_meta if isinstance(anchor.message_meta, dict) else {}
    return validate_reasoning_effort(meta.get("reasoning_effort"))
