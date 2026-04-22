"""Tests for the data:image/*;base64,… redaction extension to sanitize_tool_args."""

from app.utils.sanitize import sanitize_tool_args


# ─── The new behavior ──────────────────────────────────────────────────────────

def test_string_value_containing_only_base64_image_is_redacted():
    args = {"image_paths": "data:image/jpeg;base64," + "A" * 2000}
    out = sanitize_tool_args(args)
    assert out["image_paths"].startswith("[base64 image")
    assert "data:image/" not in out["image_paths"]


def test_list_of_base64_images_each_item_redacted():
    payload_a = "data:image/png;base64," + "B" * 1000
    payload_b = "data:image/webp;base64," + "C" * 800
    args = {"image_paths": [payload_a, payload_b, "workspace/slide.jpg"]}
    out = sanitize_tool_args(args)
    assert out["image_paths"][0].startswith("[base64 image")
    assert out["image_paths"][1].startswith("[base64 image")
    # Non-base64 items pass through untouched
    assert out["image_paths"][2] == "workspace/slide.jpg"


def test_supported_mime_subtypes_are_redacted():
    for mime in ("jpeg", "png", "webp", "gif"):
        payload = f"data:image/{mime};base64," + "X" * 500
        out = sanitize_tool_args({"x": payload})
        assert out["x"].startswith("[base64 image"), f"failed for mime={mime}"


def test_size_kb_is_reported_in_redaction_placeholder():
    # base64 expands to ~4/3 of raw. 3000 base64 chars → ~2.25 KB decoded.
    payload = "data:image/jpeg;base64," + "A" * 3000
    out = sanitize_tool_args({"x": payload})
    # Placeholder form: "[base64 image, <N> KB]"
    assert "KB" in out["x"]
    # Sanity: the reported size is > 0
    import re
    m = re.search(r"\[base64 image, (\d+) KB\]", out["x"])
    assert m is not None, f"unexpected placeholder format: {out['x']}"
    assert int(m.group(1)) >= 1


# ─── Out of scope — do NOT redact ──────────────────────────────────────────────

def test_non_image_data_uris_are_not_redacted_by_this_util():
    # data:text/plain is not our concern; leave it as-is for other callers.
    args = {"x": "data:text/plain,hello"}
    out = sanitize_tool_args(args)
    assert out["x"] == "data:text/plain,hello"


def test_non_supported_image_mime_is_not_redacted():
    # data:image/bmp or data:application/pdf — not in our whitelist
    args = {"x": "data:image/bmp;base64," + "A" * 500}
    out = sanitize_tool_args(args)
    assert out["x"].startswith("data:image/bmp")


# ─── Regression lock — pre-existing behaviors still work ────────────────────────

def test_existing_sensitive_field_names_still_redacted():
    out = sanitize_tool_args({"password": "hunter2", "api_key": "sk-abc"})
    assert out["password"] == "******"
    assert out["api_key"] == "******"


def test_connection_uri_still_redacted():
    out = sanitize_tool_args({"url": "mysql://user:secret@host:3306/db"})
    assert out["url"] == "******"


def test_secrets_md_path_still_redacts_content():
    out = sanitize_tool_args({"path": "secrets.md", "content": "KEY=abc"})
    assert out["content"] == "******"


def test_empty_and_none_inputs_unchanged():
    assert sanitize_tool_args(None) is None
    assert sanitize_tool_args({}) == {}
