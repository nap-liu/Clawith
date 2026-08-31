"""Published page API request models and shared limits."""

import uuid

from pydantic import BaseModel, Field


MAX_BULK_PAGE_ACCESS_UPDATES = 100


class PageSessionRequest(BaseModel):
    short_id: str = Field(..., min_length=1, max_length=16)


class PageAccessUpdate(BaseModel):
    access_mode: str
    allowed_user_ids: list[uuid.UUID] = Field(default_factory=list)


class PageBulkAccessUpdate(PageAccessUpdate):
    page_ids: list[uuid.UUID] = Field(..., min_length=1, max_length=MAX_BULK_PAGE_ACCESS_UPDATES)


class PageRequestResolution(BaseModel):
    status: str
