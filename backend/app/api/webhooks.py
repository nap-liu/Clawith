"""Webhook receiver endpoint for external trigger integration.

Provides a public POST endpoint that external services (GitHub, Grafana, etc.)
can send events to, which triggers the corresponding agent.
"""

import hashlib
import hmac
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from loguru import logger
from sqlalchemy import select

from app.core.events import get_redis
from app.database import async_session
from app.models.trigger import AgentTrigger
from app.services.webhook_inbox import (
    WebhookPayloadTooLarge,
    allocate_webhook_event_id,
    persist_webhook_payload,
    stage_webhook_payload,
)

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])

RATE_LIMIT = 5       # max hits per minute per token
MAX_PAYLOAD_SIZE = 500 * 1024 * 1024  # match the frontend nginx 500MB ceiling


async def _record_and_count_hits(token: str) -> int:
    """Record the current hit in Redis and return the rolling 60-second count."""
    redis = await get_redis()
    now = time.time()
    key = f"webhook:rate:{token}"
    member = f"{now}:{hashlib.sha1(f'{token}:{now}'.encode()).hexdigest()[:8]}"
    async with redis.pipeline(transaction=True) as pipe:
        pipe.zremrangebyscore(key, 0, now - 60)
        pipe.zadd(key, {member: now})
        pipe.zcard(key)
        pipe.expire(key, 120)
        _, _, count, _ = await pipe.execute()
    return int(count)


@router.post("/t/{token}")
async def receive_webhook(token: str, request: Request):
    """Receive a webhook POST from an external service.

    Public endpoint — no authentication required.
    Security is provided by:
    - Unique, unguessable URL token
    - Optional HMAC signature verification
    - Rate limiting (5 requests/minute per token)
    - Payload size limit (500MB, streamed without content truncation)
    """
    # Rate limiting — use per-agent limit if available
    hit_count = await _record_and_count_hits(token)

    # We'll check per-agent rate limit after finding the trigger below.
    # For now, apply a generous global ceiling to prevent memory abuse.
    if hit_count >= 60:  # hard ceiling: 60/min regardless of config
        logger.warning(f"Webhook hard rate limit exceeded for token {token[:8]}...")
        return JSONResponse({"ok": True}, status_code=429)

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_PAYLOAD_SIZE:
                logger.warning(
                    f"Webhook payload too large for token {token[:8]}...: "
                    f"{content_length} bytes"
                )
                return JSONResponse({"ok": True}, status_code=413)
        except ValueError:
            pass

    # Resolve the target before consuming a potentially large request body.
    async with async_session() as db:
        result = await db.execute(
            select(AgentTrigger).where(
                AgentTrigger.type == "webhook",
                AgentTrigger.is_enabled.is_(True),
            )
        )
        triggers = result.scalars().all()

        # Find the trigger matching this token
        target = None
        for trigger in triggers:
            cfg = trigger.config or {}
            if cfg.get("token") == token:
                target = trigger
                break

        if not target:
            # Return 200 OK to avoid leaking whether the token exists
            return JSONResponse({"ok": True})

        # Per-agent rate limit check
        from app.models.agent import Agent
        agent_result = await db.execute(select(Agent).where(Agent.id == target.agent_id))
        agent_obj = agent_result.scalar_one_or_none()
        mode = (target.config or {}).get("webhook_mode", "legacy")
        if mode == "legacy":
            agent_rate_limit = (agent_obj.webhook_rate_limit if agent_obj else None) or RATE_LIMIT
            # Re-check hits against agent-specific limit. hit_count is the rolling
            # 60s count from Redis (current hit already counted).
            if hit_count > agent_rate_limit:  # > because current hit is already counted
                logger.warning(f"Webhook per-agent rate limit ({agent_rate_limit}/min) for token {token[:8]}...")
                # Log audit entry so user can see dropped webhooks
                try:
                    from app.models.audit import AuditLog
                    db.add(AuditLog(
                        agent_id=target.agent_id,
                        action="webhook_rate_limited",
                        details={
                            "trigger_name": target.name,
                            "limit": agent_rate_limit,
                            "token_prefix": token[:8],
                        },
                    ))
                    await db.commit()
                except Exception:
                    pass
                return JSONResponse({"ok": True}, status_code=429)

        target_id = target.id
        target_agent_id = target.agent_id
        target_name = target.name
        secret = str((target.config or {}).get("secret") or "") or None
        queue_max = (agent_obj.webhook_queue_max if agent_obj else None) or 1000

    try:
        staged = await stage_webhook_payload(
            request.stream(),
            max_bytes=MAX_PAYLOAD_SIZE,
            secret=secret,
        )
    except WebhookPayloadTooLarge as exc:
        logger.warning(
            f"Webhook payload too large for token {token[:8]}...: "
            f"{exc.size} bytes"
        )
        return JSONResponse({"ok": True}, status_code=413)

    try:
        if secret:
            signature = request.headers.get("x-hub-signature-256", "")
            if not staged.signature or not hmac.compare_digest(signature, staged.signature):
                logger.warning(f"Webhook signature mismatch for trigger {target_name}")
                return JSONResponse({"ok": True})

        async with async_session() as db:
            event_id = await allocate_webhook_event_id(db)

        event_ref = await persist_webhook_payload(
            staged,
            agent_id=target_agent_id,
            trigger_id=target_id,
            event_id=event_id,
            content_type=request.headers.get("content-type", "application/octet-stream"),
        )

        # Join webhook append to the same row-lock domain used by daemon claim
        # and completion advance. The JSONB queue stores only immutable file
        # references, never the request body itself.
        async with async_session() as db:
            locked_result = await db.execute(
                select(AgentTrigger)
                .where(AgentTrigger.id == target_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            target = locked_result.scalar_one_or_none()
            cfg = dict(target.config or {}) if target else {}
            if (
                not target
                or not target.is_enabled
                or cfg.get("token") != token
                or (str(cfg.get("secret") or "") or None) != secret
            ):
                return JSONResponse({"ok": True})

            mode = cfg.get("webhook_mode", "legacy")
            if mode == "legacy":
                new_config = {
                    **cfg,
                    "_webhook_pending": True,
                    "_webhook_event": event_ref,
                    "_webhook_payload": None,
                }
            else:  # queue / merge
                queue = list(cfg.get("_webhook_queue") or [])
                if len(queue) >= queue_max:
                    logger.warning(f"Webhook queue full ({queue_max}) for trigger {target.name}")
                    return JSONResponse(
                        {"ok": False, "error": "queue full"},
                        status_code=503,
                    )
                queue.append(event_ref)
                new_config = {**cfg, "_webhook_queue": queue}
            target.config = new_config
            await db.commit()

        logger.info(
            f"Webhook event {event_id} queued for trigger {target_name} "
            f"(agent {target_agent_id}, bytes={staged.size}, sha256={staged.sha256})"
        )
    finally:
        staged.cleanup()

    return JSONResponse({"ok": True})
