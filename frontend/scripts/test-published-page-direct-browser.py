"""Validate a real published report through the Docker frontend proxy."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.request import Request, urlopen

from websockets.sync.client import connect

REPORT_URL = os.environ.get(
    "PUBLISHED_PAGE_TEST_URL",
    "http://clawith-published-page-direct-render-frontend:3000/p/browserdirect",
)


def wait_for_http() -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            with urlopen(REPORT_URL, timeout=2) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.2)
    raise AssertionError(f"published page did not become ready: {REPORT_URL}")


def assert_http_contract() -> None:
    with urlopen(REPORT_URL, timeout=5) as response:
        viewer_body = response.read()
        viewer_headers = {name.lower(): value for name, value in response.headers.items()}
    assert b'<div id="root">' in viewer_body
    assert viewer_headers["content-type"].startswith("text/html")
    assert "no-store" in viewer_headers["cache-control"]

    separator = "&" if "?" in REPORT_URL else "?"
    with urlopen(f"{REPORT_URL}{separator}__report_embed=1", timeout=5) as response:
        report_body = response.read()
        report_headers = {name.lower(): value for name, value in response.headers.items()}
    assert b'<script type="module">' in report_body
    assert report_headers["content-type"] == "text/html"
    assert report_headers["cache-control"] == "no-store"
    for name in (
        "content-security-policy",
        "x-frame-options",
        "cross-origin-opener-policy",
        "cross-origin-embedder-policy",
        "cross-origin-resource-policy",
        "permissions-policy",
        "x-content-type-options",
    ):
        assert name not in report_headers, (name, report_headers)


def chromium_result(url: str, *, test_popup: bool = False) -> dict:
    profile = tempfile.TemporaryDirectory(prefix="clawith-direct-browser-")
    download_dir = Path(profile.name) / "downloads"
    download_dir.mkdir()
    process = subprocess.Popen(
        [
            "chromium",
            "--headless",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-background-networking",
            "--no-first-run",
            "--no-default-browser-check",
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=9222",
            f"--user-data-dir={profile.name}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                with urlopen("http://127.0.0.1:9222/json/version", timeout=1):
                    break
            except OSError:
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("Chromium DevTools endpoint did not start")
                time.sleep(0.1)
        request = Request("http://127.0.0.1:9222/json/new?about%3Ablank", method="PUT")
        with urlopen(request, timeout=5) as response:
            target = json.load(response)
        with connect(target["webSocketDebuggerUrl"], max_size=None) as websocket:
            request_id = 0
            diagnostics: list[str] = []

            def record_diagnostic(message: dict) -> None:
                method = message.get("method")
                params = message.get("params", {})
                if method == "Runtime.exceptionThrown":
                    diagnostics.append(f"runtime: {params.get('exceptionDetails')}")
                elif (
                    method == "Runtime.consoleAPICalled"
                    and params.get("type") == "error"
                ):
                    diagnostics.append(f"console: {params.get('args')}")
                elif (
                    method == "Log.entryAdded"
                    and params.get("entry", {}).get("level") == "error"
                ):
                    diagnostics.append(f"log: {params['entry'].get('text')}")

            def command(method: str, params: dict | None = None) -> dict:
                nonlocal request_id
                request_id += 1
                expected_id = request_id
                websocket.send(
                    json.dumps(
                        {
                            "id": expected_id,
                            "method": method,
                            "params": params or {},
                        }
                    )
                )
                while True:
                    message = json.loads(websocket.recv())
                    record_diagnostic(message)
                    if message.get("id") == expected_id:
                        if "error" in message:
                            raise AssertionError(message["error"])
                        return message.get("result", {})

            def evaluate(
                expression: str,
                *,
                user_gesture: bool = False,
                await_promise: bool = False,
            ) -> object:
                result = command(
                    "Runtime.evaluate",
                    {
                        "expression": expression,
                        "returnByValue": True,
                        "userGesture": user_gesture,
                        "awaitPromise": await_promise,
                    },
                )
                if result.get("exceptionDetails"):
                    raise AssertionError(result["exceptionDetails"])
                return result["result"].get("value")

            command("Runtime.enable")
            command("Log.enable")
            command("Page.enable")
            command("Page.navigate", {"url": url})

            deadline = time.monotonic() + 20
            raw_result = None
            while time.monotonic() < deadline:
                raw_result = evaluate(
                    "(() => { const frame = document.querySelector('.published-page-viewer-frame'); "
                    "return frame && frame.contentDocument && frame.contentDocument.querySelector('#result') "
                    "&& frame.contentDocument.querySelector('#result').textContent; })()"
                )
                if raw_result and raw_result != "waiting":
                    result = json.loads(str(raw_result))
                    result["platformWatermark"] = bool(
                        evaluate("!!document.querySelector('[data-platform-watermark]')")
                    )
                    if test_popup:
                        result["popup"] = bool(
                            evaluate(
                                "(() => { const popup = window.open('about:blank', '_blank'); "
                                "if (!popup) return false; popup.close(); return true; })()",
                                user_gesture=True,
                            )
                        )
                        result["mediaPlayback"] = bool(
                            evaluate(
                                "(async () => { const AudioContextClass = window.AudioContext || "
                                "window.webkitAudioContext; if (!AudioContextClass) return false; "
                                "const context = new AudioContextClass(); await context.resume(); "
                                "const oscillator = context.createOscillator(); const gain = context.createGain(); "
                                "gain.gain.value = 0; oscillator.connect(gain).connect(context.destination); "
                                "oscillator.start(); oscillator.stop(context.currentTime + 0.01); "
                                "await new Promise(resolve => setTimeout(resolve, 20)); "
                                "await context.close(); return true; })()",
                                user_gesture=True,
                                await_promise=True,
                            )
                        )
                        command(
                            "Browser.setDownloadBehavior",
                            {"behavior": "allow", "downloadPath": str(download_dir)},
                        )
                        result["downloadTriggered"] = bool(
                            evaluate(
                                "(() => { const link = document.createElement('a'); "
                                "link.href = 'data:text/plain,published-browser-download'; "
                                "link.download = 'published-browser-download.txt'; "
                                "document.body.appendChild(link); link.click(); link.remove(); return true; })()",
                                user_gesture=True,
                            )
                        )
                        download_deadline = time.monotonic() + 5
                        while time.monotonic() < download_deadline:
                            if (
                                download_dir / "published-browser-download.txt"
                            ).exists():
                                break
                            time.sleep(0.1)
                        result["downloaded"] = (
                            download_dir / "published-browser-download.txt"
                        ).exists()
                    assert diagnostics == [], diagnostics
                    return result
                time.sleep(0.1)
            raise AssertionError(f"browser result timed out: {raw_result!r}")
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        profile.cleanup()


def assert_browser_result(result: dict, *, top_level: bool, expected_hash: str) -> None:
    assert result.get("fatal") is None, result
    assert result["initialHash"] == expected_hash
    assert result["topLevel"] is top_level
    assert result["origin"] == REPORT_URL.removesuffix("/p/browserdirect")
    assert result["localStorage"] == "available"
    if top_level:
        assert result["cookie"] is True
    else:
        assert isinstance(result["cookie"], bool)
    assert result["sdkAsset"] is True
    assert result["cssAsset"] is True
    assert result["dynamicImport"] == 7
    assert result["workerValue"] == 11
    assert result["font"] is True
    assert result["form"] is True
    assert result["download"] is True
    assert result["media"] is True
    assert result["navigation"] is True
    assert result["unsandboxed"] is True
    if top_level:
        assert result["popup"] is True
        assert result["mediaPlayback"] is True
        assert result["downloadTriggered"] is True
        assert result["downloaded"] is True
    assert result["health"] is True
    assert result["watermark"] is True
    assert result["platformWatermark"] is True
    assert result["computedColor"] == "rgb(1, 2, 3)"
    assert result["errors"] == []


def main() -> None:
    wait_for_http()
    assert_http_contract()
    expected_hash = "#viewer-hash"
    assert_browser_result(
        chromium_result(f"{REPORT_URL}{expected_hash}", test_popup=True),
        top_level=False,
        expected_hash=expected_hash,
    )
    print("published-page unrestricted-viewer Chromium tests passed")


if __name__ == "__main__":
    main()
