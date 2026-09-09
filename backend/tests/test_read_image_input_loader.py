"""Media input compatibility: lossless data URLs, partial batches and joint payloads."""

import base64

import httpx
import pytest

from app.services.media_ai_io import MediaAIError, MediaInput, load_media, load_understanding_media
from app.services.media_ai_provider import understand_response
from test_media_ai_provider import PNG, WAV, MP4

pytestmark = pytest.mark.asyncio


async def test_base64_images_audio_video_are_lossless_and_bad_data_is_explicit():
    for mime, data in [("image/png", PNG), ("audio/wav", WAV), ("video/mp4", MP4)]:
        source = f"data:{mime};base64,{base64.b64encode(data).decode()}"
        loaded = await load_media("test-agent", [source])
        assert len(loaded) == 1 and loaded[0].data == data and loaded[0].data_url == source
    with pytest.raises(MediaAIError):
        await load_media("test-agent", ["data:image/png;base64,bad"])


async def test_partial_batch_keeps_valid_members_in_order():
    valid = "data:image/png;base64," + base64.b64encode(PNG).decode()
    media, errors = await load_understanding_media("test-agent", [valid, "data:image/png;base64,bad", valid])
    assert len(media) == 2 and all(item.data == PNG for item in media)
    assert errors == [{"index": 2, "source": "data:image/png;base64,bad", "code": "invalidMedia"}]


async def test_joint_batch_is_one_standard_provider_request(monkeypatch):
    requests = []
    original = httpx.AsyncClient

    async def respond(request):
        import json
        requests.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=(
            'data: {"choices":[{"delta":{"content":"All media compared"},"finish_reason":null}]}\n\n'
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'))

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(**{**kwargs, "transport": httpx.MockTransport(respond)}))
    media = [MediaInput("workspace/local.png", "image/png", url="https://files.example/local.png?signature=exact"),
             MediaInput("https://third.example/image", "image/png", url="https://third.example/image?token=original"),
             MediaInput("inline", "image/png", data=PNG),
             MediaInput("audio", "audio/wav", data=WAV), MediaInput("video", "video/mp4", data=MP4)]
    config = {"model_id": "test-model", "provider": "openai", "api_protocol": "openai_compatible",
              "base_url": "https://provider.example/v1", "api_key": "fixture", "model": "multimodal",
              "input_modalities": ["image", "audio", "video"]}
    response = await understand_response(config, "Compare every input", media)
    assert response.content == "All media compared" and len(requests) == 1
    content = requests[0]["messages"][0]["content"]
    assert [item["type"] for item in content] == ["text", "image_url", "image_url", "image_url", "input_audio", "video_url"]
    assert content[1]["image_url"]["url"] == media[0].url
    assert content[2]["image_url"]["url"] == media[1].url
    assert content[3]["image_url"]["url"] == media[2].data_url
