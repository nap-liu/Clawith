"""Indexed name filters for the chat-session picker."""

from sqlalchemy import or_, select

from app.models.chat_session import ChatSession
from app.models.user import Identity, User


def session_name_search(query: str):
    """Match session, group, or direct-counterpart names without reading messages."""
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    person_match = (
        select(User.id)
        .outerjoin(Identity, User.identity_id == Identity.id)
        .where(
            User.id == ChatSession.user_id,
            or_(
                User.display_name.ilike(pattern, escape="\\"),
                Identity.username.ilike(pattern, escape="\\"),
            ),
        )
        .exists()
    )
    return or_(
        ChatSession.title.ilike(pattern, escape="\\"),
        ChatSession.group_name.ilike(pattern, escape="\\"),
        person_match,
    )
