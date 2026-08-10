"""Canonical, stable contract for the builtin ``send_media`` tool."""

from __future__ import annotations


SEND_MEDIA_DESCRIPTION = (
    "Send one audio or video source to the current conversation, an exact existing "
    "person/group Session, or a directly resolved person. Use exactly one source: "
    "file_path for an Agent-owned workspace file, or url plus url_mode for a third-party "
    "URL. url_mode='external' accepts HTTPS only, publishes the URL without downloading "
    "it, and is "
    "available only when the resolved destination can render a third-party media URL; "
    "url_mode='managed' accepts HTTP or HTTPS and downloads the media into this Agent's "
    "workspace before delivery "
    "so history uses platform-managed playback. The tool contract is always available "
    "even when the resolved IM channel returns unsupported. Audio/video is rendered as "
    "a dedicated tool-call card."
)

SEND_MEDIA_PARAMETERS_SCHEMA = {
    "type": "object",
    "properties": {
        "media_type": {
            "type": "string",
            "enum": ["audio", "video"],
            "description": (
                "Required media kind. It must match the actual managed/workspace file; "
                "external URLs are rendered using this declared kind."
            ),
        },
        "file_path": {
            "type": "string",
            "description": (
                "Agent-owned workspace-relative file path, for example "
                "workspace/media/briefing.mp3. Use either file_path or url, never both."
            ),
        },
        "url": {
            "type": "string",
            "description": (
                "Third-party media URL. external mode requires HTTPS; managed mode "
                "accepts HTTP or HTTPS. Use with url_mode and omit file_path. "
                "The full URL may be persisted in the standard tool-call record."
            ),
        },
        "url_mode": {
            "type": "string",
            "enum": ["external", "managed"],
            "description": (
                "Required with url. external requires HTTPS, publishes the third-party "
                "URL without downloading, and does not guarantee future availability. "
                "managed accepts HTTP or HTTPS and imports the media into "
                "workspace/media/imported before delivery."
            ),
        },
        "cover_image_path": {
            "type": "string",
            "description": (
                "Video only. Optional Agent-owned workspace-relative image path. "
                "An Agent cover wins; required channels generate a fallback when omitted."
            ),
        },
        "session_id": {
            "type": "string",
            "description": (
                "Exact existing person-or-group ChatSession UUID from list_sessions or "
                "search_sessions. Omit for the current conversation or when using user_id."
            ),
        },
        "user_id": {
            "type": "string",
            "description": (
                "Canonical natural-person user UUID from search_contacts or Relationships. "
                "Omit for current-conversation or exact-session delivery."
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
                "Optional caption/business text delivered as a separate ordinary message "
                "after the standalone media message."
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
            "description": "Show the download action on send_media cards in both Web and H5 chat.",
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
