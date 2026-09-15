"""Agent browser login through the shared temporary-credential/login owners."""

import json
import re
import uuid
from datetime import timedelta
from urllib.parse import urlencode, urlsplit

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from app.core.security import create_access_token, decode_access_token, set_access_token_cookie
from app.database import async_session
from app.models.agent import Agent
from app.models.audit import AuditLog
from app.models.openapi_application import OpenAPICredential
from app.models.user import User
from app.schemas.schemas import UserOut
from app.services.platform_service import platform_service
from app.services.authentication_state import require_active_authentication_principal
from app.services.execution_identity import is_human_interactive_turn
from app.services.openapi_applications import digest, fail, now
from app.services.openapi_login import AGENT_AUDIENCE, sign_login_code
from app.services.published_page_access import set_page_session_cookie

LOGIN_LINK_SECONDS = 300
LOGIN_TOKEN_SECONDS = 3600


def report_target(value: str, base: str) -> str:
    """Validate only the destination syntax/origin, never report visibility."""
    value = str(value or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{1,16}", value):
        return f"/p/{value}"
    parsed = urlsplit(value)
    origin = urlsplit(base)
    if parsed.scheme or parsed.netloc:
        if (parsed.scheme, parsed.netloc) != (origin.scheme, origin.netloc):
            fail("invalid_redirect_uri", 400)
    if parsed.query or parsed.fragment or not re.fullmatch(r"/p/[A-Za-z0-9_-]{1,16}", parsed.path):
        fail("invalid_redirect_uri", 400)
    return parsed.path


async def create_agent_login_link(agent_id, user_id, session_id, turn_anchor_id, arguments):
    try:
        async with async_session() as db:
            if not user_id or not session_id or not turn_anchor_id or not await is_human_interactive_turn(
                db, agent_id=agent_id, actor_user_id=user_id,
                session_id=uuid.UUID(str(session_id)), turn_anchor_id=turn_anchor_id,
            ):
                fail("human_conversation_required")
            user = await require_active_authentication_principal(db, await db.get(User, user_id))
            agent = await db.get(Agent, agent_id)
            if not agent or agent.tenant_id != user.tenant_id:
                fail("tenant_mismatch")
            base = await platform_service.get_configured_public_base_url(db)
            if not base:
                fail("public_url_not_configured", 503)
            target = report_target(arguments.get("page_url"), base)
            expires_at = now() + timedelta(seconds=LOGIN_LINK_SECONDS)
            code = sign_login_code({"aud": AGENT_AUDIENCE, "exp": expires_at,
                                    "redirect_uri": target})
            await db.execute(delete(OpenAPICredential).where(
                OpenAPICredential.kind == "agent_login", OpenAPICredential.expires_at < now() - timedelta(days=1),
            ))
            db.add(OpenAPICredential(
                token_hash=digest(code), application_id=None, generation=0, kind="agent_login",
                user_id=user.id, redirect_uri=target, expires_at=expires_at,
            ))
            db.add(AuditLog(user_id=user.id, agent_id=agent_id, action="agent_login.issue",
                            details={"session_id": session_id, "redirect_uri": target}))
            await db.commit()
        url = f"{base}/openapi/login#{urlencode({'agent_code': code})}"
        return {"content": [{"type": "text", "annotations": {"audience": ["assistant"]},
                             "text": json.dumps({"login_url": url, "expires_in": LOGIN_LINK_SECONDS,
                                                 "token_expires_in": LOGIN_TOKEN_SECONDS})}]}
    except (ValueError, HTTPException) as exc:
        detail = exc.detail if isinstance(exc, HTTPException) else "invalid_login_request"
        return {"isError": True, "content": [{"type": "text", "text": str(detail)}]}


async def exchange_agent_login(db, code, payload, request, response):
    value = await db.scalar(select(OpenAPICredential).where(
        OpenAPICredential.token_hash == digest(code), OpenAPICredential.kind == "agent_login",
        OpenAPICredential.application_id.is_(None),
    ).with_for_update())
    if not value or value.redirect_uri != payload.get("redirect_uri"):
        fail("invalid_login_code", 401)
    user = await require_active_authentication_principal(db, await db.scalar(
        select(User).where(User.id == value.user_id).options(selectinload(User.identity)),
    ), status_code=401)
    token = request.cookies.get("access_token")
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        token = authorization[7:]
    current = {}
    if token:
        try:
            current = decode_access_token(token)
        except HTTPException:
            pass
    same_login = (value.consumed_at is not None and current.get("login_code_hash") == value.token_hash
                  and current.get("sub") == str(user.id))
    if not same_login:
        if value.consumed_at or value.expires_at <= now():
            fail("invalid_login_code", 401)
        value.consumed_at = now()
        token = create_access_token(str(user.id), user.role, timedelta(seconds=LOGIN_TOKEN_SECONDS),
                                    login_code_hash=value.token_hash)
        current = decode_access_token(token)
        db.add(AuditLog(user_id=user.id, action="agent_login.complete",
                        details={"redirect_uri": value.redirect_uri}))
    redirect_uri = value.redirect_uri
    await db.commit()
    # Re-entry reuses the exact token and expiry; it is not another redemption.
    set_access_token_cookie(response, request, token)
    set_page_session_cookie(response, request, user.id, expires_at=current["exp"])
    return {"access_token": token, "token_type": "bearer", "user": UserOut.model_validate(user),
            "redirect_uri": redirect_uri}
