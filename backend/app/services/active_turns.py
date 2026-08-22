"""Process-local lifecycle registry for currently executing root turns.

A turn is registered once per cancellable root task. Nested agent calls inherit the
same context and therefore do not create duplicate entries or ambiguous cancel
targets.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4


@dataclass(slots=True)
class DurableTurnAnchor:
    agent_id: UUID
    session_id: str
    message_id: UUID


@dataclass(slots=True)
class ActiveTurn:
    turn_id: str
    owner_user_id: UUID
    agent_id: UUID
    session_id: str
    turn_type: str | None
    turn_anchor_id: UUID | None
    durable_anchors: list[DurableTurnAnchor]
    title: str | None
    started_at: datetime
    task: asyncio.Task[object]
    cancel_task: asyncio.Task[object]
    inherited_tasks: set[asyncio.Task[object]]
    cancel_requested: bool
    cancel_sent: bool
    stop_reserved: bool
    stop_token: str | None
    stop_resolved: asyncio.Event
    anchor_gate: asyncio.Lock


_turns: dict[str, ActiveTurn] = {}
_lock = asyncio.Lock()
_current_turn: ContextVar[ActiveTurn | None] = ContextVar(
    "active_root_turn",
    default=None,
)


async def ensure_active_turn(
    *,
    owner_user_id: UUID,
    agent_id: UUID,
    session_id: str,
    turn_type: str | None = None,
    turn_anchor_id: UUID | None = None,
    title: str | None = None,
    turn_anchor_agent_id: UUID | None = None,
) -> ActiveTurn:
    """Return the inherited root turn, or register the current task as one."""

    task = asyncio.current_task()
    if task is None:
        raise RuntimeError("active turn registration requires an asyncio task")

    existing = _current_turn.get()
    if (
        existing is not None
        and not existing.task.done()
        and (task is existing.task or task in existing.inherited_tasks)
    ):
        while True:
            async with _lock:
                if not existing.stop_reserved:
                    if existing.cancel_requested:
                        raise asyncio.CancelledError
                    if existing.turn_anchor_id is None and turn_anchor_id is not None:
                        existing.turn_anchor_id = turn_anchor_id
                    if turn_anchor_id is not None and not any(
                        anchor.agent_id == (turn_anchor_agent_id or agent_id)
                        and anchor.session_id == session_id
                        and anchor.message_id == turn_anchor_id
                        for anchor in existing.durable_anchors
                    ):
                        existing.durable_anchors.append(
                            DurableTurnAnchor(
                                agent_id=turn_anchor_agent_id or agent_id,
                                session_id=session_id,
                                message_id=turn_anchor_id,
                            )
                        )
                    if existing.turn_type is None and turn_type is not None:
                        existing.turn_type = turn_type
                    if not existing.title and title:
                        existing.title = title
                    return existing
                resolved = existing.stop_resolved
            await resolved.wait()

    stop_resolved = asyncio.Event()
    stop_resolved.set()

    record = ActiveTurn(
        turn_id=str(uuid4()),
        owner_user_id=owner_user_id,
        agent_id=agent_id,
        session_id=session_id,
        turn_type=turn_type,
        turn_anchor_id=turn_anchor_id,
        durable_anchors=(
            [
                DurableTurnAnchor(
                    agent_id=turn_anchor_agent_id or agent_id,
                    session_id=session_id,
                    message_id=turn_anchor_id,
                )
            ]
            if turn_anchor_id is not None
            else []
        ),
        title=title,
        started_at=datetime.now(UTC),
        task=task,
        cancel_task=task,
        inherited_tasks=set(),
        cancel_requested=False,
        cancel_sent=False,
        stop_reserved=False,
        stop_token=None,
        stop_resolved=stop_resolved,
        anchor_gate=asyncio.Lock(),
    )
    async with _lock:
        _turns[record.turn_id] = record
    _current_turn.set(record)

    def _cleanup(_: asyncio.Task[object]) -> None:
        try:
            asyncio.get_running_loop().create_task(_unregister(record))
        except RuntimeError:
            pass

    task.add_done_callback(_cleanup)
    return record


async def _unregister(record: ActiveTurn) -> None:
    async with _lock:
        if _turns.get(record.turn_id) is record:
            _turns.pop(record.turn_id, None)


@asynccontextmanager
async def active_turn_boundary() -> AsyncIterator[None]:
    """Bound registration to one logical operation on a reusable consumer task."""

    inherited = _current_turn.get()
    owns_boundary = inherited is None
    try:
        yield
    finally:
        if owns_boundary:
            record = _current_turn.get()
            if record is not None:
                await _unregister(record)
            _current_turn.set(inherited)


def set_active_turn_cancel_task(task: asyncio.Task[object] | None) -> None:
    """Temporarily direct cancellation to a child task with normalized handling."""

    record = _current_turn.get()
    if record is not None:
        record.cancel_task = task or record.task
        if task is not None:
            record.inherited_tasks.add(task)


def is_current_turn_cancel_requested() -> bool:
    record = _current_turn.get()
    return bool(record and record.cancel_requested)


async def list_active_turns(*, owner_user_id: UUID | None = None) -> list[ActiveTurn]:
    async with _lock:
        stale = [turn_id for turn_id, record in _turns.items() if record.task.done()]
        for turn_id in stale:
            _turns.pop(turn_id, None)
        records = list(_turns.values())
    if owner_user_id is not None:
        records = [record for record in records if record.owner_user_id == owner_user_id]
    return sorted(records, key=lambda record: record.started_at)


async def get_active_turn(
    turn_id: str,
    *,
    owner_user_id: UUID | None = None,
) -> ActiveTurn | None:
    """Resolve one live turn without mutating or cancelling it."""

    async with _lock:
        record = _turns.get(turn_id)
        if record is None or record.task.done():
            return None
        if owner_user_id is not None and record.owner_user_id != owner_user_id:
            return None
        return record


async def reserve_active_turn_stop(
    turn_id: str,
    *,
    owner_user_id: UUID | None = None,
) -> tuple[ActiveTurn | None, str | None]:
    """Freeze one turn while its durable stop transaction is committed."""

    while True:
        async with _lock:
            record = _turns.get(turn_id)
            if record is None or record.task.done():
                return None, None
            if owner_user_id is not None and record.owner_user_id != owner_user_id:
                return None, None
            if record.cancel_requested:
                return record, None
        async with record.anchor_gate, _lock:
            if _turns.get(turn_id) is not record or record.task.done():
                return None, None
            if owner_user_id is not None and record.owner_user_id != owner_user_id:
                return None, None
            if record.cancel_requested:
                return record, None
            if not record.stop_reserved:
                token = str(uuid4())
                record.stop_reserved = True
                record.stop_token = token
                record.stop_resolved.clear()
                return record, token
            resolved = record.stop_resolved
        await resolved.wait()


async def commit_current_turn_anchor(
    commit: Callable[[], Awaitable[None]],
    *,
    agent_id: UUID,
    session_id: str,
    message_id: UUID,
) -> ActiveTurn:
    """Atomically admit one durable anchor and commit it before stop snapshots."""

    task = asyncio.current_task()
    record = _current_turn.get()
    if (
        task is None
        or record is None
        or record.task.done()
        or (task is not record.task and task not in record.inherited_tasks)
    ):
        raise RuntimeError("durable anchor commit requires an active root turn")

    while True:
        async with record.anchor_gate:
            added = False
            async with _lock:
                if _turns.get(record.turn_id) is not record or record.task.done():
                    raise asyncio.CancelledError
                if record.stop_reserved:
                    resolved = record.stop_resolved
                else:
                    if record.cancel_requested:
                        raise asyncio.CancelledError
                    if not any(
                        anchor.agent_id == agent_id
                        and anchor.session_id == session_id
                        and anchor.message_id == message_id
                        for anchor in record.durable_anchors
                    ):
                        record.durable_anchors.append(
                            DurableTurnAnchor(
                                agent_id=agent_id,
                                session_id=session_id,
                                message_id=message_id,
                            )
                        )
                        added = True
                    resolved = None
            if resolved is None:
                try:
                    await commit()
                except BaseException:
                    if added:
                        async with _lock:
                            record.durable_anchors = [
                                anchor
                                for anchor in record.durable_anchors
                                if not (
                                    anchor.agent_id == agent_id
                                    and anchor.session_id == session_id
                                    and anchor.message_id == message_id
                                )
                            ]
                    raise
                return record
        await resolved.wait()


async def release_active_turn_stop(record: ActiveTurn, token: str) -> None:
    """Unfreeze a reservation after its durable transaction failed."""

    async with _lock:
        if record.stop_reserved and record.stop_token == token:
            record.stop_reserved = False
            record.stop_token = None
            record.stop_resolved.set()


async def finalize_active_turn_stop(
    record: ActiveTurn,
    token: str,
) -> ActiveTurn | None:
    """Publish a committed reservation and inject cancellation exactly once."""

    async with _lock:
        if _turns.get(record.turn_id) is not record or record.task.done():
            if record.stop_token == token:
                record.stop_reserved = False
                record.stop_token = None
                record.stop_resolved.set()
            return None
        if not record.stop_reserved or record.stop_token != token:
            return record if record.cancel_requested else None
        record.stop_reserved = False
        record.stop_token = None
        record.cancel_requested = True
        record.stop_resolved.set()
        if not record.cancel_sent:
            record.cancel_sent = True
            target = record.cancel_task
            if target.done():
                target = record.task
            target.cancel()
        return record


async def wait_for_current_turn_stop_resolution(*, allow_cancelled: bool = False) -> None:
    """Pause a terminal write while its control-plane stop is unresolved."""

    record = _current_turn.get()
    if record is None:
        return
    while record.stop_reserved:
        await record.stop_resolved.wait()
    if record.cancel_requested and not allow_cancelled:
        raise asyncio.CancelledError


async def cancel_active_turn(
    turn_id: str,
    *,
    owner_user_id: UUID | None = None,
) -> ActiveTurn | None:
    """Cancel one exact root turn, optionally constrained to its owner."""

    async with _lock:
        record = _turns.get(turn_id)
        if record is None or record.task.done():
            return None
        if owner_user_id is not None and record.owner_user_id != owner_user_id:
            return None
        if record.cancel_requested:
            return record
        record.cancel_requested = True
        record.stop_reserved = False
        record.stop_token = None
        record.stop_resolved.set()
        if not record.cancel_sent:
            record.cancel_sent = True
            target = record.cancel_task
            if target.done():
                target = record.task
            target.cancel()
        return record


async def wait_until_stopped(record: ActiveTurn, *, timeout: float = 3.0) -> bool:
    """Wait briefly for cleanup without propagating the cancelled task's result."""

    if record.task is asyncio.current_task() or record.task.done():
        return record.task.done()
    done, _ = await asyncio.wait({record.task}, timeout=timeout)
    return bool(done)


async def reset_active_turns_for_testing() -> None:
    """Clear registry state between tests without cancelling unrelated test tasks."""

    async with _lock:
        _turns.clear()
    _current_turn.set(None)
