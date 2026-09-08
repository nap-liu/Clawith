"""Small Redis fast-path for a durably committed STOP."""

from __future__ import annotations

import asyncio
import json
import uuid

from loguru import logger

from app.core.events import get_redis
from app.services.active_turn_stop import stop_local_registered_turns

TURN_CONTROL_CHANNEL = "platform:turn-control"


async def publish_turn_tree_stopped(
    turn_anchors: dict[str, str],
    *,
    subagent_lease_owners: dict[str, str | None] | None = None,
    reason: str = "Durable conversation STOP received",
) -> None:
    """Wake remote owners after DB cancellation; DB remains authoritative."""

    try:
        redis = await get_redis()
        await redis.publish(
            TURN_CONTROL_CHANNEL,
            json.dumps(
                {
                    "turn_anchors": turn_anchors,
                    "subagent_lease_owners": subagent_lease_owners or {},
                    "reason": reason,
                }
            ),
        )
    except Exception as exc:
        logger.warning("[turn_control] stop publish failed: {}", exc)


async def _cancel_local(payload: dict) -> None:
    anchors = {
        str(session_id): str(anchor_id)
        for session_id, anchor_id in dict(payload.get("turn_anchors") or {}).items()
    }
    await stop_local_registered_turns(
        anchors, reason=str(payload.get("reason") or "Durable conversation STOP received"),
    )

    leases = {
        uuid.UUID(str(run_id)): (str(owner) if owner is not None else None)
        for run_id, owner in dict(
            payload.get("subagent_lease_owners") or {}
        ).items()
    }
    if leases:
        from app.services.subagent_runtime import cancel_local_subagent_tasks

        await cancel_local_subagent_tasks(
            list(leases),
            expected_lease_owners=leases,
        )


async def turn_control_subscriber_loop() -> None:
    """Block on events; reconnect backoff is infrastructure, not polling."""

    delay = 1.0
    while True:
        pubsub = None
        try:
            redis = await get_redis()
            pubsub = redis.pubsub()
            await pubsub.subscribe(TURN_CONTROL_CHANNEL)
            delay = 1.0
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    payload = json.loads(message.get("data") or "{}")
                    await _cancel_local(payload)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[turn_control] subscriber reconnecting: {}", exc)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)
        finally:
            if pubsub is not None:
                try:
                    await pubsub.aclose()
                except Exception:
                    pass
