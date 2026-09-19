"""Project provider account state onto tenant-level platform users."""

import uuid

from sqlalchemy import distinct, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.identity import IdentityProvider
from app.models.org import OrgMember
from app.models.user import User


async def sync_tenant_user_statuses(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    changed_provider_id: uuid.UUID,
) -> dict[str, int]:
    """Recompute affected tenant users from all active provider accounts.

    Source rows remain authoritative facts.  The tenant user is enabled when at
    least one active provider connection reports an active linked account, and
    disabled only when none do.  This keeps the result independent of provider
    synchronization order and never changes the global Identity state.
    """
    affected_user_ids = set(
        (
            await db.scalars(
                select(distinct(OrgMember.user_id)).where(
                    OrgMember.tenant_id == tenant_id,
                    OrgMember.provider_id == changed_provider_id,
                    OrgMember.user_id.is_not(None),
                )
            )
        ).all()
    )
    affected_user_ids.discard(None)
    if not affected_user_ids:
        return {"enabled": 0, "disabled": 0}

    # Every writer of the derived User flag takes these locks.  This makes a
    # concurrent login and directory sync resolve in commit order without a
    # second state machine or provider-specific coordination.
    users = (
        await db.scalars(
            select(User)
            .where(
                User.tenant_id == tenant_id,
                User.id.in_(affected_user_ids),
            )
            .order_by(User.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).all()

    active_user_ids = set(
        (
            await db.scalars(
                select(distinct(OrgMember.user_id))
                .join(IdentityProvider, IdentityProvider.id == OrgMember.provider_id)
                .where(
                    OrgMember.tenant_id == tenant_id,
                    OrgMember.user_id.in_(affected_user_ids),
                    OrgMember.status == "active",
                    or_(
                        IdentityProvider.tenant_id == tenant_id,
                        IdentityProvider.tenant_id.is_(None),
                    ),
                    IdentityProvider.is_active.is_(True),
                )
            )
        ).all()
    )
    changed = {"enabled": 0, "disabled": 0}
    for user in users:
        # Keep the legacy projection fail-closed as well.  This preserves an
        # administrator suspension if an emergency rollback runs an older
        # binary that only understands User.is_active.
        next_active = user.id in active_user_ids and not user.is_login_suspended
        if user.is_active == next_active:
            continue
        user.is_active = next_active
        changed["enabled" if next_active else "disabled"] += 1
    await db.flush()
    return changed
