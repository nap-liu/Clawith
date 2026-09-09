"""Validated updates for ordinary Digital Employee settings."""
from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, or_, select

from app.models.agent import Agent
from app.models.llm import LLMModel
from app.services.model_capabilities import purpose_clause
from app.models.participant import Participant
from app.models.tenant import Tenant
from app.services.llm.reasoning import ReasoningEffort


class AgentSettingsPatch(BaseModel):
    """Only ordinary settings; security, approval and lifecycle fields are excluded."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=100)
    avatar_url: str | None = Field(
        default=None,
        max_length=500,
        description=(
            "Avatar image URL. Use a publicly accessible http(s) URL, or for a file in this "
            "Digital Employee's workspace use the relative managed path "
            "'/api/agents/{agent_id}/files/download?path=workspace/...'. Never prepend the "
            "platform scheme and host to a managed /api/ path."
        ),
    )
    role_description: str | None = Field(default=None, max_length=500)
    bio: str | None = None
    welcome_message: str | None = None
    primary_model: str | None = Field(
        default=None,
        description="Model UUID or unique display label; null inherits the company default.",
    )
    fallback_model: str | None = Field(
        default=None,
        description="Fallback model UUID or unique display label; null disables the override.",
    )
    imagination: float | None = Field(
        default=None,
        ge=0,
        le=2,
        description="Imagination from 0 (stable) to 2 (rich); null inherits the model default.",
    )
    reasoning_effort: ReasoningEffort | None = Field(
        default=None,
        description="Reasoning level; none disables it and null inherits the model default.",
    )
    context_window_size: int | None = Field(
        default=None, ge=10, le=500, description="Conversation context rounds."
    )
    daily_memory_load_days: int | None = Field(
        default=None,
        ge=0,
        le=30,
        description="Recent Daily Memory days to load; 0 disables Daily Memory only.",
    )
    max_tool_rounds: int | None = Field(
        default=None, ge=5, le=200, description="Maximum tool-call rounds per message."
    )
    max_tokens_per_day: int | None = Field(default=None, ge=0)
    max_tokens_per_month: int | None = Field(default=None, ge=0)
    max_triggers: int | None = Field(default=None, ge=1, le=100)
    min_poll_interval_min: int | None = Field(default=None, ge=1, le=60)
    webhook_rate_limit: int | None = Field(default=None, ge=1, le=60)
    im_thinking_output_enabled: bool | None = Field(
        default=None,
        description=(
            "Show public Agent-authored tool-round progress in external IM. "
            "This never exposes raw provider reasoning."
        ),
    )
    timezone: str | None = Field(default=None, max_length=50)
    heartbeat_enabled: bool | None = None
    heartbeat_interval_minutes: int | None = Field(default=None, ge=1, le=10080)
    heartbeat_active_hours: str | None = Field(default=None, max_length=20)

    @field_validator("avatar_url")
    @classmethod
    def validate_avatar_url(cls, value: str | None) -> str | None:
        if value in (None, ""):
            return None
        parsed = urlsplit(value)
        if value.startswith("/api/"):
            return value
        if parsed.scheme in {"http", "https"}:
            public_origin = urlsplit(os.environ.get("PUBLIC_BASE_URL", ""))
            is_platform_origin = bool(
                public_origin.scheme in {"http", "https"}
                and parsed.scheme == public_origin.scheme
                and parsed.netloc == public_origin.netloc
            )
            if (
                is_platform_origin
                and parsed.path.startswith("/api/agents/")
                and "/files/download" in parsed.path
            ):
                raise ValueError(
                    "managed Agent file avatars must use the relative /api/agents/.../files/download "
                    "path; remove the scheme and host"
                )
            return value
        raise ValueError("avatar_url must be an http(s) URL or a managed /api/ path")

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str | None) -> str | None:
        if value in (None, ""):
            return None
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        return value

    @field_validator("heartbeat_active_hours")
    @classmethod
    def validate_active_hours(cls, value: str | None) -> str | None:
        if value is None:
            return None
        match = re.fullmatch(r"(\d{2}):(\d{2})-(\d{2}):(\d{2})", value)
        if not match:
            raise ValueError("heartbeat_active_hours must use HH:MM-HH:MM")
        start_h, start_m, end_h, end_m = map(int, match.groups())
        if start_h > 23 or end_h > 23 or start_m > 59 or end_m > 59:
            raise ValueError("heartbeat_active_hours contains an invalid time")
        return value

    @model_validator(mode="after")
    def reject_null_for_required_columns(self):
        required_values = {
            "name": self.name,
            "role_description": self.role_description,
            "context_window_size": self.context_window_size,
            "daily_memory_load_days": self.daily_memory_load_days,
            "max_tool_rounds": self.max_tool_rounds,
            "max_triggers": self.max_triggers,
            "min_poll_interval_min": self.min_poll_interval_min,
            "webhook_rate_limit": self.webhook_rate_limit,
            "im_thinking_output_enabled": self.im_thinking_output_enabled,
            "heartbeat_enabled": self.heartbeat_enabled,
            "heartbeat_interval_minutes": self.heartbeat_interval_minutes,
            "heartbeat_active_hours": self.heartbeat_active_hours,
        }
        null_fields = [
            field for field, value in required_values.items()
            if field in self.model_fields_set and value is None
        ]
        if null_fields:
            raise ValueError(f"settings cannot be null: {', '.join(sorted(null_fields))}")
        return self


@dataclass(frozen=True)
class AgentSettingsUpdateResult:
    changes: list[tuple[str, object, object]]
    clamps: list[dict]


async def _resolve_model_id(db, tenant_id: uuid.UUID | None, ref: str) -> uuid.UUID:
    base = select(LLMModel).where(
        LLMModel.enabled.is_(True),
        purpose_clause(),
        or_(LLMModel.tenant_id == tenant_id, LLMModel.tenant_id.is_(None)),
    )
    try:
        model_id = uuid.UUID(str(ref))
    except (TypeError, ValueError):
        model_id = None
    if model_id is not None:
        model = (await db.execute(base.where(LLMModel.id == model_id))).scalar_one_or_none()
        if model is None:
            raise ValueError("model is unavailable")
        return model.id

    models = (
        await db.execute(
            base.where(func.lower(LLMModel.label) == str(ref).strip().lower())
            .order_by(LLMModel.tenant_id.is_(None))
        )
    ).scalars().all()
    if not models:
        raise ValueError("model is unavailable")
    tenant_models = [model for model in models if model.tenant_id == tenant_id]
    candidates = tenant_models or models
    if len(candidates) != 1:
        raise ValueError("model label is ambiguous; use its UUID")
    return candidates[0].id


async def apply_agent_settings_patch(
    db,
    agent: Agent,
    raw_patch: dict,
) -> AgentSettingsUpdateResult:
    """Validate and apply one atomic ordinary-settings patch to ``agent``."""

    patch = AgentSettingsPatch.model_validate(raw_patch)
    requested = patch.model_dump(exclude_unset=True)
    if not requested:
        return AgentSettingsUpdateResult(changes=[], clamps=[])
    if agent.scope != "standard" or agent.agent_type != "native" or agent.is_deleted:
        raise ValueError("only active standard Digital Employees support these settings")

    resolved: dict[str, object] = {}
    for public_name, db_name in (
        ("primary_model", "primary_model_id"),
        ("fallback_model", "fallback_model_id"),
    ):
        if public_name not in requested:
            continue
        ref = requested.pop(public_name)
        resolved[db_name] = None if ref in (None, "") else await _resolve_model_id(db, agent.tenant_id, ref)
    if "imagination" in requested:
        resolved["temperature"] = requested.pop("imagination")
    resolved.update(requested)

    clamps: list[dict] = []
    clamped_fields = {"heartbeat_interval_minutes", "min_poll_interval_min", "webhook_rate_limit"}
    tenant = None
    if clamped_fields & resolved.keys() and agent.tenant_id:
        tenant = (
            await db.execute(select(Tenant).where(Tenant.id == agent.tenant_id))
        ).scalar_one_or_none()
    if tenant and "heartbeat_interval_minutes" in resolved:
        requested_value = resolved["heartbeat_interval_minutes"]
        applied = max(requested_value, tenant.min_heartbeat_interval_minutes)
        if applied != requested_value:
            clamps.append(_clamp("heartbeat_interval_minutes", requested_value, applied, "company_floor"))
            resolved["heartbeat_interval_minutes"] = applied
    if tenant and "min_poll_interval_min" in resolved:
        requested_value = resolved["min_poll_interval_min"]
        applied = max(requested_value, tenant.min_poll_interval_floor)
        if applied != requested_value:
            clamps.append(_clamp("min_poll_interval_min", requested_value, applied, "company_floor"))
            resolved["min_poll_interval_min"] = applied
    if tenant and "webhook_rate_limit" in resolved:
        requested_value = resolved["webhook_rate_limit"]
        applied = min(requested_value, tenant.max_webhook_rate_ceiling)
        if applied != requested_value:
            clamps.append(_clamp("webhook_rate_limit", requested_value, applied, "company_ceiling"))
            resolved["webhook_rate_limit"] = applied

    changes: list[tuple[str, object, object]] = []
    for field, value in resolved.items():
        old_value = getattr(agent, field)
        if old_value != value:
            setattr(agent, field, value)
            changes.append((field, old_value, value))

    changed_fields = {field for field, _, _ in changes}
    if {"name", "avatar_url"} & changed_fields:
        participant = (
            await db.execute(
                select(Participant).where(
                    Participant.type == "agent",
                    Participant.ref_id == agent.id,
                )
            )
        ).scalar_one_or_none()
        if participant:
            if "name" in changed_fields:
                participant.display_name = agent.name
            if "avatar_url" in changed_fields:
                participant.avatar_url = agent.avatar_url
    await db.flush()
    return AgentSettingsUpdateResult(changes=changes, clamps=clamps)


def _clamp(field: str, requested, applied, reason: str) -> dict:
    return {"field": field, "requested": requested, "applied": applied, "reason": reason}


def public_setting_name(db_field: str) -> str:
    return {
        "primary_model_id": "primary_model",
        "fallback_model_id": "fallback_model",
        "temperature": "imagination",
        "reasoning_effort": "reasoning_effort",
    }.get(db_field, db_field)


__all__ = [
    "AgentSettingsPatch",
    "AgentSettingsUpdateResult",
    "apply_agent_settings_patch",
    "public_setting_name",
]
