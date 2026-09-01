"""DingTalk automatic channel provisioning.

This service wraps DingTalk's registration device flow, persists deadline-bound
poll state, and writes successful credentials into the existing DingTalk
ChannelConfig runtime path.
"""

from __future__ import annotations

import asyncio
import math
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from loguru import logger
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import async_session
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.channel_config import ChannelConfig
from app.models.dingtalk_provisioning import (
    DINGTALK_PROVISIONING_ACTIVE_STATUSES,
    DINGTALK_PROVISIONING_STATUS_CANCELLED,
    DINGTALK_PROVISIONING_STATUS_CONFIGURED,
    DINGTALK_PROVISIONING_STATUS_EXPIRED,
    DINGTALK_PROVISIONING_STATUS_FAILED,
    DINGTALK_PROVISIONING_STATUS_POLLING,
    DINGTALK_PROVISIONING_STATUS_WAITING,
    DINGTALK_WELCOME_STATUS_FAILED,
    DINGTALK_WELCOME_STATUS_PENDING,
    DINGTALK_WELCOME_STATUS_SENT,
    DINGTALK_WELCOME_STATUS_SKIPPED,
    DingTalkChannelProvisioningSession,
)
from app.models.identity import IdentityProvider
from app.models.org import OrgMember
from app.models.tenant import Tenant
from app.services.channel_session import find_or_create_channel_session
from app.services.dingtalk_credentials import dingtalk_credential_fingerprint
from app.services.im_delivery import (
    DELIVERY_LEASE,
    IMDeliveryPart,
    IMDeliveryResult,
    attach_delivery_to_meta,
)
from app.services.user_output import sanitize_user_visible_text
from app.services.dingtalk_provisioning_constants import (
    DINGTALK_BINDING_WELCOME_FALLBACK_NAME,
    DINGTALK_PROVISIONING_OPERATION_FORCE,
    DINGTALK_PROVISIONING_OPERATION_INITIAL,
    DINGTALK_REGISTRATION_TERMINAL_STATUSES,
    DINGTALK_WELCOME_RETRY_DELAYS_SECONDS,
)
from app.services.dingtalk_provisioning_types import (
    DingTalkRegistrationError,
    PollingWindow,
    StreamStarter,
    StreamStopper,
    WelcomeDeliveryClaim,
    WelcomeSender,
)
from app.services.dingtalk_registration import (
    DingTalkRegistrationClient,
    _as_string,
    _as_utc,
    _bounded_polling_window,
    _configured_dingtalk_fingerprint,
    _default_registration_client,
    _extract_payload,
    _merge_registration_result,
    _now,
    _session_baseline_fingerprint,
    _session_operation,
)
from app.services.dingtalk_welcome_delivery import (
    _build_dingtalk_binding_welcome_message,
    _claim_welcome_message_delivery,
    _compact_welcome_result,
    _default_welcome_sender,
    _finalize_welcome_message_delivery,
    _find_requester_dingtalk_member,
    _is_retryable_welcome_failure,
    _next_welcome_retry_at,
    _record_welcome_attempt,
    _send_dingtalk_binding_welcome_message,
    _session_response,
    _set_registration_welcome_result,
    _welcome_delivery,
    _welcome_operation_key,
    _welcome_result_from_sent_message,
    get_dingtalk_provisioning_status_response,
)


async def start_dingtalk_channel_provisioning(
    db: AsyncSession,
    *,
    agent: Agent,
    requested_by_user_id: uuid.UUID | None,
    force_reconfigure: bool = False,
    restart_existing: bool | None = None,
    registration_client: DingTalkRegistrationClient | None = None,
    stream_starter: StreamStarter | None = None,
    stream_stopper: StreamStopper | None = None,
    welcome_sender: WelcomeSender | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Start or reuse DingTalk provisioning and return its current state."""
    settings = get_settings()
    client = registration_client or _default_registration_client()
    base = _as_utc(now or _now())

    # Serialize starts per Agent and lock the current channel row when present.
    # ``restart_existing`` remains accepted for compatibility, but a still-valid
    # flow is never replaced implicitly. Callers must cancel it explicitly.
    await db.execute(select(Agent.id).where(Agent.id == agent.id).with_for_update())
    config_result = await db.execute(
        select(ChannelConfig)
        .where(
            ChannelConfig.agent_id == agent.id,
            ChannelConfig.channel_type == "dingtalk",
        )
        .with_for_update()
    )
    config = config_result.scalar_one_or_none()
    current_fingerprint = _configured_dingtalk_fingerprint(config)
    if current_fingerprint and not force_reconfigure:
        return {
            "status": "already_configured",
            "flow_action": "already_configured",
            "authorization_url": None,
            "message": (
                "当前数字员工的钉钉通道已经配置完成，无需重复配置。"
                "只有用户明确要求强制重配时才应重新授权；重新授权会创建新的钉钉机器人应用。"
            ),
        }

    operation = (
        DINGTALK_PROVISIONING_OPERATION_FORCE
        if current_fingerprint
        else DINGTALK_PROVISIONING_OPERATION_INITIAL
    )
    active_result = await db.execute(
        select(DingTalkChannelProvisioningSession)
        .where(
            DingTalkChannelProvisioningSession.agent_id == agent.id,
            DingTalkChannelProvisioningSession.status.in_(DINGTALK_PROVISIONING_ACTIVE_STATUSES),
        )
        .order_by(
            DingTalkChannelProvisioningSession.created_at.desc(),
            DingTalkChannelProvisioningSession.id.desc(),
        )
        .with_for_update()
    )
    active_sessions = list(active_result.scalars())
    active = active_sessions[0] if active_sessions else None

    # Normalize any historical duplicate active rows while keeping the newest.
    for stale in active_sessions[1:]:
        _stop_session(
            stale,
            status=DINGTALK_PROVISIONING_STATUS_CANCELLED,
            error="已由更新的钉钉授权流程替换",
        )

    reusable = (
        active is not None
        and _as_utc(active.expires_at) > base
        and _session_operation(active) == operation
        and (
            operation == DINGTALK_PROVISIONING_OPERATION_INITIAL
            or (
                bool(_session_baseline_fingerprint(active))
                and _session_baseline_fingerprint(active) == current_fingerprint
            )
        )
    )
    if reusable:
        response = _session_response(
            active,
            message="当前钉钉授权流程仍有效，请继续使用原授权链接完成配置。",
        )
        response["flow_action"] = "reused"
        return response

    had_existing = active is not None
    if active is not None:
        # Close the boundary where DingTalk has already completed the old flow
        # but the connector has not consumed SUCCESS yet. Returning the
        # configured result avoids handing the user a second authorization link
        # and therefore avoids creating a duplicate robot application.
        await poll_dingtalk_provisioning_session(
            db,
            active,
            registration_client=client,
            stream_starter=stream_starter,
            stream_stopper=stream_stopper,
            welcome_sender=welcome_sender,
            now=base,
        )
        if active.status == DINGTALK_PROVISIONING_STATUS_CONFIGURED:
            response = _session_response(
                active,
                message="原钉钉授权已经成功，数字员工通道配置已完成。",
            )
            response["flow_action"] = "configured_existing"
            return response

    # Do not cancel the previous flow until DingTalk has successfully issued
    # the replacement. A transient begin failure therefore remains recoverable.
    begin = await client.begin()
    window = _bounded_polling_window(
        expires_in=begin.get("expires_in"),
        interval=begin.get("interval"),
        now=base,
    )

    if active:
        _stop_session(
            active,
            status=DINGTALK_PROVISIONING_STATUS_CANCELLED,
            error="当前配置或配置操作已变化，请使用新的钉钉授权流程",
        )
    for previous in active_sessions:
        if previous.status in DINGTALK_PROVISIONING_ACTIVE_STATUSES:
            _stop_session(
                previous,
                status=DINGTALK_PROVISIONING_STATUS_CANCELLED,
                error="已由新的钉钉授权流程替换",
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
        registration_result={
            "operation": operation,
            **(
                {"baseline_fp": current_fingerprint}
                if operation == DINGTALK_PROVISIONING_OPERATION_FORCE
                else {}
            ),
        },
    )
    db.add(session)
    await db.flush()

    response = _session_response(
        session,
        message="请打开授权链接，在钉钉中完成数字员工机器人授权。授权完成后平台会自动完成通道配置。",
    )
    response["flow_action"] = "replaced" if had_existing else "created"
    return response




async def _configure_dingtalk_channel(
    db: AsyncSession,
    session: DingTalkChannelProvisioningSession,
    *,
    client_id: str,
    client_secret: str,
    dingtalk_agent_id: str,
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
        "agent_id": dingtalk_agent_id or client_id,
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

    deadline_reached = _as_utc(session.expires_at) <= base

    client = registration_client or _default_registration_client()
    session.poll_attempt_count += 1
    session.last_poll_at = base

    try:
        poll_result = await client.poll(session.device_code)
    except Exception as exc:
        error = f"钉钉授权结果查询失败: {type(exc).__name__}"
        if deadline_reached:
            _stop_session(
                session,
                status=DINGTALK_PROVISIONING_STATUS_EXPIRED,
                error=f"钉钉授权链接已过期；最后一次{error}",
            )
        else:
            session.last_error = error
            session.status = DINGTALK_PROVISIONING_STATUS_POLLING
            session.next_poll_at = min(
                _as_utc(session.expires_at),
                base + timedelta(seconds=session.poll_interval_seconds),
            )
        return session.status

    status = _as_string(poll_result.get("status")).upper() or "FAIL"
    client_id = _as_string(poll_result.get("client_id"))
    client_secret = _as_string(poll_result.get("client_secret"))
    dingtalk_agent_id = _as_string(poll_result.get("agent_id"))
    # DingTalk can return usable application credentials while its registration
    # status still reads APPROVING. The credential pair is the readiness signal;
    # incomplete APPROVING responses remain in the normal polling path.
    credentials_ready_while_approving = (
        status == "APPROVING" and bool(client_id) and bool(client_secret)
    )
    if (
        status not in DINGTALK_REGISTRATION_TERMINAL_STATUSES
        and not credentials_ready_while_approving
    ):
        _merge_registration_result(session, last_poll_status=status)
        if deadline_reached:
            error = (
                "钉钉授权链接已过期"
                if status == "WAITING"
                else f"钉钉应用仍处于 {status} 状态，配置等待时间已结束"
            )
            _stop_session(
                session,
                status=DINGTALK_PROVISIONING_STATUS_EXPIRED,
                error=error,
            )
        else:
            session.status = DINGTALK_PROVISIONING_STATUS_POLLING
            session.next_poll_at = min(
                _as_utc(session.expires_at),
                base + timedelta(seconds=session.poll_interval_seconds),
            )
            session.last_error = None
        return session.status
    if status == "EXPIRED":
        error = _as_string(poll_result.get("message"))
        _stop_session(
            session,
            status=DINGTALK_PROVISIONING_STATUS_EXPIRED,
            error=error if error.lower() != "ok" else "钉钉授权链接已过期",
        )
        return session.status
    if status == "SUCCESS" or credentials_ready_while_approving:
        if not client_id or not client_secret:
            _stop_session(session, status=DINGTALK_PROVISIONING_STATUS_FAILED, error="钉钉授权成功但未返回完整凭据")
            return session.status

        await db.execute(select(Agent.id).where(Agent.id == session.agent_id).with_for_update())
        config_result = await db.execute(
            select(ChannelConfig)
            .where(
                ChannelConfig.agent_id == session.agent_id,
                ChannelConfig.channel_type == "dingtalk",
            )
            .with_for_update()
        )
        current_config = config_result.scalar_one_or_none()
        current_fingerprint = _configured_dingtalk_fingerprint(current_config)
        operation = _session_operation(session)
        if operation == DINGTALK_PROVISIONING_OPERATION_FORCE:
            baseline_fingerprint = _session_baseline_fingerprint(session)
            if not baseline_fingerprint or baseline_fingerprint != current_fingerprint:
                _merge_registration_result(session, completion_reason="configuration_changed")
                _stop_session(
                    session,
                    status=DINGTALK_PROVISIONING_STATUS_CANCELLED,
                    error="钉钉通道配置已在授权期间发生变化，本次强制重配未覆盖当前配置",
                )
                return session.status
        elif current_fingerprint:
            _merge_registration_result(session, completion_reason="already_configured")
            _stop_session(
                session,
                status=DINGTALK_PROVISIONING_STATUS_CANCELLED,
                error="钉钉通道已由其他操作配置完成，本次授权未覆盖当前配置",
            )
            return session.status

        try:
            async with db.begin_nested():
                await _configure_dingtalk_channel(
                    db,
                    session,
                    client_id=client_id,
                    client_secret=client_secret,
                    dingtalk_agent_id=dingtalk_agent_id,
                )
        except IntegrityError:
            _merge_registration_result(session, completion_reason="configuration_conflict")
            _stop_session(
                session,
                status=DINGTALK_PROVISIONING_STATUS_CANCELLED,
                error="钉钉通道配置已被其他操作更新，本次授权未覆盖当前配置",
            )
            return session.status

        registration_values = {"client_id": client_id}
        if credentials_ready_while_approving:
            registration_values.update(
                {
                    "last_poll_status": status,
                    "completion_reason": "credentials_ready_while_approving",
                }
            )
        if dingtalk_agent_id:
            registration_values["agent_id"] = dingtalk_agent_id
        _merge_registration_result(session, **registration_values)
        session.welcome_status = DINGTALK_WELCOME_STATUS_PENDING
        session.welcome_attempt_count = 0
        session.welcome_next_retry_at = base
        session.welcome_last_error = None
        session.welcome_sent_at = None
        _stop_session(
            session,
            status=DINGTALK_PROVISIONING_STATUS_CONFIGURED,
        )
        return session.status

    error = _as_string(poll_result.get("message"))
    if not error or error.lower() == "ok":
        error = f"钉钉授权失败: {status or 'UNKNOWN'}"
    _stop_session(
        session,
        status=DINGTALK_PROVISIONING_STATUS_FAILED,
        error=error,
    )
    return session.status


async def retry_due_dingtalk_welcome_messages(
    db: AsyncSession,
    *,
    welcome_sender: WelcomeSender | None = None,
    now: datetime | None = None,
    limit: int | None = None,
) -> int:
    """Retry due completion messages for already-configured DingTalk channels."""
    base = _as_utc(now or _now())
    stmt = (
        select(DingTalkChannelProvisioningSession.id)
        .join(Agent, Agent.id == DingTalkChannelProvisioningSession.agent_id)
        .join(Tenant, Tenant.id == Agent.tenant_id)
        .where(
            DingTalkChannelProvisioningSession.status == DINGTALK_PROVISIONING_STATUS_CONFIGURED,
            DingTalkChannelProvisioningSession.welcome_status == DINGTALK_WELCOME_STATUS_PENDING,
            DingTalkChannelProvisioningSession.welcome_next_retry_at.is_not(None),
            DingTalkChannelProvisioningSession.welcome_next_retry_at <= base,
            Agent.is_deleted.is_(False),
            Tenant.is_active.is_(True),
        )
        .order_by(DingTalkChannelProvisioningSession.welcome_next_retry_at.asc())
    )
    if limit:
        stmt = stmt.limit(limit)

    result = await db.execute(stmt)
    session_ids = list(result.scalars())
    processed_count = 0
    for session_id in session_ids:
        session = (
            await db.execute(
                select(DingTalkChannelProvisioningSession)
                .where(
                    DingTalkChannelProvisioningSession.id == session_id,
                    DingTalkChannelProvisioningSession.status
                    == DINGTALK_PROVISIONING_STATUS_CONFIGURED,
                    DingTalkChannelProvisioningSession.welcome_status
                    == DINGTALK_WELCOME_STATUS_PENDING,
                    DingTalkChannelProvisioningSession.welcome_next_retry_at.is_not(None),
                    DingTalkChannelProvisioningSession.welcome_next_retry_at <= base,
                )
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if session is None:
            await db.commit()
            continue

        processed_count += 1
        config_result = await db.execute(
            select(ChannelConfig).where(
                ChannelConfig.agent_id == session.agent_id,
                ChannelConfig.channel_type == "dingtalk",
                ChannelConfig.is_configured.is_(True),
            )
        )
        config = config_result.scalar_one_or_none()
        expected_client_id = _as_string((session.registration_result or {}).get("client_id"))
        if (
            not config
            or not config.app_id
            or not config.app_secret
            or (expected_client_id and config.app_id != expected_client_id)
        ):
            error = "钉钉通道已被删除或替换，停止补发配置完成通知"
            session.welcome_status = DINGTALK_WELCOME_STATUS_FAILED
            session.welcome_next_retry_at = None
            session.welcome_last_error = error
            session.last_error = error
            await db.commit()
            continue

        welcome_result = await _send_dingtalk_binding_welcome_message(
            db,
            session,
            client_id=config.app_id,
            client_secret=config.app_secret,
            welcome_sender=welcome_sender,
            now=base,
        )
        if welcome_result is None:
            _record_welcome_attempt(session, None, now=base)
            await db.commit()
        if session.welcome_status == DINGTALK_WELCOME_STATUS_SENT:
            logger.info(
                f"[DingTalk Provisioning] Welcome message delivered for session {session.id} "
                f"after {session.welcome_attempt_count} attempt(s)"
            )
        elif session.welcome_status == DINGTALK_WELCOME_STATUS_PENDING:
            logger.info(
                f"[DingTalk Provisioning] Welcome retry scheduled for session {session.id} "
                f"at {session.welcome_next_retry_at.isoformat()}"
            )
        elif session.welcome_status == DINGTALK_WELCOME_STATUS_SKIPPED:
            logger.info(
                f"[DingTalk Provisioning] Welcome retry skipped for session {session.id}: "
                "requesting user has no active DingTalk member mapping"
            )
        else:
            logger.warning(
                f"[DingTalk Provisioning] Welcome retries exhausted for session {session.id}: "
                f"{session.welcome_last_error}"
            )

    return processed_count


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
    stmt = (
        select(DingTalkChannelProvisioningSession)
        .join(Agent, Agent.id == DingTalkChannelProvisioningSession.agent_id)
        .join(Tenant, Tenant.id == Agent.tenant_id)
        .where(
            DingTalkChannelProvisioningSession.status.in_(DINGTALK_PROVISIONING_ACTIVE_STATUSES),
            DingTalkChannelProvisioningSession.next_poll_at.is_not(None),
            DingTalkChannelProvisioningSession.next_poll_at <= base,
            Agent.is_deleted.is_(False),
            Tenant.is_active.is_(True),
        )
        .order_by(DingTalkChannelProvisioningSession.next_poll_at.asc())
        .with_for_update(skip_locked=True)
    )
    if limit:
        stmt = stmt.limit(limit)

    result = await db.execute(stmt)
    due_sessions = result.scalars().all()
    processed_agents: set[uuid.UUID] = set()
    polled_count = 0
    for due_session in due_sessions:
        if due_session.agent_id in processed_agents:
            continue
        processed_agents.add(due_session.agent_id)
        active_result = await db.execute(
            select(DingTalkChannelProvisioningSession)
            .where(
                DingTalkChannelProvisioningSession.agent_id == due_session.agent_id,
                DingTalkChannelProvisioningSession.status.in_(DINGTALK_PROVISIONING_ACTIVE_STATUSES),
            )
            .order_by(
                DingTalkChannelProvisioningSession.created_at.desc(),
                DingTalkChannelProvisioningSession.id.desc(),
            )
            .with_for_update()
        )
        active_sessions = list(active_result.scalars())
        if not active_sessions:
            continue
        session = active_sessions[0]
        for stale in active_sessions[1:]:
            _stop_session(
                stale,
                status=DINGTALK_PROVISIONING_STATUS_CANCELLED,
                error="已由更新的钉钉授权流程替换",
            )
        if session.next_poll_at is None or _as_utc(session.next_poll_at) > base:
            continue
        await poll_dingtalk_provisioning_session(
            db,
            session,
            registration_client=registration_client,
            stream_starter=stream_starter,
            welcome_sender=welcome_sender,
            now=base,
        )
        polled_count += 1
    await db.flush()
    return polled_count


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
                async with async_session() as db:
                    welcome_count = await retry_due_dingtalk_welcome_messages(
                        db,
                        limit=settings.DINGTALK_PROVISIONING_POLL_BATCH_SIZE,
                    )
                    await db.commit()
                if count:
                    logger.info(f"[DingTalk Provisioning] Polled {count} session(s)")
                if welcome_count:
                    logger.info(f"[DingTalk Provisioning] Retried {welcome_count} welcome message(s)")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[DingTalk Provisioning] Poll loop iteration failed: {exc}")
            await asyncio.sleep(self.loop_interval_seconds)


dingtalk_provisioning_poller = DingTalkProvisioningPoller()
