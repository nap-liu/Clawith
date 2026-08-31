"""One bounded, renewable write queue per Agent workspace."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from functools import wraps
from inspect import signature
from typing import Any, ParamSpec, TypeVar

from app.services.redis_lease_lock import RedisLeaseBusyError, redis_lease_lock

WORKSPACE_LOCK_ACQUIRE_TIMEOUT_SECONDS = 30.0
_P = ParamSpec("_P")
_R = TypeVar("_R")
_held_agents: ContextVar[dict[str, asyncio.Task[Any]] | None] = ContextVar(
    "workspace_write_agents",
    default=None,
)


class WorkspaceLockTimeoutError(RuntimeError):
    """The workspace write queue did not become available within its bound."""


@asynccontextmanager
async def workspace_locks(
    agent_id: uuid.UUID,
    paths: list[str],
    *,
    acquire_timeout_seconds: float = WORKSPACE_LOCK_ACQUIRE_TIMEOUT_SECONDS,
) -> AsyncIterator[None]:
    """Serialize workspace mutations for one Agent, regardless of file path.

    Path-level locks allowed broad operations such as ``workspace`` to race an
    exact file mutation such as ``workspace/report.md``. A workspace is the
    actual consistency boundary, so every mutation for one Agent shares one
    bounded Redis queue. The underlying lease renews while held and remains
    cancellation-safe.
    """
    del paths  # Kept in the public signature for existing mutation call sites.
    resource = str(agent_id)
    task = asyncio.current_task()
    held = _held_agents.get() or {}
    if task is not None and held.get(resource) is task:
        yield
        return
    try:
        async with redis_lease_lock(
            resource,
            namespace="workspace-write",
            acquire_timeout_seconds=acquire_timeout_seconds,
        ):
            token = _held_agents.set({**held, resource: task})
            try:
                yield
            finally:
                _held_agents.reset(token)
    except RedisLeaseBusyError as exc:
        raise WorkspaceLockTimeoutError(
            "Workspace remained busy after waiting "
            f"{acquire_timeout_seconds:g} seconds. Another operation is still writing this Agent's workspace. "
            "No new workspace write was started; retry later or continue with a task that does not modify the workspace."
        ) from exc


def serialize_workspace_write(
    func: Callable[_P, Awaitable[_R]],
) -> Callable[_P, Awaitable[_R]]:
    """Put one complete Agent workspace mutation in the shared write queue."""
    call_signature = signature(func)

    @wraps(func)
    async def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        bound = call_signature.bind(*args, **kwargs)
        agent_id = bound.arguments.get("agent_id")
        if agent_id is None:
            agent_id = bound.arguments["agent"].id
        async with workspace_locks(agent_id, []):
            return await func(*args, **kwargs)

    return wrapped
