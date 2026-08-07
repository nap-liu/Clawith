import json

import pytest

from app.services import dingtalk_stream


@pytest.mark.asyncio
async def test_native_video_uploads_file_and_thumbnail_then_uses_sample_video(tmp_path, monkeypatch):
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"video")
    thumbnail = tmp_path / "thumb.jpg"
    thumbnail.write_bytes(b"jpeg")
    uploads = []
    send_args = {}

    monkeypatch.setattr(
        dingtalk_stream,
        "_create_dingtalk_video_thumbnail",
        lambda _path: thumbnail,
    )

    async def upload(_key, _secret, path, media_type):
        uploads.append((path, media_type))
        return "@video" if media_type == "file" else "@thumbnail"

    async def send(*args, **kwargs):
        send_args["args"] = args
        send_args["kwargs"] = kwargs
        return True

    monkeypatch.setattr(dingtalk_stream, "_upload_dingtalk_media", upload)
    monkeypatch.setattr(dingtalk_stream, "_send_dingtalk_media_message", send)

    sent, code = await dingtalk_stream._send_dingtalk_native_video(
        "app", "secret", "staff", video, "1"
    )

    assert (sent, code) == (True, "MEDIA_SENT")
    assert uploads == [(str(video), "file"), (str(thumbnail), "image")]
    assert send_args["args"][3:6] == ("@video", "video", "1")
    assert send_args["kwargs"]["pic_media_id"] == "@thumbnail"
    assert not thumbnail.exists()


@pytest.mark.asyncio
async def test_sample_video_payload_never_falls_back_to_file(monkeypatch):
    posted = {}

    async def get_token(_key, _secret):
        return "token"

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"processQueryKey": "ok"}

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            posted["url"] = url
            posted["json"] = kwargs["json"]
            return Response()

    monkeypatch.setattr(dingtalk_stream.dingtalk_token_manager, "get_token", get_token)
    monkeypatch.setattr(dingtalk_stream.httpx, "AsyncClient", Client)

    sent = await dingtalk_stream._send_dingtalk_media_message(
        "app", "secret", "staff", "@video", "video", "1",
        filename="demo.mp4", pic_media_id="@thumbnail", duration_ms=1500,
    )

    assert sent is True
    assert posted["json"]["msgKey"] == "sampleVideo"
    params = json.loads(posted["json"]["msgParam"])
    assert params == {
        "duration": "1500",
        "videoMediaId": "@video",
        "videoType": "mp4",
        "picMediaId": "@thumbnail",
    }


@pytest.mark.asyncio
async def test_sample_video_requires_thumbnail(monkeypatch):
    async def get_token(_key, _secret):
        return "token"

    monkeypatch.setattr(dingtalk_stream.dingtalk_token_manager, "get_token", get_token)
    assert await dingtalk_stream._send_dingtalk_media_message(
        "app", "secret", "staff", "@video", "video", "1"
    ) is False
