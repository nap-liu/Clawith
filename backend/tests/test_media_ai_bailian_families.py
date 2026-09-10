"""Provider-facing family contracts protect paid generation submissions."""

import json

import httpx
import pytest

from app.services import media_ai_provider as provider
from app.services.media_ai_io import MediaAIError, MediaInput
from test_media_ai_provider import mock_http


def config(model, kind="video"):
    return provider.connection({"api_key": "test-key", f"{kind}_model": model})


def picture(role=None, name="reference"):
    return MediaInput(source=name, mime_type="image/png", url=f"https://images.example/{name}.png", role=role)


@pytest.mark.asyncio
@pytest.mark.parametrize("model,kind,args,media,expected", [
    ("kling/kling-v3-omni-image-generation", "image", {"ratio": "9:16", "parameters": {"seed": 7}}, [picture()],
     {"parameters": {"n": 1, "resolution": "1k", "aspect_ratio": "9:16", "seed": 7}}),
    ("vidu/viduq3-fast_reference2image", "image", {"size": "1024*1024"}, [picture(), picture(name="second")],
     {"parameters": {"n": 1, "size": "1024*1024"}}),
    ("kling/kling-v3-omni-video-generation", "video", {"resolution": "720P", "ratio": "9:16", "parameters": {"negative_prompt": "blur", "multi_shot": True}}, [picture("reference_image")],
     {"parameters": {"duration": 5, "mode": "std", "aspect_ratio": "9:16"}, "roles": ["refer"], "input": {"negative_prompt": "blur", "multi_shot": True}}),
    ("pixverse/pixverse-c1-t2v", "video", {"resolution": "360P", "duration": 1, "ratio": "9:16"}, [],
     {"parameters": {"duration": 1, "size": "360*640"}}),
    ("pixverse/pixverse-v6-r2v-omni", "video", {"parameters": {"resolution": "360P", "aspect_ratio": "9:16"}}, [picture()],
     {"parameters": {"resolution": "360P", "aspect_ratio": "9:16", "duration": 5}, "roles": ["image_url"]}),
    ("vidu/viduq3-turbo_start-end2video", "video", {"resolution": "540P", "duration": 1}, [picture("last_frame", "last"), picture("first_frame", "first")],
     {"parameters": {"resolution": "540P", "duration": 1}, "roles": ["image", "image"], "urls": ["https://images.example/first.png", "https://images.example/last.png"]}),
    ("happyhorse-1.1-r2v", "video", {"resolution": "480P", "duration": 3}, [picture()],
     {"parameters": {"resolution": "480P", "duration": 3, "ratio": "16:9"}, "roles": ["reference_image"]}),
    ("MiniMax/MiniMax-H3", "video", {"duration": 4}, [picture("reference_image")],
     {"parameters": {"resolution": "768P", "duration": 4, "ratio": "adaptive"}, "roles": ["image_url"]}),
])
async def test_family_submission_preserves_fields_and_reference_semantics(monkeypatch, model, kind, args, media, expected):
    def upstream(request):
        body = json.loads(request.content)
        assert body["model"] == model
        assert body["parameters"] == expected["parameters"]
        if "roles" in expected:
            assert [item["type"] for item in body["input"]["media"]] == expected["roles"]
        if "urls" in expected:
            assert [item["url"] for item in body["input"]["media"]] == expected["urls"]
        for key, value in expected.get("input", {}).items():
            assert body["input"][key] == value
        if kind == "image":
            assert request.url.path.endswith("/image-generation/generation")
            assert request.headers["x-dashscope-async"] == "enable"
            assert len(body["input"]["messages"][0]["content"]) == len(media) + 1
        return httpx.Response(200, json={"output": {"task_id": "accepted", "task_status": "PENDING"}})

    mock_http(monkeypatch, upstream)
    cfg = config(model, kind)
    path, payload = provider.generation_payload(cfg, {"output_type": kind, "prompt": "scene", **args}, media)
    assert (await provider.request(cfg, path, payload, asynchronous=kind == "video"))["output"]["task_id"] == "accepted"


@pytest.mark.parametrize("options", [{"result_type": "series"}, {"enable_sequential": True}, {"enable_interleave": True}])
def test_multi_artifact_modes_rejected_before_paid_submission(options):
    with pytest.raises(MediaAIError, match="unsupportedOptions"):
        provider.generation_payload(config("kling/kling-v3-omni-image-generation", "image"),
                                    {"output_type": "image", "prompt": "scene", "parameters": options}, [])


@pytest.mark.asyncio
async def test_edit_preserves_source_duration_and_async_image_can_be_polled(monkeypatch):
    def upstream(request):
        if request.method == "POST":
            body = json.loads(request.content)
            assert body["parameters"] == {"resolution": "720P"}
            assert body["input"]["media"] == [{"type": "video", "url": "https://images.example/source.mp4"}]
            return httpx.Response(200, json={"output": {"task_id": "accepted"}})
        assert request.url.path == "/api/v1/tasks/accepted"
        return httpx.Response(200, json={"output": {"task_id": "accepted", "task_status": "SUCCEEDED", "choices": [
            {"message": {"content": [{"image": "https://images.example/generated.png"}]}}
        ]}})

    mock_http(monkeypatch, upstream)
    cfg = config("happyhorse-1.0-video-edit")
    media = [MediaInput(source="source", mime_type="video/mp4", url="https://images.example/source.mp4", role="reference_video")]
    path, payload = provider.generation_payload(cfg, {"output_type": "video", "prompt": "warm light"}, media)
    await provider.request(cfg, path, payload, asynchronous=True)
    image_cfg = config("vidu/viduq3-fast_reference2image", "image")
    result = await provider.poll_generation(image_cfg, "accepted")
    assert provider.result_url(result, "image") == "https://images.example/generated.png"
