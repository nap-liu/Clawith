"""Authorization and visibility checks for managed CLI tools."""

from __future__ import annotations

from typing import Optional

from fastapi import HTTPException, status

from app.models.tool import Tool
from app.models.user import User


def _require_manage(user: User, tool: Optional[Tool] = None) -> None:
    """org_admin of the tool's tenant, or platform_admin anywhere."""
    if user.role == "platform_admin":
        return
    if user.role != "org_admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "org_admin required")
    if tool is not None:
        if tool.tenant_id is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "only platform_admin may manage global tools")
        if tool.tenant_id != user.tenant_id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "tool belongs to another tenant")


def _visible(user: User, tool: Tool) -> bool:
    """Scope check: user's own tenant + global."""
    if user.role == "platform_admin":
        return True
    return tool.tenant_id is None or tool.tenant_id == user.tenant_id
