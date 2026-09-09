"""Shared constants and helpers for durable turn-inbox handling."""

from __future__ import annotations

import uuid

TURN_INBOX_MAX_MESSAGES = 20
TURN_INBOX_MAX_BYTES = 24 * 1024
CHANNEL_RECEIPT_ANCHOR_KEY = "channel_receipt_anchor"
CHANNEL_RECEIPT_PROVIDER_META_KEY = "channel_receipt"
CHANNEL_RECEIPT_CLEANUP_DIAGNOSTICS_KEY = "channel_receipt_cleanup_diagnostics"
CHANNEL_RECEIPT_CLEANUP_BATCH_SIZE = 8
CHANNEL_RECEIPT_CLEANUP_MAX_ATTEMPTS = 3
CHANNEL_RECEIPT_CLEANUP_DIAGNOSTIC_RING_SIZE = 8
CHANNEL_RECEIPT_STARTUP_MAX_BATCHES = 512
TURN_INBOX_CHANNELS = frozenset(
    {
        "web",
        "mcp",
        "miniprogram",
        "wechat_miniprogram",
        "agent",
        "dingtalk",
        "feishu",
        "wecom",
        "wechat",
        "slack",
        "discord",
        "teams",
        "microsoft_teams",
        "whatsapp",
    }
)


def is_turn_inbox_channel(source_channel: str | None) -> bool:
    return str(source_channel or "").lower() in TURN_INBOX_CHANNELS


def _marker_cleanup_message_ids(marker: dict) -> list[uuid.UUID]:
    raw_cleanup_ids = marker.get("cleanup_message_ids")
    raw_ids = (
        list(raw_cleanup_ids)
        if isinstance(raw_cleanup_ids, list)
        else [marker.get("message_id")]
    )
    parsed: list[uuid.UUID] = []
    for raw_id in raw_ids:
        try:
            message_id = uuid.UUID(str(raw_id))
        except (TypeError, ValueError):
            continue
        if message_id not in parsed:
            parsed.append(message_id)
    return parsed
