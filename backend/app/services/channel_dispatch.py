"""统一的、与通道无关的 IM 消息派发 / 串行化层。

对外只暴露一个入口 ``run_channel_message`` + 一个纯数据接口 ``ChannelReactions``
+ 一个 per-session 的 ``asyncio.Lock`` 注册表。所有外部 IM 通道
(飞书/钉钉/企微/Slack/Discord/Teams/WhatsApp/微信)都调同一个入口,做到:

1. 同一会话的多轮消息严格串行(全程互斥,消除 web 端渲染交错);
2. ``/commands`` 不排队、立即执行;
3. 一束可选的生命周期钩子贯穿整轮(含多轮工具循环),由各通道自行实现 reaction/进度。

它独立于 ``llm.compactor`` 的 ``_session_locks``:两者是不同的注册表(不同的 ``dict``)。
工具循环内的 ``maybe_compact`` 从 ``compactor._session_locks`` 取锁,而此时本轮已持有
本模块的处理锁;若两张表合并、同一协程对同 key 再次 ``acquire`` 处理锁,会因
``asyncio.Lock`` 不可重入而死锁。
"""

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from loguru import logger
from sqlalchemy import text

from app.database import async_session

Hook0 = Callable[[], Awaitable[None]]


@dataclass
class ChannelReactions:
    """各 IM 通道可选实现的整轮生命周期钩子(全部可选,缺省即无副作用)。

    轮边界钩子由 ``run_channel_message`` 在锁内触发;循环内钩子由通道传给
    ``channel_llm._call_agent_llm``,在多轮工具循环中触发。
    """

    # —— 轮边界钩子(run_channel_message 在锁内触发)——
    on_consume: Hook0 | None = None
    on_complete: Callable[[str], Awaitable[None]] | None = None
    on_error: Callable[[BaseException], Awaitable[None]] | None = None

    # —— 循环内钩子(由通道传给 _call_agent_llm)——
    on_tool_call: Callable[[dict], Awaitable[None]] | None = None
    on_thinking: Callable[[str], Awaitable[None]] | None = None
    on_chunk: Callable[[str], Awaitable[None]] | None = None


# Process-wide per-session locks. Keyed by ``f"{channel}:{external_conv_id}"`` so
# every message that resolves to the SAME chat session serializes on one lock.
# Independent from compactor._session_locks (see module docstring).
_session_locks: dict[str, asyncio.Lock] = {}
_session_locks_guard = asyncio.Lock()
_running_turns: dict[str, set[asyncio.Task]] = {}
_running_turns_guard = asyncio.Lock()


async def _get_session_lock(lock_key: str) -> asyncio.Lock:
    """Get-or-create the per-session lock for ``lock_key`` (FIFO-fair acquire)."""
    async with _session_locks_guard:
        lock = _session_locks.get(lock_key)
        if lock is None:
            lock = asyncio.Lock()
            _session_locks[lock_key] = lock
        return lock


async def _safe(hook: Callable[..., Awaitable[None]] | None, *args: object) -> None:
    """Best-effort fire a lifecycle hook: None -> no-op; exception -> logged, swallowed.

    Reactions are side effects; a failing reaction must never break the turn.
    """
    if hook is None:
        return
    try:
        await hook(*args)
    except Exception as exc:  # noqa: BLE001 — reactions are best-effort
        logger.warning(f"[channel_dispatch] reaction hook failed (ignored): {exc}")


async def _register_running_turn(lock_key: str, task: asyncio.Task) -> None:
    async with _running_turns_guard:
        _running_turns.setdefault(lock_key, set()).add(task)


async def _clear_running_turn(lock_key: str, task: asyncio.Task) -> None:
    async with _running_turns_guard:
        tasks = _running_turns.get(lock_key)
        if not tasks:
            return
        tasks.discard(task)
        if not tasks:
            _running_turns.pop(lock_key, None)


async def cancel_running_turn(lock_key: str) -> bool:
    """Cancel all running or queued non-command IM turns for this lock key."""
    async with _running_turns_guard:
        tasks = [task for task in _running_turns.get(lock_key, set()) if not task.done()]
        if not tasks:
            return False
        for task in tasks:
            task.cancel()
        return True


@asynccontextmanager
async def _distributed_session_lock(lock_key: str):
    """Cross-replica PostgreSQL advisory lock for durable trigger turns."""
    lock_id = int.from_bytes(
        hashlib.blake2b(lock_key.encode("utf-8"), digest_size=8).digest(),
        byteorder="big",
        signed=True,
    )
    async with async_session() as db:
        bind = db.get_bind()
        if bind.dialect.name != "postgresql":
            yield
            return
        await db.execute(text("SELECT pg_advisory_lock(:lock_id)"), {"lock_id": lock_id})
        try:
            yield
        finally:
            await db.execute(text("SELECT pg_advisory_unlock(:lock_id)"), {"lock_id": lock_id})


async def run_channel_message(
    lock_key: str,
    *,
    is_command: bool,
    reactions: ChannelReactions,
    work: Callable[[], Awaitable[str]],
    distributed: bool = False,
) -> str:
    """Unified entry every IM channel calls to process one inbound message turn.

    - ``is_command=True``: run ``work`` immediately, NO lock, NO reactions
      (``/commands`` must not queue behind in-flight turns).
    - otherwise: acquire the per-session lock for ``lock_key`` and run the whole
      turn under it (mutual exclusion across the full turn), firing the turn-
      boundary reactions (on_consume / on_complete / on_error) inside the lock.

    ``work`` must encompass the channel's full turn (user-row write through reply
    persistence). Loop-internal reactions (on_tool_call/on_thinking/on_chunk) are
    threaded by the channel into ``_call_agent_llm`` inside ``work``.
    """
    if is_command:
        return await work()

    current_task = asyncio.current_task()
    if current_task is not None:
        await _register_running_turn(lock_key, current_task)
    try:
        lock = await _get_session_lock(lock_key)
        async with lock:
            async def _run_locked() -> str:
                await _safe(reactions.on_consume)
                try:
                    reply = await work()
                except BaseException as exc:
                    await _safe(reactions.on_error, exc)
                    raise
                await _safe(reactions.on_complete, reply)
                return reply

            if distributed:
                async with _distributed_session_lock(lock_key):
                    return await _run_locked()
            return await _run_locked()
    finally:
        if current_task is not None:
            await _clear_running_turn(lock_key, current_task)
