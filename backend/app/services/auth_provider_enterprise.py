"""Enterprise IM authentication provider implementations."""

from urllib.parse import quote, urlencode

import httpx
from fastapi import HTTPException
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.security import create_access_token, hash_password
from app.models.identity import IdentityProvider
from app.models.user import User, Identity
from app.services.auth_provider import BaseAuthProvider, ExternalUserInfo
from app.services.google_workspace_oauth import GOOGLE_HTTP_PROXY
from app.services.identity_provider_lookup import get_preferred_identity_provider
from loguru import logger

class FeishuAuthProvider(BaseAuthProvider):
    """Feishu (Lark) OAuth provider implementation."""

    provider_type = "feishu"

    FEISHU_TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v1/oidc/access_token"
    FEISHU_USER_INFO_URL = "https://open.feishu.cn/open-apis/authen/v1/user_info"
    FEISHU_APP_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal"

    def __init__(self, provider: IdentityProvider | None = None, config: dict | None = None):
        super().__init__(provider, config)
        self.app_id = self.config.get("app_id")
        self.app_secret = self.config.get("app_secret")
        self._app_access_token: str | None = None

    async def get_authorization_url(self, redirect_uri: str, state: str) -> str:
        app_id = self.app_id or ""
        base_url = "https://open.feishu.cn/open-apis/authen/v1/authorize"
        params = f"app_id={app_id}&redirect_uri={redirect_uri}&state={state}"
        return f"{base_url}?{params}"

    async def get_app_access_token(self) -> str:
        """Get or refresh the Feishu app access token.

        Cached in Redis (preferred) with in-memory fallback.
        Key: clawith:token:feishu_tenant:{app_id}
        TTL: 6900s (7200s validity - 5 min early refresh)
        """
        from app.core.token_cache import get_cached_token, set_cached_token

        cache_key = f"clawith:token:feishu_tenant:{self.app_id}"
        cached = await get_cached_token(cache_key)
        if cached:
            self._app_access_token = cached
            return cached

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self.FEISHU_APP_TOKEN_URL,
                json={"app_id": self.app_id, "app_secret": self.app_secret},
            )
            data = resp.json()
            token = data.get("app_access_token", "") or data.get("tenant_access_token", "")
            expire = data.get("expire", 7200)
            if token:
                ttl = max(expire - 300, 60)
                await set_cached_token(cache_key, token, ttl)
            self._app_access_token = token
            return token

    async def exchange_code_for_token(self, code: str, redirect_uri: str | None = None) -> dict:
        app_token = await self.get_app_access_token()

        async with httpx.AsyncClient() as client:
            token_resp = await client.post(
                self.FEISHU_TOKEN_URL,
                json={"grant_type": "authorization_code", "code": code},
                headers={"Authorization": f"Bearer {app_token}"},
            )
            token_data = token_resp.json()
            return token_data.get("data", {})

    async def get_user_info(self, access_token: str) -> ExternalUserInfo:
        async with httpx.AsyncClient() as client:
            info_resp = await client.get(
                self.FEISHU_USER_INFO_URL, headers={"Authorization": f"Bearer {access_token}"}
            )
            info_data = info_resp.json().get("data", {})
            open_id = info_data.get("open_id", "")
            provider_user_id = ""

            # Resolve stable employee user_id via contact API (open_id is per-app
            # and changes across apps; user_id is org-stable). Without this,
            # cross-app/SSO identity mapping breaks.
            if open_id:
                try:
                    app_token = await self.get_app_access_token()
                    if app_token:
                        contact_resp = await client.get(
                            f"https://open.feishu.cn/open-apis/contact/v3/users/{open_id}",
                            params={"user_id_type": "open_id"},
                            headers={"Authorization": f"Bearer {app_token}"},
                        )
                        contact_data = contact_resp.json()
                        if contact_data.get("code") == 0:
                            contact_user = contact_data.get("data", {}).get("user", {})
                            provider_user_id = contact_user.get("user_id", "")
                            if provider_user_id:
                                info_data["user_id"] = provider_user_id
                            if not info_data.get("email"):
                                info_data["email"] = (
                                    contact_user.get("email")
                                    or contact_user.get("enterprise_email")
                                    or ""
                                )
                            if not info_data.get("mobile"):
                                info_data["mobile"] = contact_user.get("mobile", "")
                except Exception as e:
                    logger.warning(f"Feishu contact lookup failed during SSO user info fetch: {e}")

            provider_user_id = provider_user_id or info_data.get("user_id", "") or open_id
            logger.info(f"Feishu user info: {info_data}")

            return ExternalUserInfo(
                provider_type=self.provider_type,
                provider_user_id=provider_user_id,
                provider_union_id=info_data.get("union_id"),
                name=info_data.get("name", ""),
                email=info_data.get("email", ""),
                avatar_url=info_data.get("avatar_url", ""),
                mobile=info_data.get("mobile", ""),
                raw_data=info_data,
            )

    async def _find_user_by_legacy_fields(self, db: AsyncSession, user_info: ExternalUserInfo) -> User | None:
        """Feishu legacy lookup removed (open_id/union_id no longer stored on User)."""
        return None

    async def _update_legacy_user_fields(self, user: User, user_info: ExternalUserInfo):
        """No-op: legacy Feishu fields removed from User."""
        return

    async def _set_legacy_user_fields(self, user: User, user_info: ExternalUserInfo):
        """No-op: legacy Feishu fields removed from User."""
        return


class DingTalkAuthProvider(BaseAuthProvider):
    """DingTalk OAuth provider implementation."""

    provider_type = "dingtalk"

    DINGTALK_TOKEN_URL = "https://api.dingtalk.com/v1.0/oauth2/userAccessToken"
    DINGTALK_USER_INFO_URL = "https://api.dingtalk.com/v1.0/contact/users/me"

    def __init__(self, provider: IdentityProvider | None = None, config: dict | None = None):
        super().__init__(provider, config)
        self.app_key = self.config.get("app_key")
        self.app_secret = self.config.get("app_secret")
        self.corp_id = self.config.get("corp_id")

    async def get_authorization_url(self, redirect_uri: str, state: str) -> str:
        app_id = self.app_key or ""
        base_url = "https://login.dingtalk.com/oauth2/auth"
        from urllib.parse import quote
        # Contact.User.Read is required for GET /v1.0/contact/users/me (user info on callback)
        # contact.user.mobile requires the fieldMobile permission in DingTalk console
        # fieldEmail requires the fieldEmail permission in DingTalk console
        scope = "openid corpid Contact.User.Read fieldEmail contact.user.mobile"
        params = (
            f"client_id={app_id}&redirect_uri={quote(redirect_uri)}&"
            f"state={state}&response_type=code&scope={quote(scope)}&prompt=consent"
        )
        # corp_id is optional: restricts the login page to a specific enterprise.
        # If not configured, DingTalk shows a company picker (still works for SSO).
        if self.corp_id:
            params = f"corpId={self.corp_id}&" + params
        return f"{base_url}?{params}"

    async def exchange_code_for_token(self, code: str, redirect_uri: str | None = None) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self.DINGTALK_TOKEN_URL,
                json={
                    "clientId": self.app_key,
                    "clientSecret": self.app_secret,
                    "code": code,
                    "grantType": "authorization_code",
                },
            )
            resp_data = resp.json()
            if resp.status_code != 200:
                logger.error(f"DingTalk token exchange failed (HTTP {resp.status_code}): {resp_data}")
                return {}

            # New DingTalk OAuth2 returns flat JSON with camelCase fields
            return {
                "access_token": resp_data.get("accessToken"),
                "refresh_token": resp_data.get("refreshToken"),
                "expires_in": resp_data.get("expireIn"),
            }

    async def get_user_info(self, access_token: str) -> ExternalUserInfo:
        async with httpx.AsyncClient() as client:
            headers = {"x-acs-dingtalk-access-token": access_token}
            info_resp = await client.get(self.DINGTALK_USER_INFO_URL, headers=headers)
            info_data = info_resp.json()
            if info_resp.status_code != 200:
                # Common error: errCode=403 means Contact.User.Read scope not granted.
                # Ensure 'Contact.User.Read' is included in the OAuth scope AND
                # that the app has been authorized by the employee in the login flow.
                err_msg = info_data.get('message') or info_data.get('errmsg') or str(info_data)
                logger.error(
                    f"DingTalk user info fetch failed (HTTP {info_resp.status_code}): {info_data}. "
                    "This usually means the 'Contact.User.Read' OAuth scope is missing from "
                    "the authorization URL, or the app lacks the corresponding permission."
                )
                raise Exception(f"Failed to fetch user info: {err_msg}")

            # DingTalk new OAuth2 returns openId, unionId, nick, avatarUrl, mobile, email
            logger.info(f"DingTalk user info: {info_data}")
            return ExternalUserInfo(
                provider_type=self.provider_type,
                provider_user_id=info_data.get("unionId"),
                provider_union_id=info_data.get("unionId"),
                name=info_data.get("nick", ""),
                email=info_data.get("email", ""),
                avatar_url=info_data.get("avatarUrl", ""),
                mobile=info_data.get("mobile", ""),
                raw_data=info_data,
            )


class WeComAuthProvider(BaseAuthProvider):
    """WeCom (Enterprise WeChat) OAuth provider implementation.

    Authentication flow:
    1. gettoken (corp_id + secret) -> access_token
    2. auth/getuserinfo (access_token + OAuth code) -> userid + user_ticket
    3. auth/getuserdetail (access_token + user_ticket) -> avatar, email, mobile
    4. user/get (access_token + userid) -> name, position (non-sensitive fields)

    Note: Steps 3 and 4 require the calling server IP to be whitelisted in the
    WeCom self-built app settings. This is a one-time setup per tenant.
    (Contrast with getuserinfo in step 2, which only requires trusted domain,
    not IP whitelist.)
    """

    provider_type = "wecom"

    # All WeCom self-built app API calls go to qyapi.weixin.qq.com
    # The old api.weixin.qq.com endpoints are legacy WeCom Public Account APIs
    # and no longer work for self-built apps.
    WECOM_TOKEN_URL = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
    WECOM_USER_INFO_URL = "https://qyapi.weixin.qq.com/cgi-bin/auth/getuserinfo"
    WECOM_USER_DETAIL_URL = "https://qyapi.weixin.qq.com/cgi-bin/auth/getuserdetail"
    WECOM_USER_GET_URL = "https://qyapi.weixin.qq.com/cgi-bin/user/get"

    def __init__(self, provider: IdentityProvider | None = None, config: dict | None = None):
        super().__init__(provider, config)
        # corp_id and agent_id are used for the OAuth redirect URL
        self.corp_id = self.config.get("corp_id") or self.config.get("app_id")
        # secret is the self-built app's AgentSecret (not the contact-sync secret)
        self.secret = self.config.get("secret") or self.config.get("app_secret")
        self.agent_id = self.config.get("agent_id")

    async def get_authorization_url(self, redirect_uri: str, state: str) -> str:
        """Construct the WeCom web-login SSO redirect URL.

        Uses the 'Scan QR Code to Login' flow (CorpPinCorp), which redirects users
        to authenticate with their WeCom account then returns them to redirect_uri
        with a code parameter.
        """
        from urllib.parse import quote
        base_url = "https://open.work.weixin.qq.com/wwlogin/sso/login"
        params = (
            f"loginType=CorpPinCorp"
            f"&appid={self.corp_id}"
            f"&agentid={self.agent_id}"
            f"&redirect_uri={quote(redirect_uri)}"
            f"&state={state}"
        )
        return f"{base_url}?{params}"

    async def exchange_code_for_token(self, code: str, redirect_uri: str | None = None) -> dict:
        """Exchange OAuth code for a packed token string containing all user data.

        Three sequential API calls:
          1. gettoken -> access_token
          2. auth/getuserinfo (code) -> userid + user_ticket
          3a. auth/getuserdetail (user_ticket) -> avatar, email, mobile [sensitive]
          3b. user/get (userid) -> name, position [non-sensitive, best-effort]

        Returns a packed JSON dict disguised as the access_token field so
        the existing BaseAuthProvider interface (get_user_info) can consume it.
        """
        import json

        async with httpx.AsyncClient(timeout=10) as client:
            # Step 1: Get app-level access token using corp credentials
            token_resp = await client.get(
                self.WECOM_TOKEN_URL,
                params={"corpid": self.corp_id, "corpsecret": self.secret},
            )
            token_data = token_resp.json()
            access_token = token_data.get("access_token")
            if not access_token:
                logger.error(f"[WeCom SSO] gettoken failed: {token_data}")
                return {}

            # Step 2: Exchange OAuth code for userid + user_ticket
            # auth/getuserinfo returns userid (lowercase 'u') for internal employees.
            # user_ticket is a temporary credential (valid ~1800s) representing
            # the employee's own OAuth authorization, required for sensitive fields.
            info_resp = await client.get(
                self.WECOM_USER_INFO_URL,
                params={"access_token": access_token, "code": code},
            )
            info_data = info_resp.json()
            # The key is lowercase 'userid' in the new auth endpoint (not 'UserId')
            userid = info_data.get("userid") or info_data.get("UserId", "")
            user_ticket = info_data.get("user_ticket", "")
            if not userid:
                logger.error(f"[WeCom SSO] getuserinfo missing userid: {info_data}")
                return {}

            # Step 3a: Fetch sensitive profile fields using user_ticket.
            # Since June 2022, new self-built apps cannot get avatar/email/mobile
            # from user/get directly. The user_ticket (from OAuth consent) unlocks them.
            # Returns: userid, gender, avatar, qr_code, mobile, email, biz_mail, address
            sensitive_data: dict = {}
            if user_ticket:
                try:
                    detail_resp = await client.post(
                        self.WECOM_USER_DETAIL_URL,
                        params={"access_token": access_token},
                        json={"user_ticket": user_ticket},
                    )
                    detail_json = detail_resp.json()
                    if detail_json.get("errcode") == 0:
                        sensitive_data = detail_json
                        logger.info(f"[WeCom SSO] getuserdetail succeeded for {userid}")
                    else:
                        logger.warning(f"[WeCom SSO] getuserdetail failed: {detail_json}")
                except Exception as e:
                    logger.warning(f"[WeCom SSO] getuserdetail error: {e}")
            else:
                logger.info(
                    f"[WeCom SSO] No user_ticket for {userid}; "
                    "sensitive fields (avatar/email/mobile) will be unavailable. "
                    "Ensure the WeCom app has 'snsapi_privateinfo' scope."
                )

            # Step 3b: Fetch non-sensitive profile fields from user/get (name, position).
            # These fields are NOT restricted by the June 2022 policy and are available
            # via the standard app access token (IP whitelist required).
            basic_data: dict = {}
            try:
                get_resp = await client.get(
                    self.WECOM_USER_GET_URL,
                    params={"access_token": access_token, "userid": userid},
                )
                get_json = get_resp.json()
                if get_json.get("errcode") == 0:
                    basic_data = get_json
                    logger.info(f"[WeCom SSO] user/get succeeded for {userid}")
                else:
                    logger.warning(f"[WeCom SSO] user/get failed: {get_json}")
            except Exception as e:
                logger.warning(f"[WeCom SSO] user/get error: {e}")

            # Pack all data for get_user_info() to consume
            packed_token = json.dumps({
                "userid": userid,
                "sensitive": sensitive_data,  # from getuserdetail (avatar, email, mobile)
                "basic": basic_data,           # from user/get (name, position)
            })
            return {"access_token": packed_token}

    async def get_user_info(self, access_token: str) -> ExternalUserInfo:
        """Parse the packed token into a standardized ExternalUserInfo.

        Priority for each field:
          - email: sensitive_data (getuserdetail) > biz_mail > basic_data (user/get)
          - avatar: sensitive_data > basic_data
          - mobile: sensitive_data only (restricted post-2022 in user/get)
          - name: basic_data (non-sensitive, from user/get)
        """
        import json
        try:
            data = json.loads(access_token)
            userid = data.get("userid", "")
            sensitive = data.get("sensitive", {})
            basic = data.get("basic", {})

            # Name from user/get (non-sensitive, always available when IP is whitelisted)
            name = basic.get("name") or f"WeCom {userid}"

            # Email: prefer personal email from getuserdetail, fall back to biz_mail
            email = (
                sensitive.get("email")
                or sensitive.get("biz_mail")
                or basic.get("email")
                or basic.get("biz_mail")
                or ""
            )

            # Avatar from getuserdetail (restricted post-2022 in user/get)
            avatar_url = sensitive.get("avatar") or basic.get("avatar") or ""

            # Mobile only from getuserdetail (restricted post-2022 in user/get)
            mobile = sensitive.get("mobile") or ""

            # Merge raw_data so OrgMember has full context
            raw = {**basic, **sensitive, "userid": userid}

            return ExternalUserInfo(
                provider_type=self.provider_type,
                provider_user_id=userid,
                name=name,
                email=email,
                avatar_url=avatar_url,
                mobile=mobile,
                raw_data=raw,
            )
        except Exception as e:
            logger.error(f"[WeCom SSO] get_user_info parse error: {e}")
            return ExternalUserInfo(
                provider_type=self.provider_type,
                provider_user_id="",
                name="",
                raw_data={"error": str(e)},
            )
