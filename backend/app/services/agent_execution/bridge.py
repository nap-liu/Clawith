"""Keep root lifecycle and independent jobs in the supervising process."""

import asyncio
from contextvars import Context, ContextVar
from functools import wraps
from importlib import import_module

from loguru import logger

supervisor_calls: ContextVar[dict | None] = ContextVar("execution_supervisor_calls", default=None)
_background_tasks = set()


async def _invoke(entrypoint, args, kwargs):
    module, name = entrypoint.split(":", 1)
    return await getattr(import_module(module), name)(*args, **kwargs)


async def dispatch_background(entrypoint, *args, _task_name=None, **kwargs):
    """Acknowledge ownership before the originating Turn may exit."""
    calls = supervisor_calls.get()
    if calls is not None:
        await calls["dispatch"](entrypoint, *args, _task_name=_task_name, **kwargs)
        return
    task = asyncio.create_task(_invoke(entrypoint, args, kwargs), context=Context(), name=_task_name or entrypoint)
    _background_tasks.add(task)

    def finished(task):
        _background_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error("[agent_execution] independent job failed: {}", task.exception())

    task.add_done_callback(finished)


def supervised_operation(func):
    """Route process-owned admission/dedup logic to its existing supervisor."""
    entrypoint = f"{func.__module__}:{func.__name__}"

    @wraps(func)
    async def wrapped(*args, **kwargs):
        calls = supervisor_calls.get()
        if calls is not None:
            return await calls["operation"](entrypoint, args, kwargs)
        return await func(*args, **kwargs)

    return wrapped


def make_supervisor_calls():
    from app.services.active_turns import (
        commit_current_turn_anchor, ensure_active_turn, wait_for_current_turn_stop_resolution,
    )

    async def register(**kwargs):
        await ensure_active_turn(**kwargs)

    async def commit(commit, **kwargs):
        await commit_current_turn_anchor(commit, **kwargs)

    async def operation(entrypoint, args, kwargs):
        return await asyncio.create_task(_invoke(entrypoint, args, kwargs), context=Context())

    return {"register": register, "commit": commit,
            "wait_for_stop": wait_for_current_turn_stop_resolution,
            "dispatch": dispatch_background, "operation": operation}
