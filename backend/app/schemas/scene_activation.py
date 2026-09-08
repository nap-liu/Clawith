"""Management-only references for automatic scene activation."""

from pydantic import BaseModel, ConfigDict, Field


class SceneTargetSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_ref: str = Field(min_length=1, max_length=2048)
    label: str = Field(default="", max_length=200)
    source_channel: str = Field(default="", max_length=40)
    is_group: bool = False


class SceneAutoActivation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    targets: list[SceneTargetSelection] = Field(default_factory=list, max_length=100)
