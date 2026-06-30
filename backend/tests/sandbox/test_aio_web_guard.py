"""The browser-global CDP guard blocks cross-context escape methods."""
from app.services.sandbox.remote.aio_sandbox_backend import _is_browser_global_method


def test_target_methods_are_blocked():
    assert _is_browser_global_method("Target.getTargets") is True
    assert _is_browser_global_method("Target.attachToTarget") is True
    assert _is_browser_global_method("Target.createBrowserContext") is True


def test_browser_methods_are_blocked():
    assert _is_browser_global_method("Browser.close") is True
    assert _is_browser_global_method("Browser.getVersion") is True


def test_session_scoped_methods_are_allowed():
    for m in [
        "Page.navigate",
        "DOM.querySelector",
        "Runtime.evaluate",
        "Input.dispatchMouseEvent",
        "Network.setCookie",
        "Emulation.setUserAgentOverride",
        "Fetch.enable",
    ]:
        assert _is_browser_global_method(m) is False
