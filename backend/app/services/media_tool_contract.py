"""Canonical, stable contract for the builtin ``send_media`` tool."""

from __future__ import annotations

MAX_MEDIA_DISPLAY_TITLE_LENGTH = 160

MANAGED_MEDIA_BLOCKED_HEADERS = frozenset({
    "accept-encoding",
    "cf-connecting-ip",
    "client-ip",
    "connection",
    "content-length",
    "forwarded",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "range",
    "te",
    "trailer",
    "transfer-encoding",
    "true-client-ip",
    "upgrade",
    "via",
    "x-agent",
    "x-clawith",
    "x-forwarded",
    "x-original-url",
    "x-real-ip",
    "x-rewrite-url",
    "x-session",
    "x-tenant",
})
MANAGED_MEDIA_BLOCKED_HEADER_PREFIXES = (
    "x-agent-",
    "x-clawith-",
    "x-forwarded-",
    "x-session-",
    "x-tenant-",
)
def normalize_media_display_title(value: object) -> str:
    """Return one safe, compact card title without changing file identity."""
    if not isinstance(value, str):
        return ""
    printable = "".join(character if character.isprintable() else " " for character in value)
    return " ".join(printable.split())[:MAX_MEDIA_DISPLAY_TITLE_LENGTH].strip()


SEND_MEDIA_DESCRIPTION = (
    "Send one audio or video item to the current conversation, an existing session, "
    "or a specified user. Choose exactly one source: file_path for a file in the caller's "
    "workspace, or url together with url_mode. external mode forwards an HTTPS URL "
    "without downloading it and works only when the destination supports remote media. "
    "managed mode downloads an HTTP or HTTPS URL, validates the media, stores a managed "
    "copy, and then delivers that copy. Optional headers apply only to managed downloads. "
    "The media item is sent separately from the optional message text, and title changes "
    "only its display label."
)

SEND_MEDIA_PARAMETERS_SCHEMA = {
    "type": "object",
    "properties": {
        "media_type": {
            "type": "string",
            "enum": ["audio", "video"],
            "description": (
                "Required media kind. It must match a workspace file or managed download. "
                "For external URLs, the destination renders the declared kind."
            ),
        },
        "file_path": {
            "type": "string",
            "description": (
                "An existing file path relative to the caller's workspace, for example "
                "exports/briefing.mp3. Use either file_path or url, never both."
            ),
        },
        "url": {
            "type": "string",
            "description": (
                "Remote media URL. external mode requires HTTPS; managed mode "
                "accepts HTTP or HTTPS. Use with url_mode and omit file_path. "
                "The full URL is retained in the tool-call record."
            ),
        },
        "url_mode": {
            "type": "string",
            "enum": ["external", "managed"],
            "description": (
                "Required with url. external requires HTTPS, sends the remote "
                "URL without downloading, and does not guarantee future availability. "
                "managed accepts HTTP or HTTPS, validates the response, and stores a copy "
                "before delivery."
            ),
        },
        "headers": {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "description": (
                "Optional HTTP request headers for url_mode='managed' only, expressed as "
                "a JSON object of string names and values. Authorization, Cookie, Referer, "
                "Origin, User-Agent, Accept, and custom headers are supported. Supplied "
                "headers override browser-style defaults and are reused for redirects. "
                "After merging, transport-controlled routing, framing, proxy, client-IP, "
                "download-control, and service-reserved identity headers are silently "
                "removed; all other valid headers are sent unchanged. Names must be valid "
                "HTTP tokens, and values may contain printable ASCII characters or tabs."
            ),
        },
        "cover_image_path": {
            "type": "string",
            "description": (
                "Video only. Optional image path relative to the caller's workspace. "
                "When omitted, destinations that require a cover generate a fallback."
            ),
        },
        "session_id": {
            "type": "string",
            "description": (
                "Exact existing person-or-group session UUID from list_sessions or "
                "search_sessions. Omit for the current conversation or when using user_id."
            ),
        },
        "user_id": {
            "type": "string",
            "description": (
                "Canonical user UUID from search_contacts. Omit for current-conversation "
                "or exact-session delivery."
            ),
        },
        "channel": {
            "type": "string",
            "enum": [
                "feishu",
                "dingtalk",
                "wecom",
                "slack",
                "teams",
                "discord",
                "whatsapp",
                "wechat",
            ],
            "description": (
                "Optional direct-person route. Use only with user_id to disambiguate "
                "available routes. A route may return unsupported without changing this "
                "tool contract."
            ),
        },
        "message": {
            "type": "string",
            "description": (
                "Optional text delivered as a separate ordinary message "
                "after the standalone media message."
            ),
        },
        "title": {
            "type": "string",
            "maxLength": MAX_MEDIA_DISPLAY_TITLE_LENGTH,
            "description": (
                "Optional concise display title for the media card. This does not rename "
                "the file and is not delivered as message text."
            ),
        },
    },
    "required": ["media_type"],
    "oneOf": [
        {
            "required": ["file_path"],
            "not": {"anyOf": [{"required": ["url"]}, {"required": ["url_mode"]}]},
        },
        {
            "required": ["url", "url_mode"],
            "not": {"required": ["file_path"]},
        },
    ],
    "not": {"required": ["session_id", "user_id"]},
    "additionalProperties": False,
}

SEND_MEDIA_CONFIG = {"allow_download": False}
SEND_MEDIA_CONFIG_SCHEMA = {
    "fields": [
        {
            "key": "allow_download",
            "label": "Allow media download",
            "type": "boolean",
            "default": False,
            "description": "Show the download action on send_media cards in supported chat clients.",
        }
    ]
}

SEND_MEDIA_TOOL_SEED = {
    "name": "send_media",
    "display_name": "Send Media",
    "description": SEND_MEDIA_DESCRIPTION,
    "category": "communication",
    "icon": "🎬",
    "is_default": True,
    "parameters_schema": SEND_MEDIA_PARAMETERS_SCHEMA,
    "config": SEND_MEDIA_CONFIG,
    "config_schema": SEND_MEDIA_CONFIG_SCHEMA,
}

SEND_MEDIA_FUNCTION_TOOL = {
    "type": "function",
    "function": {
        "name": "send_media",
        "description": (
            f"{SEND_MEDIA_DESCRIPTION} Choose exactly one target mode: "
            "(1) CURRENT conversation: omit both session_id and user_id; "
            "(2) EXACT existing person or group conversation: pass the exact session_id; "
            "(3) PERSON: pass a canonical user_id and channel only when needed to "
            "disambiguate routes. Groups require session_id. Never invent IDs. Inspect "
            "the JSON status/code: sent or already_sent means do not retry; unsupported "
            "means the resolved route cannot deliver that source mode; unknown means "
            "delivery may have happened, so never retry the same tool call automatically."
        ),
        "parameters": SEND_MEDIA_PARAMETERS_SCHEMA,
    },
}
