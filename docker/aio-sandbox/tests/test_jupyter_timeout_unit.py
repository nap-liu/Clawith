"""Unit-level lifecycle checks for the patched AIO Jupyter service."""

import asyncio
import queue
import time
import unittest
from unittest.mock import AsyncMock

from app.models.jupyter import KernelStatus
from app.services.jupyter import JupyterService, KernelSession


class _StreamingClient:
    def execute(self, _code, **_kwargs):
        return "message-id"

    def get_iopub_msg(self, timeout):
        return {
            "msg_type": "stream",
            "parent_header": {"msg_id": "message-id"},
            "content": {"name": "stdout", "text": "tick\n"},
        }


class _SilentClient:
    def execute(self, _code, **_kwargs):
        return "message-id"

    def get_iopub_msg(self, timeout):
        time.sleep(timeout)
        raise queue.Empty


class JupyterDeadlineUnitTest(unittest.TestCase):
    def service(self):
        service = JupyterService.__new__(JupyterService)
        service._large_code_staging_enabled = False
        return service

    def test_continuous_output_does_not_extend_absolute_deadline(self):
        started = time.monotonic()
        result = self.service()._execute_code_sync(
            _StreamingClient(), "while True: print('tick')", 0.02
        )

        self.assertEqual(result["status"], "timeout")
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertTrue(result["outputs"])

    def test_queue_empty_at_deadline_is_timeout_not_kernel_error(self):
        result = self.service()._execute_code_sync(
            _SilentClient(), "while True: pass", 0.02
        )

        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["outputs"], [])


class _Manager:
    def interrupt_kernel(self):
        pass

    def shutdown_kernel(self, now=False):
        pass


class _Client:
    def stop_channels(self):
        pass


class JupyterRecoveryUnitTest(unittest.IsolatedAsyncioTestCase):
    async def run_timeout(self, *, interrupted: bool):
        session = KernelSession(_Manager(), _Client(), "python3.10", "session-id")
        session.interrupt_and_wait = lambda _msg_id, _timeout: interrupted
        cleanup_called = False

        def cleanup():
            nonlocal cleanup_called
            cleanup_called = True

        session.cleanup = cleanup

        service = JupyterService.__new__(JupyterService)
        service._default_kernel = "python3.10"
        service.sessions = {session.session_id: session}
        service._get_or_create_session_async = AsyncMock(return_value=session)
        service._execute_code_sync = lambda *_args: {
            "msg_id": "message-id",
            "status": "timeout",
            "execution_count": None,
            "outputs": [],
            "code": "while True: pass",
        }
        service._log_execute_code_event = lambda **_kwargs: None

        result = await service.execute_code(
            "while True: pass", timeout=1, session_id=session.session_id
        )
        return service, result, cleanup_called

    async def test_interruptible_timeout_keeps_session(self):
        service, result, cleanup_called = await self.run_timeout(interrupted=True)

        self.assertEqual(result.status, KernelStatus.TIMEOUT)
        self.assertIn("session-id", service.sessions)
        self.assertFalse(cleanup_called)
        self.assertIn("kernel interrupted", result.outputs[-1].evalue)

    async def test_uninterruptible_timeout_resets_session(self):
        service, result, cleanup_called = await self.run_timeout(interrupted=False)

        self.assertEqual(result.status, KernelStatus.TIMEOUT)
        self.assertNotIn("session-id", service.sessions)
        self.assertTrue(cleanup_called)
        self.assertIn("kernel reset", result.outputs[-1].evalue)


if __name__ == "__main__":
    unittest.main()
