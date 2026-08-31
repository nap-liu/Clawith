"""WeCom organization-directory sync adapter."""

import uuid
from datetime import datetime, timedelta

import httpx

from app.models.identity import IdentityProvider
from app.services.org_sync_base import BaseOrgSyncAdapter
from app.services.org_sync_models import ExternalDepartment, ExternalUser


class WeComOrgSyncAdapter(BaseOrgSyncAdapter):
    """WeCom organization sync adapter."""

    provider_type = "wecom"

    WECOM_API_URL = "https://qyapi.weixin.qq.com"
    WECOM_TOKEN_URL = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
    # Use simplelist (newer API) instead of the deprecated department/list.
    # The simplelist endpoint is accessible to the contact assistant token
    # (obtained via the 通讯录同步 Secret) without requiring app-level IP whitelist.
    WECOM_DEPT_LIST_URL = "https://qyapi.weixin.qq.com/cgi-bin/department/simplelist"
    WECOM_USER_LIST_URL = "https://qyapi.weixin.qq.com/cgi-bin/user/list"
    # Fallback APIs for contact assistant token (cannot call user/list):
    # list_id returns {userid, open_userid} for all dept members
    # user/get returns full details for a single user by userid
    WECOM_USER_LIST_ID_URL = "https://qyapi.weixin.qq.com/cgi-bin/user/list_id"
    WECOM_USER_GET_URL = "https://qyapi.weixin.qq.com/cgi-bin/user/get"

    def __init__(self, provider: IdentityProvider | None = None, config: dict | None = None, tenant_id: uuid.UUID | None = None):
        super().__init__(provider, config, tenant_id)
        # corp_id: the enterprise's WeCom corp ID
        # secret: the 通讯录同步 (contact-sync) secret — used for department/simplelist and user/list_id
        self.corp_id = self.config.get("corp_id") or self.config.get("app_id") or self.config.get("corpid")
        self.secret = self.config.get("secret") or self.config.get("app_secret") or self.config.get("corpsecret")
        self._access_token: str | None = None
        self._token_expires_at: datetime | None = None

    async def _fetch_token(self, corp_id: str, secret: str) -> str:
        """Fetch a fresh WeCom access_token for the given corp_id/secret pair."""
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                self.WECOM_TOKEN_URL,
                params={"corpid": corp_id, "corpsecret": secret},
            )
            data = resp.json()
            if data.get("errcode") == 0:
                return data.get("access_token") or ""
            raise RuntimeError(f"[WeCom] gettoken failed for corpid={corp_id}: {data}")

    @property
    def api_base_url(self) -> str:
        return self.WECOM_API_URL

    async def get_access_token(self) -> str:
        """Get valid access token for WeCom 通讯录同步 (contact-sync) API.

        This token can call department/simplelist and user/list_id.
        It cannot call user/list or user/get (those raise errcode 48009).
        Cached in Redis (preferred) with in-memory fallback.
        """
        from app.core.token_cache import get_cached_token, set_cached_token

        _corp_key = self.corp_id or self.bot_id or ""
        cache_key = f"clawith:token:wecom:{_corp_key}"

        if _corp_key:
            cached = await get_cached_token(cache_key)
            if cached:
                self._access_token = cached
                return cached

        if self.corp_id and self.secret:
            token = await self._fetch_token(self.corp_id, self.secret)
            if token:
                ttl = max(7200 - 300, 300)
                await set_cached_token(cache_key, token, ttl)
                self._access_token = token
                self._token_expires_at = datetime.now() + timedelta(seconds=ttl)
            return token

        if not self.corp_id and self.bot_id and self.bot_secret:
            token = await self._fetch_token(self.bot_id, self.bot_secret)
            if token:
                ttl = max(7200 - 300, 300)
                await set_cached_token(cache_key, token, ttl)
                self._access_token = token
                self._token_expires_at = datetime.now() + timedelta(seconds=ttl)
            return token

        raise ValueError("WeCom corp_id or secret missing in provider config")


    async def fetch_departments(self) -> list[ExternalDepartment]:
        """Fetch all departments from WeCom using the simplelist endpoint.

        department/simplelist is accessible to the 通讯录助手 (contact assistant)
        token obtained from the 通讯录同步 Secret, unlike the deprecated
        department/list which requires strict app-level IP whitelist.
        """
        token = await self.get_access_token()
        all_depts: list[ExternalDepartment] = []

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                self.WECOM_DEPT_LIST_URL,
                # id omitted → returns all departments
                params={"access_token": token},
            )
            data = resp.json()
            if data.get("errcode") != 0:
                raise RuntimeError(f"WeCom department list error: {data.get('errmsg') or data}")

            # simplelist response: {"department_id": [{"id":x, "parentid":x, "name":…, "order":…}]}
            items = data.get("department_id", []) or data.get("department", [])
            for item in items:
                dept_id = str(item.get("id"))
                parentid = item.get("parentid", 0)
                parent_id = str(parentid) if parentid and parentid != 0 else None

                all_depts.append(
                    ExternalDepartment(
                        external_id=dept_id,
                        name=item.get("name", ""),
                        parent_external_id=parent_id,
                        member_count=0,  # simplelist does not return member count
                        raw_data=item,
                    )
                )
        return all_depts

    async def fetch_users(self, department_external_id: str) -> list[ExternalUser]:
        """Fetch user stubs for a department using user/list_id.

        WeCom API strategy for org sync:
        - user/list  (bulk detail) → errcode 48009 for contact-sync token; removed.
        - user/get   (per-user detail) → IP-whitelisted only; removed.
        - user/list_id (ID only)   → works with contact-sync token; used here.

        Only userid and open_userid are obtained in org sync. Full profile
        data (name, avatar, email, mobile) is enriched passively when each
        user completes their first WeCom SSO login (via auth/getuserdetail).
        """
        token = await self.get_access_token()
        return await self._fetch_user_stubs(token, department_external_id)

    async def _fetch_user_stubs(self, sync_token: str, department_external_id: str) -> list[ExternalUser]:
        """Fetch minimal user stubs via user/list_id.

        Returns placeholder ExternalUser objects with only userid and open_userid
        populated. The name is intentionally set to the userid so the passive
        SSO enrichment in sso_service.link_identity() can detect the placeholder
        and overwrite it with the real name from auth/getuserdetail.
        """
        user_stubs: list[ExternalUser] = []
        cursor = ""

        async with httpx.AsyncClient(timeout=15) as client:
            while True:
                params: dict = {
                    "access_token": sync_token,
                    "department_id": department_external_id,
                    "limit": 1000,
                }
                if cursor:
                    params["cursor"] = cursor

                resp = await client.get(self.WECOM_USER_LIST_ID_URL, params=params)
                data = resp.json()
                if data.get("errcode") != 0:
                    raise RuntimeError(f"WeCom user/list_id error: {data.get('errmsg') or data}")

                for entry in data.get("dept_user", []):
                    uid = entry.get("userid", "")
                    if not uid:
                        continue
                    # Use userid as the name placeholder so link_identity() knows
                    # to overwrite it once the user logs in via SSO.
                    user_stubs.append(ExternalUser(
                        external_id=uid,
                        name=uid,  # placeholder — enriched on first SSO login
                        open_id=entry.get("open_userid", ""),
                        department_external_id=department_external_id,
                        department_ids=[department_external_id],
                    ))

                cursor = data.get("next_cursor", "")
                if not cursor:
                    break

        return user_stubs



