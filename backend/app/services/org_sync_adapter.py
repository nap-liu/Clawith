"""Organization sync adapter compatibility entry point and provider registry."""

import asyncio
import uuid
from datetime import datetime, timedelta

import httpx
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.identity import IdentityProvider
from app.services.org_sync_base import BaseOrgSyncAdapter
from app.services.org_sync_feishu import FeishuOrgSyncAdapter
from app.services.org_sync_google_workspace import GoogleWorkspaceOrgSyncAdapter
from app.services.org_sync_models import (
    ExternalDepartment,
    ExternalUser,
    _normalize_contact,
    _utcnow,
    build_department_path_map,
    derive_member_department_paths,
    is_virtual_directory_root_values,
    normalize_contact_for_match,
)
from app.services.org_sync_wecom import WeComOrgSyncAdapter


class DingTalkOrgSyncAdapter(BaseOrgSyncAdapter):
    """DingTalk organization sync adapter."""

    provider_type = "dingtalk"

    DINGTALK_API_URL = "https://oapi.dingtalk.com"
    DINGTALK_TOKEN_URL = "https://oapi.dingtalk.com/gettoken"
    DINGTALK_AUTH_SCOPES_URL = "https://oapi.dingtalk.com/topapi/auth/scopes"
    DINGTALK_AUTH_SCOPES_LEGACY_URL = "https://oapi.dingtalk.com/auth/scopes"
    DINGTALK_DEPT_LIST_URL = "https://oapi.dingtalk.com/topapi/v2/department/listsub"
    DINGTALK_DEPT_GET_URL = "https://oapi.dingtalk.com/topapi/v2/department/get"
    DINGTALK_USER_LIST_URL = "https://oapi.dingtalk.com/topapi/v2/user/list"
    DINGTALK_REQUEST_INTERVAL_SECONDS = 0.1
    DINGTALK_RATE_LIMIT_RETRY_SECONDS = 1.0
    DINGTALK_MAX_RATE_LIMIT_RETRIES = 5
    DINGTALK_DEFAULT_USER_FETCH_SKIP_DEPARTMENT_NAMES: tuple[str, ...] = ()

    def __init__(self, provider: IdentityProvider | None = None, config: dict | None = None, tenant_id: uuid.UUID | None = None):
        super().__init__(provider, config, tenant_id)
        self.app_key = self.config.get("app_key") or self.config.get("appkey") or self.config.get("app_id")
        self.app_secret = self.config.get("app_secret") or self.config.get("appsecret") or self.config.get("app_secret_key")
        self._access_token: str | None = None
        self._token_expires_at: datetime | None = None
        self._dept_path_map: dict[str, str] = {}

    def _configured_user_fetch_skip_department_names(self) -> set[str]:
        raw_names = (self.config or {}).get("skip_user_fetch_department_names")
        if raw_names is None:
            raw_names = self.DINGTALK_DEFAULT_USER_FETCH_SKIP_DEPARTMENT_NAMES
        if isinstance(raw_names, str):
            raw_names = raw_names.split(",")
        return {str(name).strip() for name in (raw_names or []) if str(name).strip()}

    def _is_user_fetch_skipped_department_name(self, name: str | None) -> bool:
        return str(name or "").strip() in self._configured_user_fetch_skip_department_names()

    def _should_skip_department_user_fetch(self, dept: ExternalDepartment) -> bool:
        skip_names = self._configured_user_fetch_skip_department_names()
        if not skip_names:
            return False

        path = self._dept_path_map.get(str(dept.external_id), "")
        segments = [str(dept.name or "").strip()]
        segments.extend(part.strip() for part in path.split("/") if part.strip())
        return any(segment in skip_names for segment in segments)

    @property
    def api_base_url(self) -> str:
        return self.DINGTALK_API_URL

    async def get_access_token(self) -> str:
        """Get or refresh the DingTalk access token.

        Cached in Redis (preferred) with in-memory fallback.
        Key: clawith:token:dingtalk_corp:{app_key}
        TTL: expires_in - 300s (5 min early refresh)
        """
        from app.core.token_cache import get_cached_token, set_cached_token

        if not self.app_key or not self.app_secret:
            raise ValueError("DingTalk app_key/app_secret missing in provider config")

        cache_key = f"clawith:token:dingtalk_corp:{self.app_key}"
        cached = await get_cached_token(cache_key)
        if cached:
            self._access_token = cached
            return cached

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                self.DINGTALK_TOKEN_URL,
                params={"appkey": self.app_key, "appsecret": self.app_secret},
            )
            data = resp.json()
            if data.get("errcode") != 0:
                raise RuntimeError(f"DingTalk token error: {data.get('errmsg') or data}")
            token = data.get("access_token") or ""
            expires_in = int(data.get("expires_in") or 7200)
            if token:
                ttl = max(expires_in - 300, 60)
                await set_cached_token(cache_key, token, ttl)
                self._access_token = token
                self._token_expires_at = datetime.now() + timedelta(seconds=max(expires_in - 60, 60))
            return token

    async def fetch_departments(self) -> list[ExternalDepartment]:
        token = await self.get_access_token()
        all_depts: list[ExternalDepartment] = []
        # dept_index: external_id -> (name, parent_external_id_str | None)
        dept_index: dict[str, tuple[str, str | None]] = {}
        added_dept_ids: set[str] = set()

        def add_department(item: dict) -> int | None:
            raw_dept_id = item.get("dept_id") or item.get("id")
            if raw_dept_id is None:
                return None

            dept_id = int(raw_dept_id)
            external_id = str(dept_id)
            dept_name = item.get("name") or f"Department {dept_id}"

            raw_parent_id = item.get("parent_id") or item.get("parentid")
            if dept_id == 1 or not raw_parent_id or int(raw_parent_id) == dept_id:
                parent_external = None
            else:
                parent_external = str(int(raw_parent_id))

            dept_index[external_id] = (dept_name, parent_external)
            if external_id not in added_dept_ids:
                added_dept_ids.add(external_id)
                all_depts.append(
                    ExternalDepartment(
                        external_id=external_id,
                        name=dept_name,
                        parent_external_id=parent_external,
                        member_count=item.get("member_count", 0) or 0,
                        raw_data=item,
                    )
                )
            return dept_id

        seen: set[int] = set()
        queue: list[int] = []
        _request_count = 0

        async with httpx.AsyncClient() as client:
            authorized_dept_ids = await self._fetch_authorized_department_ids(client, token)
            if not authorized_dept_ids:
                raise RuntimeError(
                    "DingTalk app has no authorized departments. "
                    "Please authorize at least one department in DingTalk Contacts permissions."
                )

            for dept_id in authorized_dept_ids:
                dept_detail = await self._fetch_department_detail(client, token, dept_id)
                if not dept_detail:
                    raise RuntimeError(
                        f"DingTalk authorized department {dept_id} detail is unavailable"
                    )
                add_department(dept_detail)
                if not self._is_user_fetch_skipped_department_name(dept_detail.get("name")):
                    queue.append(dept_id)

            while queue:
                parent_id = queue.pop(0)
                if parent_id in seen:
                    continue
                seen.add(parent_id)

                # DingTalk has tenant/app-level minute quotas; keep sync conservative.
                if _request_count > 0:
                    await asyncio.sleep(self.DINGTALK_REQUEST_INTERVAL_SECONDS)
                _request_count += 1

                data = await self._post_dingtalk_with_retry(
                    client,
                    self.DINGTALK_DEPT_LIST_URL,
                    token,
                    {"dept_id": parent_id},
                )
                if data.get("errcode") != 0:
                    raise RuntimeError(f"DingTalk department list error: {data.get('errmsg') or data}")

                result = data.get("result")
                if isinstance(result, list):
                    items = result
                elif isinstance(result, dict):
                    items = result.get("department", []) or []
                else:
                    items = []

                for item in items:
                    dept_id = add_department(item)
                    if dept_id is None:
                        continue
                    if self._is_user_fetch_skipped_department_name(item.get("name")):
                        logger.info(
                            "[OrgSync][DingTalk] Skipping department tree expansion for {} ({})",
                            dept_id,
                            item.get("name"),
                        )
                        continue
                    if dept_id not in seen:
                        queue.append(dept_id)

        authorized_external_ids = {str(dept_id) for dept_id in authorized_dept_ids}
        for department in all_depts:
            if (
                department.external_id in authorized_external_ids
                and department.parent_external_id
                and department.parent_external_id not in added_dept_ids
            ):
                department.parent_external_id = None
                dept_index[department.external_id] = (department.name, None)

        self._dept_path_map = self._build_dept_paths(dept_index)
        return all_depts

    async def _fetch_authorized_department_ids(self, client: httpx.AsyncClient, token: str) -> list[int]:
        resp = await client.post(
            self.DINGTALK_AUTH_SCOPES_URL,
            params={"access_token": token},
        )
        data = resp.json()
        if data.get("errcode") == 0:
            dept_ids = self._extract_authorized_department_ids(data)
            if dept_ids:
                return dept_ids
            logger.warning(
                "[OrgSync][DingTalk] topapi authorization scope returned no departments: {}",
                data,
            )
        else:
            logger.warning(
                "[OrgSync][DingTalk] topapi authorization scope failed, trying legacy endpoint: {}",
                data.get("errmsg") or data,
            )

        legacy_resp = await client.get(
            self.DINGTALK_AUTH_SCOPES_LEGACY_URL,
            params={"access_token": token},
        )
        legacy_data = legacy_resp.json()
        if legacy_data.get("errcode") == 0:
            return self._extract_authorized_department_ids(legacy_data)

        logger.error(
            "[OrgSync][DingTalk] Failed to fetch authorization scope; refusing root fallback. "
            "topapi={}, legacy={}",
            data.get("errmsg") or data,
            legacy_data.get("errmsg") or legacy_data,
        )
        raise RuntimeError(
            "DingTalk authorization scope could not be determined; directory sync stopped"
        )

    async def _post_dingtalk_with_retry(
        self,
        client: httpx.AsyncClient,
        url: str,
        token: str,
        body: dict,
    ) -> dict:
        for attempt in range(self.DINGTALK_MAX_RATE_LIMIT_RETRIES + 1):
            resp = await client.post(
                url,
                params={"access_token": token},
                json=body,
            )
            data = resp.json()
            if not self._is_rate_limited(data):
                return data

            if attempt >= self.DINGTALK_MAX_RATE_LIMIT_RETRIES:
                return data

            logger.warning(
                "[OrgSync][DingTalk] Rate limited by DingTalk API {}; retrying in {}s",
                url,
                self.DINGTALK_RATE_LIMIT_RETRY_SECONDS,
            )
            await asyncio.sleep(self.DINGTALK_RATE_LIMIT_RETRY_SECONDS)

        return data

    async def _fetch_department_detail(self, client: httpx.AsyncClient, token: str, dept_id: int) -> dict | None:
        data = await self._post_dingtalk_with_retry(
            client,
            self.DINGTALK_DEPT_GET_URL,
            token,
            {"dept_id": dept_id},
        )
        if data.get("errcode") != 0:
            logger.warning(
                "[OrgSync][DingTalk] Failed to fetch department detail for %s: %s",
                dept_id,
                data.get("errmsg") or data,
            )
            return None
        result = data.get("result") or {}
        return result if isinstance(result, dict) else None

    @staticmethod
    def _extract_authorized_department_ids(data: dict) -> list[int]:
        payload = data.get("result") if isinstance(data.get("result"), dict) else data
        auth_org_scopes = payload.get("auth_org_scopes") or {}
        dept_ids = auth_org_scopes.get("authed_dept") or auth_org_scopes.get("authed_depts") or []

        result: list[int] = []
        seen: set[int] = set()
        for dept_id in dept_ids:
            try:
                normalized = int(dept_id)
            except (TypeError, ValueError):
                continue
            if normalized not in seen:
                seen.add(normalized)
                result.append(normalized)
        return result

    @staticmethod
    def _is_not_in_authorized_scope(data: dict) -> bool:
        message = str(data.get("errmsg") or data)
        return "不在授权范围" in message or "not in" in message.lower() and "scope" in message.lower()

    @staticmethod
    def _is_rate_limited(data: dict) -> bool:
        message = str(data.get("errmsg") or data)
        return data.get("errcode") == 90002 or "次数过多" in message or "rate limit" in message.lower()

    async def fetch_users(self, department_external_id: str) -> list[ExternalUser]:
        token = await self.get_access_token()
        users: list[ExternalUser] = []
        cursor = 0
        dept_id = int(department_external_id)

        async with httpx.AsyncClient() as client:
            while True:
                # DingTalk has tenant/app-level minute quotas; keep sync conservative.
                await asyncio.sleep(self.DINGTALK_REQUEST_INTERVAL_SECONDS)

                data = await self._post_dingtalk_with_retry(
                    client,
                    self.DINGTALK_USER_LIST_URL,
                    token,
                    {"dept_id": dept_id, "cursor": cursor, "size": 100},
                )
                if data.get("errcode") != 0:
                    raise RuntimeError(f"DingTalk user list error: {data.get('errmsg') or data}")

                result = data.get("result", {}) or {}
                items = result.get("list", []) or []
                for item in items:
                    external_id = item.get("userid") or item.get("user_id") or ""
                    # Get user's actual department list from DingTalk data
                    dept_id_list = item.get("dept_id_list", [])
                    department_ids = [str(did) for did in dept_id_list] if dept_id_list else [department_external_id]
                    # Use last level department (last item in list is most specific)
                    last_dept_id = department_ids[-1] if department_ids else department_external_id
                    last_dept_path = self._dept_path_map.get(last_dept_id, "")
                    user = ExternalUser(
                        external_id=external_id,
                        unionid=item.get("unionid", "") or "",
                        open_id=item.get("openid", "") or "",
                        name=item.get("name", ""),
                        nickname=item.get("nick", "") or item.get("nickname", "") or "",
                        # DingTalk commonly returns the corporate mailbox in
                        # org_email while leaving email present but blank.
                        email=item.get("org_email", "") or item.get("email", "") or "",
                        avatar_url=item.get("avatar", "") or "",
                        title=item.get("title", "") or "",
                        department_external_id=last_dept_id,
                        department_path=last_dept_path,
                        department_ids=department_ids,
                        mobile=item.get("mobile", "") or "",
                        status="active" if item.get("active", True) else "inactive",
                        raw_data=item,
                    )
                    users.append(user)

                if not result.get("has_more"):
                    break
                cursor = int(result.get("next_cursor") or 0)

        return users

    def _build_dept_paths(self, dept_index: dict[str, tuple[str, str | None]]) -> dict[str, str]:
        paths: dict[str, str] = {}

        def compute_path(dept_id: str, visited: set[str] | None = None) -> str:
            if dept_id in paths:
                return paths[dept_id]
            if visited is None:
                visited = set()
            if dept_id in visited:
                # Cycle guard
                paths[dept_id] = dept_id
                return dept_id
            visited.add(dept_id)
            name, parent_id = dept_index.get(dept_id, ("", None))
            if is_virtual_directory_root_values(
                external_id=dept_id,
                name=name,
                parent_id=parent_id,
            ):
                paths[dept_id] = ""
                return ""
            if not parent_id or parent_id not in dept_index:
                paths[dept_id] = name
                return name
            parent_path = compute_path(parent_id, visited)
            full = f"{parent_path}/{name}" if parent_path else name
            paths[dept_id] = full
            return full

        for did in list(dept_index.keys()):
            compute_path(did)
        return paths


SYNC_ADAPTER_CLASSES = {
    "feishu": FeishuOrgSyncAdapter,
    "dingtalk": DingTalkOrgSyncAdapter,
    "wecom": WeComOrgSyncAdapter,
    "google_workspace": GoogleWorkspaceOrgSyncAdapter,
}


async def get_org_sync_adapter(
    db: AsyncSession,
    provider_type: str,
    tenant_id: uuid.UUID | None = None,
    provider_id: uuid.UUID | None = None,
    provider: IdentityProvider | None = None,
) -> BaseOrgSyncAdapter | None:
    """Factory function to create org sync adapter.

    Args:
        db: Database session
        provider_type: Type of provider (feishu, dingtalk, etc.)
        tenant_id: Optional tenant ID
        provider_id: Optional specific provider ID (if not provided, uses first found by type)

    Returns:
        Adapter instance or None if not supported
    """
    # A supplied provider avoids opening a database read transaction before the
    # adapter performs potentially slow provider network requests.
    if provider is not None:
        pass
    elif provider_id:
        result = await db.execute(
            select(IdentityProvider).where(IdentityProvider.id == provider_id)
        )
    else:
        query = select(IdentityProvider).where(IdentityProvider.provider_type == provider_type)
        if tenant_id:
            query = query.where(IdentityProvider.tenant_id == tenant_id)
        else:
            query = query.where(IdentityProvider.tenant_id.is_(None))
        result = await db.execute(query)
    if provider is None:
        provider = result.scalar_one_or_none()

    config = provider.config if provider else {}
    directory_protocol = ((config or {}).get("capabilities") or {}).get(
        "directory_protocol"
    ) or (config or {}).get("directory_protocol") or (
        "scim" if provider_type == "scim" else None
    )
    if provider_type in {"oauth2", "scim"} and directory_protocol == "scim":
        from app.services.scim_org_sync_adapter import ScimOrgSyncAdapter

        return ScimOrgSyncAdapter(
            provider=provider,
            config=config,
            tenant_id=provider.tenant_id if provider else tenant_id,
        )

    adapter_class = SYNC_ADAPTER_CLASSES.get(provider_type)
    if not adapter_class:
        return None

    return adapter_class(
        provider=provider,
        config=config,
        tenant_id=provider.tenant_id if provider else tenant_id,
    )
