"""Local docker-based sandbox backend."""

import os
import time

from loguru import logger

from app.services.sandbox.base import BaseSandboxBackend, ExecutionResult, SandboxCapabilities
from app.services.sandbox.config import SandboxConfig

# Lazy import docker to make it optional
_docker = None


def _get_docker():
    """Lazy load docker SDK."""
    global _docker
    if _docker is None:
        try:
            import docker

            _docker = docker
        except ImportError:
            raise ImportError("docker package is required for docker backend. Install it with: pip install docker")
    return _docker


_DOCKER_IMAGE_NAMES = {
    "python": "python:3.11-slim",
    "bash": "bash:5.2",
    "node": "node:18-slim",
}

_DOMESTIC_REGISTRY_PATTERNS = (
    "docker.m.daocloud.io",
    "enterprise-public-cn-beijing.cr.volces.com",
    "registry.cn-",
    "ccr.ccs.tencentyun.com",
    "swr.cn-",
)


def _is_domestic_registry(registry: str) -> bool:
    hostname = registry.split("/", 1)[0].lower()
    return hostname in _DOMESTIC_REGISTRY_PATTERNS[:2] or any(
        hostname.startswith(prefix) for prefix in _DOMESTIC_REGISTRY_PATTERNS[2:]
    )


def _docker_images() -> dict[str, str]:
    """Resolve sandbox images through the configured domestic registry."""
    registry = os.getenv("CLAWITH_IMAGE_MIRROR", "docker.m.daocloud.io").strip().rstrip("/")
    if not _is_domestic_registry(registry):
        raise RuntimeError("CLAWITH_IMAGE_MIRROR must reference an approved domestic registry")
    return {language: f"{registry}/library/{image}" for language, image in _DOCKER_IMAGE_NAMES.items()}


def _docker_daemon_uses_proxy(client) -> bool:
    """Return whether image pulls would traverse a daemon HTTP/HTTPS proxy."""
    info = client.info()
    return bool(info.get("HttpProxy") or info.get("HttpsProxy"))


# Docker run command mapping
_DOCKER_COMMANDS = {
    "python": ["python3", "-c"],
    "bash": ["bash", "-c"],
    "node": ["node", "-e"],
}


class DockerBackend(BaseSandboxBackend):
    """Docker-based sandbox backend.

    This backend executes code inside Docker containers for better isolation.
    It requires the docker SDK to be installed and docker daemon to be running.
    """

    name = "docker"

    def __init__(self, config: SandboxConfig):
        self.config = config
        self._client = None

    @property
    def client(self):
        """Lazy load docker client."""
        if self._client is None:
            docker_lib = _get_docker()
            self._client = docker_lib.from_env()
        return self._client

    def get_capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            supported_languages=["python", "bash", "node"],
            max_timeout=self.config.max_timeout,
            max_memory_mb=256,
            network_available=self.config.allow_network,
            filesystem_available=True,
        )

    async def health_check(self) -> bool:
        """Check if docker is available and running."""
        try:
            self.client.ping()
            return True
        except Exception:
            return False

    async def execute(
        self, code: str, language: str, timeout: int = 30, work_dir: str | None = None, **kwargs
    ) -> ExecutionResult:
        """Execute code inside a docker container."""
        start_time = time.time()

        # Validate language
        images = _docker_images()
        if language not in images:
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="",
                exit_code=1,
                duration_ms=int((time.time() - start_time) * 1000),
                error=f"Unsupported language: {language}. Use: {', '.join(images)}",
            )

        # Get image and command
        image = images[language]

        # Prepare environment
        env = {
            "HOME": "/root",
            "PYTHONDONTWRITEBYTECODE": "1",
        }

        # Build docker run command
        if language == "python":
            cmd = ["python3", "-c", code]
        elif language == "bash":
            cmd = ["bash", "-c", code]
        elif language == "node":
            cmd = ["node", "-e", code]
        else:
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="",
                exit_code=1,
                duration_ms=int((time.time() - start_time) * 1000),
                error=f"Unsupported language: {language}",
            )

        # Resource limits
        cpu_limit = self.config.cpu_limit
        memory_limit = self.config.memory_limit

        # Network config
        network = None if not self.config.allow_network else "bridge"

        try:
            # Pull image if needed
            try:
                self.client.images.get(image)
            except Exception:
                if _docker_daemon_uses_proxy(self.client):
                    return ExecutionResult(
                        success=False,
                        stdout="",
                        stderr="",
                        exit_code=1,
                        duration_ms=int((time.time() - start_time) * 1000),
                        error=(
                            "Docker daemon proxy is enabled; image pulls are disabled. "
                            "Disable the daemon HTTP/HTTPS proxy and retry."
                        ),
                    )
                self.client.images.pull(image)

            # Run container
            container = self.client.containers.run(
                image,
                cmd,
                detach=False,
                mem_limit=memory_limit,
                cpu_period=100000,  # Docker default
                cpu_quota=int(float(cpu_limit) * 100000),
                network_mode=network,
                environment=env,
                remove=True,
                stdout=True,
                stderr=True,
            )

            # Wait for container with timeout
            result = container.wait(timeout=timeout)

            # Get output
            stdout = container.logs(stdout=True, stderr=False).decode("utf-8", errors="replace")[:10000]
            stderr = container.logs(stdout=False, stderr=True).decode("utf-8", errors="replace")[:5000]

            duration_ms = int((time.time() - start_time) * 1000)
            exit_code = result.get("StatusCode", 1)

            return ExecutionResult(
                success=exit_code == 0,
                stdout=stdout,
                stderr=stderr,
                exit_code=exit_code,
                duration_ms=duration_ms,
                error=None if exit_code == 0 else f"Exit code: {exit_code}",
            )

        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            error_msg = str(e)
            logger.exception("[Docker] Execution error")

            # Handle timeout specifically
            if "timeout" in error_msg.lower():
                return ExecutionResult(
                    success=False,
                    stdout="",
                    stderr="",
                    exit_code=124,
                    duration_ms=duration_ms,
                    error=f"Code execution timed out after {timeout}s",
                )

            return ExecutionResult(
                success=False,
                stdout="",
                stderr="",
                exit_code=1,
                duration_ms=duration_ms,
                error=f"Docker execution error: {error_msg[:200]}",
            )
