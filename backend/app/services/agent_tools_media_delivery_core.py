from __future__ import annotations

from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit
import uuid

from sqlalchemy import select

from app.database import async_session
from app.models.channel_config import ChannelConfig
from app.models.chat_session import ChatSession
from app.services.chat_attachments import MEDIA_PROBE_CHUNK_BYTES, sniff_image_kind_bytes, sniff_media_mime_bytes
from app.services.recipient_resolver import RecipientResolutionError, resolve_human_channel_recipient

_PLATFORM_SESSION_CHANNELS = frozenset({"web", "miniprogram", "wechat_miniprogram"})

async def _preflight_managed_media_target(
    *,
    agent_id: uuid.UUID,
    session_id: str,
    user_id: str,
    channel: str | None,
    media_kind: str,
    intent_id: str,
) -> dict | None:
    """Reject unusable destinations before a managed URL consumes bandwidth."""
    base = {
        "type": "media_delivery_result",
        "version": 1,
        "media_kind": media_kind,
        "intent_id": intent_id,
    }
    async with async_session() as db:
        if user_id:
            try:
                route = await resolve_human_channel_recipient(
                    db,
                    agent_id,
                    user_id,
                    channel=channel,
                )
            except RecipientResolutionError as exc:
                return {
                    **base,
                    "status": "unsupported",
                    "code": exc.code,
                    "message": exc.message,
                    "available_channels": exc.available_channels,
                }
            if route.channel != "dingtalk":
                return {
                    **base,
                    "status": "unsupported",
                    "code": "CHANNEL_MEDIA_UNSUPPORTED",
                    "channel": route.channel,
                }
            if not str(route.member.external_id or "").strip():
                return {
                    **base,
                    "status": "unsupported",
                    "code": "RECIPIENT_MEDIA_ROUTE_UNAVAILABLE",
                    "channel": route.channel,
                }
            if not await _has_configured_dingtalk_media_channel(db, agent_id):
                return {
                    **base,
                    "status": "unsupported",
                    "code": "CHANNEL_MEDIA_UNSUPPORTED",
                    "channel": route.channel,
                }
            return None

        try:
            target_session_id = uuid.UUID(str(session_id))
        except (TypeError, ValueError):
            return {
                **base,
                "status": "failed",
                "code": "SESSION_NOT_FOUND_OR_FORBIDDEN",
            }
        session = (
            await db.execute(
                select(ChatSession).where(
                    ChatSession.id == target_session_id,
                    ChatSession.agent_id == agent_id,
                )
            )
        ).scalar_one_or_none()
        if session is None:
            return {
                **base,
                "status": "failed",
                "code": "SESSION_NOT_FOUND_OR_FORBIDDEN",
            }
        resolved_channel = str(session.source_channel or "").strip()
        session_fields = {"session_id": str(session.id), "channel": resolved_channel}
        if resolved_channel in _PLATFORM_SESSION_CHANNELS:
            return None
        if resolved_channel != "dingtalk":
            return {
                **base,
                **session_fields,
                "status": "unsupported",
                "code": "CHANNEL_MEDIA_UNSUPPORTED",
            }
        external_conv_id = str(session.external_conv_id or "").strip()
        if not external_conv_id or "__archived_" in external_conv_id:
            return {
                **base,
                **session_fields,
                "status": "failed",
                "code": "SESSION_ROUTE_UNAVAILABLE",
            }
        expected_prefix = "dingtalk_group_" if session.is_group else "dingtalk_p2p_"
        wrong_prefix = "dingtalk_p2p_" if session.is_group else "dingtalk_group_"
        if not external_conv_id.startswith(expected_prefix) or external_conv_id.startswith(wrong_prefix):
            return {
                **base,
                **session_fields,
                "status": "failed",
                "code": "SESSION_ROUTE_MISMATCH",
            }
        if not await _has_configured_dingtalk_media_channel(db, agent_id):
            return {
                **base,
                **session_fields,
                "status": "unsupported",
                "code": "CHANNEL_MEDIA_UNSUPPORTED",
            }
    return None

async def _has_configured_dingtalk_media_channel(db, agent_id: uuid.UUID) -> bool:
    config = (
        await db.execute(
            select(ChannelConfig).where(
                ChannelConfig.agent_id == agent_id,
                ChannelConfig.channel_type == "dingtalk",
                ChannelConfig.is_configured.is_(True),
            )
        )
    ).scalar_one_or_none()
    return bool(config and config.app_id and config.app_secret)

_MEDIA_DELIVERY_MESSAGES = {
    "INVALID_MEDIA_TYPE": "媒体类型必须是 audio 或 video。",
    "COVER_NOT_ALLOWED_FOR_AUDIO": "音频不支持封面参数。",
    "INVALID_MEDIA_SOURCE": "必须且只能提供一个媒体来源：file_path，或 url 与 url_mode。",
    "INVALID_URL_MODE": "使用 url 时，url_mode 必须是 external 或 managed。",
    "INVALID_MEDIA_HEADERS": ("headers 只支持 managed URL，且请求头名称和值必须是合法的 HTTP 字符串。"),
    "INVALID_MEDIA_URL": "媒体 URL 无效；external 仅支持 HTTPS，managed 支持 HTTP(S)。",
    "MEDIA_URL_FORBIDDEN_TARGET": "媒体 URL 指向内网、本机或其他受保护地址，平台拒绝访问。",
    "MEDIA_URL_DNS_FAILED": "媒体 URL 的域名无法解析。",
    "MEDIA_URL_TOO_MANY_REDIRECTS": "媒体 URL 重定向次数过多。",
    "MEDIA_URL_HTTP_ERROR": "下载第三方媒体时远端返回了错误状态。",
    "MEDIA_URL_TOO_LARGE": "第三方媒体超过平台允许的大小。",
    "MEDIA_URL_EMPTY": "第三方媒体内容为空。",
    "MEDIA_URL_FETCH_TIMEOUT": "下载第三方媒体超时。",
    "MEDIA_URL_FETCH_FAILED": "下载第三方媒体失败。",
    "MEDIA_STORAGE_FAILED": "第三方媒体写入 Agent 工作区失败。",
    "EXTERNAL_URL_NOT_SUPPORTED_BY_CHANNEL": "目标通道不支持直接发送第三方媒体 URL；可改用 managed 托管模式。",
    "INVALID_FILE_PATH": "媒体文件路径无效，必须使用当前 Agent 工作区内的相对路径。",
    "MEDIA_NOT_FOUND": "工作区中找不到要发送的媒体文件。",
    "MEDIA_KIND_MISMATCH": "声明的媒体类型与文件实际内容不一致。",
    "INVALID_COVER_PATH": "视频封面路径无效。",
    "INVALID_VIDEO_COVER": "视频封面不存在或不是支持的图片格式。",
    "AMBIGUOUS_MEDIA_TARGET": "不能同时指定 session_id 和 user_id。",
    "CHANNEL_REQUIRES_USER_TARGET": "channel 只能与 user_id 一起使用。",
    "SESSION_REQUIRED": "未找到当前会话；请指定有效的 session_id 或 user_id。",
    "MISSING_DELIVERY_INTENT_ID": "缺少稳定的工具调用 ID，平台未执行发送。",
    "SESSION_NOT_FOUND_OR_FORBIDDEN": "目标 Session 不存在或当前 Agent 无权访问。",
    "SESSION_ROUTE_UNAVAILABLE": "目标 Session 没有可用的 IM 投递路由。",
    "SESSION_ROUTE_MISMATCH": "目标 Session 的人员/群类型与 IM 路由不一致。",
    "CHANNEL_MEDIA_UNSUPPORTED": "目标通道暂不支持此音视频发送能力。",
    "RECIPIENT_MEDIA_ROUTE_UNAVAILABLE": "目标人员没有可用的音视频投递路由。",
    "RECIPIENT_NOT_FOUND": "未找到目标人员。",
    "MEDIA_TOO_LARGE": "媒体文件超过目标通道允许的大小。",
    "VIDEO_COVER_TOO_LARGE": "视频封面超过允许的大小。",
    "MEDIA_BUNDLE_TOO_LARGE": "视频与封面的合计大小超过允许值。",
    "MEDIA_UPLOAD_FAILED": "媒体上传到目标通道失败。",
    "MEDIA_SEND_FAILED": "目标通道拒绝或未完成媒体发送。",
    "MEDIA_DELIVERY_FAILED": "媒体发送失败。",
    "MEDIA_DELIVERY_STATE_UNKNOWN": "发送结果不确定，媒体可能已经送达；不要自动重试，以免重复发送。",
    "MEDIA_SENT_CAPTION_FAILED": "媒体已发送，但后续说明文字发送失败；不要重发媒体。",
}

def _describe_media_delivery_result(payload: dict) -> dict:
    """Add stable human/Agent-facing error semantics to a media result."""
    status = str(payload.get("status") or "")
    code = str(payload.get("code") or "")
    if status in {"sent", "already_sent"} and code != "MEDIA_SENT_CAPTION_FAILED":
        return payload
    message = _MEDIA_DELIVERY_MESSAGES.get(
        code,
        "媒体发送未完成，请根据 status 和 code 向用户说明结果。",
    )
    if code == "MEDIA_SENT_CAPTION_FAILED":
        agent_action = "Tell the user the media was sent but its caption failed; do not resend the media."
    elif status == "unknown":
        agent_action = "Report the uncertain result and do not retry this call automatically."
    else:
        agent_action = "Tell the user the media delivery did not complete; do not claim success."
    return {
        **payload,
        "message": payload.get("message") or message,
        "retryable": False,
        "agent_action": agent_action,
    }

def _sniff_media_file_kind(file_path: Path) -> str | None:
    """Recognize common playable containers from bytes, not the filename."""
    mime = _sniff_media_file_mime(file_path)
    return mime.split("/", 1)[0] if mime else None

def _sniff_media_file_mime(file_path: Path) -> str | None:
    """Return the concrete media MIME represented by the file bytes."""
    try:
        with file_path.open("rb") as handle:
            size = file_path.stat().st_size
            if size <= MEDIA_PROBE_CHUNK_BYTES * 2:
                probe = handle.read()
            else:
                head = handle.read(MEDIA_PROBE_CHUNK_BYTES)
                handle.seek(max(0, size - MEDIA_PROBE_CHUNK_BYTES))
                probe = head + handle.read(MEDIA_PROBE_CHUNK_BYTES)
    except OSError:
        return None
    return sniff_media_mime_bytes(probe, file_path.name)

def _sniff_image_file(file_path: Path) -> str | None:
    """Return the common image format represented by the file bytes."""
    try:
        with file_path.open("rb") as handle:
            head = handle.read(16)
    except OSError:
        return None
    return sniff_image_kind_bytes(head)

def _external_media_filename(media_url: str, media_kind: str) -> str:
    name = PurePosixPath(unquote(urlsplit(media_url).path)).name.strip()
    name = name.replace("\\", "_").replace("/", "_")
    return name[:180] or f"external-{media_kind}"

__all__ = [
    "_PLATFORM_SESSION_CHANNELS",
    "_preflight_managed_media_target",
    "_has_configured_dingtalk_media_channel",
    "_MEDIA_DELIVERY_MESSAGES",
    "_describe_media_delivery_result",
    "_sniff_media_file_kind",
    "_sniff_media_file_mime",
    "_sniff_image_file",
    "_external_media_filename",
]
