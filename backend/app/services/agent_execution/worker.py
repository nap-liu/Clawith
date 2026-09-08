"""Single-use execution child; never starts the ASGI application lifecycle."""

from __future__ import annotations

import asyncio
import ctypes
import importlib
import os
import signal
import socket
import sys
from contextlib import suppress

from app.services.agent_execution.ipc import Peer


async def _invoke(entrypoint, arguments, agent_id, context, acquire, release, calls):
    from app.services.agent_execution.bridge import supervisor_calls
    from app.services.agent_execution.context import bind_context
    from app.services.agent_execution.provider import provider_bridge
    from app.services.agent_execution.runtime import execution_agent

    module, name = entrypoint.split(":", 1)
    function = getattr(importlib.import_module(module), name)
    execution_agent.set(agent_id)
    provider_bridge.set((acquire, release))
    supervisor_calls.set(calls)
    with bind_context(context):
        if arguments.pop("_execution_db", False):
            from app.database import async_session

            async with async_session() as db:
                return await function(db=db, **arguments)
        return await function(**arguments)


def _bind_parent_lifetime(parent_pid):
    """Also reap blocked descendants when their own supervisor dies."""
    if sys.platform == "linux":
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "Cannot bind execution lifetime")
        if os.getppid() != parent_pid:
            raise SystemExit("Execution supervisor exited during startup")


async def main(fd, parent_pid):
    _bind_parent_lifetime(parent_pid)
    reader, writer = await asyncio.open_connection(sock=socket.socket(fileno=fd))
    peer = Peer(reader, writer, side="child")
    await peer.send(("ready",))
    _, entrypoint, arguments, agent_id, context, acquire, release, calls = await peer.receive()
    # The parent already imported these modules before writing the request;
    # register the complete model graph before the first independent DB read.
    import app.models.registry  # noqa: F401

    listener = asyncio.create_task(peer.listen())
    task = asyncio.create_task(_invoke(entrypoint, arguments, agent_id, context, acquire, release, calls))
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        value = await task
        await peer.send(("result", value, None))
    except BaseException as exc:
        with suppress(ConnectionError, BrokenPipeError):
            await peer.send(("result", None, exc))
    finally:
        listener.cancel()
        await asyncio.gather(listener, return_exceptions=True)
        await peer.close()
        from app.database import engine
        from app.core.events import close_redis

        await close_redis()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]), int(sys.argv[2])))
