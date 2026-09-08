"""DingTalk native group-mention identity resolution."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.recipient_resolver import resolve_group_mention_recipient


async def prepare_group_user_mentions(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    canonical_user_ids: list[str],
) -> tuple[list[str], list[str]]:
    """Resolve canonical users to ephemeral DingTalk staff IDs and names."""
    target_ids: list[str] = []
    display_names: list[str] = []
    for canonical_user_id in canonical_user_ids:
        route = await resolve_group_mention_recipient(
            db,
            agent_id,
            canonical_user_id,
        )
        staff_id = str(route.member.external_id or "").strip()
        if not staff_id:
            raise ValueError("dingtalk_staff_id_unavailable")
        if staff_id not in target_ids:
            target_ids.append(staff_id)
            display_names.append(
                str(route.user.display_name or route.member.name or "用户")
            )
    return target_ids, display_names
