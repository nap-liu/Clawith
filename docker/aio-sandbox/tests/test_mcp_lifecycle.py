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


def request(
    path: str,
    *,
    method: str = "GET",
    data: dict | None = None,
    timeout: float = 10.0,
) -> tuple[int, dict]:
    payload = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=payload,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
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
        # The MCP router is mounted lazily by the upstream image.  Warm that
        # one-time import before installing short-lived test entries so a cold
        # start cannot consume the client deadline and restore the config while
        # the request is still waiting to enter the handler.
        status, body = request("/v1/mcp/browser/tools?timeout=30", timeout=40)
        self.assertEqual(status, 200, body)
        self.assertTrue(body.get("success"), body)

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

    def test_distinct_stdio_processes_share_one_agent_workspace(self):
        status, body = request("/v1/mcp/browser/tools?timeout=30", timeout=40)
        self.assertEqual(status, 200, body)

        original = CONFIG_PATH.read_text()
        token = uuid.uuid4().hex[:10]
        writer = f"clawith-shared-{token}-writer"
        reader = f"clawith-shared-{token}-reader"
        workspace = Path(f"/tmp/clawith-shared-{token}")
        server_script = Path(f"/tmp/clawith-shared-{token}.py")
        marker = f"shared-marker-{token}"

        try:
            workspace.mkdir(mode=0o777)
            workspace.chmod(0o777)
            server_script.write_text(
                "from pathlib import Path\n"
                "from mcp.server.fastmcp import FastMCP\n"
                "mcp = FastMCP('shared-workspace-test')\n"
                "@mcp.tool()\n"
                "def write_shared(value: str) -> str:\n"
                "    Path('shared.txt').write_text(value)\n"
                "    return value\n"
                "@mcp.tool()\n"
                "def read_shared() -> str:\n"
                "    return Path('shared.txt').read_text()\n"
                "mcp.run(transport='stdio')\n"
            )
            server_script.chmod(0o644)

            config = json.loads(original)
            servers = config.setdefault("mcpServers", {})
            shared_entry = {
                "type": "stdio",
                "command": "/opt/python3.12/bin/python",
                "args": [str(server_script)],
                "cwd": str(workspace),
            }
            servers[writer] = shared_entry
            servers[reader] = shared_entry
            with CONFIG_PATH.open("r+") as handle:
                handle.seek(0)
                json.dump(config, handle)
                handle.truncate()

            status, write_body = request(
                f"/v1/mcp/{writer}/tools/write_shared?timeout=45",
                method="POST",
                data={"value": marker},
                timeout=60,
            )
            self.assertEqual(status, 200, write_body)

            status, read_body = request(
                f"/v1/mcp/{reader}/tools/read_shared?timeout=45",
                method="POST",
                data={},
                timeout=60,
            )
            self.assertEqual(status, 200, read_body)
            self.assertIn(marker, json.dumps(read_body), read_body)
        finally:
            with CONFIG_PATH.open("r+") as handle:
                handle.seek(0)
                handle.write(original)
                handle.truncate()
            server_script.unlink(missing_ok=True)
            shared_file = workspace / "shared.txt"
            shared_file.unlink(missing_ok=True)
            workspace.rmdir()


if __name__ == "__main__":
    unittest.main()
