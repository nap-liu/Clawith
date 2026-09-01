"""Enterprise management API routes: LLM pool, enterprise info, approvals, audit logs."""

import logging
import sys
import uuid
from datetime import datetime, timezone

logger = logging.getLogger("app.api.enterprise")

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.domain import resolve_base_url
from app.core.security import encrypt_data, get_current_admin, get_current_user, require_role
from app.database import async_session, get_db
from app.models.agent import Agent
from app.models.audit import ApprovalRequest, AuditLog, EnterpriseInfo
from app.models.identity import IdentityProvider
from app.models.llm import LLMModel
from app.models.org import OrgDepartment, OrgMember
from app.models.user import User
from app.schemas.oauth2 import OAuth2Config, OAuth2ProviderCreate, OAuth2ProviderUpdate
from app.schemas.schemas import (
    ApprovalAction,
    ApprovalRequestOut,
    AuditLogOut,
    EnterpriseInfoOut,
    EnterpriseInfoUpdate,
    IdentityProviderOut,
    LLMModelClone,
    LLMModelCreate,
    LLMModelOut,
    LLMModelUpdate,
    UserInviteRequest,
)
from app.services.autonomy_service import autonomy_service
from app.services.enterprise_sync import enterprise_sync_service
from app.services.llm import LLMMessage, create_llm_client, get_model_api_key, get_provider_manifest
from app.services.org_directory import canonical_org_member_id_subquery
from app.services.org_sync_adapter import derive_member_department_paths as _derive_member_department_paths_impl
from app.services.platform_service import platform_service
from app.services.sso_service import sso_service

router = APIRouter(prefix="/enterprise", tags=["enterprise"])
settings = get_settings()


def _is_platform_admin_user(user: User) -> bool:
    """Return true for tenant-role or identity-level platform admins."""
    return user.role == "platform_admin" or bool(getattr(getattr(user, "identity", None), "is_platform_admin", False))


def _assert_tenant_scope(user: User, tenant_id: uuid.UUID | None) -> None:
    """Reject cross-tenant model access for every non-platform admin."""
    if _is_platform_admin_user(user):
        return
    if user.tenant_id is None or tenant_id != user.tenant_id:
        raise HTTPException(status_code=403, detail="Cannot access another tenant's model")


def _validate_model_context_budget(model: LLMModel) -> None:
    """Reject a model configuration that leaves no practical input window."""
    from app.services.llm.client import get_max_tokens
    from app.services.llm.context_budget import resolve_context_budget

    max_output_tokens = get_max_tokens(
        model.provider,
        model.model,
        model.max_output_tokens,
    )
    budget = resolve_context_budget(model, max_output_tokens=max_output_tokens)
    if not budget.configured or budget.input_capacity < 1024:
        raise HTTPException(
            status_code=422,
            detail=(
                "Usable context must leave at least 1024 input tokens after "
                "the model output reservation"
            ),
        )



async def derive_member_department_paths(*args, **kwargs):
    root = sys.modules.get("app.api.enterprise")
    override = getattr(root, "derive_member_department_paths", None) if root else None
    target = (
        override
        if override is not None and override is not derive_member_department_paths
        else _derive_member_department_paths_impl
    )
    return await target(*args, **kwargs)


__all__ = [name for name in globals() if not name.startswith("__")]
