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
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from loguru import logger

from app.services.active_turns import active_turn_boundary
from app.services.redis_lease_lock import redis_lease_lock
from app.services.workload_capacity import (
    WorkloadKind,
    get_workload_capacity,
)

if TYPE_CHECKING:
    from app.models.chat_session import ChatSession

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


# Process-wide per-session locks. Channel adapters use
# ``channel_session_lock_key`` so two agents never share a lock merely because
# the provider reports the same external conversation/user id.
# Independent from compactor._session_locks (see module docstring).
_session_locks: dict[str, asyncio.Lock] = {}
_session_locks_guard = asyncio.Lock()
_running_turns: dict[str, set[asyncio.Task]] = {}
_running_turns_guard = asyncio.Lock()
_turn_owner_admission: dict[str, tuple[asyncio.Task, asyncio.Event]] = {}
_channel_interjection: ContextVar[bool] = ContextVar(
    "channel_interjection",
    default=False,
)
_channel_interjection_lock_key: ContextVar[str | None] = ContextVar(
    "channel_interjection_lock_key",
    default=None,
)
_channel_interjection_reactions: ContextVar[ChannelReactions | None] = ContextVar(
    "channel_interjection_reactions",
    default=None,
)
_promoted_turn: ContextVar[tuple[UUID, str] | None] = ContextVar(
    "channel_promoted_turn",
    default=None,
)
_active_reactions: dict[str, tuple[asyncio.Task, ChannelReactions]] = {}
_pending_receipt_anchors: dict[UUID, tuple[str, ChannelReactions]] = {}
_send_locks: dict[str, asyncio.Lock] = {}
_send_locks_guard = asyncio.Lock()


def channel_session_lock_key(
    agent_id: UUID | str,
    source_channel: str,
    external_conv_id: str,
) -> str:
    """Return the lock identity of one agent's active external-channel session.

    ``ChatSession`` uses the same three fields as its durable uniqueness
    boundary. Provider ids are only unique within one bot/agent, so omitting
    ``agent_id`` incorrectly serializes the same person across every agent.
    """
    return f"channel-session:{source_channel}:{agent_id}:{external_conv_id}"


def chat_session_lock_key(session: "ChatSession") -> str:
    """Return the canonical turn-lock key for one durable chat session.

    External-channel sessions use their database uniqueness boundary so an
    adapter can derive the same key before it enters the turn. First-party and
    internal sessions have no external route, so their durable UUID is the key.
    """
    if session.external_conv_id:
        return channel_session_lock_key(
            session.agent_id,
            session.source_channel,
            session.external_conv_id,
        )
    return str(session.id)


async def _get_session_lock(lock_key: str) -> asyncio.Lock:
    """Get-or-create the per-session lock for ``lock_key`` (FIFO-fair acquire)."""
    async with _session_locks_guard:
        lock = _session_locks.get(lock_key)
        if lock is None:
            lock = asyncio.Lock()
            _session_locks[lock_key] = lock
    return lock


async def _get_send_lock(lock_key: str) -> asyncio.Lock:
    """Get-or-create the per-session send lock for channel outbound operations."""
    async with _send_locks_guard:
        lock = _send_locks.get(lock_key)
        if lock is None:
            lock = asyncio.Lock()
            _send_locks[lock_key] = lock
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


def is_channel_turn_interjection() -> bool:
    """Return whether this IM work arrived while another local owner runs."""

    return _channel_interjection.get()


def mark_channel_promoted_turn(agent_id: UUID, session_id: str) -> None:
    """Record an inbox promotion for the dispatcher to start after commit."""

    _promoted_turn.set((agent_id, session_id))


async def mark_channel_turn_admitted() -> None:
    """Release local follow-up ingestion once the first owner has a DB anchor."""

    lock_key = _channel_interjection_lock_key.get()
    if lock_key is None:
        return
    async with _running_turns_guard:
        current = _turn_owner_admission.get(lock_key)
        if current is not None:
            current[1].set()


async def register_channel_receipt_anchor(message_id: UUID) -> None:
    """Retain one interjected message's hooks until the inbox consumes it.

    Admission and consumption are deliberately separate. A pending message must
    not move the user-visible IM progress receipt before the shared turn loop has
    actually injected it at a round boundary.
    """

    lock_key = _channel_interjection_lock_key.get()
    reactions = _channel_interjection_reactions.get()
    if lock_key is None or reactions is None or reactions.on_consume is None:
        return
    async with _running_turns_guard:
        active = _active_reactions.get(lock_key)
        if active is None or active[0].done():
            return
        _pending_receipt_anchors[message_id] = (lock_key, reactions)


async def advance_channel_receipt_anchor(message_ids: list[UUID]) -> None:
    """Move one running IM turn's progress receipt to its last consumed input."""

    if not message_ids:
        return
    active_reactions: ChannelReactions | None = None
    next_reactions: ChannelReactions | None = None
    async with _running_turns_guard:
        for message_id in message_ids:
            candidate = _pending_receipt_anchors.pop(message_id, None)
            if candidate is None:
                continue
            lock_key, reactions = candidate
            active = _active_reactions.get(lock_key)
            if active is None or active[0].done():
                continue
            active_reactions = active[1]
            next_reactions = reactions

    if active_reactions is None or next_reactions is None:
        return

    # The stable bundle is already threaded through every loop callback. Replace
    # its hooks in place so later thinking/tool events and terminal cleanup all
    # target the newly consumed provider message without changing the lifecycle
    # root turn anchor.
    await _safe(active_reactions.on_complete, "")
    active_reactions.on_consume = next_reactions.on_consume
    active_reactions.on_complete = next_reactions.on_complete
    active_reactions.on_error = next_reactions.on_error
    active_reactions.on_tool_call = next_reactions.on_tool_call
    active_reactions.on_thinking = next_reactions.on_thinking
    active_reactions.on_chunk = next_reactions.on_chunk
    await _safe(active_reactions.on_consume)


async def cancel_running_turn(lock_key: str) -> bool:
    """Cancel all running or queued non-command IM turns for this lock key."""
    async with _running_turns_guard:
        tasks = [task for task in _running_turns.get(lock_key, set()) if not task.done()]
        if not tasks:
            return False
        for task in tasks:
            task.cancel()
        return True


async def has_running_turn(lock_key: str) -> bool:
    """Return whether this IM session currently has a running or queued turn."""
    async with _running_turns_guard:
        return any(not task.done() for task in _running_turns.get(lock_key, set()))


async def run_channel_message(
    lock_key: str,
    *,
    is_command: bool,
    reactions: ChannelReactions,
    work: Callable[[], Awaitable[str]],
    distributed: bool = False,
    workload_kind: WorkloadKind | str = WorkloadKind.INTERACTIVE,
    tenant_id: UUID | str | None = None,
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

    ``tenant_id`` should be the durable tenant UUID at every tenant-aware entry.
    The lock key fallback preserves existing adapters and isolates their local
    traffic, but it is not a substitute for a real tenant quota identity.

    ``distributed=True`` adds a renewable Redis lease across replicas. Redis
    acquisition is bounded and a Redis outage raises a retryable lease error;
    the turn never waits or executes while holding a database connection.
    """
    if is_command:
        return await work()

    current_task = asyncio.current_task()
    if current_task is not None:
        await _register_running_turn(lock_key, current_task)
    interjection = False
    owner_admitted_event: asyncio.Event | None = None
    if current_task is not None:
        async with _running_turns_guard:
            current = _turn_owner_admission.get(lock_key)
            if current is not None and not current[0].done():
                interjection = current_task is not current[0]
                owner_admitted_event = current[1]
            else:
                owner_admitted_event = asyncio.Event()
                _turn_owner_admission[lock_key] = (
                    current_task,
                    owner_admitted_event,
                )
    try:
        if interjection:
            if owner_admitted_event is not None:
                await owner_admitted_event.wait()
            interjection_token = _channel_interjection.set(True)
            lock_key_token = _channel_interjection_lock_key.set(lock_key)
            reactions_token = _channel_interjection_reactions.set(reactions)
            promoted_token = _promoted_turn.set(None)
            try:
                # This path is ingestion-only. If the old owner has already
                # terminated, ingestion promotes a durable inbox row and still
                # returns without running an LLM outside normal governance.
                reply = await work()
                promoted = _promoted_turn.get()
                if promoted is not None:
                    from app.services.turn_inbox import kick_promoted_turn_inbox

                    await kick_promoted_turn_inbox(
                        agent_id=promoted[0],
                        session_id=promoted[1],
                    )
                return reply
            finally:
                _promoted_turn.reset(promoted_token)
                _channel_interjection_reactions.reset(reactions_token)
                _channel_interjection_lock_key.reset(lock_key_token)
                _channel_interjection.reset(interjection_token)
        lock_key_token = _channel_interjection_lock_key.set(lock_key)
        try:
            capacity = get_workload_capacity()
            async with (
                capacity.slot(
                    workload_kind,
                    tenant_id or "unscoped",
                ),
                active_turn_boundary(),
            ):
                lock = await _get_session_lock(lock_key)
                async with lock:

                    async def _run_locked() -> str:
                        if current_task is not None:
                            async with _running_turns_guard:
                                _active_reactions[lock_key] = (
                                    current_task,
                                    reactions,
                                )
                        await _safe(reactions.on_consume)
                        try:
                            reply = await work()
                        except BaseException as exc:
                            await _safe(reactions.on_error, exc)
                            raise
                        await _safe(reactions.on_complete, reply)
                        return reply

                    if distributed:
                        async with redis_lease_lock(
                            lock_key,
                            namespace="channel-session-turn",
                        ):
                            return await _run_locked()
                    return await _run_locked()
        finally:
            _channel_interjection_lock_key.reset(lock_key_token)
    finally:
        if current_task is not None:
            async with _running_turns_guard:
                current = _turn_owner_admission.get(lock_key)
                if current is not None and current[0] is current_task:
                    _turn_owner_admission.pop(lock_key, None)
                    current[1].set()
                active = _active_reactions.get(lock_key)
                if active is not None and active[0] is current_task:
                    _active_reactions.pop(lock_key, None)
                    stale_ids = [
                        message_id
                        for message_id, candidate in _pending_receipt_anchors.items()
                        if candidate[0] == lock_key
                    ]
                    for message_id in stale_ids:
                        _pending_receipt_anchors.pop(message_id, None)
            await _clear_running_turn(lock_key, current_task)


async def run_channel_send(lock_key: str, work: Callable[[], Awaitable[object]]) -> object:
    """Serialize outbound channel operations for the same session/route."""
    lock = await _get_send_lock(lock_key)
    async with lock:
        return await work()
