import pytest

from app.services.sandbox.local.docker_backend import (
    _docker_daemon_uses_proxy,
    _docker_images,
    _is_domestic_registry,
)


class _DockerClient:
    def __init__(self, info: dict[str, str]):
        self._info = info

    def info(self) -> dict[str, str]:
        return self._info


def test_sandbox_images_use_domestic_registry_by_default(monkeypatch):
    monkeypatch.delenv("CLAWITH_IMAGE_MIRROR", raising=False)

    assert _docker_images() == {
        "python": "docker.m.daocloud.io/library/python:3.11-slim",
        "bash": "docker.m.daocloud.io/library/bash:5.2",
        "node": "docker.m.daocloud.io/library/node:18-slim",
    }


def test_daemon_proxy_detection_blocks_http_or_https_proxy():
    assert not _docker_daemon_uses_proxy(_DockerClient({}))
    assert _docker_daemon_uses_proxy(_DockerClient({"HttpProxy": "http://proxy.internal:3128"}))
    assert _docker_daemon_uses_proxy(_DockerClient({"HttpsProxy": "http://proxy.internal:3128"}))


@pytest.mark.parametrize(
    "registry",
    [
        "docker.m.daocloud.io",
        "enterprise-public-cn-beijing.cr.volces.com",
        "registry.cn-hangzhou.aliyuncs.com/team",
        "ccr.ccs.tencentyun.com/team",
        "swr.cn-east-3.myhuaweicloud.com/team",
    ],
)
def test_domestic_registry_allowlist(registry):
    assert _is_domestic_registry(registry)


def test_sandbox_rejects_non_domestic_registry(monkeypatch):
    monkeypatch.setenv("CLAWITH_IMAGE_MIRROR", "docker.io")

    with pytest.raises(RuntimeError, match="approved domestic registry"):
        _docker_images()
