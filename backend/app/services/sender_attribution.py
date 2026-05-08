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

        <sender id="<uuid>">display name</sender>
        {content}

    If ``user_id`` is None we return the content unchanged — we never lie
    about a sender. Callers must decide their own fallback (typically:
    skip wrapping for system / tool / orphan messages).
    """
    if user_id is None:
        return content
    if isinstance(user_id, str):
        user_id = uuid.UUID(user_id)  # raises ValueError on malformed input
    safe_name = _xml_text_escape(display_name or "Unknown")
    return f'<sender id="{user_id}">{safe_name}</sender>\n{content}'
