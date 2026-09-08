"""Private, inherited-socket RPC for one parent and its own execution child.

This is trusted Python IPC, never a network API. Pickle frames must only travel
over the socketpair created by the supervisor. Callback acknowledgements retain
the original await ordering, including nested callbacks during inbox drains.
"""

from __future__ import annotations

import asyncio
import inspect
import io
import pickle
import struct
from contextlib import suppress
from contextvars import copy_context
from types import MappingProxyType

def _merge_argument(original, updated):
    """Preserve observable in-place callback edits (for example tool receipts)."""
    if isinstance(original, dict) and isinstance(updated, dict):
        for key in list(original):
            if key not in updated:
                del original[key]
        for key, value in updated.items():
            if key in original and type(original[key]) is type(value):
                _merge_argument(original[key], value)
            else:
                original[key] = value
            if not isinstance(value, (dict, list)):
                original[key] = value
    elif isinstance(original, list) and isinstance(updated, list):
        original[:] = updated


def _failure(content, attributes):
    from app.services.llm.failure_outcome import LLMFailure

    return LLMFailure(content, **attributes)


def _exception(kind, args, attributes):
    value = BaseException.__new__(kind)
    BaseException.__init__(value, *args)
    value.__dict__.update(attributes)
    return value


class _Writer(pickle.Pickler):
    def __init__(self, output, peer):
        super().__init__(output, protocol=5)
        self.peer = peer

    def persistent_id(self, value):
        if getattr(value, "_execution_peer", None) is self.peer:
            return ("callback", value._execution_owner, value._execution_id, None)
        if inspect.iscoroutinefunction(value) or (
            inspect.isfunction(value) and "<locals>" in value.__qualname__
        ):
            key = str(id(value))
            self.peer.callbacks[key] = value
            self.peer.callback_contexts[key] = copy_context()
            return ("callback", self.peer.side, key, inspect.signature(value))
        return None

    def reducer_override(self, value):
        from app.services.llm.failure_outcome import LLMFailure

        if isinstance(value, LLMFailure):
            return _failure, (str(value), {
                "code": value.code, "message_key": value.message_key,
                "retryable": value.retryable, "allow_failover": value.allow_failover,
                "details": dict(value.details),
            })
        if isinstance(value, BaseException) and type(value).__module__ != "builtins":
            return _exception, (type(value), value.args, value.__dict__)
        if isinstance(value, MappingProxyType):
            return dict, (dict(value),)
        return NotImplemented


class _Reader(pickle.Unpickler):
    def __init__(self, source, peer):
        super().__init__(source)
        self.peer = peer

    def persistent_load(self, value):
        kind, owner, key, signature = value
        if kind != "callback":
            raise ValueError("Invalid execution callback")
        if owner == self.peer.side:
            return self.peer.callbacks[key]

        async def callback(*args, **kwargs):
            from app.services.agent_execution.context import export_context

            return await self.peer.call(key, args, kwargs, export_context())

        callback._execution_peer = self.peer
        callback._execution_owner = owner
        callback._execution_id = key
        if signature is not None:
            callback.__signature__ = signature
        return callback


class Peer:
    def __init__(self, reader, writer, *, side):
        self.reader = reader
        self.writer = writer
        self.side = side
        self.callbacks = {}
        self.callback_contexts = {}
        self.pending = {}
        self.tasks = set()
        self.write_lock = asyncio.Lock()
        self.sequence = 0
        self.result = asyncio.get_running_loop().create_future()

    async def send(self, value):
        output = io.BytesIO()
        _Writer(output, self).dump(value)
        data = output.getvalue()
        async with self.write_lock:
            self.writer.write(struct.pack("!Q", len(data)))
            self.writer.write(data)
            await self.writer.drain()

    async def receive(self):
        size = struct.unpack("!Q", await self.reader.readexactly(8))[0]
        return _Reader(io.BytesIO(await self.reader.readexactly(size)), self).load()

    async def call(self, key, args, kwargs, context):
        self.sequence += 1
        request_id = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.send(("call", request_id, key, args, kwargs, context))
            value, updated_args, updated_kwargs = await future
            for original, updated in zip(args, updated_args):
                _merge_argument(original, updated)
            _merge_argument(kwargs, updated_kwargs)
            return value
        finally:
            self.pending.pop(request_id, None)

    async def _dispatch(self, request):
        from app.services.active_turns import inherit_active_turn
        from app.services.agent_execution.context import bind_context

        _, request_id, key, args, kwargs, context = request
        try:
            with inherit_active_turn(), bind_context(context):
                value = self.callbacks[key](*args, **kwargs)
                if inspect.isawaitable(value):
                    value = await value
            await self.send(("return", request_id, (value, args, kwargs), None))
        except BaseException as exc:
            if not self.writer.is_closing():
                with suppress(ConnectionError, BrokenPipeError):
                    await self.send(("return", request_id, None, exc))

    async def listen(self):
        try:
            while True:
                message = await self.receive()
                kind = message[0]
                if kind == "call":
                    callback_context = self.callback_contexts.get(message[2])
                    task = asyncio.create_task(
                        self._dispatch(message),
                        context=callback_context.copy() if callback_context is not None else None,
                    )
                    self.tasks.add(task)
                    task.add_done_callback(self.tasks.discard)
                elif kind == "return":
                    _, request_id, value, error = message
                    future = self.pending.get(request_id)
                    if future is not None and not future.done():
                        future.set_exception(error) if error is not None else future.set_result(value)
                elif kind == "result":
                    if not self.result.done():
                        self.result.set_result(message[1:])
                    return
                else:
                    raise ValueError("Unexpected execution IPC message")
        except (EOFError, ConnectionError, asyncio.IncompleteReadError):
            if not self.result.done():
                self.result.set_exception(ConnectionError("Execution process disconnected"))
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("Execution process disconnected"))

    async def close(self):
        self.writer.close()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        with suppress(ConnectionError, BrokenPipeError):
            await self.writer.wait_closed()
        if self.result.done() and not self.result.cancelled():
            self.result.exception()
