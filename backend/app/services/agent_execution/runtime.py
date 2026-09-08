"""Supervise one existing Agent invocation outside the ingress event loop."""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import sys
import uuid
from contextlib import suppress
from contextvars import ContextVar
from functools import wraps
from inspect import signature

from loguru import logger

from app.services.agent_execution.ipc import Peer

execution_agent: ContextVar[str | None] = ContextVar("execution_agent", default=None)


class ExecutionProcessError(RuntimeError):
    """The isolated execution ended without a terminal result."""


def isolation_enabled():
    return os.environ.get("AGENT_EXECUTION_ISOLATION", "1").lower() not in {"0", "false"}


async def _terminate(process):
    if process.returncode is None:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), 2.0)
        except TimeoutError:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()


async def _close_execution(process, peer, listeners, provider_leases, parent_socket):
    if process is not None:
        await _terminate(process)
    for task in listeners:
        task.cancel()
    await asyncio.gather(*listeners, return_exceptions=True)
    if peer is not None:
        await peer.close()
    else:
        parent_socket.close()
    await provider_leases.close()


async def run_isolated(entrypoint, arguments, *, agent_id=None, context=None):
    """Run trusted application code through a private inherited socketpair.

    Admission, timeouts and resource settings retain their existing behavior.
    """
    from app.services.agent_execution.context import export_context
    from app.services.agent_execution.bridge import make_supervisor_calls
    from app.services.agent_execution.provider import ProviderLeases

    parent_socket, child_socket = socket.socketpair()
    process = None
    peer = None
    listeners = []
    provider_leases = ProviderLeases()
    try:
        environment = dict(os.environ)
        environment.pop("INSTANCE_ID", None)
        # Children never inherit live DB/Redis connections, connector roles,
        # schedulers, or schema bootstrap work.
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "app.services.agent_execution.worker", str(child_socket.fileno()),
            str(os.getpid()),
            pass_fds=(child_socket.fileno(),), start_new_session=True, env=environment,
            stdin=asyncio.subprocess.DEVNULL,
        )
        child_socket.close()
        reader, writer = await asyncio.open_connection(sock=parent_socket)
        peer = Peer(reader, writer, side="parent")
        ready = await peer.receive()
        if ready != ("ready",):
            raise ExecutionProcessError("Invalid execution startup handshake")
        await peer.send(("start", entrypoint, arguments, str(agent_id or ""),
                         export_context() if context is None else context,
                         provider_leases.acquire, provider_leases.release, make_supervisor_calls()))
        logger.info("[agent_execution] started agent={} pid={}", agent_id, process.pid)
        listeners = [asyncio.create_task(peer.listen())]
        completed, _ = await asyncio.wait([peer.result, *listeners], return_when=asyncio.FIRST_COMPLETED)
        if peer.result not in completed and not peer.result.done():
            for task in completed:
                task.result()
            raise ExecutionProcessError("Agent execution exited without a result")
        value, error = peer.result.result()
    except (ConnectionError, asyncio.IncompleteReadError, TimeoutError) as exc:
        raise ExecutionProcessError("Agent execution process became unavailable") from exc
    finally:
        child_socket.close()
        cleanup = asyncio.create_task(
            _close_execution(process, peer, listeners, provider_leases, parent_socket)
        )
        cancelled_during_cleanup = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled_during_cleanup = True
        cleanup.result()
        if cancelled_during_cleanup:
            raise asyncio.CancelledError
    # Business exceptions are not evidence that the IPC transport failed.
    if error is not None:
        raise error
    return value


def isolate_agent_execution(func):
    """Preserve the call contract while moving its execution into a child."""
    contract = signature(func)
    entrypoint = f"{func.__module__}:{func.__name__}"

    @wraps(func)
    async def wrapped(*args, **kwargs):
        arguments = dict(contract.bind(*args, **kwargs).arguments)
        agent_id = arguments.get("agent_id")
        if not isolation_enabled() or execution_agent.get() is not None:
            return await func(*args, **kwargs)
        from app.services.active_turns import ensure_active_turn, resolve_execution_owner
        from app.services.llm.failure_outcome import make_llm_failure

        user_id = arguments.get("user_id") or arguments.get("execution_user_id")
        session_id = arguments.get("session_id")
        legacy_background = arguments.get("db") is not None
        registration_session = session_id
        if legacy_background and not registration_session:
            registration_session = f"{arguments.get('turn_type', 'background')}:{uuid.uuid4()}"
        if user_id is None and legacy_background and agent_id:
            agent_uuid = uuid.UUID(str(agent_id))
            user_id = await resolve_execution_owner(agent_uuid, agent_uuid)
        if agent_id and user_id and registration_session:
            try:
                owner_id, agent_uuid = uuid.UUID(str(user_id)), uuid.UUID(str(agent_id))
            except (TypeError, ValueError):
                pass
            else:
                owner_id = await resolve_execution_owner(agent_uuid, owner_id)
                await ensure_active_turn(
                    owner_user_id=owner_id, agent_id=agent_uuid,
                    session_id=str(registration_session),
                    turn_type=arguments.get("turn_type", "background" if legacy_background else None),
                    turn_anchor_id=arguments.get("turn_anchor_id"),
                    turn_anchor_agent_id=arguments.get("turn_anchor_agent_id"),
                )
        from app.models.llm import LLMModel
        from app.services.llm.runtime_model import RuntimeLLMModel

        for name in ("model", "primary_model", "fallback_model"):
            if isinstance(arguments.get(name), LLMModel):
                arguments[name] = RuntimeLLMModel.from_orm(arguments[name])
        # The legacy background entry accepts a DB session. Only snapshot-free
        # inputs cross the boundary; its child opens an independent session.
        db = arguments.pop("db", None)
        if db is not None:
            await db.commit()
            arguments["_execution_db"] = True
        try:
            return await run_isolated(entrypoint, arguments, agent_id=agent_id)
        except ExecutionProcessError as exc:
            logger.error("[agent_execution] failed agent={} session={}: {}", agent_id, session_id, exc)
            return make_llm_failure(
                code="agent_execution_unavailable", message_key="errors.agentExecutionUnavailable",
            )

    return wrapped
