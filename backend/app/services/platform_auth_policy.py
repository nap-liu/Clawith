"""Platform-wide switches for interactive authentication and self-registration."""

from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.system_settings import SystemSetting
from app.services.authentication_state import require_active_authentication_principal

PASSWORD_LOGIN_KEY = "password_login_enabled"
ACCOUNT_REGISTRATION_KEY = "account_registration_enabled"


@dataclass(frozen=True, slots=True)
class PlatformAuthPolicy:
    password_login_enabled: bool = True
    account_registration_enabled: bool = True


class AccountRegistrationDisabled(PermissionError):
    """Raised when an interactive SSO flow would create a platform account."""


async def get_platform_auth_policy(db: AsyncSession) -> PlatformAuthPolicy:
    rows = (
        await db.execute(
            select(SystemSetting).where(
                SystemSetting.key.in_((PASSWORD_LOGIN_KEY, ACCOUNT_REGISTRATION_KEY))
            )
        )
    ).scalars().all()
    values = {row.key: row.value for row in rows}

    def enabled(key: str) -> bool:
        value = values.get(key)
        return bool(value.get("enabled", True)) if isinstance(value, dict) else True

    return PlatformAuthPolicy(
        password_login_enabled=enabled(PASSWORD_LOGIN_KEY),
        account_registration_enabled=enabled(ACCOUNT_REGISTRATION_KEY),
    )


async def enforce_new_account_policy(db: AsyncSession, is_new: bool) -> None:
    if is_new and not (await get_platform_auth_policy(db)).account_registration_enabled:
        await db.rollback()
        raise AccountRegistrationDisabled("Account registration is disabled")


async def enforce_sso_login_policy(db: AsyncSession, user, is_new: bool) -> None:
    """Apply account-creation and active-principal gates to every SSO login."""
    try:
        await require_active_authentication_principal(db, user)
    except HTTPException:
        await db.rollback()
        raise
    await enforce_new_account_policy(db, is_new)
