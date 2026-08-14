"""Black-box regression tests for the Clawith Jupyter timeout patch.

Run inside the patched aio-sandbox container:

    SANDBOX_TEST_API_URL=http://127.0.0.1:8080 \
      python /opt/clawith-tests/test_jupyter_lifecycle.py -v
"""

import json
import os
import time
import unittest
import urllib.error
import urllib.request
import uuid


API_URL = os.environ.get("SANDBOX_TEST_API_URL", "http://127.0.0.1:8080")


def request(method: str, path: str, data: dict | None = None, timeout: float = 20):
    payload = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request(
        API_URL + path,
        data=payload,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


class JupyterLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.token = uuid.uuid4().hex[:10]
        self.session_id = f"clawith-jupyter-timeout-{self.token}"
        self.created_sessions: set[str] = set()
        status, body = request(
            "POST",
            "/v1/jupyter/sessions/create",
            {"session_id": self.session_id, "kernel_name": "python3.10", "cwd": "/tmp"},
            timeout=30,
        )
        self.assertEqual(status, 200, body)
        actual = (body.get("data") or {}).get("session_id")
        if actual:
            self.session_id = actual
            self.created_sessions.add(actual)

    def tearDown(self):
        for session_id in self.created_sessions:
            request("DELETE", f"/v1/jupyter/sessions/{session_id}")
        archive_dir = f"/tmp/clawith-jupyter-archive-{self.token}"
        try:
            import shutil

            shutil.rmtree(archive_dir)
        except FileNotFoundError:
            pass

    def execute(self, code: str, *, timeout: int = 1):
        status, body = request(
            "POST",
            "/v1/jupyter/execute",
            {
                "session_id": self.session_id,
                "kernel_name": "python3.10",
                "cwd": "/tmp",
                "code": code,
                "timeout": timeout,
            },
            timeout=timeout + 8,
        )
        self.assertEqual(status, 200, body)
        returned_session = (body.get("data") or {}).get("session_id")
        if returned_session:
            self.created_sessions.add(returned_session)
            self.session_id = returned_session
        return body

    def assert_hard_timeout(self, body: dict):
        self.assertFalse(body["success"], body)
        self.assertEqual(body["data"]["status"], "timeout", body)
        outputs = body["data"].get("outputs") or []
        errors = [output for output in outputs if output.get("ename") == "HardTimeout"]
        self.assertTrue(errors, body)
        self.assertIn("hard_timeout", errors[-1].get("evalue", ""))

    def test_infinite_loop_times_out_and_followup_runs(self):
        started = time.monotonic()
        result = self.execute("while True:\n    pass")
        elapsed = time.monotonic() - started

        self.assert_hard_timeout(result)
        self.assertLess(elapsed, 6, result)

        followup = self.execute("print('jupyter-followup-ok')", timeout=5)
        self.assertTrue(followup["success"], followup)
        texts = [
            output.get("text", "")
            for output in followup["data"].get("outputs") or []
            if output.get("output_type") == "stream"
        ]
        self.assertIn("jupyter-followup-ok", "".join(texts))

    def test_continuous_output_cannot_extend_deadline(self):
        result = self.execute(
            "import time\n"
            "while True:\n"
            "    print('tick', flush=True)\n"
            "    time.sleep(0.01)"
        )
        self.assert_hard_timeout(result)

    def test_uninterruptible_cell_resets_only_its_kernel(self):
        result = self.execute(
            "import signal\n"
            "marker_before_timeout = 'must-be-cleared'\n"
            "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
            "while True:\n"
            "    pass"
        )
        self.assert_hard_timeout(result)
        errors = [
            output
            for output in result["data"].get("outputs") or []
            if output.get("ename") == "HardTimeout"
        ]
        self.assertIn("kernel reset", errors[-1].get("evalue", ""))

        followup = self.execute(
            "print('fresh-kernel-ok', 'marker_before_timeout' in globals())",
            timeout=5,
        )
        self.assertTrue(followup["success"], followup)
        texts = [
            output.get("text", "")
            for output in followup["data"].get("outputs") or []
            if output.get("output_type") == "stream"
        ]
        self.assertIn("fresh-kernel-ok False", "".join(texts))

    def test_self_including_archive_stops_growing_after_timeout(self):
        archive_dir = f"/tmp/clawith-jupyter-archive-{self.token}"
        archive_path = f"{archive_dir}/recursive.zip"
        result = self.execute(
            "import os, pathlib, shutil\n"
            f"root = pathlib.Path({archive_dir!r})\n"
            "root.mkdir(parents=True, exist_ok=True)\n"
            "(root / 'payload.bin').write_bytes(os.urandom(1024 * 1024))\n"
            "shutil.make_archive(str(root / 'recursive'), 'zip', root_dir=root)"
        )
        self.assert_hard_timeout(result)

        first_size = os.path.getsize(archive_path)
        time.sleep(0.5)
        second_size = os.path.getsize(archive_path)
        self.assertEqual(first_size, second_size)


if __name__ == "__main__":
    unittest.main()
