from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import pytest

from app.services.storage_runtime.base import StorageVersion, WriteCondition
from app.services.storage_runtime.s3 import S3StorageBackend


def test_s3_backend_passes_max_pool_connections(monkeypatch):
    config_instances: list[object] = []
    client_calls: list[dict] = []

    class FakeConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            config_instances.append(self)

    fake_boto3 = Mock()
    fake_boto3.client.side_effect = lambda *args, **kwargs: client_calls.append(kwargs) or object()

    import builtins

    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "boto3":
            return fake_boto3
        if name == "botocore.config":
            return type("FakeBotocoreConfigModule", (), {"Config": FakeConfig})()
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    backend = S3StorageBackend(
        bucket="bucket",
        endpoint_url="http://minio:9000",
        access_key_id="key",
        secret_access_key="secret",
        max_pool_connections=64,
    )

    backend._client_or_raise()

    assert len(config_instances) == 1
    assert config_instances[0].kwargs["max_pool_connections"] == 64
    assert len(client_calls) == 1
    assert client_calls[0]["config"] is config_instances[0]


@pytest.mark.asyncio
async def test_s3_require_absent_uses_atomic_put_precondition(monkeypatch):
    put_calls: list[dict] = []

    class FakeClient:
        async def put_object(self, **kwargs):
            put_calls.append(kwargs)

    @asynccontextmanager
    async def fake_async_client():
        yield FakeClient()

    backend = S3StorageBackend(bucket="bucket", prefix="agents")
    monkeypatch.setattr(backend, "_async_client", fake_async_client)
    monkeypatch.setattr(
        backend,
        "get_version",
        AsyncMock(return_value=StorageVersion(key="a/memory.md", exists=True, is_dir=False)),
    )

    result = await backend.write_bytes_if_match(
        "a/memory.md",
        b"memory",
        condition=WriteCondition(require_absent=True),
        content_type="text/markdown",
    )

    assert result.ok is True
    assert put_calls == [{
        "Bucket": "bucket",
        "Key": "agents/a/memory.md",
        "Body": b"memory",
        "ContentType": "text/markdown",
        "IfNoneMatch": "*",
    }]


@pytest.mark.asyncio
async def test_s3_read_text_lines_streams_and_closes_body(monkeypatch):
    class FakeBody:
        def __init__(self):
            self.closed = False

        def iter_lines(self):
            yield from [b"zero", b"one", "二".encode(), b"three"]

        def close(self):
            self.closed = True

    body = FakeBody()
    client = Mock()
    client.get_object.return_value = {"Body": body}
    backend = S3StorageBackend(bucket="bucket", prefix="agents")
    monkeypatch.setattr(backend, "_client_or_raise", lambda: client)

    result = await backend.read_text_lines(
        "a/report.txt",
        offset=1,
        limit=2,
    )

    assert result.lines == ["one", "二"]
    assert result.total_lines == 4
    assert body.closed is True
    client.get_object.assert_called_once_with(
        Bucket="bucket",
        Key="agents/a/report.txt",
    )
