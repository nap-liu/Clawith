"""Inbound media handling for the DingTalk Stream adapter."""

import uuid
from typing import Optional

import httpx
from loguru import logger


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
    """Resolve the compatibility entry point at call time for monkeypatching."""
    from app.services import dingtalk_stream

    return await dingtalk_stream._download_dingtalk_media(
        app_key,
        app_secret,
        download_code,
    )


async def _store_dingtalk_upload(
    agent_id: uuid.UUID,
    filename: str,
    file_bytes: bytes,
    *,
    content_type: str | None = None,
) -> str | None:
    """Resolve the compatibility entry point at call time for monkeypatching."""
    from app.services import dingtalk_stream

    return await dingtalk_stream._store_dingtalk_upload(
        agent_id,
        filename,
        file_bytes,
        content_type=content_type,
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
