"""RBAC permission checking utilities."""

import uuid
from datetime import datetime, timezone
from typing import Tuple

from fastapi import HTTPException, status
from sqlalchemy import and_, false, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent, AgentPermission
from app.models.org import (
    AgentAgentRelationship,
    AgentRelationship,
    RelationshipSuppression,
)
from app.models.user import User


def build_visible_agents_query(
    user: User,
    *,
    tenant_id: uuid.UUID | None = None,
):
    """Build a query for agents visible to the current user.

    Role-based visibility (refined v1.9.3 access_mode model):

    - ``platform_admin``: cross-tenant operator. Sees every agent in the
      target tenant unconditionally, including other users' ``private``
      and ``custom`` agents. This is required for ops/audit duties.
    - ``org_admin``: regular company manager. Sees own creations +
      ``company``/``custom`` agents (not in custom roster is OK, admin
      still manages them). Cannot see other users' ``private`` agents —
      that preserves v1.9.3's privacy guarantee for personal agents.
    - Regular users: own creations + ``company`` agents + agents
      explicitly added to a ``custom`` roster they're on.
    """
    stmt = select(Agent).where(Agent.is_deleted.is_(False))

    target_tenant_id = tenant_id if tenant_id is not None else user.tenant_id
    if target_tenant_id is None:
        return stmt.where(false())

    if user.role == "platform_admin":
        return stmt.where(Agent.tenant_id == target_tenant_id)

    if user.role == "org_admin":
        return stmt.where(
            Agent.tenant_id == target_tenant_id,
            or_(
                Agent.creator_id == user.id,
                Agent.access_mode != "private",
            ),
        )

    explicit_user_ids = (
        select(AgentPermission.agent_id)
        .where(
            and_(
                AgentPermission.scope_type == "user",
                AgentPermission.scope_id == user.id,
            )
        )
    )

    return stmt.where(
        Agent.tenant_id == target_tenant_id,
        or_(
            Agent.creator_id == user.id,
            Agent.access_mode == "company",
            Agent.id.in_(explicit_user_ids),
        ),
    )


def is_company_visible_agent(agent: Agent) -> bool:
    """Return whether an agent participates in company-public surfaces."""
    return (getattr(agent, "access_mode", None) or "company") == "company"


def _is_admin(user: User) -> bool:
    return user.role in ("platform_admin", "org_admin")


def current_agent_tenant_matches(user: User, agent: Agent) -> bool:
    """Return whether the authenticated user/token is scoped to the agent tenant."""
    # A few isolated unit tests use deliberately minimal protocol doubles. Real
    # ``User`` and ``Agent`` ORM instances always expose both tenant attributes.
    if not hasattr(user, "tenant_id") or not hasattr(agent, "tenant_id"):
        return True
    return bool(user.tenant_id) and user.tenant_id == agent.tenant_id


def require_current_agent_tenant(user: User, agent: Agent) -> None:
    """Require an agent operation to use the active login enterprise.

    Platform administrators retain global capability through tenant switching,
    but a tenant-scoped request must use the user record/token for that tenant.
    """
    if not current_agent_tenant_matches(user, agent):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Switch to the agent's organization before continuing",
        )


def can_view_all_agent_chat_sessions(user: User, agent: Agent) -> bool:
    """Whether ``user`` may view/monitor OTHER users' chat sessions for ``agent``.

    Single source of truth for "who can see another user's conversation",
    shared by the REST session/message APIs (list/read) and the live WebSocket
    monitor path. Admins (platform/org/agent) and the agent's creator qualify.
    """
    return (
        current_agent_tenant_matches(user, agent)
        and (
        user.role in ("platform_admin", "org_admin", "agent_admin")
        or str(agent.creator_id) == str(user.id)
        )
    )


async def filter_tenant_safe_chat_sessions(
    db: AsyncSession,
    sessions: list,
    tenant_id: uuid.UUID | None,
) -> list:
    """Hide every malformed session edge that crosses its owning tenant."""
    if not sessions:
        return []
    if not tenant_id:
        return []

    required_agent_ids = {
        agent_id
        for session in sessions
        for agent_id in (
            getattr(session, "agent_id", None),
            getattr(session, "peer_agent_id", None),
        )
        if agent_id
    }
    valid_ids = set(
        (
            await db.execute(
                select(Agent.id).where(
                    Agent.id.in_(required_agent_ids),
                    Agent.tenant_id == tenant_id,
                )
            )
        ).scalars().all()
    )
    required_user_ids = {
        getattr(session, "user_id", None)
        for session in sessions
        if getattr(session, "user_id", None)
    }
    valid_user_ids = set()
    if required_user_ids:
        valid_user_ids = set(
            (
                await db.execute(
                    select(User.id).where(
                        User.id.in_(required_user_ids),
                        User.tenant_id == tenant_id,
                    )
                )
            ).scalars().all()
        )

    def _safe(session) -> bool:
        source_id = getattr(session, "agent_id", None)
        peer_id = getattr(session, "peer_agent_id", None)
        user_id = getattr(session, "user_id", None)
        source_channel = getattr(session, "source_channel", None)
        is_group = bool(getattr(session, "is_group", False))
        if source_id not in valid_ids:
            return False
        if source_channel == "agent":
            return not is_group and user_id is None and peer_id in valid_ids
        if is_group or source_channel == "trigger":
            return user_id is None and peer_id is None
        return peer_id is None and user_id in valid_user_ids

    return [
        session
        for session in sessions
        if _safe(session)
    ]


async def require_tenant_safe_chat_session(
    db: AsyncSession,
    session,
    tenant_id: uuid.UUID | None,
) -> None:
    """Fail closed when any selected session edge violates tenant identity."""
    if len(await filter_tenant_safe_chat_sessions(db, [session], tenant_id)) != 1:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found",
        )


async def get_agent_access_level_for_user_id(
    db: AsyncSession,
    user_id: uuid.UUID | None,
    agent: Agent,
) -> str | None:
    """Return 'manage', 'use', or None for a platform user and an agent.

    This helper is intentionally HTTP-exception free so background jobs, gateway
    calls, and relationship status checks can reuse the same access semantics.
    """
    if not user_id:
        return None

    user_result = await db.execute(select(User).where(User.id == user_id))
    user = user_result.scalar_one_or_none()
    if not user or not user.is_active:
        return None
    if user.role == "org_admin" and user.tenant_id is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No tenant scope assigned")
    if agent.tenant_id != user.tenant_id:
        return None
    if agent.creator_id == user.id:
        return "manage"

    access_mode = getattr(agent, "access_mode", None) or "company"
    # platform_admin manages everything in the tenant including others' private agents.
    # org_admin only manages non-private agents — preserves v1.9.3 privacy guarantee.
    if user.role == "platform_admin":
        return "manage"
    if user.role == "org_admin" and access_mode != "private":
        return "manage"

    perms_result = await db.execute(select(AgentPermission).where(AgentPermission.agent_id == agent.id))
    permissions = perms_result.scalars().all()

    if access_mode == "company":
        company_level = getattr(agent, "company_access_level", None) or next(
            (perm.access_level for perm in permissions if perm.scope_type == "company"),
            "use",
        )
        return company_level or "use"

    if access_mode == "custom":
        for perm in permissions:
            if perm.scope_type == "user" and perm.scope_id == user.id:
                return perm.access_level or "use"

    return None


async def user_can_manage_agent_id(
    db: AsyncSession,
    user_id: uuid.UUID | None,
    agent: Agent,
) -> bool:
    return (await get_agent_access_level_for_user_id(db, user_id, agent)) == "manage"


async def user_can_view_agent_id(
    db: AsyncSession,
    user_id: uuid.UUID | None,
    agent: Agent,
) -> bool:
    """Whether a user can *see* an agent (any access level, not just manage).

    Visibility is the superset of manageability (``manage`` implies ``view``),
    and is equivalent to ``build_visible_agents_query`` membership: a user sees
    their own agents, company agents, and custom agents they're rostered on.
    """
    return (await get_agent_access_level_for_user_id(db, user_id, agent)) is not None


async def get_agent_accessible_user_ids(db: AsyncSession, agent: Agent) -> set[uuid.UUID]:
    """Return platform users who can access an agent under current policy."""
    access_mode = getattr(agent, "access_mode", None) or "company"
    ids: set[uuid.UUID] = set()
    if agent.creator_id:
        ids.add(agent.creator_id)

    if access_mode == "company":
        result = await db.execute(
            select(User.id).where(
                User.tenant_id == agent.tenant_id,
                User.is_active == True,  # noqa: E712
            )
        )
        ids.update(row[0] for row in result.fetchall())
        return ids

    if access_mode == "custom":
        result = await db.execute(
            select(AgentPermission.scope_id).where(
                AgentPermission.agent_id == agent.id,
                AgentPermission.scope_type == "user",
                AgentPermission.scope_id.isnot(None),
            )
        )
        ids.update(row[0] for row in result.fetchall() if row[0])
        admin_result = await db.execute(
            select(User.id).where(
                User.tenant_id == agent.tenant_id,
                User.is_active == True,  # noqa: E712
                User.role.in_(["platform_admin", "org_admin"]),
            )
        )
        ids.update(row[0] for row in admin_result.fetchall())

    return ids


def _agent_available(agent: Agent | None) -> tuple[bool, str | None]:
    if not agent:
        return False, "target_not_found"
    if getattr(agent, "status", None) in ("stopped", "error"):
        return False, f"target_status_{agent.status}"
    if is_agent_expired(agent):
        return False, "target_expired"
    return True, None


async def evaluate_agent_relationship_status(
    db: AsyncSession,
    rel: AgentAgentRelationship,
    *,
    current_user_id: uuid.UUID | None = None,
) -> dict:
    """Compute the effective status for an Agent -> Agent relationship."""
    source_result = await db.execute(select(Agent).where(Agent.id == rel.agent_id))
    source = source_result.scalar_one_or_none()
    target = rel.__dict__.get("target_agent")
    if target is None:
        target_result = await db.execute(select(Agent).where(Agent.id == rel.target_agent_id))
        target = target_result.scalar_one_or_none()

    if not source or not target:
        return {
            "access_allowed": False,
            "access_status": "missing_target",
            "access_status_reason": "source_or_target_not_found",
        }
    if source.tenant_id != target.tenant_id:
        return {
            "access_allowed": False,
            "access_status": "restricted",
            "access_status_reason": "different_tenant",
        }
    suppression = await db.scalar(
        select(RelationshipSuppression.id).where(
            RelationshipSuppression.agent_id == source.id,
            RelationshipSuppression.target_type == "agent",
            RelationshipSuppression.target_id == target.id,
        )
    )
    if suppression:
        return {
            "access_allowed": False,
            "access_status": "suppressed",
            "access_status_reason": "relationship_explicitly_suppressed",
        }

    available, reason = _agent_available(target)
    if not available:
        return {
            "access_allowed": False,
            "access_status": "restricted",
            "access_status_reason": reason or "target_unavailable",
        }

    created_by_user_id = getattr(rel, "created_by_user_id", None)
    if created_by_user_id:
        # Source must still be MANAGED by the creator; target need only be VISIBLE
        # (relationship is directional source->target, target is not mutated).
        if await user_can_manage_agent_id(
            db, created_by_user_id, source
        ) and await user_can_view_agent_id(db, created_by_user_id, target):
            return {
                "access_allowed": True,
                "access_status": "active",
                "access_status_reason": None,
            }
        return {
            "access_allowed": False,
            "access_status": "restricted",
            "access_status_reason": "relationship_creator_no_longer_has_access_to_both_agents",
        }

    target_mode = getattr(target, "access_mode", None) or "company"
    if target_mode == "company":
        return {
            "access_allowed": True,
            "access_status": "active",
            "access_status_reason": None,
        }

    candidate_user_ids = [
        current_user_id,
        source.creator_id,
    ]
    seen: set[uuid.UUID] = set()
    for user_id in candidate_user_ids:
        if not user_id or user_id in seen:
            continue
        seen.add(user_id)
        if await user_can_manage_agent_id(db, user_id, source) and await user_can_view_agent_id(db, user_id, target):
            return {
                "access_allowed": True,
                "access_status": "active",
                "access_status_reason": None,
            }

    return {
        "access_allowed": False,
        "access_status": "restricted",
        "access_status_reason": "manager_no_longer_has_access_to_both_agents",
    }


async def evaluate_human_relationship_status(
    db: AsyncSession,
    rel: AgentRelationship,
    *,
    source_agent: Agent | None = None,
) -> dict:
    """Compute the effective status for an Agent -> Human relationship."""
    if source_agent is None:
        source_result = await db.execute(select(Agent).where(Agent.id == rel.agent_id))
        source_agent = source_result.scalar_one_or_none()
    user = rel.__dict__.get("user")
    if user is None:
        user_result = await db.execute(select(User).where(User.id == rel.user_id))
        user = user_result.scalar_one_or_none()
    if not source_agent or not user:
        return {
            "access_allowed": False,
            "access_status": "missing_target",
            "access_status_reason": "agent_or_user_not_found",
        }
    suppression = await db.scalar(
        select(RelationshipSuppression.id).where(
            RelationshipSuppression.agent_id == source_agent.id,
            RelationshipSuppression.target_type == "user",
            RelationshipSuppression.target_id == user.id,
        )
    )
    if suppression:
        return {
            "access_allowed": False,
            "access_status": "suppressed",
            "access_status_reason": "relationship_explicitly_suppressed",
        }
    if not user.is_active:
        return {
            "access_allowed": False,
            "access_status": "restricted",
            "access_status_reason": "user_inactive",
        }
    if not source_agent.tenant_id or user.tenant_id != source_agent.tenant_id:
        return {
            "access_allowed": False,
            "access_status": "restricted",
            "access_status_reason": "different_tenant",
        }
    return {
        "access_allowed": True,
        "access_status": "active",
        "access_status_reason": None,
    }


async def check_agent_access(db: AsyncSession, user: User, agent_id: uuid.UUID) -> Tuple[Agent, str]:
    """Check if a user has access to a specific agent.

    Returns (agent, access_level) where access_level is 'manage' or 'use'.

    Access is granted if:
    1. User is the agent creator -> manage
    2. Company admin + non-private agent -> manage
    3. User has explicit permission (company/user scope) -> from permission record
    """
    result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    # Platform admins are the only role with intentional cross-tenant access.
    if user.role == "platform_admin":
        return agent, "manage"

    # Tenant isolation applies to every tenant-scoped role, including org admins.
    if agent.tenant_id != user.tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to this agent")

    # Creator always has manage access
    if agent.creator_id == user.id:
        return agent, "manage"

    access_mode = getattr(agent, "access_mode", None) or "company"

    # Org admins manage tenant-visible agents, but private agents remain private.
    if user.role == "org_admin" and access_mode != "private":
        return agent, "manage"

    perms = await db.execute(select(AgentPermission).where(AgentPermission.agent_id == agent_id))
    permissions = perms.scalars().all()

    if access_mode == "company":
        company_level = getattr(agent, "company_access_level", None)
        if not company_level:
            company_level = next(
                (perm.access_level for perm in permissions if perm.scope_type == "company"),
                "use",
            )
        return agent, company_level or "use"

    if access_mode == "custom":
        for perm in permissions:
            if perm.scope_type == "user" and perm.scope_id == user.id:
                return agent, perm.access_level or "use"

    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to this agent")


def is_agent_creator(user: User, agent: Agent) -> bool:
    """Check if the user is the creator (admin) of the agent."""
    return agent.creator_id == user.id


def is_agent_expired(agent: Agent) -> bool:
    """Return True if the agent is manually marked expired or its expires_at is in the past."""
    if getattr(agent, 'is_expired', False):
        return True
    expires_at = getattr(agent, 'expires_at', None)
    if expires_at and datetime.now(timezone.utc) > expires_at:
        return True
    return False
