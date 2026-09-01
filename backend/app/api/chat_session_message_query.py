"""Message-page loading and inline tool parsing for chat sessions."""

import json
import re
import uuid
from datetime import datetime
from datetime import timezone as tz

from fastapi import HTTPException, Response
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.chat_session_access import _load_accessible_session
from app.models.audit import ChatMessage
from app.models.user import User
from app.services.chat_message_serializer import (
    merge_tool_call_update_for_client,
    serialize_chat_message_for_client,
    serialize_tool_call_for_client,
)


async def _get_session_messages_page(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    limit: int,
    turn_limit: int | None,
    before: str | None,
    current_user: User,
    db: AsyncSession,
    response: Response | None,
):
    _, session, _ = await _load_accessible_session(db, current_user, agent_id, session_id)

    # Query messages by conversation_id only (agent-to-agent uses session_agent_id)
    # Optimized: use a single query with ORDER BY and LIMIT instead of subquery
    from sqlalchemy import asc, desc
    query = (
        select(ChatMessage)
        .where(
            ChatMessage.conversation_id == str(session_id),
            ChatMessage.message_meta["kind"].as_string().is_distinct_from(
                "subagent_event"
            ),
            ChatMessage.message_meta["kind"].as_string().is_distinct_from(
                "onboarding_turn_anchor"
            ),
            ChatMessage.message_meta["kind"].as_string().is_distinct_from(
                "project_subagent_external_continuation"
            ),
            or_(
                ChatMessage.role != "assistant",
                ChatMessage.message_meta["media_kind"].as_string().is_(None),
                ChatMessage.message_meta["media_kind"].as_string().not_in(
                    ["audio", "video"]
                ),
                ChatMessage.message_meta["delivery_status"].as_string().is_(None),
                ChatMessage.message_meta["delivery_status"].as_string() == "sent",
            ),
        )
        # id tiebreak: own-transaction tool_call/assistant rows can share a
        # created_at microsecond; keep the render order deterministic.
        .order_by(desc(ChatMessage.created_at), desc(ChatMessage.id))
    )
    # Keep accepting the legacy timestamp-only cursor, while newer clients add
    # the message UUID so rows sharing a timestamp cannot be skipped at a page boundary.
    if before:
        from datetime import datetime as dt
        try:
            before_timestamp, separator, before_message_id = before.partition('|')
            before_dt = dt.fromisoformat(before_timestamp.replace('Z', '+00:00'))
            if separator:
                cursor_id = uuid.UUID(before_message_id)
                query = query.where(or_(
                    ChatMessage.created_at < before_dt,
                    and_(ChatMessage.created_at == before_dt, ChatMessage.id < cursor_id),
                ))
            else:
                query = query.where(ChatMessage.created_at < before_dt)
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="Invalid `before` cursor. Use ISO 8601 or <ISO 8601>|<message UUID>.",
            )
    if turn_limit is not None:
        # A persisted user row is the durable turn anchor. Resolve the oldest
        # anchor in this page first, then fetch every row through the current
        # cursor. Tool-heavy turns are therefore returned whole with two
        # bounded, indexed queries instead of a raw message-row cutoff.
        anchors_result = await db.execute(
            query.where(ChatMessage.role == "user").limit(turn_limit)
        )
        anchors = list(anchors_result.scalars().all())
        if anchors:
            oldest_anchor = anchors[-1]
            at_or_after_anchor = or_(
                ChatMessage.created_at > oldest_anchor.created_at,
                and_(
                    ChatMessage.created_at == oldest_anchor.created_at,
                    ChatMessage.id >= oldest_anchor.id,
                ),
            )
            messages_result = await db.execute(
                query
                .where(at_or_after_anchor)
                .order_by(None)
                .order_by(asc(ChatMessage.created_at), asc(ChatMessage.id))
            )
            messages = list(messages_result.scalars().all())
            older_than_anchor = or_(
                ChatMessage.created_at < oldest_anchor.created_at,
                and_(
                    ChatMessage.created_at == oldest_anchor.created_at,
                    ChatMessage.id < oldest_anchor.id,
                ),
            )
            older_result = await db.execute(query.where(older_than_anchor).limit(1))
            has_more = older_result.scalar_one_or_none() is not None
        else:
            # Assistant-first greetings and legacy unanchored rows are not LLM
            # turns. Preserve the old bounded row behavior for that small tail.
            msgs_result = await db.execute(query.limit(limit + 1))
            newest_first = list(msgs_result.scalars().all())
            has_more = len(newest_first) > limit
            messages = list(reversed(newest_first[:limit]))
    else:
        msgs_result = await db.execute(query.limit(limit + 1))
        newest_first = list(msgs_result.scalars().all())
        has_more = len(newest_first) > limit
        messages = list(reversed(newest_first[:limit]))
    oldest_raw_message = messages[0] if messages else None
    next_cursor = (
        f"{oldest_raw_message.created_at.isoformat()}|{oldest_raw_message.id}"
        if oldest_raw_message is not None
        else ""
    )
    if response is not None:
        response.headers["X-Message-Has-More"] = "true" if has_more else "false"
        response.headers["X-Message-Next-Cursor"] = next_cursor

    # Reading your own first-party/channel session should clear its unread state.
    if str(session.user_id) == str(current_user.id) and not session.is_group and session.source_channel not in ("agent", "trigger"):
        session.last_read_at_by_user = datetime.now(tz.utc)
        await db.commit()

    # Resolve canonical sender IDs and display names in batches. Participant is
    # consulted only as a one-release read bridge for legacy A2A rows; its ID is
    # never returned to callers.
    from app.models.agent import Agent
    from app.models.participant import Participant

    participant_ids = {m.participant_id for m in messages if m.participant_id}
    legacy_participants: dict[str, tuple[str, uuid.UUID, str]] = {}
    if participant_ids:
        p_result = await db.execute(
            select(Participant.id, Participant.type, Participant.ref_id, Participant.display_name)
            .where(Participant.id.in_(participant_ids))
        )
        legacy_participants = {
            str(pid): (ptype, ref_id, display_name or "Unknown")
            for pid, ptype, ref_id, display_name in p_result.all()
        }

    user_ids_seen: set[uuid.UUID] = set()
    agent_ids_seen: set[uuid.UUID] = set()
    for message in messages:
        sender_user_id = getattr(message, "sender_user_id", None)
        sender_agent_id = getattr(message, "sender_agent_id", None)
        if sender_user_id:
            user_ids_seen.add(sender_user_id)
        elif sender_agent_id:
            agent_ids_seen.add(sender_agent_id)
        elif message.participant_id and str(message.participant_id) in legacy_participants:
            participant_type, ref_id, _display = legacy_participants[str(message.participant_id)]
            if participant_type == "user":
                user_ids_seen.add(ref_id)
            elif participant_type == "agent":
                agent_ids_seen.add(ref_id)
        elif message.role == "user" and session.source_channel != "agent":
            legacy_user_id = getattr(message, "user_id", None) or session.user_id
            if legacy_user_id:
                user_ids_seen.add(legacy_user_id)
        elif message.role in {"assistant", "tool_call"}:
            agent_ids_seen.add(getattr(message, "agent_id", None) or agent_id)

    user_name_cache: dict[str, str] = {}
    needs_sender_names = bool(getattr(session, "is_group", False)) or session.source_channel == "agent"
    if needs_sender_names and user_ids_seen:
        u_rows = await db.execute(select(User.id, User.display_name).where(User.id.in_(user_ids_seen)))
        user_name_cache = {str(uid): (name or "Unknown") for uid, name in u_rows.all()}
    agent_name_cache: dict[str, str] = {}
    if needs_sender_names and agent_ids_seen:
        a_rows = await db.execute(select(Agent.id, Agent.name).where(Agent.id.in_(agent_ids_seen)))
        agent_name_cache = {str(aid): (name or "Unknown") for aid, name in a_rows.all()}

    out = []
    tool_call_positions: dict[str, int] = {}
    for m in messages:
        raw_message_meta = getattr(m, "message_meta", None)
        message_meta = raw_message_meta if isinstance(raw_message_meta, dict) else {}
        if (
            m.role == "assistant"
            and message_meta.get("media_kind") in {"audio", "video"}
            and (
                message_meta.get("delivery_status") not in {None, "sent"}
            )
        ):
            continue
        sender_user_id = getattr(m, "sender_user_id", None)
        sender_agent_id = getattr(m, "sender_agent_id", None)
        legacy_sender_name = None
        if not sender_user_id and not sender_agent_id and m.participant_id:
            legacy = legacy_participants.get(str(m.participant_id))
            if legacy:
                participant_type, ref_id, legacy_sender_name = legacy
                if participant_type == "user":
                    sender_user_id = ref_id
                elif participant_type == "agent":
                    sender_agent_id = ref_id
        if not sender_user_id and not sender_agent_id:
            if m.role == "user" and session.source_channel != "agent":
                sender_user_id = getattr(m, "user_id", None) or session.user_id
            elif m.role in {"assistant", "tool_call"}:
                sender_agent_id = getattr(m, "agent_id", None) or agent_id
        sender_name = legacy_sender_name
        if sender_user_id:
            sender_name = user_name_cache.get(str(sender_user_id), sender_name)
        elif sender_agent_id:
            sender_name = agent_name_cache.get(str(sender_agent_id), sender_name)

        if m.role == "tool_call":
            entry = serialize_tool_call_for_client(
                m,
                source_channel=session.source_channel,
                sender_name=sender_name,
                sender_user_id=sender_user_id,
                sender_agent_id=sender_agent_id,
            )
            # Canonical tool events persist the model call_id, shared by their append-only
            # running/done rows. Pending confirmation rows intentionally have no call_id;
            # their database row id remains the resolve handle.
            tool_call_id = entry["toolCallId"]
            turn_anchor_id = str(message_meta.get("turn_anchor_id") or "legacy")
            entry["turnAnchorId"] = (
                turn_anchor_id if turn_anchor_id != "legacy" else None
            )
            tool_identity = f"{turn_anchor_id}:{tool_call_id}"
            previous_position = tool_call_positions.get(tool_identity)
            if previous_position is None:
                tool_call_positions[tool_identity] = len(out)
                out.append(entry)
            else:
                out[previous_position] = merge_tool_call_update_for_client(
                    out[previous_position],
                    entry,
                )
            continue

        # For agent sessions, parse inline tool_code blocks from assistant messages
        if session.source_channel == "agent" and m.role == "assistant" and "```tool_code" in (m.content or ""):
            parts = _split_inline_tools(m.content)
            for part_index, part in enumerate(parts):
                derived_id = f"{m.id}:inline:{part_index}"
                part["id"] = derived_id
                part["created_at"] = m.created_at.isoformat() if m.created_at else None
                if part.get("role") == "tool_call":
                    part["toolCallId"] = derived_id
                if sender_name:
                    part["sender_name"] = sender_name
                if sender_user_id:
                    part["sender_user_id"] = str(sender_user_id)
                if sender_agent_id:
                    part["sender_agent_id"] = str(sender_agent_id)
                out.append(part)
        else:
            entry = serialize_chat_message_for_client(
                m,
                source_channel=session.source_channel,
                sender_name=sender_name,
                sender_user_id=sender_user_id,
                sender_agent_id=sender_agent_id,
            )
            out.append(entry)

    # NB: confirmation cards are NOT merged here any more — a card is just a
    # `request_confirmation` tool_call row, already returned as a normal tool_call
    # message above (the frontend renders that specific tool as the card).

    return out


def _split_inline_tools(content: str) -> list[dict]:
    """Parse assistant content containing inline ```tool_code blocks.

    Splits into alternating text segments and tool_call entries.
    Format: ```tool_code\ntool_name\n``` ```json\n{args}\n```
    """
    # Pattern: ```tool_code\n<name>\n``` optionally followed by ```json\n<args>\n```
    pattern = re.compile(
        r'```tool_code\s*\n\s*(\w+)\s*\n```'        # tool name
        r'(?:\s*```json\s*\n(.*?)\n```)?',            # optional JSON args
        re.DOTALL
    )

    parts: list[dict] = []
    last_end = 0

    for match in pattern.finditer(content):
        # Text before this tool call
        text_before = content[last_end:match.start()].strip()
        if text_before:
            parts.append({"role": "assistant", "content": text_before})

        tool_name = match.group(1)
        args_str = match.group(2)
        tool_args = None
        if args_str:
            try:
                import json
                tool_args = json.loads(args_str.strip())
            except Exception:
                tool_args = {"raw": args_str.strip()}

        parts.append({
            "role": "tool_call",
            "content": "",
            "toolName": tool_name,
            "toolArgs": tool_args,
            "toolStatus": "done",
            "toolResult": "",
        })
        last_end = match.end()

    # Trailing text after last tool
    trailing = content[last_end:].strip()
    if trailing:
        parts.append({"role": "assistant", "content": trailing})

    # If no matches found, return the whole content as-is
    if not parts:
        parts.append({"role": "assistant", "content": content})

    return parts
