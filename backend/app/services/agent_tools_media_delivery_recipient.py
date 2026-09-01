from __future__ import annotations

import json
from pathlib import Path
import uuid

from app.database import async_session
from app.services.channel_session import find_or_create_channel_session
from app.services.agent_tools_media_delivery_runtime import _send_media_to_session


async def _send_media_to_recipient(
    *,
    agent_id: uuid.UUID,
    file_path: Path,
    workspace_path: str,
    user_id: str,
    channel: str | None,
    media_kind: str,
    caption: str,
    cover_path: Path | None,
    intent_id: str,
    origin_session_id: str | None,
    origin_turn_anchor_id: uuid.UUID | None,
    allow_download: bool = False,
    source_mode: str = "workspace",
    tool_args: dict | None = None,
) -> str:
    """Resolve a person, bind/reuse their Session, then use exact-Session delivery."""
    from app.services.recipient_resolver import (
        RecipientResolutionError,
        resolve_human_channel_recipient,
    )

    async with async_session() as db:
        try:
            route = await resolve_human_channel_recipient(db, agent_id, user_id, channel=channel)
        except RecipientResolutionError as exc:
            return json.dumps(
                {
                    "type": "media_delivery_result",
                    "version": 1,
                    "status": "unsupported",
                    "code": exc.code,
                    "message": exc.message,
                    "media_kind": media_kind,
                    "intent_id": intent_id,
                    "available_channels": exc.available_channels,
                },
                ensure_ascii=False,
            )
        if route.channel != "dingtalk":
            return json.dumps(
                {
                    "type": "media_delivery_result",
                    "version": 1,
                    "status": "unsupported",
                    "code": "CHANNEL_MEDIA_UNSUPPORTED",
                    "media_kind": media_kind,
                    "intent_id": intent_id,
                    "channel": route.channel,
                },
                ensure_ascii=False,
            )
        target_staff_id = str(route.member.external_id or "").strip()
        if not target_staff_id:
            return json.dumps(
                {
                    "type": "media_delivery_result",
                    "version": 1,
                    "status": "unsupported",
                    "code": "RECIPIENT_MEDIA_ROUTE_UNAVAILABLE",
                    "media_kind": media_kind,
                    "intent_id": intent_id,
                    "channel": route.channel,
                },
                ensure_ascii=False,
            )
        session = await find_or_create_channel_session(
            db=db,
            agent_id=agent_id,
            user_id=route.user.id,
            external_conv_id=f"dingtalk_p2p_{target_staff_id}",
            source_channel="dingtalk",
            first_message_title=caption.strip()[:30] or file_path.name[:30],
        )
        await db.commit()
        target_session_id = str(session.id)

    return await _send_media_to_session(
        agent_id=agent_id,
        session_id=target_session_id,
        file_path=file_path,
        workspace_path=workspace_path,
        media_kind=media_kind,
        caption=caption,
        cover_path=cover_path,
        intent_id=intent_id,
        origin_session_id=origin_session_id,
        origin_turn_anchor_id=origin_turn_anchor_id,
        allow_download=allow_download,
        source_mode=source_mode,
        tool_args=tool_args,
    )


__all__ = ["_send_media_to_recipient"]
