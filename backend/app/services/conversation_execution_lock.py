"""Cross-process execution lease shared by every durable conversation turn."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from functools import wraps
from inspect import signature
from typing import Any, ParamSpec, TypeVar

from app.services.redis_lease_lock import RedisLeaseError, redis_lease_lock

_P = ParamSpec("_P")
_R = TypeVar("_R")

_held_resources: ContextVar[dict[str, asyncio.Task[Any]]] = ContextVar(
    "conversation_execution_resources",
    default={},
)
_inherited_resources: ContextVar[frozenset[str]] = ContextVar(
    "supervised_conversation_execution_resources", default=frozenset(),
)


def supervised_conversation_resources() -> frozenset[str]:
    """Only leases owned by this awaited execution may cross into its child."""
    from app.services.active_turns import shares_active_turn

    task = asyncio.current_task()
    return _inherited_resources.get() | frozenset(
        resource for resource, owner in _held_resources.get().items()
        if owner is task or shares_active_turn(owner)
    )


def _durable_resource(session_id: object) -> str | None:
    try:
        durable_session_id = uuid.UUID(str(session_id))
    except (TypeError, ValueError):
        return None
    return str(durable_session_id)


@asynccontextmanager
async def conversation_execution_lock(
    *,
    agent_id: object,
    session_id: object,
) -> AsyncIterator[None]:
    """Serialize one ChatSession's model/tool loop across every process."""

    del agent_id  # A2A peers intentionally share one globally unique Session.
    resource = _durable_resource(session_id)
    task = asyncio.current_task()
    if resource is None or task is None:
        yield
        return

    from app.services.agent_execution.runtime import execution_agent

    if execution_agent.get() is not None and resource in _inherited_resources.get():
        yield
        return

    held = _held_resources.get()
    from app.services.active_turns import shares_active_turn

    if held.get(resource) is task or shares_active_turn(held.get(resource)):
        yield
        return

    async with redis_lease_lock(resource, namespace="conversation-execution"):
        token = _held_resources.set({**held, resource: task})
        try:
            yield
        finally:
            _held_resources.reset(token)


def serialize_conversation_execution(
    func: Callable[_P, Awaitable[_R]],
) -> Callable[_P, Awaitable[_R]]:
    """Apply the shared lease to a session-aware async entry point."""

    call_signature = signature(func)

    @wraps(func)
    async def _wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        bound = call_signature.bind_partial(*args, **kwargs)
        durable_agent_id = bound.arguments.get(
            "turn_anchor_agent_id"
        ) or bound.arguments.get("agent_id")
        try:
            async with conversation_execution_lock(
                agent_id=durable_agent_id,
                session_id=bound.arguments.get("session_id"),
            ):
                return await func(*args, **kwargs)
        except RedisLeaseError as exc:
            if bound.arguments.get("turn_anchor_id"):
                from app.services.turn_interruption import TurnInterrupted

                raise TurnInterrupted(str(exc)) from exc
            raise

    return _wrapped
