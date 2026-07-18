"""Black-box regression tests for AIO stdio MCP timeout cleanup.

Run inside the patched AIO container after its HTTP API is healthy. The test
uses unique server names, restores mcp-hub.json, and never contacts an external
MCP service.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import time
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path


BASE_URL = os.environ.get("AIO_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
CONFIG_PATH = Path(os.environ.get("MCP_SERVERS_CONFIG", "/opt/gem/mcp-hub.json"))


def request(path: str, *, timeout: float = 10.0) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(f"{BASE_URL}{path}", timeout=timeout) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class McpLifecycleTests(unittest.TestCase):
    def test_concurrent_timeouts_reap_children_and_leave_api_usable(self):
        original = CONFIG_PATH.read_text()
        prefix = f"clawith-timeout-{uuid.uuid4().hex[:10]}"
        names = [f"{prefix}-{index}" for index in range(3)]
        pid_paths = [Path(f"/tmp/{name}.pid") for name in names]

        try:
            config = json.loads(original)
            servers = config.setdefault("mcpServers", {})
            for name, pid_path in zip(names, pid_paths, strict=True):
                pid_path.unlink(missing_ok=True)
                servers[name] = {
                    "type": "stdio",
                    "command": "/bin/sh",
                    "args": [
                        "-c",
                        f"echo $$ > {pid_path}; exec sleep 999",
                    ],
                }

            # The production registration path deliberately overwrites this
            # chmod-666 file in place because /opt/gem itself is root-owned.
            with CONFIG_PATH.open("r+") as handle:
                handle.seek(0)
                json.dump(config, handle)
                handle.truncate()

            started = time.monotonic()
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(names)) as pool:
                results = list(
                    pool.map(
                        lambda name: request(
                            f"/v1/mcp/{name}/tools?timeout=1", timeout=10
                        ),
                        names,
                    )
                )
            elapsed = time.monotonic() - started

            self.assertLess(elapsed, 8, results)
            self.assertTrue(all(status == 500 for status, _ in results), results)

            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                if all(
                    path.exists() and not pid_is_alive(int(path.read_text().strip()))
                    for path in pid_paths
                ):
                    break
                time.sleep(0.05)

            for path in pid_paths:
                self.assertTrue(path.exists(), path)
                pid = int(path.read_text().strip())
                self.assertFalse(pid_is_alive(pid), f"stdio child PID {pid} survived")

            status, body = request("/v1/mcp/browser/tools?timeout=5", timeout=10)
            self.assertEqual(status, 200, body)
            self.assertTrue(body.get("success"), body)
        finally:
            with CONFIG_PATH.open("r+") as handle:
                handle.seek(0)
                handle.write(original)
                handle.truncate()
            for path in pid_paths:
                path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
