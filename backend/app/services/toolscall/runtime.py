"""Platform-owned runtime initialization for Bash ToolCall composition."""

from __future__ import annotations

import base64
import ipaddress
import json
import secrets
from dataclasses import dataclass
from urllib.parse import urlparse

from app.core.events import get_redis
from app.database import async_session
from app.services.platform_service import platform_service

_SIGNING_KEY_REDIS_KEY = "clawith:runtime:toolscall:signing:v1"
_SCOPE_REDIS_PREFIX = "clawith:runtime:toolscall:scope:v1:"
_BRIDGE_PATH = "/api/internal/toolscall/v1"
_UTM_NETWORK = ipaddress.ip_network("192.168.64.0/24")
_UTM_HOST_GATEWAY = "192.168.64.1"
_DEFAULT_FRONTEND_PORT = 3008


class ToolscallRuntimeUnavailable(RuntimeError):
    """The platform could not initialize the internal composition runtime."""


@dataclass(frozen=True)
class ToolscallRuntime:
    signing_seed: bytes
    bridge_url: str


async def get_toolscall_signing_seed() -> bytes:
    """Create or load the cluster-wide ephemeral signing seed from Redis."""
    try:
        redis = await get_redis()
        encoded = await redis.get(_SIGNING_KEY_REDIS_KEY)
        if not encoded:
            candidate = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
            stored = await redis.set(_SIGNING_KEY_REDIS_KEY, candidate, nx=True)
            encoded = candidate if stored else await redis.get(_SIGNING_KEY_REDIS_KEY)
        raw = encoded.decode() if isinstance(encoded, bytes) else str(encoded or "")
        seed = base64.urlsafe_b64decode(raw.encode())
    except Exception as exc:
        raise ToolscallRuntimeUnavailable(
            "toolscall runtime signing material is unavailable"
        ) from exc
    if len(seed) != 32:
        raise ToolscallRuntimeUnavailable("invalid toolscall runtime signing material")
    return seed


async def register_toolscall_scope(
    *, standard_tool_names: set[str], ttl_seconds: int
) -> str:
    """Store a bounded capability scope and return its opaque reference."""
    scope_id = secrets.token_urlsafe(18)
    payload = json.dumps(sorted(standard_tool_names), separators=(",", ":"))
    try:
        redis = await get_redis()
        stored = await redis.set(
            f"{_SCOPE_REDIS_PREFIX}{scope_id}",
            payload,
            ex=max(1, int(ttl_seconds)),
            nx=True,
        )
    except Exception as exc:
        raise ToolscallRuntimeUnavailable("toolscall runtime scope is unavailable") from exc
    if not stored:
        raise ToolscallRuntimeUnavailable("could not allocate toolscall runtime scope")
    return scope_id


async def toolscall_scope_allows(*, scope_id: str, tool_name: str) -> bool:
    """Check the server-side scope referenced by a verified compact token."""
    try:
        redis = await get_redis()
        raw = await redis.get(f"{_SCOPE_REDIS_PREFIX}{scope_id}")
        values = json.loads(raw) if raw else []
    except Exception as exc:
        raise ToolscallRuntimeUnavailable("toolscall runtime scope is unavailable") from exc
    return isinstance(values, list) and tool_name in values


def bridge_url_for_endpoint(*, aio_base_url: str, platform_base_url: str) -> str:
    """Resolve the callback address from the active AIO endpoint topology."""
    aio = urlparse(aio_base_url)
    aio_host = (aio.hostname or "").lower()
    if not aio_host:
        raise ToolscallRuntimeUnavailable("AIO endpoint has no hostname")

    # Compose/service-discovery endpoints share the backend network.
    try:
        aio_ip = ipaddress.ip_address(aio_host)
    except ValueError:
        aio_ip = None
    if aio_ip is None and aio_host == "aio-sandbox":
        return f"http://backend:8000{_BRIDGE_PATH}"

    public = urlparse(platform_base_url)
    # The local UTM endpoint reaches the Mac through its fixed host gateway;
    # retain an explicitly known frontend port. The platform service's bare
    # local fallback is backend port 8000, which is not published by Compose;
    # map that fallback to the existing frontend default instead.
    if aio_ip is not None and aio_ip in _UTM_NETWORK:
        port = public.port
        if (public.hostname or "").lower() in {
            "localhost",
            "127.0.0.1",
            "::1",
        } and port in {None, 8000}:
            port = _DEFAULT_FRONTEND_PORT
        port = port or _DEFAULT_FRONTEND_PORT
        return f"http://{_UTM_HOST_GATEWAY}:{port}{_BRIDGE_PATH}"

    if public.scheme not in {"http", "https"} or not public.netloc:
        raise ToolscallRuntimeUnavailable("platform base URL is unavailable")
    if (public.hostname or "").lower() in {"localhost", "127.0.0.1", "::1"}:
        raise ToolscallRuntimeUnavailable(
            "remote AIO endpoint cannot reach the local platform address"
        )
    return f"{platform_base_url.rstrip('/')}{_BRIDGE_PATH}"


async def initialize_toolscall_runtime(*, aio_base_url: str) -> ToolscallRuntime:
    """Initialize signing and callback routing without user configuration."""
    signing_seed = await get_toolscall_signing_seed()
    async with async_session() as db:
        platform_base_url = await platform_service.get_public_base_url(db=db)
    return ToolscallRuntime(
        signing_seed=signing_seed,
        bridge_url=bridge_url_for_endpoint(
            aio_base_url=aio_base_url,
            platform_base_url=platform_base_url,
        ),
    )
