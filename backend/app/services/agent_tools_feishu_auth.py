from __future__ import annotations

import uuid

from sqlalchemy import select

from app.database import async_session


async def _get_feishu_token(agent_id: uuid.UUID) -> tuple[str, str] | None:
    """Get (app_id, app_access_token) for the agent's configured Feishu channel."""
    import httpx
    from app.models.channel_config import ChannelConfig

    async with async_session() as db:
        result = await db.execute(
            select(ChannelConfig).where(
                ChannelConfig.agent_id == agent_id,
                ChannelConfig.channel_type == "feishu",
                ChannelConfig.is_configured == True,
            )
        )
        config = result.scalar_one_or_none()

    if not config or not config.app_id or not config.app_secret:
        return None

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": config.app_id, "app_secret": config.app_secret},
        )
        token = resp.json().get("tenant_access_token", "")

    return (config.app_id, token) if token else None


async def _get_agent_calendar_id(token: str) -> tuple[str | None, str | None]:
    """Get (calendar_id, error_msg) for the agent app's primary calendar.

    Returns (calendar_id, None) on success, or (None, human_readable_error) on failure.
    """
    import httpx

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            "https://open.feishu.cn/open-apis/calendar/v4/calendars/primary",
            headers={"Authorization": f"Bearer {token}"},
        )
    data = resp.json()
    code = data.get("code", -1)
    if code == 0:
        cals = data.get("data", {}).get("calendars", [])
        if cals:
            cal_id = cals[0].get("calendar", {}).get("calendar_id")
            return cal_id, None
        return None, "日历列表为空，请确认应用有 calendar:calendar 权限并已发布新版本"
    if code == 99991672:
        return None, (
            "❌ 飞书日历权限未开通（错误码 99991672）\n\n"
            "请在飞书开放平台为应用 cli_a9257c5136781ceb 开通以下权限并发布新版本：\n"
            "• calendar:calendar:readonly（应用身份权限）\n"
            "• calendar:calendar.event:create（应用身份权限）\n"
            "• calendar:calendar.event:read（用户身份权限）\n"
            "• calendar:calendar.event:update（用户身份权限）\n"
            "• calendar:calendar.event:delete（用户身份权限）\n\n"
            "开通步骤：飞书开放平台 → 权限管理 → 批量导入权限 → 添加以上权限 → 创建版本 → 确认发布"
        )
    return None, f"获取日历 ID 失败：{data.get('msg')} (code {code})"


async def _feishu_resolve_open_id(token: str, email: str) -> str | None:
    """Resolve a user's open_id from their email."""
    import httpx

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            "https://open.feishu.cn/open-apis/contact/v3/users/batch_get_id",
            json={"emails": [email]},
            headers={"Authorization": f"Bearer {token}"},
            params={"user_id_type": "open_id"},
        )
    data = resp.json()
    if data.get("code") != 0:
        return None
    for u in data.get("data", {}).get("user_list", []):
        oid = u.get("user_id")
        if oid:
            return oid
    return None


def _iso_to_ts(iso_str: str) -> float:
    """Convert ISO 8601 string to Unix timestamp."""
    from datetime import datetime as _dt

    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            if iso_str.endswith("Z"):
                d = _dt.fromisoformat(iso_str.replace("Z", "+00:00"))
            else:
                d = _dt.strptime(iso_str, fmt)
            return d.timestamp()
        except ValueError:
            continue
    raise ValueError(f"Cannot parse datetime: {iso_str!r}")


async def _get_feishu_credentials(agent_id: uuid.UUID) -> tuple[str, str]:
    """Retrieve Feishu app_id and app_secret for an agent.
    1. Try Agent-specific ChannelConfig
    2. Fallback to global settings (.env)
    """
    from app.models.channel_config import ChannelConfig
    from app.config import get_settings

    settings = get_settings()
    app_id = settings.FEISHU_APP_ID
    app_secret = settings.FEISHU_APP_SECRET

    try:
        async with async_session() as db:
            result = await db.execute(
                select(ChannelConfig).where(ChannelConfig.agent_id == agent_id, ChannelConfig.channel_type == "feishu")
            )
            config = result.scalar_one_or_none()
            if config and config.app_id and config.app_secret:
                app_id = config.app_id
                app_secret = config.app_secret
    except Exception:
        pass

    return app_id, app_secret


async def _get_feishu_tenant_doc_url(tenant_token: str, doc_token: str, doc_type: str = "docx") -> str:
    """Build a user-accessible document URL using the tenant's actual domain.

    The API gateway (open.feishu.cn) cannot serve user documents - we must use
    the tenant's own domain (e.g. xxx.feishu.cn or xxx.larksuite.com).
    Falls back to generating a search link if the tenant domain cannot be resolved.

    Args:
        tenant_token: A valid tenant_access_token.
        doc_token:    The document_id (docx) or wiki node token.
        doc_type:     'docx' or 'wiki' - controls the URL path prefix.
    Returns:
        A fully-formed URL string.
    """
    import httpx

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://open.feishu.cn/open-apis/tenant/v2/tenant/query",
                headers={"Authorization": f"Bearer {tenant_token}"},
            )
        data = resp.json()
        if data.get("code") == 0:
            domain = data.get("data", {}).get("tenant", {}).get("domain", "")
            if domain:
                return f"https://{domain}/{doc_type}/{doc_token}"
    except Exception:
        pass
    # Fallback: construct a search URL so the user can locate the document
    return f"https://feishu.cn/{doc_type}/{doc_token}"


async def _get_feishu_bitable_url(tenant_token: str, app_token: str, table_id: str = "") -> str:
    """Build a user-accessible Bitable URL using the tenant's actual domain.

    Constructs https://{tenant_domain}/base/{app_token}?table={table_id}
    Falls back to https://feishu.cn/base/{app_token} if domain resolution fails.

    Args:
        tenant_token: A valid tenant_access_token.
        app_token:    The Bitable app token.
        table_id:     Optional table ID to deep-link to a specific sheet.
    Returns:
        A fully-formed URL string.
    """
    import httpx

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://open.feishu.cn/open-apis/tenant/v2/tenant/query",
                headers={"Authorization": f"Bearer {tenant_token}"},
            )
        data = resp.json()
        if data.get("code") == 0:
            domain = data.get("data", {}).get("tenant", {}).get("domain", "")
            if domain:
                base_url = f"https://{domain}/base/{app_token}"
                if table_id:
                    base_url += f"?table={table_id}"
                return base_url
    except Exception:
        pass
    # Fallback
    base_url = f"https://feishu.cn/base/{app_token}"
    if table_id:
        base_url += f"?table={table_id}"
    return base_url


def _parse_feishu_url(url: str) -> dict:
    """Parse various Feishu URLs to extract tokens.
    Supports Bitable (table, view) and Docx.
    """
    import re

    result = {}

    # Bitable URL regex: e.g., https://example.feishu.cn/base/{app_token}?table={table_id}&view={view_id}
    base_match = re.search(r"/base/([a-zA-Z0-9_]+)", url)
    if base_match:
        result["app_token"] = base_match.group(1)

    table_match = re.search(r"table=([a-zA-Z0-9_]+)", url)
    if table_match:
        result["table_id"] = table_match.group(1)

    # support URL with /tblxxxxxx
    if not "table_id" in result:
        tbl_match = re.search(r"/(tbl[a-zA-Z0-9_]+)", url)
        if tbl_match:
            result["table_id"] = tbl_match.group(1)

    view_match = re.search(r"view=([a-zA-Z0-9_]+)", url)
    if view_match:
        result["view_id"] = view_match.group(1)

    # Docx URL regex
    docx_match = re.search(r"/docx/([a-zA-Z0-9_]+)", url)
    if docx_match:
        result["document_token"] = docx_match.group(1)

    # Wiki URL regex
    wiki_match = re.search(r"/wiki/([a-zA-Z0-9_]+)", url)
    if wiki_match:
        result["wiki_token"] = wiki_match.group(1)

    return result


def _check_feishu_err(resp: dict) -> str | None:
    """Check Feishu API response for errors and return a user-friendly message.

    For permission-related errors, returns detailed step-by-step instructions
    guiding the user through the Feishu UI to grant the bot app access to
    the target document or Bitable.
    """
    code = resp.get("code")
    if code != 0:
        msg = str(resp.get("msg", ""))
        msg_lower = msg.lower()
        # Common Feishu permission/access error codes:
        #   99991663 - no permission to access resource
        #   99991661 - token invalid / access denied
        #   99991668 - permission denied on resource
        #   10006    - no permission
        #   91403    - Forbidden (bitable write without app access)
        #   91404    - bitable record not found (sometimes permission)
        #   1063001  - doc permission denied
        #   1063004  - doc operation forbidden
        _perm_codes = {99991663, 10006, 99991661, 99991668, 91403, 1063001, 1063004}
        _perm_keywords = ("permission", "forbidden", "no access", "access denied", "403")
        is_perm_error = code in _perm_codes or any(kw in msg_lower for kw in _perm_keywords)
        if is_perm_error:
            return (
                f"Failed: Permission denied (code: {code}, msg: {msg}). "
                "The bot app does not have access to this document/Bitable. "
                "CRITICAL INSTRUCTION for the Agent: You MUST explicitly output ALL of the following "
                "step-by-step instructions to the user in your reply. Do NOT omit or summarize any step:\n\n"
                "---\n"
                "**The bot needs to be added to this document/table as an authorized app. "
                "Please follow these steps:**\n\n"
                "1. Open the target document or Bitable in the Feishu client (web or desktop).\n"
                "2. Click the **「...」** menu button in the top-right corner of the page.\n"
                "3. In the dropdown menu, hover over **「更多」** (More) at the bottom.\n"
                "4. In the sub-menu that appears, click **「添加文档应用」** (Add Document App).\n"
                "5. In the search box, type the name of your Feishu bot app (the one bound to this Agent's channel), then click to add it.\n"
                "6. After adding, retry the same operation.\n\n"
                "If you cannot find 「添加文档应用」, it means the document owner may need to enable this option, "
                "or you can try: click **「分享」** (Share) button -> invite the bot app directly.\n"
                "---"
            )
        return f"Failed: API Error {code} - {msg}"
    return None


__all__ = [
    "_get_feishu_token",
    "_get_agent_calendar_id",
    "_feishu_resolve_open_id",
    "_iso_to_ts",
    "_get_feishu_credentials",
    "_get_feishu_tenant_doc_url",
    "_get_feishu_bitable_url",
    "_parse_feishu_url",
    "_check_feishu_err",
]
