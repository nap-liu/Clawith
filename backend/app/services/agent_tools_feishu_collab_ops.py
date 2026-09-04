from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import async_session
from app.models.agent import Agent as AgentModel
from app.models.org import AgentRelationship, OrgMember
from app.models.user import User as UserModel
from app.services.agent_tools_feishu_auth import (
    _check_feishu_err,
    _get_feishu_credentials,
)
from app.services.agent_tools_feishu_docs import _feishu_wiki_get_node
from app.services.recipient_resolver import RecipientResolutionError, resolve_human_channel_recipient


async def _feishu_drive_share(agent_id: uuid.UUID, arguments: dict) -> str:
    """Manage Feishu drive file collaborators.
    Automatically handles both regular docs/files (Drive permissions API)
    and Wiki node documents (Wiki space members API).
    """
    import httpx
    import re as _re

    document_token = (arguments.get("document_token") or "").strip()
    doc_type = (arguments.get("doc_type") or "docx").strip()
    action = (arguments.get("action") or "list").strip()
    permission = (arguments.get("permission") or "edit").strip()

    if not document_token:
        return "❌ Missing required argument 'document_token'"

    app_id, app_secret = await _get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "❌ Agent has no Feishu channel configured."
    from app.services.feishu_service import feishu_service

    token = await feishu_service.get_tenant_access_token(app_id, app_secret)
    headers = {"Authorization": f"Bearer {token}"}

    # ── Detect if this is a Wiki node token ─────────────────────────────────
    node_info = await _feishu_wiki_get_node(document_token, token)
    is_wiki = node_info is not None
    space_id = node_info.get("space_id", "") if node_info else ""
    obj_token = node_info.get("obj_token", "") if node_info else ""

    # Permission level mapping: Feishu API uses "view" / "edit" / "full_access"
    api_perm = {"view": "view", "edit": "edit", "full_access": "full_access"}.get(permission, "edit")
    # Wiki space role mapping: only "admin" / "member" are valid roles
    wiki_role = "admin" if api_perm in ("edit", "full_access") else "member"

    # ── LIST collaborators ────────────────────────────────────────────────────
    if action == "list":
        use_token = obj_token if (is_wiki and obj_token) else document_token
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://open.feishu.cn/open-apis/drive/v1/permissions/{use_token}/members",
                params={"type": doc_type},
                headers=headers,
            )
        data = resp.json()
        if data.get("code") != 0:
            _c = data.get("code")
            if _c == 1063003 and is_wiki:
                return (
                    f"ℹ️ 文档 `{document_token}` 是知识库页面，其权限由知识库空间统一管理。\n"
                    "知识库空间 ID：`" + space_id + "`\n"
                    "请直接在飞书知识库中管理成员权限。"
                )
            if _c in (99991672, 99991668):
                return f"❌ 权限不足（code {_c}）\n需要在飞书开放平台开通：\n• drive:drive（云文档权限管理）"
            return f"❌ 获取协作者列表失败：{data.get('msg')} (code {_c})"

        members = data.get("data", {}).get("items", [])
        if not members:
            return f"📄 文档 `{document_token}` 当前没有其他协作者。"

        provider_ids = [
            str(m.get("member_id") or "") for m in members if m.get("member_type") == "openid" and m.get("member_id")
        ]
        identity_map: dict[str, tuple[str, str]] = {}
        if provider_ids:
            async with async_session() as identity_db:
                agent_result = await identity_db.execute(select(AgentModel).where(AgentModel.id == agent_id))
                source_agent = agent_result.scalar_one_or_none()
                if source_agent:
                    rows = await identity_db.execute(
                        select(OrgMember, UserModel)
                        .join(UserModel, OrgMember.user_id == UserModel.id)
                        .where(
                            OrgMember.tenant_id == source_agent.tenant_id,
                            OrgMember.open_id.in_(provider_ids),
                        )
                    )
                    for member, user in rows.all():
                        identity_map[str(member.open_id)] = (str(user.id), user.display_name)
        lines = [f"📄 文档 `{document_token}` 的协作者列表（共 {len(members)} 人）：\n"]
        for m in members:
            perm = m.get("perm", "")
            member_type = m.get("member_type", "")
            member_id = m.get("member_id", "")
            canonical = identity_map.get(str(member_id))
            if member_type == "openid" and canonical:
                user_id, display_name = canonical
                lines.append(f"• {display_name} | user_id: `{user_id}` | 权限: **{perm}**")
            else:
                lines.append(f"• 非用户协作者或未映射用户 | 权限: **{perm}**")
        return "\n".join(lines)

    # ── ADD / REMOVE collaborators ─────────────────────────────────────────────
    user_ids: list[str] = list(arguments.get("user_ids") or [])
    if not user_ids:
        return "❌ 请提供 canonical user_ids"

    resolved: list[tuple[str, str]] = []  # (display_name, open_id)
    async with async_session() as identity_db:
        for canonical_user_id in user_ids:
            try:
                route = await resolve_human_channel_recipient(
                    identity_db, agent_id, canonical_user_id, channel="feishu"
                )
            except RecipientResolutionError:
                resolved.append((str(canonical_user_id), ""))
                continue
            open_id = str(route.member.open_id or "")
            resolved.append((route.user.display_name, open_id))

    results = []
    async with httpx.AsyncClient(timeout=15) as client:
        for display, oid in resolved:
            if not oid:
                results.append(f"❌ 无法将 canonical user_id「{display}」解析到唯一 Feishu open_id，跳过")
                continue

            if action == "add":
                # ── Wiki node: use wiki space members API ──────────────────
                if is_wiki and space_id:
                    resp = await client.post(
                        f"https://open.feishu.cn/open-apis/wiki/v2/spaces/{space_id}/members",
                        json={"member_type": "openid", "member_id": oid, "member_role": wiki_role},
                        headers=headers,
                    )
                    d = resp.json()
                    _c = d.get("code")
                    if _c == 0:
                        results.append(f"✅ 已将「{display}」加入知识库空间（角色：{wiki_role}）")
                    elif _c == 131008:
                        results.append(f"ℹ️ 「{display}」已经是知识库成员，无需重复添加")
                    elif _c == 131101:
                        # Public wiki space — everyone already has access
                        results.append(f"ℹ️ 这是一个**公开知识库**，所有人已可访问。\n「{display}」无需单独添加权限。")
                    else:
                        results.append(f"❌ 添加「{display}」到知识库失败：{d.get('msg')} (code {_c})")
                    continue

                # ── Regular docx: use Drive permissions API ────────────────
                body = {
                    "member_type": "openid",
                    "member_id": oid,
                    "perm": api_perm,
                }
                resp = await client.post(
                    f"https://open.feishu.cn/open-apis/drive/v1/permissions/{document_token}/members",
                    json=body,
                    headers=headers,
                    params={"type": doc_type},
                )
                d = resp.json()
                if d.get("code") == 0:
                    results.append(f"✅ 已将「{display}」添加为**{permission}**权限协作者")
                else:
                    _c = d.get("code")
                    if _c == 99992402:
                        # Feishu platform policy: you cannot add yourself as a collaborator via API.
                        # Permissions must be granted by others, or set manually in the UI.
                        results.append(
                            f"⚠️ 飞书平台安全限制：无法通过 API 为自己添加协作权限。\n"
                            f"请手动操作：打开文档 → 右上角「分享」→ 添加自己并设置权限。"
                        )
                    elif _c in (99991672, 99991668):
                        return f"❌ 权限不足（code {_c}）\n需要在飞书开放平台开通：\n• drive:drive（云文档权限管理）"
                    else:
                        results.append(f"❌ 添加「{display}」失败：{d.get('msg')} (code {_c})")

            elif action == "remove":
                if is_wiki and space_id:
                    resp = await client.delete(
                        f"https://open.feishu.cn/open-apis/wiki/v2/spaces/{space_id}/members/{oid}",
                        headers=headers,
                        params={"member_type": "openid"},
                    )
                    d = resp.json()
                    if d.get("code") == 0:
                        results.append(f"✅ 已将「{display}」从知识库移除")
                    else:
                        results.append(f"❌ 移除「{display}」失败：{d.get('msg')} (code {d.get('code')})")
                    continue

                resp = await client.delete(
                    f"https://open.feishu.cn/open-apis/drive/v1/permissions/{document_token}/members/{oid}",
                    headers=headers,
                    params={"type": doc_type, "member_type": "openid"},
                )
                d = resp.json()
                if d.get("code") == 0:
                    results.append(f"✅ 已移除「{display}」的协作权限")
                else:
                    results.append(f"❌ 移除「{display}」失败：{d.get('msg')} (code {d.get('code')})")

    return "\n".join(results) if results else "没有需要处理的成员"


async def _feishu_drive_delete(agent_id: uuid.UUID, arguments: dict) -> str:
    """Delete a file or folder from Feishu Drive (cloud space).
    The file is moved to the recycle bin, not permanently deleted.
    For folders, the deletion is asynchronous and returns a task_id.
    """
    import httpx

    file_token = (arguments.get("file_token") or "").strip()
    file_type = (arguments.get("file_type") or "").strip()

    if not file_token:
        return "❌ Missing required argument 'file_token'"
    if not file_type:
        return "❌ Missing required argument 'file_type'. Valid values: file, docx, bitable, folder, doc, sheet, mindnote, shortcut, slides"

    valid_types = {"file", "docx", "bitable", "folder", "doc", "sheet", "mindnote", "shortcut", "slides"}
    if file_type not in valid_types:
        return f"❌ Invalid file_type '{file_type}'. Valid values: {', '.join(sorted(valid_types))}"

    app_id, app_secret = await _get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "❌ Agent has no Feishu channel configured."
    from app.services.feishu_service import feishu_service

    token = await feishu_service.get_tenant_access_token(app_id, app_secret)

    # Type label mapping for user-friendly output
    type_labels = {
        "file": "文件",
        "docx": "文档",
        "bitable": "多维表格",
        "folder": "文件夹",
        "doc": "旧版文档",
        "sheet": "电子表格",
        "mindnote": "思维笔记",
        "shortcut": "快捷方式",
        "slides": "幻灯片",
    }
    type_label = type_labels.get(file_type, file_type)

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.delete(
                f"https://open.feishu.cn/open-apis/drive/v1/files/{file_token}",
                params={"type": file_type},
                headers={"Authorization": f"Bearer {token}"},
            )
        data = resp.json()
        code = data.get("code", -1)

        if code == 0:
            # Folder deletion returns a task_id for async tracking
            task_id = data.get("data", {}).get("task_id")
            if task_id:
                return (
                    f"✅ 已提交{type_label}删除任务（异步执行中）。\n"
                    f"📋 任务 ID: `{task_id}`\n"
                    f"文件夹删除为异步操作，文件会被移至回收站。"
                )
            return f"✅ {type_label} `{file_token}` 已删除（移至回收站）。"

        # Error handling with specific codes
        msg = data.get("msg", "Unknown error")
        if code == 1061003:
            return f"❌ 未找到文件 `{file_token}`。请确认文件 token 和类型是否正确。"
        elif code == 1061004:
            return (
                f"❌ 权限不足（code {code}）\n"
                "需要满足以下条件之一：\n"
                "• 文件所有者 + 父文件夹编辑权限\n"
                "• 父文件夹的所有者或 full_access 权限\n"
                "同时需要在飞书开放平台开通：drive:drive 或 space:document:delete"
            )
        elif code == 1061007:
            return f"❌ 文件 `{file_token}` 已被删除。"
        elif code == 1061045:
            return f"⚠️ 接口频率限制，请稍后重试。（每秒最多 5 次）"
        else:
            return f"❌ 删除{type_label}失败：{msg} (code {code})"

    except Exception as e:
        return f"❌ 删除文件异常: {str(e)[:300]}"


async def _resolve_feishu_open_id(
    agent_id: uuid.UUID,
    canonical_user_id: object,
) -> tuple[str, str]:
    """Resolve a public canonical user_id to one internal Feishu open_id."""
    async with async_session() as db:
        route = await resolve_human_channel_recipient(db, agent_id, canonical_user_id, channel="feishu")
        open_id = str(route.member.open_id or "").strip()
        if not open_id:
            raise RecipientResolutionError(
                "recipient_unreachable",
                "user_id has no usable Feishu open_id",
            )
        return route.user.display_name, open_id


# ─── Feishu Approval Tools ───────────────────────────────────────────────────


async def _feishu_approval_create(agent_id: uuid.UUID, arguments: dict) -> str:
    app_id, app_secret = await _get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "❌ Agent has no Feishu channel configured."

    approval_code = arguments.get("approval_code", "").strip()
    canonical_user_id = arguments.get("user_id", "").strip()
    form_data = arguments.get("form_data", "").strip()

    if not approval_code or not canonical_user_id or not form_data:
        return "❌ form_data, user_id and approval_code are required."

    try:
        _, open_id = await _resolve_feishu_open_id(agent_id, canonical_user_id)
    except RecipientResolutionError as exc:
        return exc.as_json()

    from app.services.feishu_service import feishu_service

    try:
        resp = await feishu_service.create_approval_instance(app_id, app_secret, approval_code, open_id, form_data)
        err = _check_feishu_err(resp)
        if err:
            return err

        instance_code = resp.get("data", {}).get("instance_code", "")
        return f"✅ 审批发起成功！\n审批实例 ID: `{instance_code}`"
    except Exception as e:
        return f"Failed: {str(e)[:300]}"


async def _feishu_approval_query(agent_id: uuid.UUID, arguments: dict) -> str:
    app_id, app_secret = await _get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "❌ Agent has no Feishu channel configured."

    approval_code = arguments.get("approval_code", "").strip()
    status = arguments.get("status")

    if not approval_code:
        return "❌ approval_code is required."

    from app.services.feishu_service import feishu_service

    try:
        resp = await feishu_service.query_approval_instances(app_id, app_secret, approval_code, status)
        err = _check_feishu_err(resp)
        if err:
            return err

        data = resp.get("data", {})
        instance_codes = data.get("instance_code_list", [])

        return f"✅ 查询完成。共发现 {len(instance_codes)} 个符合条件的审批实例。\n实例列表: {instance_codes}"
    except Exception as e:
        return f"Failed: {str(e)[:300]}"


async def _feishu_approval_get(agent_id: uuid.UUID, arguments: dict) -> str:
    app_id, app_secret = await _get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "❌ Agent has no Feishu channel configured."

    instance_id = arguments.get("instance_id", "").strip()
    if not instance_id:
        return "❌ instance_id is required."

    from app.services.feishu_service import feishu_service

    try:
        resp = await feishu_service.get_approval_instance(app_id, app_secret, instance_id)
        err = _check_feishu_err(resp)
        if err:
            return err

        data = resp.get("data", {})
        import json

        return f"✅ 审批实例查询结果:\n```json\n{json.dumps(data, ensure_ascii=False, indent=2)}\n```"
    except Exception as e:
        return f"Failed: {str(e)[:300]}"


# ─── Feishu User Search ───────────────────────────────────────────────────────


async def _feishu_user_search(agent_id: uuid.UUID, arguments: dict) -> str:
    """Search related people by display name and return canonical IDs only."""

    name = (arguments.get("name") or "").strip()
    if not name:
        return "❌ Missing required argument 'name'"

    async with async_session() as db:
        query = (
            select(AgentRelationship)
            .join(UserModel, AgentRelationship.user_id == UserModel.id)
            .where(
                AgentRelationship.agent_id == agent_id,
                UserModel.is_active.is_(True),
                UserModel.display_name.ilike(f"%{name}%"),
            )
            .options(
                selectinload(AgentRelationship.user),
                selectinload(AgentRelationship.member),
            )
            .order_by(UserModel.display_name, UserModel.id)
        )
        relationships = (await db.execute(query)).scalars().all()
        matches: list[tuple[UserModel, OrgMember]] = []
        seen: set[uuid.UUID] = set()
        for relationship in relationships:
            if relationship.user_id in seen:
                continue
            try:
                route = await resolve_human_channel_recipient(
                    db,
                    agent_id,
                    relationship.user_id,
                    channel="feishu",
                )
            except RecipientResolutionError:
                continue
            seen.add(route.user.id)
            matches.append((route.user, route.member))

    if not matches:
        return f"🔍 未找到与「{name}」匹配且可通过飞书联系的关系用户。"

    lines = [f"🔍 找到 {len(matches)} 位匹配「{name}」的关系用户：\n"]
    for user, member in matches:
        lines.append(f"• **{user.display_name}**")
        lines.append(f"  user_id: `{user.id}`")
        if member.department_path:
            lines.append(f"  部门: {member.department_path}")
    if len(matches) > 1:
        lines.append("\n存在重名，请根据 user_id 和部门选择准确对象。")
    return "\n".join(lines)


__all__ = [
    "_feishu_drive_share",
    "_feishu_drive_delete",
    "_resolve_feishu_open_id",
    "_feishu_approval_create",
    "_feishu_approval_query",
    "_feishu_approval_get",
    "_feishu_user_search",
]
