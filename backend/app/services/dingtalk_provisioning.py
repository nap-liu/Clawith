"""DingTalk automatic channel provisioning.

This service wraps DingTalk's registration device flow, persists bounded poll
state, and writes successful credentials into the existing DingTalk
ChannelConfig runtime path.
"""

from __future__ import annotations

import asyncio
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable

import httpx
from loguru import logger
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import async_session
from app.models.audit import ChatMessage
from app.models.agent import Agent
from app.models.channel_config import ChannelConfig
from app.models.dingtalk_provisioning import (
    DINGTALK_PROVISIONING_ACTIVE_STATUSES,
    DINGTALK_PROVISIONING_STATUS_CANCELLED,
    DINGTALK_PROVISIONING_STATUS_CONFIGURED,
    DINGTALK_PROVISIONING_STATUS_EXPIRED,
    DINGTALK_PROVISIONING_STATUS_FAILED,
    DINGTALK_PROVISIONING_STATUS_POLLING,
    DINGTALK_PROVISIONING_STATUS_WAITING,
    DingTalkChannelProvisioningSession,
)
from app.models.identity import IdentityProvider
from app.models.org import OrgMember
from app.services.channel_session import find_or_create_channel_session


StreamStarter = Callable[[uuid.UUID, str, str], Awaitable[None]]
StreamStopper = Callable[[uuid.UUID], Awaitable[None]]
WelcomeSender = Callable[[str, str, str, str], Awaitable[dict[str, Any]]]
DINGTALK_BINDING_WELCOME_FALLBACK_NAME = "你的数字员工"


@dataclass(frozen=True)
class PollingWindow:
    expires_at: datetime
    next_poll_at: datetime
    poll_interval_seconds: int
    max_poll_attempts: int


class DingTalkRegistrationError(RuntimeError):
    """Raised when DingTalk registration API responses are malformed or failed."""


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _extract_payload(data: dict[str, Any]) -> dict[str, Any]:
    nested = data.get("data")
    if isinstance(nested, dict):
        merged = dict(nested)
        for key, value in data.items():
            if key not in {"data"} and key not in merged:
                merged[key] = value
        return merged
    return data


def _as_string(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _bounded_polling_window(
    *,
    expires_in: int | float | str | None,
    interval: int | float | str | None,
    now: datetime | None = None,
) -> PollingWindow:
    settings = get_settings()
    base = _as_utc(now or _now())

    try:
        raw_ttl = int(float(expires_in if expires_in is not None else 7200))
    except (TypeError, ValueError):
        raw_ttl = 7200
    ttl = max(1, min(raw_ttl, settings.DINGTALK_PROVISIONING_MAX_TTL_SECONDS))

    try:
        raw_interval = int(math.ceil(float(interval if interval is not None else 3)))
    except (TypeError, ValueError):
        raw_interval = 3
    poll_interval = max(
        settings.DINGTALK_PROVISIONING_MIN_INTERVAL_SECONDS,
        min(raw_interval, settings.DINGTALK_PROVISIONING_MAX_INTERVAL_SECONDS),
    )

    attempts_by_time = max(1, ttl // poll_interval)
    max_attempts = max(1, min(attempts_by_time, settings.DINGTALK_PROVISIONING_MAX_ATTEMPTS))
    return PollingWindow(
        expires_at=base + timedelta(seconds=ttl),
        next_poll_at=base + timedelta(seconds=poll_interval),
        poll_interval_seconds=poll_interval,
        max_poll_attempts=max_attempts,
    )


class DingTalkRegistrationClient:
    """Client for DingTalk app registration device flow."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        source: str | None = None,
        timeout_seconds: float = 15,
    ):
        settings = get_settings()
        self.base_url = (base_url or settings.DINGTALK_REGISTRATION_BASE_URL).rstrip("/")
        self.source = source or settings.DINGTALK_REGISTRATION_SOURCE
        self.timeout_seconds = timeout_seconds

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(url, json=payload)
        data = response.json()
        if response.status_code >= 400:
            raise DingTalkRegistrationError(f"DingTalk registration HTTP {response.status_code}: {path}")
        errcode = data.get("errcode")
        if errcode not in (None, 0, "0"):
            errmsg = data.get("errmsg") or data.get("message") or "unknown error"
            raise DingTalkRegistrationError(f"DingTalk registration API error {errcode}: {errmsg}")
        return _extract_payload(data)

    async def begin(self) -> dict[str, Any]:
        init_data = await self._post("/app/registration/init", {"source": self.source})
        nonce = _as_string(init_data.get("nonce"))
        if not nonce:
            raise DingTalkRegistrationError("DingTalk registration init response missing nonce")

        begin_data = await self._post("/app/registration/begin", {"nonce": nonce})
        device_code = _as_string(begin_data.get("device_code"))
        authorization_url = _as_string(begin_data.get("verification_uri_complete"))
        if not device_code:
            raise DingTalkRegistrationError("DingTalk registration begin response missing device_code")
        if not authorization_url:
            raise DingTalkRegistrationError("DingTalk registration begin response missing verification_uri_complete")
        return {
            "device_code": device_code,
            "verification_uri_complete": authorization_url,
            "verification_uri": _as_string(begin_data.get("verification_uri")) or None,
            "expires_in": begin_data.get("expires_in", 7200),
            "interval": begin_data.get("interval", 3),
        }

    async def poll(self, device_code: str) -> dict[str, Any]:
        data = await self._post("/app/registration/poll", {"device_code": device_code})
        status = _as_string(data.get("status")).upper()
        return {
            "status": status or "FAIL",
            "client_id": _as_string(data.get("client_id")) or _as_string(data.get("clientId")),
            "client_secret": _as_string(data.get("client_secret")) or _as_string(data.get("clientSecret")),
            "message": _as_string(data.get("fail_reason"))
            or _as_string(data.get("failReason"))
            or _as_string(data.get("errmsg"))
            or _as_string(data.get("message")),
        }


def _default_registration_client() -> DingTalkRegistrationClient:
    return DingTalkRegistrationClient()


async def start_dingtalk_channel_provisioning(
    db: AsyncSession,
    *,
    agent: Agent,
    requested_by_user_id: uuid.UUID | None,
    registration_client: DingTalkRegistrationClient | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Start DingTalk provisioning and return a user authorization URL."""
    settings = get_settings()
    client = registration_client or _default_registration_client()
    base = _as_utc(now or _now())
    begin = await client.begin()
    window = _bounded_polling_window(
        expires_in=begin.get("expires_in"),
        interval=begin.get("interval"),
        now=base,
    )

    await db.execute(
        update(DingTalkChannelProvisioningSession)
        .where(
            DingTalkChannelProvisioningSession.agent_id == agent.id,
            DingTalkChannelProvisioningSession.status.in_(DINGTALK_PROVISIONING_ACTIVE_STATUSES),
        )
        .values(
            status=DINGTALK_PROVISIONING_STATUS_CANCELLED,
            next_poll_at=None,
            last_error="已由新的钉钉授权流程替换",
        )
    )

    session = DingTalkChannelProvisioningSession(
        agent_id=agent.id,
        tenant_id=agent.tenant_id,
        requested_by_user_id=requested_by_user_id,
        status=DINGTALK_PROVISIONING_STATUS_WAITING,
        device_code=begin["device_code"],
        authorization_url=begin["verification_uri_complete"],
        verification_uri=begin.get("verification_uri"),
        registration_source=settings.DINGTALK_REGISTRATION_SOURCE,
        registration_base_url=settings.DINGTALK_REGISTRATION_BASE_URL.rstrip("/"),
        expires_at=window.expires_at,
        next_poll_at=window.next_poll_at,
        poll_interval_seconds=window.poll_interval_seconds,
        max_poll_attempts=window.max_poll_attempts,
    )
    db.add(session)
    await db.flush()

    return _session_response(
        session,
        message="请打开授权链接，在钉钉中完成数字员工机器人授权。授权完成后平台会自动完成通道配置。",
    )


def _session_response(session: DingTalkChannelProvisioningSession, *, message: str | None = None) -> dict[str, Any]:
    response = {
        "status": session.status,
        "provisioning_id": str(session.id),
        "authorization_url": session.authorization_url,
        "expires_at": _as_utc(session.expires_at).isoformat(),
        "next_poll_at": _as_utc(session.next_poll_at).isoformat() if session.next_poll_at else None,
        "poll_interval_seconds": session.poll_interval_seconds,
        "poll_attempt_count": session.poll_attempt_count,
        "max_poll_attempts": session.max_poll_attempts,
    }
    if message:
        response["message"] = message
    if session.last_error:
        response["last_error"] = session.last_error
    return response


def get_dingtalk_provisioning_status_response(session: DingTalkChannelProvisioningSession) -> dict[str, Any]:
    message_by_status = {
        DINGTALK_PROVISIONING_STATUS_WAITING: "正在等待用户在钉钉中完成数字员工机器人授权。",
        DINGTALK_PROVISIONING_STATUS_POLLING: "正在检查钉钉授权结果，授权完成后会自动配置数字员工通道。",
        DINGTALK_PROVISIONING_STATUS_CONFIGURED: "钉钉数字员工通道已配置完成。",
        DINGTALK_PROVISIONING_STATUS_EXPIRED: "钉钉授权链接已过期，请重新发起数字员工通道配置。",
        DINGTALK_PROVISIONING_STATUS_FAILED: "钉钉数字员工通道配置失败，请重新发起授权。",
        DINGTALK_PROVISIONING_STATUS_CANCELLED: "该钉钉数字员工通道配置流程已取消。",
    }
    return _session_response(session, message=message_by_status.get(session.status))


async def _default_stream_starter(agent_id: uuid.UUID, app_key: str, app_secret: str) -> None:
    from app.services.dingtalk_stream import dingtalk_stream_manager

    await dingtalk_stream_manager.start_client(agent_id, app_key, app_secret)


async def _default_stream_stopper(agent_id: uuid.UUID) -> None:
    from app.services.dingtalk_stream import dingtalk_stream_manager

    await dingtalk_stream_manager.stop_client(agent_id)


async def _default_welcome_sender(app_id: str, app_secret: str, user_id: str, message: str) -> dict[str, Any]:
    from app.services.dingtalk_service import send_dingtalk_message

    return await send_dingtalk_message(
        app_id=app_id,
        app_secret=app_secret,
        user_id=user_id,
        message=message,
        agent_id=app_id,
    )


def _compact_welcome_result(result: dict[str, Any]) -> dict[str, Any]:
    compacted = {
        "status": result["status"],
        "user_id": result["user_id"],
    }
    process_query_key = _as_string(result.get("process_query_key"))
    if process_query_key:
        compacted["process_query_key"] = process_query_key
    error = _as_string(result.get("error"))
    if error:
        compacted["error"] = error
    return compacted


async def _build_dingtalk_binding_welcome_message(
    db: AsyncSession,
    session: DingTalkChannelProvisioningSession,
) -> str:
    agent = await db.get(Agent, session.agent_id)
    agent_name = _as_string(getattr(agent, "name", None)) or DINGTALK_BINDING_WELCOME_FALLBACK_NAME
    return f"你好，我是{agent_name}。钉钉通道已配置完成，之后可以直接在这里和我对话。"


async def _find_requester_dingtalk_member(
    db: AsyncSession,
    session: DingTalkChannelProvisioningSession,
) -> OrgMember | None:
    if not session.requested_by_user_id:
        return None

    result = await db.execute(
        select(OrgMember)
        .join(IdentityProvider, OrgMember.provider_id == IdentityProvider.id)
        .where(
            OrgMember.user_id == session.requested_by_user_id,
            OrgMember.tenant_id == session.tenant_id,
            OrgMember.status == "active",
            OrgMember.external_id.is_not(None),
            IdentityProvider.provider_type == "dingtalk",
            IdentityProvider.is_active.is_(True),
            or_(IdentityProvider.tenant_id == session.tenant_id, IdentityProvider.tenant_id.is_(None)),
        )
        .order_by(OrgMember.synced_at.desc())
    )
    return result.scalars().first()


async def _persist_welcome_message_history(
    db: AsyncSession,
    session: DingTalkChannelProvisioningSession,
    *,
    dingtalk_user_id: str,
    message: str,
    now: datetime,
) -> None:
    if not session.requested_by_user_id:
        return
    chat_session = await find_or_create_channel_session(
        db=db,
        agent_id=session.agent_id,
        user_id=session.requested_by_user_id,
        external_conv_id=f"dingtalk_p2p_{dingtalk_user_id}",
        source_channel="dingtalk",
        first_message_title=message[:30],
    )
    db.add(
        ChatMessage(
            agent_id=session.agent_id,
            user_id=session.requested_by_user_id,
            role="assistant",
            content=message,
            conversation_id=str(chat_session.id),
        )
    )
    chat_session.last_message_at = now
    await db.flush()


async def _send_dingtalk_binding_welcome_message(
    db: AsyncSession,
    session: DingTalkChannelProvisioningSession,
    *,
    client_id: str,
    client_secret: str,
    welcome_sender: WelcomeSender | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    member = await _find_requester_dingtalk_member(db, session)
    if not member:
        logger.info(
            f"[DingTalk Provisioning] Skip welcome message for session {session.id}: "
            "requesting user has no active DingTalk member mapping"
        )
        return None

    dingtalk_user_id = _as_string(member.external_id)
    if not dingtalk_user_id:
        return None

    sender = welcome_sender or _default_welcome_sender
    message = await _build_dingtalk_binding_welcome_message(db, session)
    try:
        send_result = await sender(
            client_id,
            client_secret,
            dingtalk_user_id,
            message,
        )
    except Exception as exc:
        logger.warning(f"[DingTalk Provisioning] Welcome message send failed: {exc}")
        return {"status": "failed", "user_id": dingtalk_user_id, "error": type(exc).__name__}

    if send_result.get("errcode") not in (0, "0"):
        error = _as_string(send_result.get("errmsg")) or str(send_result)[:200]
        logger.warning(f"[DingTalk Provisioning] Welcome message send failed: {error}")
        return {"status": "failed", "user_id": dingtalk_user_id, "error": error}

    await _persist_welcome_message_history(
        db,
        session,
        dingtalk_user_id=dingtalk_user_id,
        message=message,
        now=_as_utc(now or _now()),
    )
    return {
        "status": "sent",
        "user_id": dingtalk_user_id,
        "process_query_key": _as_string(send_result.get("processQueryKey")),
    }


async def _configure_dingtalk_channel(
    db: AsyncSession,
    session: DingTalkChannelProvisioningSession,
    *,
    client_id: str,
    client_secret: str,
) -> tuple[ChannelConfig, list[uuid.UUID]]:
    replaced_agent_ids: list[uuid.UUID] = []
    conflicts = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.channel_type == "dingtalk",
            ChannelConfig.app_id == client_id,
            ChannelConfig.agent_id != session.agent_id,
            ChannelConfig.is_configured.is_(True),
        )
    )
    replaced_at = _now().isoformat()
    for conflict in conflicts.scalars().all():
        replaced_agent_ids.append(conflict.agent_id)
        conflict.app_id = None
        conflict.app_secret = None
        conflict.encrypt_key = None
        conflict.verification_token = None
        conflict.is_configured = False
        conflict.is_connected = False
        conflict.extra_config = {
            **(conflict.extra_config or {}),
            "replaced_by_agent_id": str(session.agent_id),
            "replaced_by_provisioning_session_id": str(session.id),
            "replaced_at": replaced_at,
            "replacement_reason": "dingtalk_robot_rebound",
        }
    if replaced_agent_ids:
        await db.flush()

    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == session.agent_id,
            ChannelConfig.channel_type == "dingtalk",
        )
    )
    existing = result.scalar_one_or_none()
    extra = {
        "connection_mode": "websocket",
        "agent_id": client_id,
        "provisioning_session_id": str(session.id),
        "provisioning_source": session.registration_source,
        "provisioned_at": _now().isoformat(),
    }

    if existing:
        existing.app_id = client_id
        existing.app_secret = client_secret
        existing.is_configured = True
        existing.extra_config = {**(existing.extra_config or {}), **extra}
        config = existing
    else:
        config = ChannelConfig(
            agent_id=session.agent_id,
            channel_type="dingtalk",
            app_id=client_id,
            app_secret=client_secret,
            is_configured=True,
            extra_config=extra,
        )
        db.add(config)
    await db.flush()
    return config, replaced_agent_ids


def _stop_session(
    session: DingTalkChannelProvisioningSession,
    *,
    status: str,
    error: str | None = None,
) -> None:
    session.status = status
    session.next_poll_at = None
    if error:
        session.last_error = error


async def poll_dingtalk_provisioning_session(
    db: AsyncSession,
    session: DingTalkChannelProvisioningSession,
    *,
    registration_client: DingTalkRegistrationClient | None = None,
    stream_starter: StreamStarter | None = None,
    stream_stopper: StreamStopper | None = None,
    welcome_sender: WelcomeSender | None = None,
    now: datetime | None = None,
) -> str:
    """Poll one persisted session once and apply state transitions."""
    base = _as_utc(now or _now())
    if session.status not in DINGTALK_PROVISIONING_ACTIVE_STATUSES:
        return session.status
    if _as_utc(session.expires_at) <= base:
        _stop_session(session, status=DINGTALK_PROVISIONING_STATUS_EXPIRED, error="钉钉授权链接已过期")
        return session.status
    if session.poll_attempt_count >= session.max_poll_attempts:
        _stop_session(session, status=DINGTALK_PROVISIONING_STATUS_FAILED, error="已达到最大轮询次数")
        return session.status

    client = registration_client or _default_registration_client()
    session.poll_attempt_count += 1
    session.last_poll_at = base

    try:
        poll_result = await client.poll(session.device_code)
    except Exception as exc:
        session.last_error = f"钉钉授权结果查询失败: {type(exc).__name__}"
        if session.poll_attempt_count >= session.max_poll_attempts:
            _stop_session(session, status=DINGTALK_PROVISIONING_STATUS_FAILED, error=session.last_error)
        else:
            session.status = DINGTALK_PROVISIONING_STATUS_POLLING
            session.next_poll_at = base + timedelta(seconds=session.poll_interval_seconds)
        return session.status

    status = _as_string(poll_result.get("status")).upper()
    if status == "WAITING":
        session.status = DINGTALK_PROVISIONING_STATUS_POLLING
        session.next_poll_at = base + timedelta(seconds=session.poll_interval_seconds)
        return session.status
    if status == "EXPIRED":
        _stop_session(
            session,
            status=DINGTALK_PROVISIONING_STATUS_EXPIRED,
            error=poll_result.get("message") or "钉钉授权链接已过期",
        )
        return session.status
    if status == "SUCCESS":
        client_id = _as_string(poll_result.get("client_id"))
        client_secret = _as_string(poll_result.get("client_secret"))
        if not client_id or not client_secret:
            _stop_session(session, status=DINGTALK_PROVISIONING_STATUS_FAILED, error="钉钉授权成功但未返回完整凭据")
            return session.status

        _, replaced_agent_ids = await _configure_dingtalk_channel(
            db,
            session,
            client_id=client_id,
            client_secret=client_secret,
        )
        session.registration_result = {"client_id": client_id}
        welcome_result = await _send_dingtalk_binding_welcome_message(
            db,
            session,
            client_id=client_id,
            client_secret=client_secret,
            welcome_sender=welcome_sender,
            now=base,
        )
        if welcome_result:
            session.registration_result["welcome_message"] = _compact_welcome_result(welcome_result)
            if welcome_result.get("status") == "failed":
                session.last_error = f"钉钉欢迎消息发送失败: {welcome_result.get('error') or 'unknown'}"
        _stop_session(session, status=DINGTALK_PROVISIONING_STATUS_CONFIGURED)
        stopper = stream_stopper or _default_stream_stopper
        for replaced_agent_id in replaced_agent_ids:
            await stopper(replaced_agent_id)
        starter = stream_starter or _default_stream_starter
        await starter(session.agent_id, client_id, client_secret)
        return session.status

    _stop_session(
        session,
        status=DINGTALK_PROVISIONING_STATUS_FAILED,
        error=poll_result.get("message") or f"钉钉授权失败: {status or 'UNKNOWN'}",
    )
    return session.status


async def poll_due_dingtalk_provisioning_sessions(
    db: AsyncSession,
    *,
    registration_client: DingTalkRegistrationClient | None = None,
    stream_starter: StreamStarter | None = None,
    welcome_sender: WelcomeSender | None = None,
    now: datetime | None = None,
    limit: int | None = None,
) -> int:
    """Poll due active sessions. Returns the number of sessions polled."""
    base = _as_utc(now or _now())
    await db.execute(
        update(DingTalkChannelProvisioningSession)
        .where(
            DingTalkChannelProvisioningSession.status.in_(DINGTALK_PROVISIONING_ACTIVE_STATUSES),
            DingTalkChannelProvisioningSession.expires_at <= base,
        )
        .values(
            status=DINGTALK_PROVISIONING_STATUS_EXPIRED,
            next_poll_at=None,
            last_error="钉钉授权链接已过期",
        )
    )

    stmt = (
        select(DingTalkChannelProvisioningSession)
        .where(
            DingTalkChannelProvisioningSession.status.in_(DINGTALK_PROVISIONING_ACTIVE_STATUSES),
            DingTalkChannelProvisioningSession.expires_at > base,
            DingTalkChannelProvisioningSession.next_poll_at.is_not(None),
            DingTalkChannelProvisioningSession.next_poll_at <= base,
        )
        .order_by(DingTalkChannelProvisioningSession.next_poll_at.asc())
        .with_for_update(skip_locked=True)
    )
    if limit:
        stmt = stmt.limit(limit)

    result = await db.execute(stmt)
    sessions = result.scalars().all()
    for session in sessions:
        await poll_dingtalk_provisioning_session(
            db,
            session,
            registration_client=registration_client,
            stream_starter=stream_starter,
            welcome_sender=welcome_sender,
            now=base,
        )
    await db.flush()
    return len(sessions)


class DingTalkProvisioningPoller:
    """Connector-role loop that resumes persisted DingTalk provisioning sessions."""

    def __init__(self, *, loop_interval_seconds: int = 2):
        self.loop_interval_seconds = loop_interval_seconds

    async def start_all(self) -> None:
        settings = get_settings()
        logger.info("[DingTalk Provisioning] Poller started")
        while True:
            try:
                async with async_session() as db:
                    count = await poll_due_dingtalk_provisioning_sessions(
                        db,
                        limit=settings.DINGTALK_PROVISIONING_POLL_BATCH_SIZE,
                    )
                    await db.commit()
                    if count:
                        logger.info(f"[DingTalk Provisioning] Polled {count} session(s)")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[DingTalk Provisioning] Poll loop iteration failed: {exc}")
            await asyncio.sleep(self.loop_interval_seconds)


dingtalk_provisioning_poller = DingTalkProvisioningPoller()
