"""DingTalk provisioning welcome-message delivery helpers."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.dingtalk_provisioning import (
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
from app.services.channel_session import find_or_create_channel_session
from app.services.dingtalk_provisioning_constants import (
    DINGTALK_BINDING_WELCOME_FALLBACK_NAME,
    DINGTALK_WELCOME_RETRY_DELAYS_SECONDS,
)
from app.services.dingtalk_provisioning_types import (
    WelcomeDeliveryClaim,
    WelcomeSender,
)
from app.services.dingtalk_registration import _as_string, _as_utc, _now
from app.services.im_delivery import (
    DELIVERY_LEASE,
    IMDeliveryPart,
    IMDeliveryResult,
    attach_delivery_to_meta,
)
from app.services.user_output import sanitize_user_visible_text


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
    error_code = _as_string(result.get("error_code"))
    if error_code:
        compacted["error_code"] = error_code
    return compacted


def _is_retryable_welcome_failure(error_code: Any, error: str) -> bool:
    """Return whether a failed welcome send is safe and useful to retry."""
    normalized_code = _as_string(error_code).lower()
    normalized_error = (error or "").lower()
    if normalized_code in {"", "-1", "408", "409", "425", "429"}:
        return True
    try:
        if int(normalized_code) >= 500:
            return True
    except (TypeError, ValueError):
        pass
    return any(
        marker in normalized_error
        for marker in (
            "qyapi_robot_sendmsg",
            "accesstokenpermissiondenied",
            "permissiondenied",
            "permission denied",
            "权限",
        )
    )


def _next_welcome_retry_at(*, attempt_count: int, now: datetime) -> datetime | None:
    """Return the next persisted retry deadline after ``attempt_count`` sends."""
    delay_index = max(0, attempt_count - 1)
    if delay_index >= len(DINGTALK_WELCOME_RETRY_DELAYS_SECONDS):
        return None
    return _as_utc(now) + timedelta(seconds=DINGTALK_WELCOME_RETRY_DELAYS_SECONDS[delay_index])


def _set_registration_welcome_result(
    session: DingTalkChannelProvisioningSession,
    result: dict[str, Any],
) -> None:
    # Assign a new dict rather than mutating the JSON value in place so
    # SQLAlchemy always persists the latest retry outcome.
    registration_result = dict(session.registration_result or {})
    registration_result["welcome_message"] = _compact_welcome_result(result)
    session.registration_result = registration_result


def _record_welcome_attempt(
    session: DingTalkChannelProvisioningSession,
    result: dict[str, Any] | None,
    *,
    now: datetime,
) -> None:
    """Persist one welcome attempt without changing channel configuration."""
    base = _as_utc(now)
    if result is None:
        session.welcome_status = DINGTALK_WELCOME_STATUS_SKIPPED
        session.welcome_next_retry_at = None
        session.welcome_last_error = None
        return

    session.welcome_attempt_count += 1
    _set_registration_welcome_result(session, result)
    if result.get("status") == "sent":
        session.welcome_status = DINGTALK_WELCOME_STATUS_SENT
        session.welcome_next_retry_at = None
        session.welcome_last_error = None
        session.welcome_sent_at = base
        if session.last_error and session.last_error.startswith("钉钉欢迎消息"):
            session.last_error = None
        return

    error = _as_string(result.get("error")) or "unknown"
    session.welcome_last_error = error
    retryable = bool(result.get("retryable"))
    next_retry_at = (
        _next_welcome_retry_at(attempt_count=session.welcome_attempt_count, now=base)
        if retryable
        else None
    )
    session.welcome_next_retry_at = next_retry_at
    if next_retry_at is None:
        session.welcome_status = DINGTALK_WELCOME_STATUS_FAILED
        session.last_error = f"钉钉欢迎消息发送失败，自动重试已停止: {error}"
    else:
        session.welcome_status = DINGTALK_WELCOME_STATUS_PENDING
        session.last_error = f"钉钉欢迎消息发送失败，正在自动重试: {error}"


async def _build_dingtalk_binding_welcome_message(
    db: AsyncSession,
    session: DingTalkChannelProvisioningSession,
) -> str:
    agent = await db.get(Agent, session.agent_id)
    agent_name = _as_string(getattr(agent, "name", None)) or DINGTALK_BINDING_WELCOME_FALLBACK_NAME
    message = f"你好，我是{agent_name}。钉钉通道已配置完成，之后可以直接在这里和我对话。"
    return sanitize_user_visible_text(message)


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


def _welcome_operation_key(session_id: uuid.UUID) -> str:
    return f"dingtalk-provisioning-welcome:{session_id}"


def _welcome_delivery(message: ChatMessage) -> dict[str, Any]:
    meta = message.message_meta if isinstance(message.message_meta, dict) else {}
    delivery = meta.get("delivery")
    return delivery if isinstance(delivery, dict) else {}


def _welcome_result_from_sent_message(
    message: ChatMessage,
    *,
    dingtalk_user_id: str,
) -> dict[str, Any]:
    process_query_key = ""
    for part in _welcome_delivery(message).get("parts") or []:
        if isinstance(part, dict) and part.get("send_status") == "sent":
            process_query_key = _as_string(part.get("provider_message_id"))
            if process_query_key:
                break
    return {
        "status": "sent",
        "user_id": dingtalk_user_id,
        "process_query_key": process_query_key,
        "message_id": str(message.id),
    }


async def _claim_welcome_message_delivery(
    db: AsyncSession,
    session: DingTalkChannelProvisioningSession,
    *,
    dingtalk_user_id: str,
    message: str,
    now: datetime,
) -> WelcomeDeliveryClaim | dict[str, Any]:
    """Persist one exclusive send attempt before the provider side effect."""
    if not session.requested_by_user_id:
        raise ValueError("welcome delivery requires a requesting user")

    chat_session = await find_or_create_channel_session(
        db=db,
        agent_id=session.agent_id,
        user_id=session.requested_by_user_id,
        external_conv_id=f"dingtalk_p2p_{dingtalk_user_id}",
        source_channel="dingtalk",
        first_message_title=message[:30],
    )
    row = (
        await db.execute(
            select(ChatMessage)
            .where(ChatMessage.external_event_key == _welcome_operation_key(session.id))
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is not None:
        status = _as_string(_welcome_delivery(row).get("status"))
        if status == "sent":
            return _welcome_result_from_sent_message(
                row,
                dingtalk_user_id=dingtalk_user_id,
            )
        # A committed pending receipt means a previous worker may already have
        # reached DingTalk. Once its lease is due, never duplicate that send.
        if status in {"pending", "unknown", "partial"}:
            row.message_meta = attach_delivery_to_meta(
                row.message_meta,
                IMDeliveryResult.unknown("dingtalk", "previous_attempt_outcome_unknown"),
            )
            return {
                "status": "failed",
                "user_id": dingtalk_user_id,
                "error": "先前欢迎消息投递结果未知，为避免重复发送已停止重试",
                "error_code": "delivery_unknown",
                "retryable": False,
                "message_id": str(row.id),
            }

    attempt_id = str(uuid.uuid4())
    pending_meta = attach_delivery_to_meta(
        {"artifact_role": "channel_welcome"},
        IMDeliveryResult.pending("dingtalk"),
    )
    pending_meta["delivery"]["attempt_id"] = attempt_id
    pending_meta["delivery"]["attempt_started_at"] = now.isoformat()
    if row is None:
        row = ChatMessage(
            agent_id=session.agent_id,
            user_id=session.requested_by_user_id,
            role="assistant",
            content=message,
            conversation_id=str(chat_session.id),
            external_event_key=_welcome_operation_key(session.id),
            message_meta=pending_meta,
        )
        db.add(row)
        await db.flush()
    else:
        row.message_meta = pending_meta
    chat_session.last_message_at = now
    # A crashed request becomes due after the lease and is converted to unknown
    # by the branch above, so recovery cannot duplicate a provider-side send.
    session.welcome_next_retry_at = now + DELIVERY_LEASE
    await db.flush()
    return WelcomeDeliveryClaim(
        provisioning_id=session.id,
        message_id=row.id,
        attempt_id=attempt_id,
        dingtalk_user_id=dingtalk_user_id,
        message=message,
    )


async def _finalize_welcome_message_delivery(
    db: AsyncSession,
    claim: WelcomeDeliveryClaim,
    *,
    delivery_result: IMDeliveryResult,
    welcome_result: dict[str, Any],
    now: datetime,
) -> bool:
    session = (
        await db.execute(
            select(DingTalkChannelProvisioningSession)
            .where(DingTalkChannelProvisioningSession.id == claim.provisioning_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    row = (
        await db.execute(
            select(ChatMessage).where(ChatMessage.id == claim.message_id).with_for_update()
        )
    ).scalar_one_or_none()
    if session is None or row is None:
        return False
    delivery = _welcome_delivery(row)
    if delivery.get("attempt_id") != claim.attempt_id:
        return False

    row.message_meta = attach_delivery_to_meta(row.message_meta, delivery_result)
    _record_welcome_attempt(session, welcome_result, now=now)
    await db.flush()
    return True


async def _send_dingtalk_binding_welcome_message(
    db: AsyncSession,
    session: DingTalkChannelProvisioningSession,
    *,
    client_id: str,
    client_secret: str,
    welcome_sender: WelcomeSender | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    base = _as_utc(now or _now())
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
    prepared = await _claim_welcome_message_delivery(
        db,
        session,
        dingtalk_user_id=dingtalk_user_id,
        message=message,
        now=base,
    )
    if isinstance(prepared, dict):
        _record_welcome_attempt(session, prepared, now=base)
        await db.commit()
        return prepared

    # The pending receipt and lease are committed before the external call.
    await db.commit()

    try:
        send_result = await sender(
            client_id,
            client_secret,
            dingtalk_user_id,
            message,
        )
    except Exception as exc:
        logger.warning(f"[DingTalk Provisioning] Welcome message send failed: {exc}")
        # Once control entered the provider adapter, an exception cannot prove
        # that DingTalk did not accept the message. Preserve exactly-once
        # behavior by making every exceptional outcome terminally unknown.
        delivery_result = IMDeliveryResult.unknown("dingtalk", type(exc).__name__)
        welcome_result = {
            "status": "failed",
            "user_id": dingtalk_user_id,
            "error": type(exc).__name__,
            "error_code": type(exc).__name__,
            "retryable": False,
            "message_id": str(prepared.message_id),
        }
    else:
        if send_result.get("errcode") in (0, "0"):
            process_query_key = _as_string(send_result.get("processQueryKey"))
            delivery_result = IMDeliveryResult.sent(
                "dingtalk",
                IMDeliveryPart(
                    transport="dingtalk_openapi_oto",
                    provider_message_id=process_query_key or None,
                    conversation_ref=dingtalk_user_id,
                    artifact_role="channel_welcome",
                    recallable=bool(process_query_key),
                ),
            )
            welcome_result = {
                "status": "sent",
                "user_id": dingtalk_user_id,
                "process_query_key": process_query_key,
                "message_id": str(prepared.message_id),
            }
        else:
            error = _as_string(send_result.get("errmsg")) or str(send_result)[:200]
            error_code = send_result.get("errcode")
            logger.warning(f"[DingTalk Provisioning] Welcome message send failed: {error}")
            delivery_result = IMDeliveryResult.failed(
                "dingtalk",
                str(error_code or error or "send_failed"),
            )
            welcome_result = {
                "status": "failed",
                "user_id": dingtalk_user_id,
                "error": error,
                "error_code": str(error_code) if error_code is not None else "",
                "retryable": _is_retryable_welcome_failure(error_code, error),
                "message_id": str(prepared.message_id),
            }

    finalized = await _finalize_welcome_message_delivery(
        db,
        prepared,
        delivery_result=delivery_result,
        welcome_result=welcome_result,
        now=base,
    )
    if not finalized:
        await db.rollback()
        logger.error(
            f"[DingTalk Provisioning] Lost welcome delivery attempt ownership for session {session.id}"
        )
        return {
            "status": "failed",
            "user_id": dingtalk_user_id,
            "error": "delivery_attempt_ownership_lost",
            "error_code": "delivery_attempt_ownership_lost",
            "retryable": False,
            "message_id": str(prepared.message_id),
        }
    await db.commit()
    return welcome_result
