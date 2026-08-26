"""DingTalk Stream Connection Manager.

Manages WebSocket-based Stream connections for DingTalk bots, similar to feishu_ws.py.
Uses the dingtalk-stream SDK to receive bot messages via persistent connections.
"""

import asyncio
import json
import tempfile
import threading
import uuid
from concurrent.futures import CancelledError as FutureCancelledError
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import quote_plus

import httpx
from loguru import logger
from sqlalchemy import select, update

from app.config import get_settings
from app.database import async_session
from app.models.agent import Agent
from app.models.channel_config import ChannelConfig
from app.models.tenant import Tenant
from app.services.channel_dispatch import (
    ChannelReactions,
    channel_session_lock_key,
    run_channel_message,
)
from app.services.dingtalk_credentials import dingtalk_credential_fingerprint
from app.services.dingtalk_quoted_message import parse_dingtalk_quoted_message
from app.services.dingtalk_token import dingtalk_token_manager
from app.services.im_delivery import ProviderResponseUncertainError
from app.services.storage import store_agent_upload

DINGTALK_VOICE_MAX_BYTES = 2 * 1024 * 1024
DINGTALK_VIDEO_MAX_BYTES = 20 * 1024 * 1024

# ─── DingTalk Media Helpers ─────────────────────────────


async def _get_media_download_url(
    access_token: str, download_code: str, robot_code: str
) -> Optional[str]:
    """Get media file download URL from DingTalk API."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://api.dingtalk.com/v1.0/robot/messageFiles/download",
                headers={"x-acs-dingtalk-access-token": access_token},
                json={"downloadCode": download_code, "robotCode": robot_code},
            )
            data = resp.json()
            url = data.get("downloadUrl")
            if url:
                return url
            logger.error(f"[DingTalk] Failed to get download URL: {data}")
            return None
    except Exception as e:
        logger.error(f"[DingTalk] Error getting download URL: {e}")
        return None


async def _download_file(url: str) -> Optional[bytes]:
    """Download a file from a URL and return its bytes."""
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.content
    except Exception as e:
        logger.error(f"[DingTalk] Error downloading file: {e}")
        return None


async def _download_dingtalk_media(
    app_key: str, app_secret: str, download_code: str
) -> Optional[bytes]:
    """Download a media file from DingTalk using downloadCode.

    Steps: get access_token -> get download URL -> download file bytes.
    """
    access_token = await dingtalk_token_manager.get_token(app_key, app_secret)
    if not access_token:
        return None

    download_url = await _get_media_download_url(access_token, download_code, app_key)
    if not download_url:
        return None

    return await _download_file(download_url)


async def _store_dingtalk_upload(
    agent_id: uuid.UUID,
    filename: str,
    file_bytes: bytes,
    *,
    content_type: str | None = None,
) -> str | None:
    """Store one inbound media object without crashing the stream callback."""
    try:
        _, workspace_path, _ = await store_agent_upload(
            agent_id,
            filename,
            file_bytes,
            content_type=content_type,
        )
        return workspace_path
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[DingTalk] Failed to store inbound media {filename}: {exc}")
        return None


async def _parse_dingtalk_quoted_message(
    msg_data: dict[str, Any],
    app_key: str,
    app_secret: str,
    agent_id: uuid.UUID,
) -> dict[str, Any] | None:
    return await parse_dingtalk_quoted_message(
        msg_data,
        app_key,
        app_secret,
        agent_id,
        download_media=_download_dingtalk_media,
        store_upload=_store_dingtalk_upload,
    )


async def _process_media_message(
    msg_data: dict,
    app_key: str,
    app_secret: str,
    agent_id: uuid.UUID,
) -> tuple[str, list[str] | None]:
    """Process a DingTalk message and extract text + media info.

    Returns:
        (user_text, saved_file_paths)
        - user_text: text content for the LLM
        - saved_file_paths: list of saved file paths, or None
    """
    msgtype = msg_data.get("msgtype", "text")
    logger.info(f"[DingTalk] Processing message type: {msgtype}")

    saved_file_paths: list[str] = []

    if msgtype == "text":
        # Plain text — handled by existing logic, return empty
        text_content = msg_data.get("text", {}).get("content", "").strip()
        return text_content, None

    elif msgtype == "picture":
        # Image message
        download_code = msg_data.get("content", {}).get("downloadCode", "")
        if not download_code:
            # Try alternate location
            download_code = msg_data.get("downloadCode", "")
        if not download_code:
            logger.warning("[DingTalk] Picture message without downloadCode")
            return "[用户发送了图片，但无法下载]", None

        file_bytes = await _download_dingtalk_media(app_key, app_secret, download_code)
        if not file_bytes:
            return "[用户发送了图片，但下载失败]", None

        # Save to disk via storage abstraction
        filename = f"dingtalk_img_{uuid.uuid4().hex[:8]}.jpg"
        workspace_path = await _store_dingtalk_upload(
            agent_id,
            filename,
            file_bytes,
            content_type="image/jpeg",
        )
        if workspace_path is None:
            return "[用户发送了图片，但保存失败]", None
        logger.info(f"[DingTalk] Saved image to {workspace_path} ({len(file_bytes)} bytes)")

        return (
            "[用户发送了图片]",
            [workspace_path],
        )

    elif msgtype == "richText":
        # Rich text: may contain text segments + images
        rich_text = msg_data.get("content", {}).get("richText", [])
        text_parts: list[str] = []

        for section in rich_text:
            for item in section if isinstance(section, list) else [section]:
                if "text" in item:
                    text_parts.append(item["text"])
                elif "downloadCode" in item:
                    # Inline image in rich text
                    file_bytes = await _download_dingtalk_media(
                        app_key, app_secret, item["downloadCode"]
                    )
                    if file_bytes:
                        filename = f"dingtalk_richimg_{uuid.uuid4().hex[:8]}.jpg"
                        workspace_path = await _store_dingtalk_upload(
                            agent_id,
                            filename,
                            file_bytes,
                            content_type="image/jpeg",
                        )
                        if workspace_path is not None:
                            logger.info(f"[DingTalk] Saved rich text image to {workspace_path}")
                            saved_file_paths.append(workspace_path)

        combined_text = "\n".join(text_parts).strip()
        if not combined_text:
            combined_text = "[用户发送了富文本消息]"

        return (
            combined_text,
            saved_file_paths if saved_file_paths else None,
        )

    elif msgtype == "audio":
        # Audio message — prefer recognition text if available
        content = msg_data.get("content", {})
        recognition = content.get("recognition", "")
        if recognition:
            logger.info(f"[DingTalk] Audio with recognition: {recognition[:80]}")
            return f"[语音消息] {recognition}", None

        # No recognition — try to download the audio file
        download_code = content.get("downloadCode", "")
        if download_code:
            file_bytes = await _download_dingtalk_media(app_key, app_secret, download_code)
            if file_bytes:
                duration = content.get("duration", "unknown")
                filename = f"dingtalk_audio_{uuid.uuid4().hex[:8]}.amr"
                workspace_path = await _store_dingtalk_upload(agent_id, filename, file_bytes)
                if workspace_path is not None:
                    logger.info(f"[DingTalk] Saved audio to {workspace_path} ({len(file_bytes)} bytes)")
                    return (
                        f"[用户发送了语音消息，时长{duration}ms，已保存到 {filename}]",
                        [workspace_path],
                    )
        return "[用户发送了语音消息，但无法处理]", None

    elif msgtype == "video":
        # Video message
        content = msg_data.get("content", {})
        download_code = content.get("downloadCode", "")
        if download_code:
            file_bytes = await _download_dingtalk_media(app_key, app_secret, download_code)
            if file_bytes:
                duration = content.get("duration", "unknown")
                filename = f"dingtalk_video_{uuid.uuid4().hex[:8]}.mp4"
                workspace_path = await _store_dingtalk_upload(agent_id, filename, file_bytes)
                if workspace_path is not None:
                    logger.info(f"[DingTalk] Saved video to {workspace_path} ({len(file_bytes)} bytes)")
                    return (
                        f"[用户发送了视频，时长{duration}ms，已保存到 {filename}]",
                        [workspace_path],
                    )
        return "[用户发送了视频，但无法下载]", None

    elif msgtype == "file":
        # File message
        content = msg_data.get("content", {})
        download_code = content.get("downloadCode", "")
        original_filename = content.get("fileName", "unknown_file")
        if download_code:
            file_bytes = await _download_dingtalk_media(app_key, app_secret, download_code)
            if file_bytes:
                # Preserve original filename, add prefix to avoid collision
                safe_name = f"dingtalk_{uuid.uuid4().hex[:8]}_{original_filename}"
                workspace_path = await _store_dingtalk_upload(agent_id, safe_name, file_bytes)
                if workspace_path is not None:
                    logger.info(
                        f"[DingTalk] Saved file '{original_filename}' to {workspace_path} "
                        f"({len(file_bytes)} bytes)"
                    )
                    return (
                        f"[file:{original_filename}]",
                        [workspace_path],
                    )
        return f"[用户发送了文件 {original_filename}，但无法下载]", None

    else:
        logger.warning(f"[DingTalk] Unsupported message type: {msgtype}")
        return f"[用户发送了 {msgtype} 类型消息，暂不支持]", None


# ─── DingTalk Media Upload & Send ───────────────────────

async def _upload_dingtalk_media(
    app_key: str,
    app_secret: str,
    file_path: str,
    media_type: str = "file",
    *,
    raise_on_transport_error: bool = False,
) -> Optional[str]:
    """Upload a media file to DingTalk and return the mediaId.

    Args:
        app_key: DingTalk app key (robotCode).
        app_secret: DingTalk app secret.
        file_path: Local file path to upload.
        media_type: One of 'image', 'voice', 'video', 'file'.

    Returns:
        mediaId string on success, None on failure.
    """
    access_token = await dingtalk_token_manager.get_token(app_key, app_secret)
    if not access_token:
        return None

    file_p = Path(file_path)
    if not file_p.exists():
        logger.error(f"[DingTalk] Upload failed: file not found: {file_path}")
        return None

    try:
        file_bytes = await asyncio.to_thread(file_p.read_bytes)
        async with httpx.AsyncClient(timeout=60) as client:
            # Use the legacy oapi endpoint which is more reliable and widely supported.
            # The newer api.dingtalk.com/v1.0/robot/messageFiles/upload requires
            # additional permissions and returns InvalidAction.NotFound for some apps.
            upload_url = (
                f"https://oapi.dingtalk.com/media/upload"
                f"?access_token={access_token}&type={media_type}"
            )
            resp = await client.post(
                upload_url,
                files={"media": (file_p.name, file_bytes)},
            )
            data = resp.json()
            # Legacy API returns media_id (snake_case), new API returns mediaId
            media_id = data.get("media_id") or data.get("mediaId")
            if media_id and data.get("errcode", 0) == 0:
                logger.info(
                    f"[DingTalk] Uploaded {media_type} '{file_p.name}' -> mediaId={media_id[:20]}..."
                )
                return media_id
            logger.error(f"[DingTalk] Upload failed: {data}")
            return None
    except Exception as e:
        logger.error(f"[DingTalk] Upload error: {e}")
        if raise_on_transport_error:
            raise
        return None


async def _send_dingtalk_media_message(
    app_key: str,
    app_secret: str,
    target_id: str,
    media_id: str,
    media_type: str,
    conversation_type: str,
    filename: Optional[str] = None,
    pic_media_id: Optional[str] = None,
    duration_ms: int = 60_000,
    *,
    raise_on_transport_error: bool = False,
    on_result: Callable[[dict], Awaitable[None]] | None = None,
) -> bool:
    """Send a media message via DingTalk proactive message API.

    Args:
        app_key: DingTalk app key (robotCode).
        app_secret: DingTalk app secret.
        target_id: For P2P: sender_staff_id; For group: openConversationId.
        media_id: The mediaId from upload.
        media_type: One of 'image', 'voice', 'video', 'file'.
        conversation_type: '1' for P2P, '2' for group.
        filename: Original filename (used for file/video types).
        pic_media_id: Required thumbnail mediaId for native video messages.
        duration_ms: Video/audio duration advertised to DingTalk, in milliseconds.

    Returns:
        True on success, False on failure.
    """
    access_token = await dingtalk_token_manager.get_token(app_key, app_secret)
    if not access_token:
        return False

    headers = {"x-acs-dingtalk-access-token": access_token}

    # Build msgKey and msgParam based on media_type
    if media_type == "image":
        msg_key = "sampleImageMsg"
        msg_param = json.dumps({"photoURL": media_id})
    elif media_type == "voice":
        msg_key = "sampleAudio"
        msg_param = json.dumps({"mediaId": media_id, "duration": str(duration_ms)})
    elif media_type == "video":
        if not pic_media_id:
            logger.error("[DingTalk] Native video requires picMediaId; refusing file-card fallback")
            return False
        safe_name = filename or "video.mp4"
        ext = Path(safe_name).suffix.lstrip(".") or "mp4"
        msg_key = "sampleVideo"
        msg_param = json.dumps({
            "duration": str(duration_ms),
            "videoMediaId": media_id,
            "videoType": ext,
            "picMediaId": pic_media_id,
        })
    else:
        # file
        safe_name = filename or "file"
        ext = Path(safe_name).suffix.lstrip(".") or "bin"
        msg_key = "sampleFile"
        msg_param = json.dumps({
            "mediaId": media_id,
            "fileName": safe_name,
            "fileType": ext,
        })

    data: dict
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            if conversation_type == "2":
                # Group chat
                resp = await client.post(
                    "https://api.dingtalk.com/v1.0/robot/groupMessages/send",
                    headers=headers,
                    json={
                        "robotCode": app_key,
                        "openConversationId": target_id,
                        "msgKey": msg_key,
                        "msgParam": msg_param,
                    },
                )
            else:
                # P2P chat
                resp = await client.post(
                    "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend",
                    headers=headers,
                    json={
                        "robotCode": app_key,
                        "userIds": [target_id],
                        "msgKey": msg_key,
                        "msgParam": msg_param,
                    },
                )

            try:
                data = resp.json()
            except ValueError as exc:
                raise ProviderResponseUncertainError(
                    "DingTalk media send returned an unreadable response"
                ) from exc
            # Check for error
            if resp.status_code >= 400 or data.get("errcode"):
                logger.error(f"[DingTalk] Send media failed: {data}")
                return False

    except Exception as e:
        logger.error(f"[DingTalk] Send media error: {e}")
        if raise_on_transport_error:
            raise
        return False

    logger.info(
        f"[DingTalk] Sent {media_type} message to {target_id[:16]}... "
        f"(conv_type={conversation_type})"
    )
    # Provider I/O is already successful. Observer failures must propagate so
    # callers cannot reinterpret them as send failures and emit a duplicate.
    if on_result is not None:
        await on_result(data)
    return True


def _create_dingtalk_video_thumbnail(video_path: Path) -> Path | None:
    """Create a dependency-light thumbnail accepted by DingTalk sampleVideo."""
    try:
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (640, 360), "#152033")
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((244, 104, 396, 256), radius=76, fill="#1677ff")
        draw.polygon(((305, 145), (305, 215), (365, 180)), fill="white")
        label = video_path.name[:64]
        draw.text((24, 324), label, fill="#dbe7f7")
        handle = tempfile.NamedTemporaryFile(
            prefix="clawith-dingtalk-video-", suffix=".jpg", delete=False
        )
        handle.close()
        thumbnail = Path(handle.name)
        image.save(thumbnail, format="JPEG", quality=86, optimize=True)
        return thumbnail
    except Exception as exc:
        logger.error(f"[DingTalk] Video thumbnail creation failed: {type(exc).__name__}")
        return None


async def _send_dingtalk_native_video(
    app_key: str,
    app_secret: str,
    target_id: str,
    file_path: Path,
    conversation_type: str,
    cover_image_path: Path | None = None,
    *,
    raise_on_transport_error: bool = False,
    on_result: Callable[[dict], Awaitable[None]] | None = None,
) -> tuple[bool, str]:
    """Upload video + thumbnail and send a real sampleVideo message."""
    if file_path.stat().st_size > DINGTALK_VIDEO_MAX_BYTES:
        return False, "MEDIA_TOO_LARGE"
    generated_thumbnail = cover_image_path is None
    thumbnail = cover_image_path or _create_dingtalk_video_thumbnail(file_path)
    if thumbnail is None:
        return False, "VIDEO_THUMBNAIL_FAILED"
    try:
        # DingTalk's OAPI upload expects videos used by sampleVideo as file media.
        strict_kwargs = {"raise_on_transport_error": True} if raise_on_transport_error else {}
        video_media_id = await _upload_dingtalk_media(
            app_key, app_secret, str(file_path), "file", **strict_kwargs
        )
        if not video_media_id:
            return False, "MEDIA_UPLOAD_FAILED"
        pic_media_id = await _upload_dingtalk_media(
            app_key, app_secret, str(thumbnail), "image", **strict_kwargs
        )
        if not pic_media_id:
            return False, "VIDEO_THUMBNAIL_UPLOAD_FAILED"
        sent = await _send_dingtalk_media_message(
            app_key,
            app_secret,
            target_id,
            video_media_id,
            "video",
            conversation_type,
            filename=file_path.name,
            pic_media_id=pic_media_id,
            on_result=on_result,
            **strict_kwargs,
        )
        return sent, "MEDIA_SENT" if sent else "MEDIA_SEND_FAILED"
    finally:
        if generated_thumbnail:
            try:
                thumbnail.unlink(missing_ok=True)
            except OSError:
                logger.warning("[DingTalk] Failed to remove temporary video thumbnail")


# ─── Stream Manager ─────────────────────────────────────


def _dingtalk_lock_key(
    agent_id: uuid.UUID,
    conversation_type: str,
    conversation_id: str,
    sender_staff_id: str,
) -> str:
    """构造与 DB 会话一一对应的 per-session 锁 key(与 dingtalk.py 的 conv_id 同构)。"""
    conv_id = (
        f"dingtalk_group_{conversation_id}"
        if conversation_type == "2"
        else f"dingtalk_p2p_{sender_staff_id}"
    )
    return channel_session_lock_key(agent_id, "dingtalk", conv_id)


def _make_dingtalk_reactions(app_key: str, app_secret: str, message_id: str, conversation_id: str) -> ChannelReactions:
    """钉钉的整轮 reaction:按 thinking/tool 事件动态切换,完成或失败时撤回。"""
    from app.services.dingtalk_reaction import DingTalkReactionController, add_reaction, recall_reaction

    async def _attach(reaction: str, _ak=app_key, _as=app_secret, _mid=message_id, _cid=conversation_id) -> bool:
        return await add_reaction(_ak, _as, _mid, _cid, reaction)

    async def _recall(reaction: str, _ak=app_key, _as=app_secret, _mid=message_id, _cid=conversation_id) -> None:
        await recall_reaction(_ak, _as, _mid, _cid, reaction)

    controller = DingTalkReactionController(
        attach_reaction=_attach,
        recall_reaction=_recall,
    )
    return ChannelReactions(
        on_consume=controller.on_consume,
        on_complete=controller.on_complete,
        on_error=controller.on_error,
        on_tool_call=controller.on_tool_call,
        on_thinking=controller.on_thinking,
    )


def _fire_and_forget(loop, coro):
    """Schedule a coroutine on the main loop and log any unhandled exception."""
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    def _on_done(f):
        try:
            f.result()
        except FutureCancelledError:
            logger.debug("[DingTalk Stream] fire-and-forget coroutine was cancelled")
        except Exception:
            logger.exception("[DingTalk Stream] Unhandled error in fire-and-forget coroutine")
    future.add_done_callback(_on_done)


def _connector_role_enabled() -> bool:
    roles = {
        role.strip().lower()
        for role in (get_settings().PROCESS_ROLE or "all").split(",")
        if role.strip()
    }
    return not roles or "all" in roles or "connector" in roles


@dataclass
class _StreamRuntime:
    generation: int
    fingerprint: str
    app_key: str
    thread: threading.Thread
    stop_event: threading.Event
    loop: asyncio.AbstractEventLoop | None = None
    async_stop_event: asyncio.Event | None = None
    # None means the process has not published its first observed state yet.
    # This ensures a stale persisted is_connected=true is cleared on startup
    # even when the first connection attempt fails.
    ready: bool | None = None
    persisted_ready: bool | None = None


class DingTalkStreamManager:
    """Manages DingTalk Stream clients for all agents."""

    def __init__(self):
        self._runtimes: dict[uuid.UUID, _StreamRuntime] = {}
        self._locks: dict[uuid.UUID, asyncio.Lock] = {}
        self._state_locks: dict[uuid.UUID, asyncio.Lock] = {}
        self._generation: dict[uuid.UUID, int] = {}
        self._main_loop: asyncio.AbstractEventLoop | None = None

    def _lock_for(self, agent_id: uuid.UUID) -> asyncio.Lock:
        lock = self._locks.get(agent_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[agent_id] = lock
        return lock

    def _state_lock_for(self, agent_id: uuid.UUID) -> asyncio.Lock:
        lock = self._state_locks.get(agent_id)
        if lock is None:
            lock = asyncio.Lock()
            self._state_locks[agent_id] = lock
        return lock

    async def start_client(
        self,
        agent_id: uuid.UUID,
        app_key: str,
        app_secret: str,
        stop_existing: bool = True,
        verify_persisted: bool = True,
    ):
        """Start a DingTalk Stream client for a specific agent."""
        if not _connector_role_enabled():
            logger.debug(
                f"[DingTalk Stream] Ignore start for {agent_id}: PROCESS_ROLE has no connector ownership"
            )
            return
        if not app_key or not app_secret:
            logger.warning(f"[DingTalk Stream] Missing credentials for {agent_id}, skipping")
            return
        if verify_persisted:
            try:
                desired = await self._load_persisted_desired_credentials(agent_id)
            except Exception:
                logger.exception(
                    f"[DingTalk Stream] Cannot verify committed credentials for {agent_id}; "
                    "deferring to reconciler"
                )
                return
            if desired is None or dingtalk_credential_fingerprint(*desired) != (
                dingtalk_credential_fingerprint(app_key, app_secret)
            ):
                logger.debug(
                    f"[DingTalk Stream] Deferring start for {agent_id} until credentials commit"
                )
                return

        if self._main_loop is None:
            self._main_loop = asyncio.get_running_loop()
        fingerprint = dingtalk_credential_fingerprint(app_key, app_secret)
        async with self._lock_for(agent_id):
            runtime = self._runtimes.get(agent_id)
            if (
                runtime is not None
                and runtime.fingerprint == fingerprint
                and runtime.thread.is_alive()
            ):
                return
            if runtime is not None and (stop_existing or runtime.fingerprint != fingerprint):
                stopped = await self._stop_runtime_locked(agent_id, runtime)
                if not stopped:
                    logger.error(
                        f"[DingTalk Stream] Refusing to start a second client for {agent_id}: "
                        "the previous runner did not exit"
                    )
                    return

            generation = self._generation.get(agent_id, 0) + 1
            self._generation[agent_id] = generation
            stop_event = threading.Event()
            thread = threading.Thread(
                target=self._run_client_thread,
                args=(agent_id, app_key, app_secret, stop_event, generation, fingerprint),
                name=f"dingtalk-stream-{str(agent_id)[:8]}",
                daemon=True,
            )
            runtime = _StreamRuntime(
                generation=generation,
                fingerprint=fingerprint,
                app_key=app_key,
                thread=thread,
                stop_event=stop_event,
            )
            self._runtimes[agent_id] = runtime
            thread.start()
            logger.info(
                f"[DingTalk Stream] Client runner started for agent {agent_id} "
                f"(AppKey: {app_key[:8]}..., generation={generation})"
            )

    def _run_client_thread(
        self,
        agent_id: uuid.UUID,
        app_key: str,
        app_secret: str,
        stop_event: threading.Event,
        generation: int,
        fingerprint: str,
    ):
        """Run the DingTalk Stream client with auto-reconnect."""
        main_loop = self._main_loop
        try:
            import dingtalk_stream
        except ImportError:
            logger.warning(
                "[DingTalk Stream] dingtalk-stream package not installed. "
                "Install with: pip install dingtalk-stream"
            )
            if main_loop and main_loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self._handle_runner_exit(agent_id, generation, fingerprint),
                    main_loop,
                )
            return

        RETRY_DELAYS = [2, 5, 15, 30, 60]  # exponential backoff, seconds

        class ClawithChatbotHandler(dingtalk_stream.ChatbotHandler):
            """Custom handler that dispatches messages to the shared LLM pipeline."""

            async def process(self, callback: dingtalk_stream.CallbackMessage):
                """Handle incoming bot message from DingTalk Stream.

                NOTE: The SDK invokes this method in the thread's own asyncio loop,
                so we must dispatch to the main FastAPI loop for DB + LLM work.
                """
                try:
                    # Parse the raw data
                    incoming = dingtalk_stream.ChatbotMessage.from_dict(callback.data)
                    msg_data = callback.data if isinstance(callback.data, dict) else json.loads(callback.data)

                    msgtype = msg_data.get("msgtype", "text")
                    sender_staff_id = incoming.sender_staff_id or ""
                    sender_id = incoming.sender_id or ""
                    if not sender_staff_id and sender_id:
                        sender_staff_id = sender_id  # fallback
                    sender_nick = incoming.sender_nick or ""
                    message_id = incoming.message_id or ""
                    conversation_id = incoming.conversation_id or ""
                    conversation_type = incoming.conversation_type or "1"
                    conversation_title = (incoming.conversation_title or "").strip()

                    logger.info(
                        f"[DingTalk Stream] Received {msgtype} message from {sender_staff_id}"
                    )

                    if msgtype == "text":
                        # Plain text — use existing logic
                        text_list = incoming.get_text_list()
                        user_text = " ".join(text_list).strip() if text_list else ""
                        if not user_text:
                            return dingtalk_stream.AckMessage.STATUS_OK, "empty message"

                        logger.info(
                            f"[DingTalk Stream] Text from {sender_staff_id}: {user_text[:80]}"
                        )

                        from app.api.dingtalk import process_dingtalk_message
                        from app.services.channel_commands import is_channel_command

                        if main_loop and main_loop.is_running():
                            lock_key = _dingtalk_lock_key(
                                agent_id,
                                conversation_type,
                                conversation_id,
                                sender_staff_id,
                            )
                            is_cmd = is_channel_command(user_text)
                            reactions = _make_dingtalk_reactions(
                                app_key, app_secret, message_id, conversation_id
                            )

                            async def _work(_text=user_text, _md=msg_data,
                                            _is_cmd=is_cmd,
                                            _ssid=sender_staff_id,
                                            _cid=conversation_id, _ctype=conversation_type,
                                            _nick=sender_nick,
                                            _mid=message_id, _sid=sender_id,
                                            _title=conversation_title, _reactions=reactions):
                                from app.api.dingtalk import _check_message_dedup

                                if await _check_message_dedup(_mid):
                                    return ""
                                quoted_message = None
                                if not _is_cmd:
                                    quoted_message = await _parse_dingtalk_quoted_message(
                                        _md,
                                        app_key,
                                        app_secret,
                                        agent_id,
                                    )
                                await process_dingtalk_message(
                                    agent_id=agent_id,
                                    sender_staff_id=_ssid,
                                    user_text=_text,
                                    conversation_id=_cid,
                                    conversation_type=_ctype,
                                    sender_nick=_nick,
                                    message_id=_mid,
                                    sender_id=_sid,
                                    conversation_title=_title,
                                    channel_reactions=_reactions,
                                    quoted_message=quoted_message,
                                )
                                return ""

                            _fire_and_forget(
                                main_loop,
                                run_channel_message(
                                    lock_key, is_command=is_cmd, reactions=reactions, work=_work
                                ),
                            )
                            # ACK immediately; serialization+LLM run on main_loop
                        else:
                            logger.warning("[DingTalk Stream] Main loop not available")

                    else:
                        # Non-text message: process media in the main loop
                        if main_loop and main_loop.is_running():
                            lock_key = _dingtalk_lock_key(
                                agent_id,
                                conversation_type,
                                conversation_id,
                                sender_staff_id,
                            )
                            reactions = _make_dingtalk_reactions(
                                app_key, app_secret, message_id, conversation_id
                            )

                            async def _work_media(_md=msg_data, _ak=app_key, _as=app_secret,
                                                  _ssid=sender_staff_id, _cid=conversation_id,
                                                  _ctype=conversation_type,
                                                  _nick=sender_nick, _mid=message_id,
                                                  _sid=sender_id, _title=conversation_title,
                                                  _reactions=reactions):
                                from app.api.dingtalk import _check_message_dedup

                                if await _check_message_dedup(_mid):
                                    return ""
                                await self._handle_media_and_dispatch(
                                    msg_data=_md,
                                    app_key=_ak,
                                    app_secret=_as,
                                    agent_id=agent_id,
                                    sender_staff_id=_ssid,
                                    conversation_id=_cid,
                                    conversation_type=_ctype,
                                    sender_nick=_nick,
                                    message_id=_mid,
                                    sender_id=_sid,
                                    conversation_title=_title,
                                    channel_reactions=_reactions,
                                )
                                return ""

                            _fire_and_forget(
                                main_loop,
                                run_channel_message(
                                    lock_key,
                                    is_command=False,  # 媒体消息不会是命令
                                    reactions=reactions,
                                    work=_work_media,
                                ),
                            )
                            # ACK immediately; serialization+LLM run on main_loop
                        else:
                            logger.warning("[DingTalk Stream] Main loop not available")

                    return dingtalk_stream.AckMessage.STATUS_OK, "ok"
                except Exception as e:
                    # Keep channel credentials and provider payload details out of logs.
                    logger.error(
                        f"[DingTalk Stream] Error in message handler: {type(e).__name__}"
                    )
                    import traceback
                    traceback.print_exc()
                    return dingtalk_stream.AckMessage.STATUS_SYSTEM_EXCEPTION, str(e)

            @staticmethod
            async def _handle_media_and_dispatch(
                msg_data: dict,
                app_key: str,
                app_secret: str,
                agent_id: uuid.UUID,
                sender_staff_id: str,
                conversation_id: str,
                conversation_type: str,
                sender_nick: str = "",
                message_id: str = "",
                sender_id: str = "",
                conversation_title: str = "",
                channel_reactions: ChannelReactions | None = None,
            ):
                """Download media, then dispatch to process_dingtalk_message."""
                from app.api.dingtalk import process_dingtalk_message
                from app.services.confirmation_service import (
                    find_dingtalk_pending_confirmation,
                    redeliver_pending_confirmation,
                )

                external_conv_id = (
                    f"dingtalk_group_{conversation_id}"
                    if conversation_type == "2"
                    else f"dingtalk_p2p_{sender_staff_id}"
                )
                pending = await find_dingtalk_pending_confirmation(
                    agent_id=agent_id,
                    external_conv_id=external_conv_id,
                )
                if pending is not None and pending.force_confirmation:
                    await redeliver_pending_confirmation(pending)
                    logger.info(
                        "[DingTalk Stream] Discarded media before download because "
                        "confirmation %s is required",
                        pending.row_id,
                    )
                    return

                user_text, saved_file_paths = await _process_media_message(
                    msg_data=msg_data,
                    app_key=app_key,
                    app_secret=app_secret,
                    agent_id=agent_id,
                )

                if not user_text:
                    logger.info("[DingTalk Stream] Empty content after media processing, skipping")
                    return

                quoted_message = await _parse_dingtalk_quoted_message(
                    msg_data,
                    app_key,
                    app_secret,
                    agent_id,
                )

                await process_dingtalk_message(
                    agent_id=agent_id,
                    sender_staff_id=sender_staff_id,
                    user_text=user_text,
                    conversation_id=conversation_id,
                    conversation_type=conversation_type,
                    saved_file_paths=saved_file_paths,
                    sender_nick=sender_nick,
                    message_id=message_id,
                    sender_id=sender_id,
                    conversation_title=conversation_title,
                    channel_reactions=channel_reactions,
                    quoted_message=quoted_message,
                )

        class ClawithCardCallbackHandler(dingtalk_stream.CallbackHandler):
            """Relays interactive-card button clicks to the agent — generic, no coupling."""

            @staticmethod
            def _extract_button(content) -> "tuple[str, str]":
                # Faithfully pull the clicked button's value + display label.
                # value: cardPrivateData.actionIds[0]; label: cardPrivateData.params.text.
                value, label = "", ""
                if isinstance(content, dict):
                    cpd = content.get("cardPrivateData") or {}
                    if isinstance(cpd, dict):
                        ids = cpd.get("actionIds")
                        if isinstance(ids, list) and ids:
                            value = str(ids[0] or "")
                        params = cpd.get("params") or {}
                        if isinstance(params, dict):
                            label = str(params.get("text") or "")
                            if not value:
                                value = str(params.get("action") or params.get("value") or "")
                if not label:
                    label = value
                if not value:
                    value = label
                return value, label

            async def process(self, callback: dingtalk_stream.CallbackMessage):
                try:
                    cb = dingtalk_stream.CardCallbackMessage.from_dict(callback.data)
                    out_track_id = cb.card_instance_id or ""
                    staff_id = cb.user_id or ""
                    content = cb.content or {}
                    logger.info(
                        f"[DingTalk Stream] card callback outTrackId={out_track_id} "
                        f"user={staff_id} content={content}"
                    )
                    value, label = self._extract_button(content)
                    if (
                        out_track_id
                        and (value or label)
                        and main_loop is not None
                        and not main_loop.is_closed()
                    ):
                        from app.services.confirmation_service import resolve_confirmation_via_dingtalk

                        _fire_and_forget(
                            main_loop,
                            resolve_confirmation_via_dingtalk(out_track_id, staff_id, value, label),
                        )
                    return dingtalk_stream.AckMessage.STATUS_OK, "ok"
                except Exception as e:
                    logger.error(f"[DingTalk Stream] card callback error: {e}")
                    return dingtalk_stream.AckMessage.STATUS_SYSTEM_EXCEPTION, str(e)

        try:
            credential = dingtalk_stream.Credential(client_id=app_key, client_secret=app_secret)
            client = dingtalk_stream.DingTalkStreamClient(credential=credential)
            client.register_callback_handler(
                dingtalk_stream.chatbot.ChatbotMessage.TOPIC,
                ClawithChatbotHandler(),
            )
            client.register_callback_handler(
                dingtalk_stream.Card_Callback_Router_Topic,
                ClawithCardCallbackHandler(),
            )
            logger.info(
                f"[DingTalk Stream] registered callback topics for agent {agent_id}: "
                f"{list(client.callback_handler_map.keys())}"
            )
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            async_stop_event = asyncio.Event()
            runtime = self._runtimes.get(agent_id)
            if runtime is not None and runtime.generation == generation:
                runtime.loop = loop
                runtime.async_stop_event = async_stop_event
            if stop_event.is_set():
                async_stop_event.set()
            loop.run_until_complete(
                self._run_managed_client(
                    agent_id=agent_id,
                    generation=generation,
                    client=client,
                    async_stop_event=async_stop_event,
                    retry_delays=RETRY_DELAYS,
                )
            )
        except Exception as exc:
            logger.exception(f"[DingTalk Stream] Client runner failed for {agent_id}: {exc}")
        finally:
            try:
                pending = asyncio.all_tasks(loop) if "loop" in locals() and not loop.is_closed() else set()
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                if "loop" in locals() and not loop.is_closed():
                    loop.close()
            except Exception:
                logger.exception(f"[DingTalk Stream] Failed to close runner loop for {agent_id}")
            if main_loop and main_loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self._handle_runner_exit(agent_id, generation, fingerprint),
                    main_loop,
                )
            logger.info(
                f"[DingTalk Stream] Client runner exited for agent {agent_id} "
                f"(generation={generation})"
            )

    async def _open_connection(
        self,
        client,
        http_client: httpx.AsyncClient,
    ) -> dict:
        """Open the DingTalk gateway with a bounded, cancellable HTTP call."""
        topics = []
        if getattr(client, "_is_event_required", False):
            topics.append({"type": "EVENT", "topic": "*"})
        topics.extend(
            {"type": "CALLBACK", "topic": topic}
            for topic in client.callback_handler_map
        )
        response = await http_client.post(
            client.OPEN_CONNECTION_API,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "DingTalkStream/managed-platform",
            },
            json={
                "clientId": client.credential.client_id,
                "clientSecret": client.credential.client_secret,
                "subscriptions": topics,
                "ua": "dingtalk-sdk-python/platform-managed",
                "localIp": client.get_host_ip(),
            },
        )
        response.raise_for_status()
        return response.json()

    async def _open_connection_or_stop(
        self,
        client,
        http_client: httpx.AsyncClient,
        async_stop_event: asyncio.Event,
    ) -> dict | None:
        open_task = asyncio.create_task(self._open_connection(client, http_client))
        stop_task = asyncio.create_task(async_stop_event.wait())
        done, pending = await asyncio.wait(
            {open_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stop_task in done and stop_task.result():
            open_task.cancel()
            await asyncio.gather(open_task, return_exceptions=True)
            return None
        stop_task.cancel()
        await asyncio.gather(stop_task, return_exceptions=True)
        return open_task.result()

    async def _run_managed_client(
        self,
        *,
        agent_id: uuid.UUID,
        generation: int,
        client,
        async_stop_event: asyncio.Event,
        retry_delays: list[int],
    ) -> None:
        """Run the SDK protocol with an explicit, stoppable reconnect loop."""
        import websockets

        client.pre_start()
        retry_count = 0
        failure_notified = False
        async with httpx.AsyncClient(timeout=15) as gateway_http:
            while not async_stop_event.is_set():
                websocket = None
                keepalive_task = None
                try:
                    connection = await self._open_connection_or_stop(
                        client,
                        gateway_http,
                        async_stop_event,
                    )
                    if connection is None and async_stop_event.is_set():
                        break
                    if not connection:
                        raise RuntimeError("DingTalk open connection returned no endpoint")
                    uri = f'{connection["endpoint"]}?ticket={quote_plus(connection["ticket"])}'
                    logger.info(
                        f"[DingTalk Stream] Connecting for agent {agent_id} "
                        f"(generation={generation}, retry={retry_count})"
                    )
                    async with websockets.connect(uri) as websocket:
                        client.websocket = websocket
                        retry_count = 0
                        failure_notified = False
                        await self._publish_connection_state(agent_id, generation, ready=True)
                        keepalive_task = asyncio.create_task(client.keepalive(websocket))
                        while not async_stop_event.is_set():
                            receive_task = asyncio.create_task(websocket.recv())
                            stop_task = asyncio.create_task(async_stop_event.wait())
                            done, pending = await asyncio.wait(
                                {receive_task, stop_task},
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            for task in pending:
                                task.cancel()
                            if pending:
                                await asyncio.gather(*pending, return_exceptions=True)
                            if stop_task in done and stop_task.result():
                                if not receive_task.done():
                                    receive_task.cancel()
                                await websocket.close()
                                break
                            raw_message = receive_task.result()
                            json_message = json.loads(raw_message)
                            asyncio.create_task(client.background_task(json_message))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if not async_stop_event.is_set():
                        retry_count += 1
                        logger.warning(
                            f"[DingTalk Stream] Connection error for {agent_id} "
                            f"(generation={generation}, retry={retry_count}): {exc}"
                        )
                        if retry_count >= 6 and not failure_notified:
                            failure_notified = True
                            main_loop = self._main_loop
                            if main_loop and main_loop.is_running():
                                asyncio.run_coroutine_threadsafe(
                                    self._notify_connection_failed(agent_id, str(exc)),
                                    main_loop,
                                )
                finally:
                    if keepalive_task is not None:
                        keepalive_task.cancel()
                        await asyncio.gather(keepalive_task, return_exceptions=True)
                    client.websocket = None
                    await self._publish_connection_state(agent_id, generation, ready=False)

                if async_stop_event.is_set():
                    break
                delay = retry_delays[min(max(retry_count - 1, 0), len(retry_delays) - 1)]
                try:
                    await asyncio.wait_for(async_stop_event.wait(), timeout=delay)
                except TimeoutError:
                    pass

    async def _publish_connection_state(
        self,
        agent_id: uuid.UUID,
        generation: int,
        *,
        ready: bool,
    ) -> None:
        main_loop = self._main_loop
        if main_loop is None or not main_loop.is_running():
            return
        future = asyncio.run_coroutine_threadsafe(
            self._set_connection_state(agent_id, generation, ready=ready),
            main_loop,
        )

        def _log_failure(done):
            try:
                done.result()
            except FutureCancelledError:
                return
            except Exception:
                logger.exception(
                    f"[DingTalk Stream] Connection-state task failed for {agent_id}"
                )

        future.add_done_callback(_log_failure)

    async def _set_connection_state(
        self,
        agent_id: uuid.UUID,
        generation: int,
        *,
        ready: bool,
    ) -> None:
        async with self._state_lock_for(agent_id):
            runtime = self._runtimes.get(agent_id)
            if runtime is None or runtime.generation != generation:
                return
            runtime.ready = ready
            if runtime.persisted_ready != ready:
                if await self._set_persisted_connected(agent_id, ready):
                    current = self._runtimes.get(agent_id)
                    if current is runtime and current.generation == generation:
                        current.persisted_ready = ready
            logger.info(
                f"[DingTalk Stream] Agent {agent_id} connection ready={ready} "
                f"(generation={generation})"
            )

    async def _handle_runner_exit(
        self,
        agent_id: uuid.UUID,
        generation: int,
        fingerprint: str,
    ) -> None:
        async with self._lock_for(agent_id):
            runtime = self._runtimes.get(agent_id)
            if (
                runtime is None
                or runtime.generation != generation
                or runtime.fingerprint != fingerprint
            ):
                return
            self._runtimes.pop(agent_id, None)
            async with self._state_lock_for(agent_id):
                await self._set_persisted_connected(agent_id, False)

    async def _set_persisted_connected(self, agent_id: uuid.UUID, connected: bool) -> bool:
        async def _write() -> None:
            async with async_session() as db:
                await db.execute(
                    update(ChannelConfig)
                    .where(
                        ChannelConfig.agent_id == agent_id,
                        ChannelConfig.channel_type == "dingtalk",
                    )
                    .values(is_connected=connected)
                )
                await db.commit()

        try:
            await asyncio.wait_for(_write(), timeout=5)
            return True
        except Exception:
            logger.exception(
                f"[DingTalk Stream] Failed to persist connection state for {agent_id}; "
                "the live connection state remains authoritative"
            )
            return False

    async def _load_persisted_desired_credentials(
        self,
        agent_id: uuid.UUID,
    ) -> tuple[str, str] | None:
        async with async_session() as db:
            result = await db.execute(
                select(ChannelConfig)
                .join(Agent, Agent.id == ChannelConfig.agent_id)
                .join(Tenant, Tenant.id == Agent.tenant_id)
                .where(
                    ChannelConfig.agent_id == agent_id,
                    ChannelConfig.channel_type == "dingtalk",
                    ChannelConfig.is_configured.is_(True),
                    Agent.is_deleted.is_(False),
                    Tenant.is_active.is_(True),
                )
            )
            config = result.scalar_one_or_none()
        if not config or not config.app_id or not config.app_secret:
            return None
        extra = config.extra_config if isinstance(config.extra_config, dict) else {}
        if extra.get("connection_mode", "websocket") != "websocket":
            return None
        return config.app_id, config.app_secret

    async def _notify_connection_failed(self, agent_id: uuid.UUID, error_msg: str):
        """Send notification to agent creator when DingTalk connection permanently fails."""
        try:
            from app.models.agent import Agent
            from app.services.notification_service import send_notification
            async with async_session() as db:
                result = await db.execute(select(Agent).where(Agent.id == agent_id))
                agent = result.scalar_one_or_none()
                if agent and agent.creator_id:
                    await send_notification(
                        db,
                        user_id=agent.creator_id,
                        agent_id=agent_id,
                        type="channel_error",
                        title=f"DingTalk connection failed for {agent.name}",
                        body=(
                            f"Failed to connect after multiple retries. "
                            f"Last error: {error_msg[:200]}. "
                            f"Please check your DingTalk app credentials and try reconfiguring the channel."
                        ),
                        link=f"/agents/{agent_id}#settings",
                    )
                    await db.commit()
        except Exception as e:
            logger.error(f"[DingTalk Stream] Failed to send connection failure notification: {e}")

    async def stop_client(
        self,
        agent_id: uuid.UUID,
        verify_persisted: bool = True,
    ):
        """Stop a running Stream client for an agent."""
        if not _connector_role_enabled():
            logger.debug(
                f"[DingTalk Stream] Ignore stop for {agent_id}: PROCESS_ROLE has no connector ownership"
            )
            return
        if verify_persisted:
            try:
                desired = await self._load_persisted_desired_credentials(agent_id)
            except Exception:
                logger.exception(
                    f"[DingTalk Stream] Cannot verify committed deletion for {agent_id}; "
                    "deferring to reconciler"
                )
                return
            if desired:
                logger.debug(
                    f"[DingTalk Stream] Deferring stop for {agent_id} until deletion commits"
                )
                return
        async with self._lock_for(agent_id):
            runtime = self._runtimes.get(agent_id)
            if runtime is None:
                return
            await self._stop_runtime_locked(agent_id, runtime)

    async def _stop_runtime_locked(
        self,
        agent_id: uuid.UUID,
        runtime: _StreamRuntime,
    ) -> bool:
        runtime.stop_event.set()
        if runtime.loop is not None and runtime.async_stop_event is not None and runtime.loop.is_running():
            runtime.loop.call_soon_threadsafe(runtime.async_stop_event.set)
        if runtime.thread.is_alive():
            logger.info(
                f"[DingTalk Stream] Stopping client for agent {agent_id} "
                f"(generation={runtime.generation})"
            )
            await asyncio.to_thread(runtime.thread.join, 10)
        if runtime.thread.is_alive():
            logger.error(
                f"[DingTalk Stream] Client runner for {agent_id} did not exit within 10s "
                f"(generation={runtime.generation})"
            )
            return False
        current = self._runtimes.get(agent_id)
        if current is runtime:
            self._runtimes.pop(agent_id, None)
            async with self._state_lock_for(agent_id):
                await self._set_persisted_connected(agent_id, False)
        return True

    async def start_all(self):
        """Continuously reconcile persisted desired state into live clients."""
        if not _connector_role_enabled():
            logger.info("[DingTalk Stream] Reconciler skipped: PROCESS_ROLE has no connector ownership")
            return
        logger.info("[DingTalk Stream] Reconciler started")
        while True:
            try:
                await self.reconcile_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[DingTalk Stream] Reconcile iteration failed")
            await asyncio.sleep(2)

    async def reconcile_once(self) -> None:
        async with async_session() as db:
            result = await db.execute(
                select(ChannelConfig)
                .join(Agent, Agent.id == ChannelConfig.agent_id)
                .join(Tenant, Tenant.id == Agent.tenant_id)
                .where(
                    ChannelConfig.is_configured.is_(True),
                    ChannelConfig.channel_type == "dingtalk",
                    Agent.is_deleted.is_(False),
                    Tenant.is_active.is_(True),
                )
            )
            configs = result.scalars().all()

        desired: dict[uuid.UUID, tuple[str, str]] = {}
        for config in configs:
            extra = config.extra_config if isinstance(config.extra_config, dict) else {}
            connection_mode = extra.get("connection_mode", "websocket")
            if connection_mode == "websocket" and config.app_id and config.app_secret:
                desired[config.agent_id] = (config.app_id, config.app_secret)

        for agent_id in list(self._runtimes):
            if agent_id not in desired:
                await self.stop_client(agent_id, verify_persisted=False)
        for agent_id, (app_key, app_secret) in desired.items():
            await self.start_client(
                agent_id,
                app_key,
                app_secret,
                stop_existing=False,
                verify_persisted=False,
            )

    def status(self) -> dict:
        """Return status of all active Stream clients."""
        return {
            str(agent_id): bool(runtime.ready)
            for agent_id, runtime in self._runtimes.items()
        }


dingtalk_stream_manager = DingTalkStreamManager()
