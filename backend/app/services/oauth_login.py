"""Shared OAuth authorization-code login flow.

API adapters may validate their own return URLs, state, and channel context before
calling this module.  The return URL is deliberately not accepted here: code
exchange must use the same provider request regardless of whether login started
from the regular SSO callback or an H5 platform-login page.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.services.auth_provider import BaseAuthProvider, ExternalUserInfo


@dataclass(slots=True)
class OAuthCodeLoginResult:
    """Resolved platform user and provider data for one OAuth code."""

    token_data: dict[str, Any]
    user_info: ExternalUserInfo
    user: User
    is_new: bool


class OAuthCodeLoginError(Exception):
    """A safe, transport-independent OAuth login failure."""

    def __init__(self, reason: str, public_message: str, status_code: int):
        super().__init__(public_message)
        self.reason = reason
        self.public_message = public_message
        self.status_code = status_code


def _provider_context(auth_provider: BaseAuthProvider) -> tuple[str, str | None]:
    provider_model = getattr(auth_provider, "provider", None)
    provider_id = getattr(provider_model, "id", None)
    return auth_provider.provider_type, str(provider_id) if provider_id else None


def _safe_upstream_status(token_data: dict[str, Any]) -> int | str | None:
    """Return a short error identifier without logging provider messages or codes."""

    for key in ("status", "error_code", "error"):
        value = token_data.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and 0 < len(value) <= 32:
            if all(character.isalnum() or character in {"-", "_", "."} for character in value):
                return value
    return None


def _known_response_fields(token_data: dict[str, Any]) -> list[str]:
    """Describe the response shape without logging provider-controlled field names."""

    known_fields = {
        "access_token",
        "error",
        "error_code",
        "expires_in",
        "message",
        "msg",
        "refresh_token",
        "status",
        "token_type",
    }
    return sorted(known_fields.intersection(token_data))


def _token_data_has_user_fields(token_data: dict[str, Any]) -> bool:
    candidate_fields = {"userId", "userName", "userCode", "mobile", "userInfo", "openid"}
    if candidate_fields.intersection(token_data):
        return True
    user_info = token_data.get("userInfo")
    return isinstance(user_info, dict) and bool(candidate_fields.intersection(user_info))


async def _get_user_info(
    auth_provider: BaseAuthProvider,
    access_token: str,
    token_data: dict[str, Any],
) -> ExternalUserInfo:
    provider_type, provider_id = _provider_context(auth_provider)
    try:
        user_info = await auth_provider.get_user_info(access_token)
    except Exception as exc:
        fallback = getattr(auth_provider, "get_user_info_from_token_data", None)
        if fallback is None or not _token_data_has_user_fields(token_data):
            logger.warning(
                "OAuth userinfo failed: provider_type={} provider_id={} error_type={}",
                provider_type,
                provider_id,
                type(exc).__name__,
            )
            raise OAuthCodeLoginError(
                "userinfo_failed",
                "Failed to get user information from provider",
                502,
            ) from exc

        try:
            user_info = await fallback(token_data)
        except Exception as fallback_exc:
            logger.warning(
                "OAuth token-data userinfo fallback failed: provider_type={} provider_id={} error_type={}",
                provider_type,
                provider_id,
                type(fallback_exc).__name__,
            )
            raise OAuthCodeLoginError(
                "userinfo_failed",
                "Failed to get user information from provider",
                502,
            ) from fallback_exc

    if not user_info.provider_user_id:
        raise OAuthCodeLoginError(
            "userinfo_missing_user_id",
            "userinfo response missing user ID field",
            502,
        )
    return user_info


async def exchange_oauth_code_for_user(
    db: AsyncSession,
    auth_provider: BaseAuthProvider,
    code: str,
    *,
    tenant_id: str | None = None,
) -> OAuthCodeLoginResult:
    """Exchange a code and resolve a Clawith user through the shared SSO path.

    ``redirect_uri`` is intentionally absent.  Callers can validate it locally,
    but it must never change the provider token request for this login flow.
    """

    token_data = await auth_provider.exchange_code_for_token(code)
    if not isinstance(token_data, dict):
        token_data = {}

    access_token = token_data.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        provider_type, provider_id = _provider_context(auth_provider)
        logger.warning(
            "OAuth code exchange rejected: provider_type={} provider_id={} upstream_status={} response_keys={}",
            provider_type,
            provider_id,
            _safe_upstream_status(token_data),
            _known_response_fields(token_data),
        )
        raise OAuthCodeLoginError(
            "token_exchange_failed",
            "OAuth provider rejected the authorization code",
            400,
        )

    user_info = await _get_user_info(auth_provider, access_token, token_data)
    user, is_new = await auth_provider.find_or_create_user(
        db,
        user_info,
        tenant_id=tenant_id,
    )
    if not user:
        raise OAuthCodeLoginError(
            "user_resolution_failed",
            "Failed to resolve user",
            500,
        )
    if not user.is_active:
        raise OAuthCodeLoginError(
            "account_disabled",
            "Account is disabled",
            403,
        )

    return OAuthCodeLoginResult(
        token_data=token_data,
        user_info=user_info,
        user=user,
        is_new=is_new,
    )
