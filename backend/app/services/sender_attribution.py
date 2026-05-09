"""System-injected sender prefix for group-chat messages.

Prepends a ``<sender id="..">name</sender>\\n`` tag line to user messages.
The model treats the leading tag as authoritative sender identity (see
``Message Sender Tag`` rule in the system prompt).
"""

import uuid


_LINE_BREAKING_WHITESPACE = str.maketrans({"\n": " ", "\r": " ", "\t": " "})


def _xml_text_escape(value: str) -> str:
    """Escape characters that would break XML element text content.

    Used for ``display_name`` placed inside the tag. Content AFTER the tag
    is plain text and needs no escaping at all. Newline/CR/Tab are also
    collapsed to a single space so a hostile or malformed display_name
    cannot violate the "tag occupies exactly one line" invariant.
    """
    cleaned = value.translate(_LINE_BREAKING_WHITESPACE)
    return cleaned.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def wrap_with_sender(
    content: str,
    user_id: uuid.UUID | str | None,
    display_name: str | None,
) -> str:
    """Prefix a user message with a system-injected sender tag line.

    Format::

        <sender id="<user_id>">display name</sender>
        {content}

    ``user_id`` MUST be the platform ``User.id`` (the ``users`` table primary
    key UUID). This id is allocated once when the user first registers and
    is **stable across sessions, channels, and devices** for the same person.
    Callers must not pass session-scoped, request-scoped, or freshly-generated
    UUIDs here — the agent will use this id to invoke tools like
    ``send_platform_message`` and approval routing, and those calls require
    the id to map to a real platform user.

    If ``user_id`` is ``None`` we return the content unchanged — we never
    fabricate a sender. Callers must decide their own fallback (typically:
    skip wrapping for system / tool / orphan messages).
    """
    if user_id is None:
        return content
    if isinstance(user_id, str):
        user_id = uuid.UUID(user_id)  # raises ValueError on malformed input
    safe_name = _xml_text_escape(display_name or "Unknown")
    return f'<sender id="{user_id}">{safe_name}</sender>\n{content}'
