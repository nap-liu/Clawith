"""DingTalk emotion reaction service for dynamic IM turn progress."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress

from loguru import logger

from app.services.dingtalk_token import dingtalk_token_manager

REACTION_REPLY_URL = "https://api.dingtalk.com/v1.0/robot/emotion/reply"
REACTION_RECALL_URL = "https://api.dingtalk.com/v1.0/robot/emotion/recall"
TEXT_EMOTION_ID = "2659900"
TEXT_EMOTION_BACKGROUND_ID = "im_bg_1"

DEFAULT_THINKING_REACTION = "🤔思考中"
HEARTBEAT_REACTION = "⏳"
DEFAULT_TOOL_REACTION = "🛠️"

AttachReaction = Callable[[str], Awaitable[bool]]
RecallReaction = Callable[[str], Awaitable[None]]

_PACKAGE_COMMAND_RE = re.compile(
    r"\b("
    r"npm\s+(?:install|i|add)|"
    r"pnpm\s+(?:install|i|add)|"
    r"yarn\s+(?:add|install)|"
    r"bun\s+(?:add|install)|"
    r"pip\s+install|"
    r"uv\s+add|"
    r"brew\s+install"
    r")\b",
    re.IGNORECASE,
)


def _reaction_payload(
    *,
    app_key: str,
    message_id: str,
    conversation_id: str,
    reaction_name: str,
) -> dict:
    return {
        "robotCode": app_key,
        "openMsgId": message_id,
        "openConversationId": conversation_id,
        "emotionType": 2,
        "emotionName": reaction_name,
        "textEmotion": {
            "emotionId": TEXT_EMOTION_ID,
            "emotionName": reaction_name,
            "text": reaction_name,
            "backgroundId": TEXT_EMOTION_BACKGROUND_ID,
        },
    }


async def _post_reaction(
    *,
    app_key: str,
    app_secret: str,
    message_id: str,
    conversation_id: str,
    reaction_name: str,
    url: str,
    action: str,
) -> bool:
    import httpx

    if not message_id or not conversation_id or not app_key:
        return False

    token = await dingtalk_token_manager.get_token(app_key, app_secret)
    if not token:
        logger.warning(f"[DingTalk Reaction] Failed to get access token for {action}")
        return False

    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.post(
            url,
            headers={
                "x-acs-dingtalk-access-token": token,
                "Content-Type": "application/json",
            },
            json=_reaction_payload(
                app_key=app_key,
                message_id=message_id,
                conversation_id=conversation_id,
                reaction_name=reaction_name,
            ),
        )
    if resp.status_code == 200:
        logger.info(
            f"[DingTalk Reaction] {action} reaction={reaction_name} "
            f"msg={message_id[:16]}"
        )
        return True
    logger.warning(
        f"[DingTalk Reaction] {action} failed reaction={reaction_name}: "
        f"{resp.status_code} {resp.text[:200]}"
    )
    return False


async def add_reaction(
    app_key: str,
    app_secret: str,
    message_id: str,
    conversation_id: str,
    reaction_name: str,
) -> bool:
    """Attach a DingTalk text-emotion reaction with short retries."""
    for delay in (0.0, 0.4, 1.2):
        if delay:
            await asyncio.sleep(delay)
        try:
            if await _post_reaction(
                app_key=app_key,
                app_secret=app_secret,
                message_id=message_id,
                conversation_id=conversation_id,
                reaction_name=reaction_name,
                url=REACTION_REPLY_URL,
                action="add",
            ):
                return True
        except Exception as exc:  # noqa: BLE001 - IM reaction is best-effort
            logger.warning(f"[DingTalk Reaction] Add error reaction={reaction_name}: {exc}")
    return False


async def recall_reaction(
    app_key: str,
    app_secret: str,
    message_id: str,
    conversation_id: str,
    reaction_name: str,
) -> None:
    """Recall a DingTalk text-emotion reaction with delayed retries."""
    for delay in (0.0, 1.5, 5.0):
        if delay:
            await asyncio.sleep(delay)
        try:
            if await _post_reaction(
                app_key=app_key,
                app_secret=app_secret,
                message_id=message_id,
                conversation_id=conversation_id,
                reaction_name=reaction_name,
                url=REACTION_RECALL_URL,
                action="recall",
            ):
                return
        except Exception as exc:  # noqa: BLE001 - IM reaction is best-effort
            logger.warning(f"[DingTalk Reaction] Recall error reaction={reaction_name}: {exc}")
    logger.warning(
        f"[DingTalk Reaction] All recall attempts failed "
        f"reaction={reaction_name} msg={message_id[:16]}"
    )


async def add_thinking_reaction(
    app_key: str,
    app_secret: str,
    message_id: str,
    conversation_id: str,
) -> bool:
    """Backward-compatible wrapper for the initial thinking reaction."""
    return await add_reaction(
        app_key,
        app_secret,
        message_id,
        conversation_id,
        DEFAULT_THINKING_REACTION,
    )


async def recall_thinking_reaction(
    app_key: str,
    app_secret: str,
    message_id: str,
    conversation_id: str,
) -> None:
    """Backward-compatible wrapper for recalling the initial thinking reaction."""
    await recall_reaction(
        app_key,
        app_secret,
        message_id,
        conversation_id,
        DEFAULT_THINKING_REACTION,
    )


def _extract_command(args: object) -> str:
    if isinstance(args, dict):
        command = args.get("command") or args.get("cmd") or args.get("script")
        if isinstance(command, str):
            return command
    return ""


def resolve_tool_reaction(tool_name: object, args: object | None = None) -> str:
    """Map a tool event to the DingTalk reaction text shown on the user message."""
    name = str(tool_name or "").strip().lower().replace("-", "_")
    command = _extract_command(args)

    if command and _PACKAGE_COMMAND_RE.search(command):
        return "📦"
    if name in {"bash", "exec", "process", "execute_code", "execute_code_aio", "svc"}:
        if command and re.search(r"\b(which|where)\b", command, re.IGNORECASE):
            return "🔍"
        return DEFAULT_TOOL_REACTION
    if (
        name in {"read", "view", "read_file", "read_image", "list_files", "read_session_messages"}
        or name.startswith("read_")
        or name.startswith("list_")
    ):
        return "📂"
    if (
        name in {"write", "edit", "patch", "write_file", "edit_file", "move_file"}
        or name.startswith("write_")
        or name.startswith("edit_")
        or name.startswith("patch_")
    ):
        return "✍️"
    if (
        name in {"web_search", "search", "browser.search", "browser_search"}
        or "web_search" in name
        or name.startswith("search_")
    ):
        return "🌐"
    if name in {"fetch", "open", "open_url", "browser.open", "browser_open", "read_webpage"}:
        return "🔗"
    if "browser" in name and any(part in name for part in ("open", "navigate", "goto")):
        return "🔗"
    return DEFAULT_TOOL_REACTION


class DingTalkReactionController:
    """Switch one DingTalk reaction as the turn moves between thinking and tools."""

    def __init__(
        self,
        *,
        attach_reaction: AttachReaction,
        recall_reaction: RecallReaction,
        initial_reaction: str = DEFAULT_THINKING_REACTION,
        min_switch_interval_seconds: float = 5.0,
        heartbeat_enabled: bool = True,
        silence_seconds: float = 55.0,
        heartbeat_interval_seconds: float = 60.0,
    ) -> None:
        self._attach_reaction = attach_reaction
        self._recall_reaction = recall_reaction
        self._current = initial_reaction
        self._attached = False
        self._disposed = False
        self._min_switch_interval_seconds = min_switch_interval_seconds
        self._silence_seconds = silence_seconds
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._heartbeat_enabled = heartbeat_enabled
        self._last_switch_at = 0.0
        self._last_activity_at = 0.0
        self._lock = asyncio.Lock()
        self._heartbeat_task: asyncio.Task | None = None

    async def on_consume(self) -> None:
        async with self._lock:
            if self._disposed or self._attached:
                return
            ok = await self._attach_reaction(self._current)
            if not ok:
                return
            now = time.monotonic()
            self._attached = True
            self._last_switch_at = 0.0
            self._last_activity_at = now
            if self._heartbeat_enabled and self._heartbeat_task is None:
                self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def on_tool_call(self, evt: dict) -> None:
        if (evt or {}).get("status") != "running":
            return
        self._last_activity_at = time.monotonic()
        await self._switch(resolve_tool_reaction((evt or {}).get("name"), (evt or {}).get("args")))

    async def on_thinking(self, _text: str) -> None:
        self._last_activity_at = time.monotonic()
        await self._switch(DEFAULT_THINKING_REACTION)

    async def on_complete(self, _reply: str) -> None:
        await self.dispose()

    async def on_error(self, _exc: BaseException) -> None:
        await self.dispose()

    async def dispose(self) -> None:
        heartbeat_task: asyncio.Task | None = None
        async with self._lock:
            if self._disposed:
                return
            self._disposed = True
            heartbeat_task = self._heartbeat_task
            self._heartbeat_task = None
            if self._attached:
                await self._recall_reaction(self._current)
                self._attached = False
        if heartbeat_task is not None and heartbeat_task is not asyncio.current_task():
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task

    async def _switch(self, next_reaction: str, *, force: bool = False) -> None:
        async with self._lock:
            if self._disposed or not self._attached or not next_reaction:
                return
            if next_reaction == self._current:
                return
            now = time.monotonic()
            if (
                not force
                and self._min_switch_interval_seconds > 0
                and now - self._last_switch_at < self._min_switch_interval_seconds
            ):
                return

            previous = self._current
            await self._recall_reaction(previous)
            ok = await self._attach_reaction(next_reaction)
            if ok:
                self._current = next_reaction
                self._attached = True
                self._last_switch_at = now
                self._last_activity_at = now
            else:
                self._attached = False

    async def _heartbeat_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._heartbeat_interval_seconds)
                if self._disposed:
                    return
                if time.monotonic() - self._last_activity_at >= self._silence_seconds:
                    await self._switch(HEARTBEAT_REACTION)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - reaction heartbeat is best-effort
            logger.warning(f"[DingTalk Reaction] heartbeat failed: {exc}")
