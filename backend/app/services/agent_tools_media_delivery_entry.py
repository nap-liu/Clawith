from __future__ import annotations

import json
from pathlib import Path
import uuid

from loguru import logger

from app.services.agent_runtime_workspace import current_agent_runtime_workspace
from app.services.agent_tools_channel_file_receipts import (
    _build_outbound_operation_key,
    _normalize_tool_workspace_rel_path,
)
from app.services.agent_tools_config_runtime import _get_tool_config
from app.services.agent_tools_media_delivery_core import (
    _describe_media_delivery_result,
    _preflight_managed_media_target,
    _sniff_image_file,
    _sniff_media_file_mime,
)
from app.services.agent_tools_media_delivery_recipient import (
    _send_media_to_recipient,
)
from app.services.agent_tools_media_delivery_replay_publish import (
    _publish_external_media_to_session,
    _replay_terminal_media_delivery,
)
from app.services.agent_tools_media_delivery_runtime import _send_media_to_session
from app.services.agent_tools_file_support import _agent_workspace_root
from app.services.media_tool_contract import normalize_media_display_title
from app.services.agent_tools_media_limits import MEDIA_TOOL_MAX_FILE_BYTES
from app.services.media_url_source import (
    MediaUrlError,
    import_managed_media_url,
    normalize_managed_media_headers,
    validate_media_url,
)
from app.services.storage import get_storage_backend
from app.services.user_output import sanitize_user_visible_text


async def _send_channel_media(
    agent_id: uuid.UUID,
    ws: Path,
    arguments: dict,
    *,
    media_kind: str,
    tool_call_id: str | None = None,
    origin_session_id: str | None = None,
    origin_turn_anchor_id: uuid.UUID | None = None,
) -> str:
    """Create one audio/video delivery without leaking renderer/IM details."""
    arguments = dict(arguments)
    if "message" in arguments:
        arguments["message"] = sanitize_user_visible_text(
            str(arguments.get("message") or "")
        ).strip()
    if "title" in arguments:
        arguments["title"] = normalize_media_display_title(
            sanitize_user_visible_text(str(arguments.get("title") or ""))
        )
    if not str(tool_call_id or "").strip():
        return json.dumps(
            {
                "type": "media_delivery_result",
                "version": 1,
                "status": "failed",
                "code": "MISSING_DELIVERY_INTENT_ID",
                "media_kind": media_kind,
            },
            ensure_ascii=False,
        )
    raw_rel_path = arguments.get("file_path", "")
    raw_url = arguments.get("url", "")
    rel_path = raw_rel_path.strip() if isinstance(raw_rel_path, str) else ""
    media_url = raw_url.strip() if isinstance(raw_url, str) else ""
    url_mode = str(arguments.get("url_mode") or "").strip().lower()
    has_headers = "headers" in arguments
    managed_headers: dict[str, str] = {}
    has_file = bool(rel_path)
    has_url = bool(media_url)
    if has_file == has_url:
        return json.dumps(
            {
                "type": "media_delivery_result",
                "version": 1,
                "status": "failed",
                "code": "INVALID_MEDIA_SOURCE",
                "media_kind": media_kind,
            },
            ensure_ascii=False,
        )
    if has_file and url_mode:
        return json.dumps(
            {
                "type": "media_delivery_result",
                "version": 1,
                "status": "failed",
                "code": "INVALID_MEDIA_SOURCE",
                "media_kind": media_kind,
            },
            ensure_ascii=False,
        )
    if has_url and url_mode not in {"external", "managed"}:
        return json.dumps(
            {
                "type": "media_delivery_result",
                "version": 1,
                "status": "failed",
                "code": "INVALID_URL_MODE",
                "media_kind": media_kind,
            },
            ensure_ascii=False,
        )
    if has_headers and not (has_url and url_mode == "managed"):
        return json.dumps(
            {
                "type": "media_delivery_result",
                "version": 1,
                "status": "failed",
                "code": "INVALID_MEDIA_HEADERS",
                "media_kind": media_kind,
            },
            ensure_ascii=False,
        )
    if has_headers:
        try:
            managed_headers = normalize_managed_media_headers(arguments.get("headers"))
        except MediaUrlError as exc:
            return json.dumps(
                {
                    "type": "media_delivery_result",
                    "version": 1,
                    "status": "failed",
                    "code": exc.code,
                    "media_kind": media_kind,
                },
                ensure_ascii=False,
            )

    canonical_user_id = str(arguments.get("user_id") or "").strip()
    requested_session_id = str(arguments.get("session_id") or "").strip()
    requested_channel = str(arguments.get("channel") or "").strip().lower() or None
    if canonical_user_id and requested_session_id:
        return json.dumps(
            {
                "type": "media_delivery_result",
                "version": 1,
                "status": "failed",
                "code": "AMBIGUOUS_MEDIA_TARGET",
                "media_kind": media_kind,
            },
            ensure_ascii=False,
        )
    if requested_channel and not canonical_user_id:
        return json.dumps(
            {
                "type": "media_delivery_result",
                "version": 1,
                "status": "failed",
                "code": "CHANNEL_REQUIRES_USER_TARGET",
                "media_kind": media_kind,
            },
            ensure_ascii=False,
        )
    target_session_id = requested_session_id or (str(origin_session_id or "").strip() if not canonical_user_id else "")
    if not target_session_id and not canonical_user_id:
        return json.dumps(
            {
                "type": "media_delivery_result",
                "version": 1,
                "status": "failed",
                "code": "SESSION_REQUIRED",
                "media_kind": media_kind,
                "intent_id": tool_call_id or "",
            },
            ensure_ascii=False,
        )

    file_path: Path | None = None
    managed_import = None
    source_mode = "workspace"
    if has_file:
        rel_path = _normalize_tool_workspace_rel_path(rel_path)
        if not rel_path:
            return json.dumps(
                {
                    "type": "media_delivery_result",
                    "version": 1,
                    "status": "failed",
                    "code": "INVALID_FILE_PATH",
                    "media_kind": media_kind,
                },
                ensure_ascii=False,
            )
        agent_root = ws.resolve()
        file_path = (agent_root / rel_path).resolve()
        try:
            file_path.relative_to(agent_root)
        except ValueError:
            return json.dumps(
                {
                    "type": "media_delivery_result",
                    "version": 1,
                    "status": "failed",
                    "code": "INVALID_FILE_PATH",
                    "media_kind": media_kind,
                },
                ensure_ascii=False,
            )
        if not file_path.exists() or not file_path.is_file():
            return json.dumps(
                {
                    "type": "media_delivery_result",
                    "version": 1,
                    "status": "failed",
                    "code": "MEDIA_NOT_FOUND",
                    "media_kind": media_kind,
                },
                ensure_ascii=False,
            )
        actual_mime = _sniff_media_file_mime(file_path)
        actual_kind = actual_mime.split("/", 1)[0] if actual_mime else None
        if actual_kind != media_kind:
            return json.dumps(
                {
                    "type": "media_delivery_result",
                    "version": 1,
                    "status": "failed",
                    "code": "MEDIA_KIND_MISMATCH",
                    "media_kind": media_kind,
                    "actual_kind": actual_kind,
                },
                ensure_ascii=False,
            )
    else:
        if url_mode == "external":
            try:
                media_url = await validate_media_url(media_url, external=True)
            except MediaUrlError as exc:
                return json.dumps(
                    {
                        "type": "media_delivery_result",
                        "version": 1,
                        "status": "failed",
                        "code": exc.code,
                        "media_kind": media_kind,
                    },
                    ensure_ascii=False,
                )

    cover_path: Path | None = None
    if media_kind == "video" and arguments.get("cover_image_path"):
        raw_cover_path = str(arguments.get("cover_image_path") or "").strip()
        cover_rel_path = _normalize_tool_workspace_rel_path(raw_cover_path)
        if not cover_rel_path:
            return json.dumps(
                {
                    "type": "media_delivery_result",
                    "version": 1,
                    "status": "failed",
                    "code": "INVALID_COVER_PATH",
                    "media_kind": media_kind,
                }
            )
        cover_path = (ws / cover_rel_path).resolve()
        try:
            cover_path.relative_to(ws.resolve())
        except ValueError:
            return json.dumps(
                {
                    "type": "media_delivery_result",
                    "version": 1,
                    "status": "failed",
                    "code": "INVALID_COVER_PATH",
                    "media_kind": media_kind,
                }
            )
        if not cover_path.exists() or not cover_path.is_file() or _sniff_image_file(cover_path) is None:
            return json.dumps(
                {
                    "type": "media_delivery_result",
                    "version": 1,
                    "status": "failed",
                    "code": "INVALID_VIDEO_COVER",
                    "media_kind": media_kind,
                }
            )

    if has_url and url_mode == "managed":
        replay = await _replay_terminal_media_delivery(
            agent_id=agent_id,
            origin_session_id=origin_session_id,
            intent_id=tool_call_id or "",
            origin_turn_anchor_id=origin_turn_anchor_id,
        )
        if replay is not None:
            return json.dumps(replay, ensure_ascii=False)
        preflight_error = await _preflight_managed_media_target(
            agent_id=agent_id,
            session_id=target_session_id,
            user_id=canonical_user_id,
            channel=requested_channel,
            media_kind=media_kind,
            intent_id=tool_call_id or "",
        )
        if preflight_error is not None:
            return json.dumps(
                _describe_media_delivery_result(preflight_error),
                ensure_ascii=False,
            )

    tool_config = await _get_tool_config(agent_id, "send_media") or {}
    allow_download = tool_config.get("allow_download") is True
    if has_url and url_mode == "external":
        if canonical_user_id:
            return json.dumps(
                _describe_media_delivery_result(
                    {
                        "type": "media_delivery_result",
                        "version": 1,
                        "status": "unsupported",
                        "code": "EXTERNAL_URL_NOT_SUPPORTED_BY_CHANNEL",
                        "media_kind": media_kind,
                        "intent_id": tool_call_id or "",
                    }
                ),
                ensure_ascii=False,
            )
        return await _publish_external_media_to_session(
            agent_id=agent_id,
            session_id=target_session_id,
            media_url=media_url,
            media_kind=media_kind,
            caption=str(arguments.get("message") or ""),
            intent_id=tool_call_id or "",
            origin_session_id=origin_session_id,
            origin_turn_anchor_id=origin_turn_anchor_id,
            allow_download=allow_download,
            tool_args=arguments,
        )

    if has_url:
        try:
            imported = await import_managed_media_url(
                media_url,
                agent_workspace=_agent_workspace_root(agent_id),
                session_id=origin_session_id or target_session_id or None,
                intent_id=tool_call_id or "",
                max_bytes=MEDIA_TOOL_MAX_FILE_BYTES,
                expected_media_kind=media_kind,
                operation_scope=(
                    _build_outbound_operation_key(
                        agent_id=agent_id,
                        origin_session_id=(origin_session_id or target_session_id or None),
                        tool_call_id=tool_call_id,
                        origin_turn_anchor_id=origin_turn_anchor_id,
                    )
                    or f"outbound-untracked:{agent_id}:{uuid.uuid4().hex}"
                ),
                request_headers=managed_headers,
            )
        except MediaUrlError as exc:
            payload = {
                "type": "media_delivery_result",
                "version": 1,
                "status": "failed",
                "code": exc.code,
                "media_kind": media_kind,
                "intent_id": tool_call_id or "",
            }
            if exc.http_status is not None:
                payload["http_status"] = exc.http_status
            if exc.actual_kind is not None:
                payload["actual_kind"] = exc.actual_kind
            return json.dumps(_describe_media_delivery_result(payload), ensure_ascii=False)
        file_path = imported.file_path
        rel_path = imported.workspace_path
        managed_import = imported
        source_mode = "managed_url"

    try:
        if managed_import is not None:
            try:
                await get_storage_backend().write_local_file(
                    current_agent_runtime_workspace(agent_id).storage_key(rel_path),
                    file_path,
                    content_type=managed_import.mime_type,
                )
            except Exception:
                logger.opt(exception=True).error("[SessionMedia] Managed import persistence failed")
                return json.dumps(
                    _describe_media_delivery_result(
                        {
                            "type": "media_delivery_result",
                            "version": 1,
                            "status": "failed",
                            "code": "MEDIA_STORAGE_FAILED",
                            "media_kind": media_kind,
                            "intent_id": tool_call_id or "",
                            "managed_path": rel_path,
                        }
                    ),
                    ensure_ascii=False,
                )

        assert file_path is not None and rel_path is not None
        if target_session_id:
            return await _send_media_to_session(
                agent_id=agent_id,
                session_id=target_session_id,
                file_path=file_path,
                workspace_path=rel_path,
                media_kind=media_kind,
                caption=str(arguments.get("message") or ""),
                cover_path=cover_path,
                intent_id=tool_call_id or "",
                origin_session_id=origin_session_id,
                origin_turn_anchor_id=origin_turn_anchor_id,
                allow_download=allow_download,
                source_mode=source_mode,
                tool_args=arguments,
            )
        if canonical_user_id:
            return await _send_media_to_recipient(
                agent_id=agent_id,
                file_path=file_path,
                workspace_path=rel_path,
                user_id=canonical_user_id,
                channel=requested_channel,
                media_kind=media_kind,
                caption=str(arguments.get("message") or ""),
                cover_path=cover_path,
                intent_id=tool_call_id or "",
                origin_session_id=origin_session_id,
                origin_turn_anchor_id=origin_turn_anchor_id,
                allow_download=allow_download,
                source_mode=source_mode,
                tool_args=arguments,
            )

        raise AssertionError("validated media target was not routed")
    finally:
        if managed_import is not None:
            close_import = getattr(managed_import, "close", None)
            if callable(close_import):
                close_import()


__all__ = [
    "MEDIA_TOOL_MAX_FILE_BYTES",
    "_send_channel_media",
]
