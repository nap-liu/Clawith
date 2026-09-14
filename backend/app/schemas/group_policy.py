"""Group-owned member access rules."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class GroupMemberRule(BaseModel):
    id: UUID
    name: str = Field(min_length=1, max_length=80)
    effect: Literal["allow", "deny"]
    enabled: bool = True
    all_members: bool = False
    member_ids: list[UUID] = Field(default_factory=list, max_length=1000)
    user_ids: list[UUID] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def validate_members(self):
        self.name = self.name.strip()
        if not self.name or (not self.all_members and not self.member_ids and not self.user_ids):
            raise ValueError("groupPolicy.invalidRule")
        if self.all_members and (self.member_ids or self.user_ids):
            raise ValueError("groupPolicy.invalidRule")
        return self


class GroupPolicyWrite(BaseModel):
    target_ref: str = Field(default="", max_length=4000)
    rules: list[GroupMemberRule] = Field(default_factory=list, max_length=100)
    expected_revision: int = Field(ge=0)

    @model_validator(mode="after")
    def unique_rules(self):
        if len({rule.id for rule in self.rules}) != len(self.rules):
            raise ValueError("groupPolicy.invalidRule")
        return self
