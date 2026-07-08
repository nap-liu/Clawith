from __future__ import annotations

from concurrent.futures import CancelledError

from app.services import dingtalk_stream


class _CancelledFuture:
    def result(self):
        raise CancelledError()

    def add_done_callback(self, callback):
        callback(self)


def test_fire_and_forget_ignores_cancelled_future(monkeypatch):
    async def noop():
        return None

    exception_logs: list[str] = []

    def fake_run_coroutine_threadsafe(coro, loop):
        coro.close()
        return _CancelledFuture()

    def fake_exception(message):
        exception_logs.append(message)

    monkeypatch.setattr(dingtalk_stream.asyncio, "run_coroutine_threadsafe", fake_run_coroutine_threadsafe)
    monkeypatch.setattr(dingtalk_stream.logger, "exception", fake_exception)

    dingtalk_stream._fire_and_forget(loop=object(), coro=noop())

    assert exception_logs == []
