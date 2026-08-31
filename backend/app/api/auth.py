"""Authentication API routes."""

from importlib import import_module
from types import ModuleType

from fastapi import APIRouter

from app.api import (
    auth_email_verification as _auth_email_verification,
    auth_login as _auth_login,
    auth_oauth as _auth_oauth,
    auth_profile as _auth_profile,
    auth_recovery as _auth_recovery,
    auth_registration as _auth_registration,
)
from app.api.auth_email_verification import resend_verification, verify_email, router as email_verification_router
from app.api.auth_login import login, router as login_router
from app.api.auth_oauth import (
    _cache_oauth_pending,
    _get_oauth_pending,
    authorize,
    bind_identity,
    exchange_auth_code,
    list_providers,
    oauth_callback,
    router as oauth_router,
    unbind_identity,
)
from app.api.auth_profile import change_password, get_me, get_my_tenants, switch_tenant, update_me, router as profile_router
from app.api.auth_recovery import forgot_password, get_email_hint, reset_password, router as recovery_router
from app.api.auth_registration import (
    _handle_normal_register,
    _handle_sso_register,
    _send_verification_email_task,
    check_duplicate,
    get_registration_config,
    register,
    register_init,
    register_sso,
    router as registration_router,
)
from app.core.security import create_access_token, get_authenticated_user, get_current_user
from app.schemas.schemas import IdentityOut, TokenResponse, UserOut

router = APIRouter(prefix="/auth")


class _AuthModuleAttrProxy:
    def __init__(self, attr_name: str):
        self._attr_name = attr_name

    def _target(self):
        return getattr(import_module("app.api.auth"), self._attr_name)

    def __call__(self, *args, **kwargs):
        return self._target()(*args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._target(), name)

    def __repr__(self) -> str:
        return repr(self._target())


def _prepare_auth_module(module: ModuleType, *, proxy_names: tuple[str, ...], endpoint_names: tuple[str, ...]) -> None:
    for proxy_name in proxy_names:
        module.__dict__[proxy_name] = _AuthModuleAttrProxy(proxy_name)
    for endpoint_name in endpoint_names:
        module.__dict__[endpoint_name].__module__ = __name__


_prepare_auth_module(
    _auth_registration,
    proxy_names=("create_access_token", "TokenResponse", "UserOut", "_send_verification_email_task"),
    endpoint_names=("get_registration_config", "check_duplicate", "register", "register_init", "register_sso"),
)
_prepare_auth_module(
    _auth_login,
    proxy_names=("create_access_token", "TokenResponse", "UserOut", "IdentityOut", "_send_verification_email_task"),
    endpoint_names=("login",),
)
_prepare_auth_module(
    _auth_recovery,
    proxy_names=(),
    endpoint_names=("get_email_hint", "forgot_password", "reset_password"),
)
_prepare_auth_module(
    _auth_profile,
    proxy_names=("create_access_token", "UserOut"),
    endpoint_names=("get_me", "update_me", "get_my_tenants", "switch_tenant", "change_password"),
)
_prepare_auth_module(
    _auth_oauth,
    proxy_names=("create_access_token", "TokenResponse", "UserOut"),
    endpoint_names=("list_providers", "authorize", "exchange_auth_code", "oauth_callback", "bind_identity", "unbind_identity"),
)
_prepare_auth_module(
    _auth_email_verification,
    proxy_names=("create_access_token", "TokenResponse", "UserOut", "IdentityOut", "_send_verification_email_task"),
    endpoint_names=("verify_email", "resend_verification"),
)

router.include_router(registration_router, tags=["auth"])
router.include_router(login_router, tags=["auth"])
router.include_router(recovery_router, tags=["auth"])
router.include_router(profile_router, tags=["auth"])
router.include_router(oauth_router, tags=["auth"])
router.include_router(email_verification_router, tags=["auth"])
