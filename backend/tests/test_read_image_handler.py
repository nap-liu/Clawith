"""Tests for tools/read_image/handler.py — orchestration."""

import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.tools.read_image import handle_read_image


@pytest.fixture
def agent_id() -> uuid.UUID:
    return uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
def jpeg_bytes() -> bytes:
    """A real, Pillow-decodable 8x8 JPEG."""
    from io import BytesIO
    from PIL import Image
    img = Image.new("RGB", (8, 8), color=(128, 128, 200))
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def _mock_llm_model(model_id: str, supports_vision: bool = True):
    m = MagicMock()
    m.id = uuid.UUID(model_id) if isinstance(model_id, str) else model_id
    m.model = "qwen3.6-plus"
    m.supports_vision = supports_vision
    m.base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    m.api_key = "sk-fake"
    return m


@pytest.mark.asyncio
async def test_config_missing_model_id_short_circuits(agent_id, tmp_path):
    config = {"model_id": None, "input_modes": {"workspace_path": {"enabled": True}}, "max_images_per_call": 6, "max_image_bytes_per_file": 5_000_000, "vision_max_output_tokens": 4096}
    with (
        patch("app.services.tools.read_image.handler._load_config", AsyncMock(return_value=(config, None))),
        patch("app.services.tools.read_image.handler._load_vision_model", AsyncMock(return_value=None)),
        patch("app.services.tools.read_image.handler._get_workspace", AsyncMock(return_value=tmp_path)),
    ):
        result = await handle_read_image(agent_id, {"image_paths": ["img.jpg"]})

    assert result.startswith("❌")
    assert "视觉模型" in result or "model" in result.lower()


@pytest.mark.asyncio
async def test_success_single_workspace_image(agent_id, tmp_path, jpeg_bytes):
    (tmp_path / "img.jpg").write_bytes(jpeg_bytes)
    from app.services.tools.read_image.input_loader import DEFAULT_CONFIG
    import copy
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["model_id"] = "00000000-0000-0000-0000-000000000010"

    async def fake_call_llm(*args, **kwargs):
        return "--- VISION MODEL OUTPUT ---\nHello world."

    with (
        patch("app.services.tools.read_image.handler._load_config", AsyncMock(return_value=(config, None))),
        patch("app.services.tools.read_image.handler._load_vision_model", AsyncMock(return_value=_mock_llm_model("00000000-0000-0000-0000-000000000010"))),
        patch("app.services.tools.read_image.handler._get_workspace", AsyncMock(return_value=tmp_path)),
        patch("app.services.llm.caller.call_llm", AsyncMock(side_effect=fake_call_llm)),
    ):
        result = await handle_read_image(agent_id, {"image_paths": ["img.jpg"]})

    assert "--- Image 1: img.jpg ---" in result
    assert "Hello world" in result


@pytest.mark.asyncio
async def test_partial_failure_inline_error_block(agent_id, tmp_path, jpeg_bytes):
    (tmp_path / "ok.jpg").write_bytes(jpeg_bytes)
    from app.services.tools.read_image.input_loader import DEFAULT_CONFIG
    import copy
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["model_id"] = "00000000-0000-0000-0000-000000000010"

    async def fake_call_llm(*args, **kwargs):
        return "Transcription of ok.jpg only."

    with (
        patch("app.services.tools.read_image.handler._load_config", AsyncMock(return_value=(config, None))),
        patch("app.services.tools.read_image.handler._load_vision_model", AsyncMock(return_value=_mock_llm_model("00000000-0000-0000-0000-000000000010"))),
        patch("app.services.tools.read_image.handler._get_workspace", AsyncMock(return_value=tmp_path)),
        patch("app.services.llm.caller.call_llm", AsyncMock(side_effect=fake_call_llm)),
    ):
        result = await handle_read_image(agent_id, {"image_paths": ["ok.jpg", "missing.jpg"]})

    assert "--- Image 1: ok.jpg ---" in result
    assert "--- Image 2: missing.jpg ---" in result
    assert "❌" in result
    assert "不存在" in result or "not" in result.lower()


@pytest.mark.asyncio
async def test_upstream_llm_failure_short_circuits(agent_id, tmp_path, jpeg_bytes):
    (tmp_path / "img.jpg").write_bytes(jpeg_bytes)
    from app.services.tools.read_image.input_loader import DEFAULT_CONFIG
    import copy
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["model_id"] = "00000000-0000-0000-0000-000000000010"

    async def blow_up(*args, **kwargs):
        raise RuntimeError("upstream 503")

    with (
        patch("app.services.tools.read_image.handler._load_config", AsyncMock(return_value=(config, None))),
        patch("app.services.tools.read_image.handler._load_vision_model", AsyncMock(return_value=_mock_llm_model("00000000-0000-0000-0000-000000000010"))),
        patch("app.services.tools.read_image.handler._get_workspace", AsyncMock(return_value=tmp_path)),
        patch("app.services.llm.caller.call_llm", AsyncMock(side_effect=blow_up)),
    ):
        result = await handle_read_image(agent_id, {"image_paths": ["img.jpg"]})

    assert result.startswith("❌")
    assert "upstream" in result.lower() or "503" in result


@pytest.mark.asyncio
async def test_multi_success_combined_block_not_duplicated(agent_id, tmp_path, jpeg_bytes):
    """With 3 successful images, the LLM response must appear ONCE under a combined
    header, not duplicated under each image block."""
    for i in range(1, 4):
        (tmp_path / f"s{i}.jpg").write_bytes(jpeg_bytes)
    from app.services.tools.read_image.input_loader import DEFAULT_CONFIG
    import copy
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["model_id"] = "00000000-0000-0000-0000-000000000010"

    unique_marker = "UNIQUE_VISION_PAYLOAD_MARKER_42"

    async def fake_call_llm(*args, **kwargs):
        return f"Transcription text including {unique_marker}."

    with (
        patch("app.services.tools.read_image.handler._load_config", AsyncMock(return_value=(config, None))),
        patch("app.services.tools.read_image.handler._load_vision_model", AsyncMock(return_value=_mock_llm_model("00000000-0000-0000-0000-000000000010"))),
        patch("app.services.tools.read_image.handler._get_workspace", AsyncMock(return_value=tmp_path)),
        patch("app.services.llm.caller.call_llm", AsyncMock(side_effect=fake_call_llm)),
    ):
        result = await handle_read_image(
            agent_id, {"image_paths": ["s1.jpg", "s2.jpg", "s3.jpg"]}
        )

    assert "--- Images 1, 2, 3:" in result
    assert "s1.jpg" in result and "s2.jpg" in result and "s3.jpg" in result
    assert result.count(unique_marker) == 1


@pytest.mark.asyncio
async def test_tightening_override_rejects_agent_loosen(agent_id, tmp_path, jpeg_bytes):
    """Agent tries max_images=10 vs tool=6; request with 8 images must short-circuit."""
    for i in range(8):
        (tmp_path / f"img{i}.jpg").write_bytes(jpeg_bytes)
    from app.services.tools.read_image.input_loader import DEFAULT_CONFIG
    import copy
    tool_cfg = copy.deepcopy(DEFAULT_CONFIG)
    tool_cfg["max_images_per_call"] = 6
    tool_cfg["model_id"] = "00000000-0000-0000-0000-000000000010"
    agent_cfg = {"max_images_per_call": 10}

    paths = [f"img{i}.jpg" for i in range(8)]

    with (
        patch("app.services.tools.read_image.handler._load_config", AsyncMock(return_value=(tool_cfg, agent_cfg))),
        patch("app.services.tools.read_image.handler._load_vision_model", AsyncMock(return_value=_mock_llm_model("00000000-0000-0000-0000-000000000010"))),
        patch("app.services.tools.read_image.handler._get_workspace", AsyncMock(return_value=tmp_path)),
    ):
        result = await handle_read_image(agent_id, {"image_paths": paths})

    assert result.startswith("❌")
    assert "6" in result  # cites the effective (tool) limit, not the agent's 10


@pytest.mark.asyncio
async def test_all_inputs_fail_no_llm_call(agent_id, tmp_path):
    """If every image fails at category-B load time, the LLM must not be
    called — we return inline errors only, no zero-image API request."""
    from app.services.tools.read_image.input_loader import DEFAULT_CONFIG
    import copy
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["model_id"] = "00000000-0000-0000-0000-000000000010"

    llm_mock = AsyncMock()  # Should never be called

    with (
        patch("app.services.tools.read_image.handler._load_config", AsyncMock(return_value=(config, None))),
        patch("app.services.tools.read_image.handler._load_vision_model", AsyncMock(return_value=_mock_llm_model("00000000-0000-0000-0000-000000000010"))),
        patch("app.services.tools.read_image.handler._get_workspace", AsyncMock(return_value=tmp_path)),
        patch("app.services.llm.caller.call_llm", llm_mock),
    ):
        # Two nonexistent files — both become category-B LoadErrors
        result = await handle_read_image(agent_id, {"image_paths": ["m1.jpg", "m2.jpg"]})

    llm_mock.assert_not_called()
    assert "--- Image 1: m1.jpg ---" in result
    assert "--- Image 2: m2.jpg ---" in result
    assert result.count("❌") >= 2  # both failures rendered inline


@pytest.mark.asyncio
async def test_recursion_defense_rejects_agent_id_none(tmp_path):
    """If the handler is ever reached with agent_id=None (which would happen
    if a vision model somehow tried to call read_image via the tool loop
    with agent_id=None inherited from handle_read_image's own call_llm
    invocation), we reject immediately. This is the defense against
    vision-model-triggered recursion."""
    result = await handle_read_image(None, {"image_paths": ["img.jpg"]})
    assert result.startswith("❌")
    assert "递归" in result or "recursion" in result.lower()
