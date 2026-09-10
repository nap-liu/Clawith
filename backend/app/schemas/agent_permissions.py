"""Normalized Agent grants and compatibility input for existing clients."""

import uuid
from typing import Literal

from app.services.llm.failure_outcome import render_message
from pydantic import BaseModel, ConfigDict, Field, model_validator


class AgentGrant(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    scope_type: Literal["company", "department", "user"]
    scope_id: uuid.UUID | None = None
    access_level: Literal["use", "manage"] = "use"

    @model_validator(mode="after")
    def validate_subject(self):
        if (self.scope_type == "company") != (self.scope_id is None):
            raise ValueError(render_message("agentPermissions.invalidSubject"))
        return self


class AgentPermissionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grants: list[AgentGrant] | None = Field(default=None, max_length=2000)
    scope_type: Literal["company", "user", "private", "custom"] | None = None
    scope_ids: list[uuid.UUID] | None = None
    access_level: Literal["use", "manage"] | None = None
    user_access: list[dict] | None = None
    department_access: list[dict] | None = None

    @model_validator(mode="after")
    def one_contract(self):
        if self.model_fields_set == {"grants"} and self.grants is None:
            raise ValueError(render_message("agentPermissions.grantList"))
        if self.grants is not None and self.model_fields_set - {"grants"}:
            raise ValueError(render_message("agentPermissions.oneContract"))
        if not self.model_fields_set:
            raise ValueError(render_message("agentPermissions.updateRequired"))
        return self


def legacy_grants(data: AgentPermissionUpdate, current: list[AgentGrant]) -> list[AgentGrant]:
    """Omitted rosters survive company changes; private explicitly clears all."""
    if data.grants is not None:
        return data.grants
    if data.scope_type in ("user", "private"):
        return []
    grants = list(current)
    if data.scope_type is not None or data.access_level is not None:
        company = next((g for g in current if g.scope_type == "company"), None)
        grants = [g for g in grants if g.scope_type != "company"]
        if data.scope_type == "company" or (data.scope_type is None and company):
            grants.append(AgentGrant(
                scope_type="company",
                access_level=data.access_level or (company.access_level if company else "use"),
            ))
    for kind, items in (("user", data.user_access), ("department", data.department_access)):
        if items is None and not (kind == "user" and data.scope_ids is not None):
            continue
        grants = [g for g in grants if g.scope_type != kind]
        for item in items or []:
            if item.get("is_required"):
                continue
            grants.append(AgentGrant(
                scope_type=kind,
                scope_id=item.get("id") or item.get(f"{kind}_id"),
                access_level=item.get("access_level", "use"),
            ))
        if kind == "user":
            grants.extend(AgentGrant(
                scope_type="user", scope_id=sid, access_level=data.access_level or "use",
            ) for sid in data.scope_ids or [])
    return grants
