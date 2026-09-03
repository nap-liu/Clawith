"""Generic and public OAuth provider implementations."""

from urllib.parse import urlencode

import httpx
from fastapi import HTTPException
from typing import Any

from sqlalchemy import select

from app.models.identity import IdentityProvider
from app.models.user import User, Identity
from app.services.auth_provider import BaseAuthProvider, ExternalUserInfo
from app.services.google_workspace_oauth import GOOGLE_HTTP_PROXY
from loguru import logger


_OAUTH_CREDENTIAL_KEYS = frozenset(
    {
        "accesstoken",
        "authorization",
        "clientsecret",
        "idtoken",
        "refreshtoken",
        "token",
    }
)


def _read_oauth_field_path(data: dict, path: str) -> Any:
    """Read a configured OAuth field from its complete dot-separated path."""
    segments = tuple(part for part in str(path).split(".") if part)

    def read(current: Any, remaining: tuple[str, ...]) -> Any:
        if not remaining:
            return current
        if isinstance(current, list):
            for item in current:
                value = read(item, remaining)
                if value not in (None, ""):
                    return value
            return None
        if not isinstance(current, dict) or remaining[0] not in current:
            return None
        return read(current[remaining[0]], remaining[1:])

    return read(data, segments)


def _without_oauth_credentials(value):
    """Recursively remove explicit OAuth credential fields from retained data."""
    if isinstance(value, dict):
        return {
            key: _without_oauth_credentials(item)
            for key, item in value.items()
            if str(key).lower().replace("_", "").replace("-", "")
            not in _OAUTH_CREDENTIAL_KEYS
        }
    if isinstance(value, list):
        return [_without_oauth_credentials(item) for item in value]
    return value


class OAuth2AuthProvider(BaseAuthProvider):
    """Generic OAuth2 provider implementation (RFC 6749 Authorization Code flow)."""

    provider_type = "oauth2"

    def __init__(self, provider=None, config=None):
        super().__init__(provider, config)
        self.client_id = self.config.get("client_id") or self.config.get("app_id", "")
        self.client_secret = self.config.get("client_secret") or self.config.get("app_secret", "")
        self.authorize_url = self.config.get("authorize_url", "")
        self.scope = self.config.get("scope", "")

        # 自动推导 token_url 和 user_info_url（如果为空）
        base = self.authorize_url.rsplit("/", 1)[0] if self.authorize_url else ""
        self.token_url = self.config.get("token_url") or f"{base}/token"
        self.user_info_url = self.config.get("user_info_url") or f"{base}/userinfo"

        # 字段映射配置（用户自定义）
        self.field_mapping = self.config.get("field_mapping") or {}

        # 标准 OIDC 字段 fallback 顺序
        self.FIELD_DEFAULTS = {
            "user_id": ["sub", "userId", "id"],
            "name": ["name", "userName", "preferred_username", "nickname"],
            "email": ["email"],
            "mobile": ["phone_number", "mobile", "phone"],
            "avatar": ["picture", "avatar_url", "avatar"],
        }

    async def get_authorization_url(self, redirect_uri: str, state: str) -> str:
        scope = " ".join(self.scope.replace("+", " ").split())
        params = urlencode(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": redirect_uri,
                "scope": scope,
                "state": state,
            }
        )
        return f"{self.authorize_url}?{params}"

    async def exchange_code_for_token(self, code: str, redirect_uri: str | None = None) -> dict:
        import base64
        credentials = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        data = {
            "grant_type": "authorization_code",
            "code": code,
        }
        if redirect_uri and self.config.get("token_exchange_redirect_uri", True) is not False:
            data["redirect_uri"] = redirect_uri

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self.token_url,
                headers={
                    "Authorization": f"Basic {credentials}",
                },
                data=data,
            )
            if resp.status_code != 200:
                logger.error("OAuth2 token exchange failed (HTTP {})", resp.status_code)
                return {}
            return resp.json()

    def _get_field(self, data: dict, field_key: str) -> str:
        """Read a configured path exactly, or use standards only when unconfigured."""
        def read(path: str) -> Any:
            return _read_oauth_field_path(data, path)

        # 1. 优先用用户配置的完整映射路径
        custom_key = self.field_mapping.get(field_key)
        if custom_key:
            value = read(custom_key)
            return "" if value in (None, "") else str(value)
        # 2. 未配置时才依次尝试标准 OIDC 根路径。
        for std_key in self.FIELD_DEFAULTS.get(field_key, []):
            value = read(std_key)
            if value not in (None, ""):
                return str(value)
        return ""

    async def get_user_info(self, access_token: str) -> ExternalUserInfo:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                self.user_info_url,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            resp_data = resp.json()
            if (
                200 <= getattr(resp, "status_code", 200) < 300
                and self.provider is not None
                and getattr(self.provider, "id", None)
            ):
                from app.services.provider_field_discovery import (
                    cache_oidc_field_samples,
                )

                await cache_oidc_field_samples(str(self.provider.id), resp_data)

            # Handle case where userinfo returns None or empty
            if not resp_data:
                logger.warning(f"OAuth2 userinfo returned empty/null for {self.provider_type}")
                info = {}
            elif "data" in resp_data and isinstance(resp_data["data"], dict):
                info = resp_data["data"]
            else:
                info = resp_data

            logger.info("OAuth2 user info received for provider={}", self.provider_type)

            # 通用字段解析（优先用户自定义映射，再 fallback 到标准字段）
            user_id = self._get_field(resp_data, "user_id")
            name = self._get_field(resp_data, "name")
            email = self._get_field(resp_data, "email")
            mobile = self._get_field(resp_data, "mobile")
            avatar_url = self._get_field(resp_data, "avatar")

            return ExternalUserInfo(
                provider_type=self.provider_type,
                provider_user_id=str(user_id),
                name=name,
                email=email,
                mobile=mobile,
                avatar_url=avatar_url,
                raw_data=info,
            )

    async def get_user_info_from_token_data(self, token_data: dict) -> ExternalUserInfo:
        """Extract user info from token exchange response (fallback when userinfo endpoint fails)."""
        if self.provider is not None and getattr(self.provider, "id", None):
            from app.services.provider_field_discovery import cache_oidc_field_samples

            await cache_oidc_field_samples(str(self.provider.id), token_data)
        info = token_data.copy()
        if "userInfo" in info and isinstance(info["userInfo"], dict):
            info = {**info, **info["userInfo"]}
        logger.info(
            "OAuth2 fallback user info received from token response for provider={}",
            self.provider_type,
        )
        user_id = self._get_field(info, "user_id") or info.get("openid", "")
        name = self._get_field(info, "name")
        email = self._get_field(info, "email")
        mobile = self._get_field(info, "mobile")
        avatar_url = self._get_field(info, "avatar")
        return ExternalUserInfo(
            provider_type=self.provider_type,
            provider_user_id=str(user_id),
            name=name,
            email=email,
            mobile=mobile,
            avatar_url=avatar_url,
            raw_data=_without_oauth_credentials(info),
        )

    async def _create_new_user(self, db, user_info, tenant_id):
        """Override to use provider_user_id as username for OAuth2."""
        from app.services.registration_service import registration_service
        # 优先用 provider_user_id（如 userId="zhangsan"），再用 email 前缀，最后 fallback
        username = (
            user_info.provider_user_id
            or (user_info.email.split("@")[0] if user_info.email else None)
            or "oauth2_user"
        )

        # Check username uniqueness via Identity
        existing = await db.execute(
            select(User).join(User.identity).where(
                Identity.username == username,
            )
        )
        if existing.scalar_one_or_none():
            logger.error(
                f"[OAuth2] Username conflict detected: {username} already exists in tenant {tenant_id}. "
                f"This should not happen if find_or_create_user match chain is working correctly."
            )
            raise HTTPException(
                status_code=409,
                detail=f"Username '{username}' already exists. Please contact administrator."
            )

        email = user_info.email or f"{username}@oauth2.local"

        # Create or find Identity
        identity = await registration_service.find_or_create_identity(
            db,
            email=email,
            phone=user_info.mobile,
            username=username,
            password=None,
            provider=self.provider,
        )

        # Create tenant-scoped User
        user = User(
            identity_id=identity.id,
            display_name=user_info.name or username,
            avatar_url=user_info.avatar_url,
            registration_source=self.provider_type,
            tenant_id=tenant_id,
            is_active=True,
        )

        db.add(user)
        await db.flush()

        # Preload identity
        user.identity = identity
        return user

class GoogleWorkspaceAuthProvider(BaseAuthProvider):
    """Google Workspace OAuth provider implementation for SSO login."""

    provider_type = "google_workspace"

    GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
    GOOGLE_USER_INFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
    GOOGLE_SSO_SCOPE = "openid email profile"
    GOOGLE_ADMIN_SCOPES = [
        "openid",
        "email",
        "profile",
        "https://www.googleapis.com/auth/admin.directory.user.readonly",
        "https://www.googleapis.com/auth/admin.directory.orgunit.readonly",
    ]

    def __init__(self, provider: IdentityProvider | None = None, config: dict | None = None):
        super().__init__(provider, config)
        self.client_id = self.config.get("client_id") or self.config.get("sso_client_id") or self.config.get("app_id")
        self.client_secret = self.config.get("client_secret") or self.config.get("sso_client_secret") or self.config.get("app_secret")
        self.scope = self.config.get("sso_scope") or self.config.get("scope") or self.GOOGLE_SSO_SCOPE

    def _build_authorization_url(
        self,
        redirect_uri: str,
        state: str,
        *,
        scopes: str | list[str] | None = None,
        access_type: str = "online",
        prompt: str = "select_account",
    ) -> str:
        from urllib.parse import quote

        scope_value = scopes or self.scope
        if isinstance(scope_value, list):
            scope_value = " ".join(scope_value)

        self.config["redirect_uri"] = redirect_uri
        params = (
            f"client_id={quote(self.client_id or '')}"
            f"&redirect_uri={quote(redirect_uri)}"
            f"&response_type=code"
            f"&scope={quote(scope_value)}"
            f"&state={quote(state or '')}"
            f"&access_type={quote(access_type)}"
            f"&include_granted_scopes=true"
            f"&prompt={quote(prompt)}"
        )
        return f"{self.GOOGLE_AUTHORIZE_URL}?{params}"

    async def get_authorization_url(self, redirect_uri: str, state: str) -> str:
        return self._build_authorization_url(
            redirect_uri,
            state,
            scopes=self.scope,
            access_type="online",
            prompt="select_account",
        )

    async def get_admin_authorization_url(self, redirect_uri: str, state: str) -> str:
        return self._build_authorization_url(
            redirect_uri,
            state,
            scopes=self.GOOGLE_ADMIN_SCOPES,
            access_type="offline",
            prompt="consent",
        )

    async def exchange_code_for_token(self, code: str, redirect_uri: str | None = None) -> dict:
        async with httpx.AsyncClient(timeout=15, proxy=GOOGLE_HTTP_PROXY) as client:
            resp = await client.post(
                self.GOOGLE_TOKEN_URL,
                data={
                    "code": code,
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri or self.config.get("redirect_uri"),
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            return resp.json()

    async def refresh_access_token(self, refresh_token: str) -> dict:
        async with httpx.AsyncClient(timeout=15, proxy=GOOGLE_HTTP_PROXY) as client:
            resp = await client.post(
                self.GOOGLE_TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            return resp.json()

    async def fetch_openid_profile(self, access_token: str) -> dict:
        async with httpx.AsyncClient(timeout=15, proxy=GOOGLE_HTTP_PROXY) as client:
            resp = await client.get(
                self.GOOGLE_USER_INFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            resp.raise_for_status()
            return resp.json()

    async def get_user_info(self, access_token: str) -> ExternalUserInfo:
        info = await self.fetch_openid_profile(access_token)
        return ExternalUserInfo(
            provider_type=self.provider_type,
            provider_user_id=info.get("sub", ""),
            name=info.get("name", "") or info.get("email", ""),
            email=info.get("email", ""),
            avatar_url=info.get("picture", ""),
            raw_data=info,
        )


class MicrosoftTeamsAuthProvider(BaseAuthProvider):
    """Microsoft Teams OAuth provider implementation."""

    provider_type = "microsoft_teams"

    # Will be implemented when needed
    async def get_authorization_url(self, redirect_uri: str, state: str) -> str:
        raise NotImplementedError("Microsoft Teams OAuth not yet implemented")

    async def exchange_code_for_token(self, code: str, redirect_uri: str | None = None) -> dict:
        raise NotImplementedError("Microsoft Teams OAuth not yet implemented")

    async def get_user_info(self, access_token: str) -> ExternalUserInfo:
        raise NotImplementedError("Microsoft Teams OAuth not yet implemented")


class GoogleAuthProvider(BaseAuthProvider):
    """Google OAuth provider implementation."""

    provider_type = "google"

    GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
    GOOGLE_USER_INFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

    def __init__(self, provider: IdentityProvider | None = None, config: dict | None = None):
        super().__init__(provider, config)
        self.client_id = self.config.get("client_id") or self.config.get("app_id")
        self.client_secret = self.config.get("client_secret") or self.config.get("app_secret")
        self.scope = self.config.get("scope") or "openid profile email"

    async def get_authorization_url(self, redirect_uri: str, state: str) -> str:
        params = {
            "client_id": self.client_id or "",
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": self.scope,
            "access_type": "offline",
            "prompt": "consent",
        }
        if state:
            params["state"] = state
        return f"{self.GOOGLE_AUTHORIZE_URL}?{urlencode(params)}"

    async def exchange_code_for_token(self, code: str, redirect_uri: str | None = None) -> dict:
        async with httpx.AsyncClient(timeout=15, proxy=GOOGLE_HTTP_PROXY) as client:
            resp = await client.post(
                self.GOOGLE_TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "code": code,
                    "redirect_uri": redirect_uri or "",
                    "grant_type": "authorization_code",
                },
            )
            data = resp.json()
            if resp.status_code != 200:
                logger.error(f"Google token exchange failed (HTTP {resp.status_code}): {data}")
                return {}
            return data

    async def get_user_info(self, access_token: str) -> ExternalUserInfo:
        async with httpx.AsyncClient(timeout=15, proxy=GOOGLE_HTTP_PROXY) as client:
            resp = await client.get(
                self.GOOGLE_USER_INFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            data = resp.json()
            if resp.status_code != 200:
                raise Exception(data.get("error_description") or data.get("error") or "Failed to fetch Google user info")

            return ExternalUserInfo(
                provider_type=self.provider_type,
                provider_user_id=data.get("sub", ""),
                name=data.get("name", ""),
                email=data.get("email", ""),
                avatar_url=data.get("picture", ""),
                raw_data=data,
            )


class GitHubAuthProvider(BaseAuthProvider):
    """GitHub OAuth provider implementation."""

    provider_type = "github"

    GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
    GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
    GITHUB_USER_INFO_URL = "https://api.github.com/user"
    GITHUB_EMAILS_URL = "https://api.github.com/user/emails"

    def __init__(self, provider: IdentityProvider | None = None, config: dict | None = None):
        super().__init__(provider, config)
        self.client_id = self.config.get("client_id") or self.config.get("app_id")
        self.client_secret = self.config.get("client_secret") or self.config.get("app_secret")
        self.scope = self.config.get("scope") or "read:user user:email"

    async def get_authorization_url(self, redirect_uri: str, state: str) -> str:
        params = {
            "client_id": self.client_id or "",
            "redirect_uri": redirect_uri,
            "scope": self.scope,
        }
        if state:
            params["state"] = state
        return f"{self.GITHUB_AUTHORIZE_URL}?{urlencode(params)}"

    async def exchange_code_for_token(self, code: str, redirect_uri: str | None = None) -> dict:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                self.GITHUB_TOKEN_URL,
                headers={"Accept": "application/json"},
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "code": code,
                },
            )
            data = resp.json()
            if resp.status_code != 200:
                logger.error(f"GitHub token exchange failed (HTTP {resp.status_code}): {data}")
                return {}
            return data

    async def get_user_info(self, access_token: str) -> ExternalUserInfo:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
        }
        async with httpx.AsyncClient(timeout=15) as client:
            user_resp = await client.get(self.GITHUB_USER_INFO_URL, headers=headers)
            user_data = user_resp.json()
            if user_resp.status_code != 200:
                raise Exception(user_data.get("message") or "Failed to fetch GitHub user info")

            email = user_data.get("email") or ""
            if not email:
                emails_resp = await client.get(self.GITHUB_EMAILS_URL, headers=headers)
                emails_data = emails_resp.json()
                if emails_resp.status_code == 200 and isinstance(emails_data, list):
                    primary = next((item for item in emails_data if item.get("primary")), None)
                    verified = next((item for item in emails_data if item.get("verified")), None)
                    fallback = primary or verified or (emails_data[0] if emails_data else {})
                    email = fallback.get("email", "")

            return ExternalUserInfo(
                provider_type=self.provider_type,
                provider_user_id=str(user_data.get("id", "")),
                name=user_data.get("name") or user_data.get("login") or "",
                email=email,
                avatar_url=user_data.get("avatar_url", ""),
                raw_data=user_data,
            )
