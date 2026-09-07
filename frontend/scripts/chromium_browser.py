"""Small CDP harness shared by the Docker-only published-page browser checks."""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.request import Request, urlopen

from websockets.sync.client import connect


class ChromiumPage:
    def __init__(self, websocket) -> None:
        self.websocket = websocket
        self.request_id = 0
        self.contexts: dict[str, int] = {}
        self.diagnostics: list[str] = []

    def command(self, method: str, params: dict | None = None) -> dict:
        self.request_id += 1
        self.websocket.send(json.dumps({
            "id": self.request_id, "method": method, "params": params or {},
        }))
        while True:
            message = json.loads(self.websocket.recv(timeout=20))
            event = message.get("method")
            data = message.get("params", {})
            if event == "Runtime.executionContextCreated":
                context = data["context"]
                auxiliary = context.get("auxData", {})
                if auxiliary.get("isDefault"):
                    self.contexts[auxiliary["frameId"]] = context["id"]
            elif event == "Runtime.executionContextsCleared":
                self.contexts.clear()
            elif event == "Runtime.exceptionThrown":
                self.diagnostics.append(f"runtime: {data.get('exceptionDetails')}")
            elif event == "Runtime.consoleAPICalled" and data.get("type") == "error":
                self.diagnostics.append(f"console: {data.get('args')}")
            elif event == "Log.entryAdded" and data.get("entry", {}).get("level") == "error":
                self.diagnostics.append(f"log: {data['entry'].get('text')}")
            if message.get("id") == self.request_id:
                assert "error" not in message, message
                return message.get("result", {})

    def evaluate(self, expression: str, *, context_id: int | None = None,
                 user_gesture: bool = False, await_promise: bool = False) -> object:
        params = {
            "expression": expression, "returnByValue": True,
            "userGesture": user_gesture, "awaitPromise": await_promise,
        }
        if context_id is not None:
            params["contextId"] = context_id
        result = self.command("Runtime.evaluate", params)
        assert not result.get("exceptionDetails"), result
        return result["result"].get("value")

    def wait_result(self, expression: str, *, context_id: int | None = None) -> dict:
        deadline = time.monotonic() + 20
        raw = None
        while time.monotonic() < deadline:
            raw = self.evaluate(expression, context_id=context_id)
            if raw and raw != "waiting":
                return json.loads(str(raw))
            time.sleep(0.1)
        raise AssertionError(f"browser result timed out: {raw!r}; {self.diagnostics}")


class ChromiumSession:
    def __init__(self) -> None:
        self.profile = tempfile.TemporaryDirectory(prefix="published-browser-")
        self.download_dir = Path(self.profile.name) / "downloads"
        self.download_dir.mkdir()
        self.process = subprocess.Popen([
            "chromium", "--headless", "--no-sandbox", "--disable-dev-shm-usage",
            "--disable-gpu", "--disable-background-networking", "--no-first-run",
            "--no-default-browser-check", "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=0", f"--user-data-dir={self.profile.name}",
            "about:blank",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            endpoint = Path(self.profile.name) / "DevToolsActivePort"
            deadline = time.monotonic() + 15
            while not endpoint.exists():
                if self.process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("Chromium DevTools endpoint did not start")
                time.sleep(0.1)
            self.endpoint = f"http://127.0.0.1:{endpoint.read_text().splitlines()[0]}"
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.profile.cleanup()

    @contextmanager
    def page(self, url: str):
        request = Request(f"{self.endpoint}/json/new?about%3Ablank", method="PUT")
        with urlopen(request, timeout=5) as response:
            target = json.load(response)
        try:
            with connect(target["webSocketDebuggerUrl"], max_size=None) as websocket:
                page = ChromiumPage(websocket)
                page.command("Runtime.enable")
                page.command("Log.enable")
                page.command("Page.enable")
                page.command("Page.navigate", {"url": url})
                yield page
        finally:
            request = Request(f"{self.endpoint}/json/close/{target['id']}", method="PUT")
            with urlopen(request, timeout=5):
                pass

    def report_result(self, url: str) -> dict:
        with self.page(url) as page:
            return page.wait_result(
                "document.querySelector('#result')?.textContent"
            )
