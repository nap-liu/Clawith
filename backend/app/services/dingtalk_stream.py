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
from app.services.dingtalk_stream_connection import DingTalkStreamConnectionMixin
from app.services.dingtalk_stream_media import (
    _download_file,
    _get_media_download_url,
    _process_media_message,
)
from app.services.dingtalk_stream_runner import DingTalkStreamRunnerMixin
from app.services.dingtalk_token import dingtalk_token_manager
from app.services.im_delivery import ProviderResponseUncertainError
from app.services.storage import store_agent_upload

DINGTALK_VOICE_MAX_BYTES = 2 * 1024 * 1024
DINGTALK_VIDEO_MAX_BYTES = 20 * 1024 * 1024

# ─── DingTalk Media Helpers ─────────────────────────────



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
    from app.services.dingtalk_reaction import make_dingtalk_reactions

    return make_dingtalk_reactions(
        app_key,
        app_secret,
        message_id,
        conversation_id,
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


class DingTalkStreamManager(DingTalkStreamRunnerMixin, DingTalkStreamConnectionMixin):
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
