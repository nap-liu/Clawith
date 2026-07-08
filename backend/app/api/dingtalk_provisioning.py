"""DingTalk automatic channel provisioning API routes."""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access, is_agent_creator
from app.core.security import get_current_user
from app.database import get_db
from app.models.dingtalk_provisioning import (
    DINGTALK_PROVISIONING_ACTIVE_STATUSES,
    DINGTALK_PROVISIONING_STATUS_CANCELLED,
    DingTalkChannelProvisioningSession,
)
from app.models.user import User
from app.services.dingtalk_provisioning import (
    get_dingtalk_provisioning_status_response,
    start_dingtalk_channel_provisioning,
)

router = APIRouter(tags=["dingtalk-provisioning"])

_ADMIN_ROLES = {"platform_admin", "org_admin", "agent_admin"}
_SENSITIVE_RESPONSE_FRAGMENTS = ("secret", "token", "password")


def _can_manage_digital_employee(user: User, agent) -> bool:
    return is_agent_creator(user, agent) or user.role in _ADMIN_ROLES


def _require_manage_access(user: User, agent) -> None:
    if not _can_manage_digital_employee(user, agent):
        raise HTTPException(status_code=403, detail="Only creator or 管理员 can configure DingTalk channel")


def _sanitize_response(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            lower_key = str(key).lower()
            if any(fragment in lower_key for fragment in _SENSITIVE_RESPONSE_FRAGMENTS):
                continue
            cleaned[key] = _sanitize_response(item)
        return cleaned
    if isinstance(value, list):
        return [_sanitize_response(item) for item in value]
    return value


async def _load_session_or_404(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    provisioning_id: uuid.UUID,
) -> DingTalkChannelProvisioningSession:
    result = await db.execute(
        select(DingTalkChannelProvisioningSession).where(
            DingTalkChannelProvisioningSession.id == provisioning_id,
            DingTalkChannelProvisioningSession.agent_id == agent_id,
        )
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="DingTalk provisioning session not found")
    return session


@router.post("/agents/{agent_id}/dingtalk-channel/provisioning")
async def start_dingtalk_channel_provisioning_route(
    agent_id: uuid.UUID,
    data: dict | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Start DingTalk robot authorization for a digital employee."""
    agent, _ = await check_agent_access(db, current_user, agent_id)
    _require_manage_access(current_user, agent)
    response = await start_dingtalk_channel_provisioning(
        db,
        agent=agent,
        requested_by_user_id=current_user.id,
    )
    return _sanitize_response(response)


@router.get("/agents/{agent_id}/dingtalk-channel/provisioning/{provisioning_id}")
async def get_dingtalk_channel_provisioning_status_route(
    agent_id: uuid.UUID,
    provisioning_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get DingTalk robot authorization status for a digital employee."""
    agent, _ = await check_agent_access(db, current_user, agent_id)
    _require_manage_access(current_user, agent)
    session = await _load_session_or_404(db, agent_id=agent_id, provisioning_id=provisioning_id)
    return _sanitize_response(get_dingtalk_provisioning_status_response(session))


@router.post("/agents/{agent_id}/dingtalk-channel/provisioning/{provisioning_id}/cancel")
async def cancel_dingtalk_channel_provisioning_route(
    agent_id: uuid.UUID,
    provisioning_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Cancel a pending DingTalk robot authorization flow."""
    agent, _ = await check_agent_access(db, current_user, agent_id)
    _require_manage_access(current_user, agent)
    session = await _load_session_or_404(db, agent_id=agent_id, provisioning_id=provisioning_id)
    if session.status in DINGTALK_PROVISIONING_ACTIVE_STATUSES:
        session.status = DINGTALK_PROVISIONING_STATUS_CANCELLED
        session.next_poll_at = None
        session.last_error = "用户取消钉钉数字员工通道配置流程"
        await db.flush()
    return _sanitize_response(get_dingtalk_provisioning_status_response(session))
