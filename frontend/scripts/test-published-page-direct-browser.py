"""Validate a real published report through the Docker frontend proxy."""

from __future__ import annotations

import os
import time
from urllib.parse import urlsplit
from urllib.request import urlopen

from chromium_browser import ChromiumSession

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


def chromium_result(url: str) -> dict:
    browser = ChromiumSession()
    try:
        with browser.page(url) as page:
            result = page.wait_result(
                "document.querySelector('.published-page-viewer-frame')"
                "?.contentDocument?.querySelector('#result')?.textContent"
            )
            result["platformWatermark"] = bool(
                page.evaluate("!!document.querySelector('[data-platform-watermark]')")
            )
            frames = page.command("Page.getFrameTree")["frameTree"].get("childFrames", [])
            report_frames = [frame["frame"] for frame in frames
                             if "__report_embed=1" in frame["frame"]["url"]]
            assert len(report_frames) == 1, frames
            context_id = page.contexts[report_frames[0]["id"]]
            # CDP targets the report's default world, so every capability below
            # is exercised inside the iframe, with the report's actual policy.
            assert page.evaluate("window !== top", context_id=context_id) is True
            result["popup"] = page.evaluate(
                "(() => { const popup = window.open('about:blank', '_blank'); "
                "if (!popup) return false; popup.close(); return true; })()",
                context_id=context_id, user_gesture=True,
            )
            result["mediaPlayback"] = page.evaluate(
                "(async () => { const context = new AudioContext(); "
                "try { await context.resume(); "
                "const oscillator = context.createOscillator(); "
                "const gain = context.createGain(); gain.gain.value = 0; "
                "oscillator.connect(gain).connect(context.destination); "
                "const start = context.currentTime; oscillator.start(); "
                "await new Promise(resolve => setTimeout(resolve, 100)); "
                "oscillator.stop(); "
                "return context.state === 'running' && context.currentTime > start; "
                "} finally { await context.close(); } })()",
                context_id=context_id, user_gesture=True, await_promise=True,
            )
            page.command("Browser.setDownloadBehavior", {
                "behavior": "allow", "downloadPath": str(browser.download_dir),
            })
            # Click the link supplied by the report fixture and inspect the bytes
            # Chromium writes, instead of trusting its DOM attributes.
            page.evaluate("document.querySelector('a[download]').click()",
                          context_id=context_id, user_gesture=True)
            downloaded = browser.download_dir / "published-report.txt"
            deadline = time.monotonic() + 5
            while not downloaded.exists() and time.monotonic() < deadline:
                time.sleep(0.1)
            result["downloadedContent"] = downloaded.read_text() if downloaded.exists() else None
            assert page.diagnostics == [], page.diagnostics
            return result
    finally:
        browser.close()


def assert_browser_result(result: dict, *, top_level: bool, expected_hash: str) -> None:
    assert result.get("fatal") is None, result
    assert result["initialHash"] == expected_hash
    assert result["topLevel"] is top_level
    assert result["origin"] == f"{urlsplit(REPORT_URL).scheme}://{urlsplit(REPORT_URL).netloc}"
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
    assert result["navigation"] is True
    assert result["unsandboxed"] is True
    assert result["popup"] is True
    assert result["mediaPlayback"] is True
    assert result["downloadedContent"] == "published-report"
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
        chromium_result(f"{REPORT_URL}{expected_hash}"),
        top_level=False,
        expected_hash=expected_hash,
    )
    print("published-page unrestricted-viewer Chromium tests passed")


if __name__ == "__main__":
    main()
