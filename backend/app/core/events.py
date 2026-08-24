"""Redis Pub/Sub events for enterprise info sync."""

import asyncio
import json

import redis.asyncio as redis

from app.config import get_settings

settings = get_settings()

_redis_client: redis.Redis | None = None
_redis_client_loop: asyncio.AbstractEventLoop | None = None


async def get_redis() -> redis.Redis:
    """Get or create the Redis client."""
    global _redis_client, _redis_client_loop
    current_loop = asyncio.get_running_loop()
    if _redis_client is None or _redis_client_loop is not current_loop:
        previous_client = _redis_client
        _redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)
        _redis_client_loop = current_loop
        if previous_client is not None:
            try:
                await previous_client.aclose()
            except RuntimeError:
                # A client created by a closed loop cannot be awaited from the
                # replacement loop. Its sockets are already owned by that loop.
                pass
    return _redis_client


async def publish_event(channel: str, data: dict) -> None:
    """Publish an event to a Redis Pub/Sub channel."""
    r = await get_redis()
    await r.publish(channel, json.dumps(data))


async def close_redis() -> None:
    """Close the Redis connection."""
    global _redis_client, _redis_client_loop
    if _redis_client:
        await _redis_client.aclose()
        _redis_client = None
        _redis_client_loop = None
