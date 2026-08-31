from __future__ import annotations

import uuid

from app.services.agent_tools import channel_feishu_sender_open_id
from app.services.agent_tools_feishu_auth import (
    _check_feishu_err,
    _get_feishu_credentials,
    _get_feishu_tenant_doc_url,
    _parse_feishu_url,
)

_ROOT_TOOL_SYMBOLS = ("channel_feishu_sender_open_id",)


def _sync_root_tool_symbols() -> None:
    from app.services import agent_tools as _agent_tools_root

    for _name in _ROOT_TOOL_SYMBOLS:
        globals()[_name] = getattr(_agent_tools_root, _name)


async def _resolve_docx_document_token(agent_id: uuid.UUID, parsed_url: dict) -> str | None:
    doc_token = parsed_url.get("document_token")
    if doc_token:
        return doc_token
    wiki_token = parsed_url.get("wiki_token")
    if wiki_token:
        app_id, app_secret = await _get_feishu_credentials(agent_id)
        if app_id and app_secret:
            from app.services.feishu_service import feishu_service

            token = await feishu_service.get_tenant_access_token(app_id, app_secret)
            node_info = await _feishu_wiki_get_node(wiki_token, token)
            if node_info and node_info.get("obj_token"):
                return node_info["obj_token"]
    return None


async def _feishu_read_doc(agent_id: uuid.UUID, arguments: dict) -> str:
    """Read full text content of a Feishu Docx."""
    url = arguments.get("url", "")
    parsed = _parse_feishu_url(url)
    doc_token = await _resolve_docx_document_token(agent_id, parsed)
    if not doc_token:
        return "Failed: Could not extract Document token from the URL."

    app_id, app_secret = await _get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "Failed: Feishu app credentials not configured for this agent."

    from app.services.feishu_service import feishu_service

    try:
        resp = await feishu_service.read_feishu_doc(app_id, app_secret, doc_token)
        err = _check_feishu_err(resp)
        if err:
            return err

        content = resp.get("data", {}).get("content", "")
        if not content:
            return "OK: Document is empty or content is unavailable."
        return f"OK: Document Content:\n{content}"
    except Exception as e:
        return f"Failed: {str(e)[:300]}"


async def _feishu_create_doc(agent_id: uuid.UUID, arguments: dict) -> str:
    """Create a new blank Feishu Docx."""
    title = arguments.get("title", "Untitled Document")
    folder_token = arguments.get("folder_token", "")

    app_id, app_secret = await _get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "Failed: Feishu app credentials not configured for this agent."

    from app.services.feishu_service import feishu_service

    try:
        resp = await feishu_service.create_feishu_doc(app_id, app_secret, folder_token or None, title)
        err = _check_feishu_err(resp)
        if err:
            return err

        doc = resp.get("data", {}).get("document", {})
        doc_id = doc.get("document_id")
        # Get the tenant's actual domain (open.feishu.cn is the API gateway, not for users)
        tenant_token = await feishu_service.get_tenant_access_token(app_id, app_secret)
        url = await _get_feishu_tenant_doc_url(tenant_token, doc_id)
        return f"OK: Document created perfectly. Document ID: {doc_id}\nURL: {url}"
    except Exception as e:
        return f"Failed: {str(e)[:300]}"


async def _feishu_append_doc(agent_id: uuid.UUID, arguments: dict) -> str:
    """Append text to the bottom of a Feishu Docx."""
    url = arguments.get("url", "")
    content = arguments.get("content", "")
    if not content:
        return "Failed: Content to append cannot be empty."

    parsed = _parse_feishu_url(url)
    doc_token = await _resolve_docx_document_token(agent_id, parsed)
    if not doc_token:
        return "Failed: Could not extract Document token from the URL."

    app_id, app_secret = await _get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "Failed: Feishu app credentials not configured for this agent."

    from app.services.feishu_service import feishu_service

    try:
        # Feishu uses the document_id as the root block_id to append entirely to the document
        resp = await feishu_service.append_feishu_doc(app_id, app_secret, doc_token, content)
        err = _check_feishu_err(resp)
        if err:
            return err

        return "OK: Content appended successfully to the end of the document."
    except Exception as e:
        return f"Failed: {str(e)[:300]}"


# ─── Feishu Wiki Tools ───────────────────────────────────────────────────────


async def _feishu_wiki_get_node(token_str: str, auth_token: str) -> dict | None:
    """Call wiki get_node API to resolve a wiki node token → {obj_token, space_id, has_child, title}.
    Returns None if the token is not a wiki node."""
    import httpx

    async with httpx.AsyncClient(timeout=5) as client:
        r = await client.get(
            "https://open.feishu.cn/open-apis/wiki/v2/spaces/get_node",
            headers={"Authorization": f"Bearer {auth_token}"},
            params={"token": token_str, "obj_type": "wiki"},
        )
    d = r.json()
    if d.get("code") != 0:
        return None
    node = d.get("data", {}).get("node", {})
    return {
        "obj_token": node.get("obj_token", ""),
        "space_id": node.get("origin_space_id", node.get("space_id", "")),
        "has_child": node.get("has_child", False),
        "title": node.get("title", ""),
        "node_token": node.get("node_token", token_str),
    }


async def _feishu_doc_search(agent_id: uuid.UUID, arguments: dict) -> str:
    """Search Feishu documents by keyword using the official document search API."""
    import httpx

    query = (arguments.get("query") or arguments.get("search_key") or "").strip()
    if not query:
        return "❌ Missing required argument 'query'"

    count = max(1, min(int(arguments.get("count", 10)), 50))
    offset = max(0, int(arguments.get("offset", 0)))
    docs_types = arguments.get("docs_types") or []
    if docs_types and not isinstance(docs_types, list):
        return "❌ 'docs_types' must be an array of strings."

    app_id, app_secret = await _get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "❌ Agent has no Feishu channel configured."

    from app.services.feishu_service import feishu_service

    token = await feishu_service.get_tenant_access_token(app_id, app_secret)
    payload: dict[str, object] = {
        "search_key": query,
        "count": count,
        "offset": offset,
    }
    if docs_types:
        payload["docs_types"] = docs_types

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            "https://open.feishu.cn/open-apis/suite/docs-api/search/object",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=payload,
        )

    data = resp.json()
    err = _check_feishu_err(data)
    if err:
        return err

    result = data.get("data", {})
    entities = result.get("docs_entities", []) or []
    total = result.get("total", len(entities))
    has_more = bool(result.get("has_more", False))
    if not entities:
        return (
            f"🔎 未找到与 `{query}` 匹配的飞书文档。"
            "\n可以尝试："
            "\n1. 缩短关键词"
            "\n2. 换同义词"
            "\n3. 指定 docs_types 过滤，例如 ['docx'] 或 ['bitable']"
        )

    lines = [
        f"🔎 飞书文档搜索结果：关键词 `{query}`",
        f"返回 {len(entities)} 条，total={total}，offset={offset}，has_more={str(has_more).lower()}",
        "",
    ]
    for idx, item in enumerate(entities, start=offset + 1):
        title = item.get("title") or "(无标题)"
        docs_token = item.get("docs_token") or ""
        docs_type = item.get("docs_type") or "unknown"
        owner_id = item.get("owner_id") or ""
        lines.append(
            f"{idx}. **{title}**\n"
            f"   - docs_type: `{docs_type}`\n"
            f"   - docs_token: `{docs_token}`\n"
            f"   - owner_id: `{owner_id}`"
        )

    lines.append("")
    lines.append("💡 后续操作建议：")
    lines.append('- 读取普通文档/知识库页：`feishu_doc_read(document_token="...")`')
    lines.append('- 管理权限：`feishu_drive_share(document_token="...", doc_type="...", action="list|add|remove")`')
    lines.append('- 删除文件：`feishu_drive_delete(file_token="...", file_type="...")`')
    if has_more:
        lines.append(f'- 下一页：`feishu_doc_search(query="{query}", offset={offset + len(entities)}, count={count})`')

    return "\n".join(lines)


async def _feishu_wiki_list(agent_id: uuid.UUID, arguments: dict) -> str:
    """List sub-pages of a Feishu Wiki node, optionally recursive."""
    import httpx

    node_token = (arguments.get("node_token") or "").strip()
    recursive = bool(arguments.get("recursive", False))

    if not node_token:
        return "❌ Missing required argument 'node_token'"

    app_id, app_secret = await _get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "❌ Agent has no Feishu channel configured."
    from app.services.feishu_service import feishu_service

    token = await feishu_service.get_tenant_access_token(app_id, app_secret)
    headers = {"Authorization": f"Bearer {token}"}

    # Resolve node → space_id
    node_info = await _feishu_wiki_get_node(node_token, token)
    if not node_info:
        return (
            f"❌ 无法解析 Wiki 节点 `{node_token}`。\n"
            "请确认 token 来自飞书知识库 URL（https://xxx.feishu.cn/wiki/NodeToken），"
            "而非普通文档 URL。"
        )

    space_id = node_info["space_id"]
    if not space_id:
        return f"❌ 无法获取知识库 space_id，请检查 token 是否正确。"

    async def _list_children(parent_token: str, depth: int) -> list[dict]:
        """Return flat list of {title, node_token, obj_token, has_child, depth}."""
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://open.feishu.cn/open-apis/wiki/v2/spaces/{space_id}/nodes",
                headers=headers,
                params={"parent_node_token": parent_token, "page_size": 50},
            )
        data = resp.json()
        if data.get("code") != 0:
            return []
        items = data.get("data", {}).get("items", [])
        result = []
        for item in items:
            entry = {
                "title": item.get("title", "(无标题)"),
                "node_token": item.get("node_token", ""),
                "obj_token": item.get("obj_token", ""),
                "has_child": item.get("has_child", False),
                "depth": depth,
            }
            result.append(entry)
            if recursive and entry["has_child"] and depth < 2:
                children = await _list_children(entry["node_token"], depth + 1)
                result.extend(children)
        return result

    pages = await _list_children(node_token, 0)
    if not pages:
        return f"📂 Wiki 页面 `{node_token}` 下没有子页面。"

    lines = [f"📂 Wiki 页面 `{node_token}` 的子页面（共 {len(pages)} 个）：\nspace_id: `{space_id}`\n"]
    for p in pages:
        indent = "  " * p["depth"]
        child_hint = " _(有子页面)_" if p["has_child"] else ""
        lines.append(
            f"{indent}• **{p['title']}**{child_hint}\n"
            f"{indent}  node_token: `{p['node_token']}`\n"
            f"{indent}  obj_token: `{p['obj_token']}`"
        )
    lines.append(
        '\n💡 用 `feishu_doc_read(document_token="<node_token>")` 读取每个子页面的内容。'
        '\n   对有子页面的条目，再次调用 `feishu_wiki_list(node_token="...")` 继续展开。'
    )
    return "\n".join(lines)


async def _feishu_doc_read(agent_id: uuid.UUID, arguments: dict) -> str:
    document_token = arguments.get("document_token", "").strip()
    if not document_token:
        url = arguments.get("url", "")
        parsed = _parse_feishu_url(url)
        document_token = parsed.get("document_token", parsed.get("wiki_token", ""))

    if not document_token:
        return "Failed: Missing required argument 'document_token'"
    max_chars = min(int(arguments.get("max_chars", 6000)), 20000)

    app_id, app_secret = await _get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "Failed: Feishu app credentials not configured for this agent."

    from app.services.feishu_service import feishu_service

    tenant_token = await feishu_service.get_tenant_access_token(app_id, app_secret)

    read_token = document_token
    wiki_hint = ""
    node_info = await _feishu_wiki_get_node(document_token, tenant_token)
    if node_info and node_info.get("obj_token"):
        read_token = node_info["obj_token"]
        if node_info.get("has_child"):
            wiki_hint = (
                "\n\n> 💡 这是一个 Wiki 目录页，它有多个子页面。"
                "使用 `feishu_wiki_list` 工具（传入相同的 node_token）可以查看所有子页面列表。"
            )

    try:
        resp = await feishu_service.read_feishu_doc(app_id, app_secret, read_token)
        err = _check_feishu_err(resp)
        if err:
            return err

        content = resp.get("data", {}).get("content", "")
        if not content:
            return f"📄 Document '{document_token}' is empty.{wiki_hint}"

        truncated = ""
        if len(content) > max_chars:
            content = content[:max_chars]
            truncated = f"\n\n_(Truncated to {max_chars} chars)_"

        return f"📄 **Document content** (`{document_token}`):\n\n{content}{truncated}{wiki_hint}"
    except Exception as e:
        return f"Failed: {str(e)[:300]}"


async def _feishu_doc_create(agent_id: uuid.UUID, arguments: dict) -> str:
    _sync_root_tool_symbols()
    title = arguments.get("title", "").strip()
    if not title:
        return "Failed: Missing required argument 'title'"

    app_id, app_secret = await _get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "Failed: Feishu app credentials not configured for this agent."

    folder_token = (arguments.get("folder_token") or "").strip()
    wiki_space_id = (arguments.get("wiki_space_id") or "").strip()
    parent_node_token = (arguments.get("parent_node_token") or "").strip()

    from app.services.feishu_service import feishu_service

    tenant_token = await feishu_service.get_tenant_access_token(app_id, app_secret)

    try:
        import httpx

        # ── Smart fallback: if folder_token is actually a wiki node token,
        #    auto-redirect to wiki creation branch. This handles LLMs that
        #    pass the wiki node token via the old folder_token param.
        if folder_token and not wiki_space_id and not parent_node_token:
            probe = await _feishu_wiki_get_node(folder_token, tenant_token)
            if probe and probe.get("space_id"):
                wiki_space_id = probe["space_id"]
                parent_node_token = probe.get("node_token", folder_token)
                folder_token = ""  # Don't use as Drive folder

        # ── Wiki branch: create as a wiki node ──────────────────────────
        # If parent_node_token is given but wiki_space_id is not,
        # resolve space_id from the parent node automatically.
        if parent_node_token and not wiki_space_id:
            node_info = await _feishu_wiki_get_node(parent_node_token, tenant_token)
            if node_info and node_info.get("space_id"):
                wiki_space_id = node_info["space_id"]

        if wiki_space_id:
            body: dict = {
                "obj_type": "docx",
                "node_type": "origin",  # Required by Feishu Wiki API: "origin" = new entity
                "title": title,
            }
            if parent_node_token:
                body["parent_node_token"] = parent_node_token

            import logging

            _wiki_log = logging.getLogger("feishu_wiki_create")
            _wiki_log.info(f"Creating wiki node in space={wiki_space_id}, body={body}")

            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"https://open.feishu.cn/open-apis/wiki/v2/spaces/{wiki_space_id}/nodes",
                    json=body,
                    headers={"Authorization": f"Bearer {tenant_token}"},
                )
            result = resp.json()
            _wiki_log.info(f"Wiki create response: code={result.get('code')}, msg={result.get('msg')}")
            err = _check_feishu_err(result)
            if err:
                return err

            node = result.get("data", {}).get("node", {})
            # obj_token is the underlying docx token used by feishu_doc_append
            doc_token = node.get("obj_token", "")
            node_token = node.get("node_token", "")
            # Wiki docs are accessed via /wiki/{node_token}, not /docx/{obj_token}
            doc_url = await _get_feishu_tenant_doc_url(tenant_token, node_token, doc_type="wiki")

            return (
                f"✅ 知识库文档创建成功！\n"
                f"标题：{title}\n"
                f"文档 Token（用于 feishu_doc_append）：{doc_token}\n"
                f"Wiki Node Token：{node_token}\n"
                f"🔗 访问链接：{doc_url}\n"
                f'下一步：调用 feishu_doc_append(document_token="{doc_token}", content="...") 写入正文内容。'
            )

        # ── Regular Drive branch (original behavior) ─────────────────────
        resp = await feishu_service.create_feishu_doc(app_id, app_secret, folder_token, title)
        err = _check_feishu_err(resp)
        if err:
            return err

        doc = resp.get("data", {}).get("document", {})
        doc_token = doc.get("document_id", "")
        doc_url = await _get_feishu_tenant_doc_url(tenant_token, doc_token)

        # Auto-share with the Feishu sender so they can access the document.
        # channel_feishu_sender_open_id is a module-level ContextVar defined in this file;
        # no import needed — it is already in scope.
        share_note = ""
        try:
            sender_open_id = channel_feishu_sender_open_id.get(None)
            if sender_open_id and doc_token:
                async with httpx.AsyncClient(timeout=10) as client:
                    share_resp = await client.post(
                        f"https://open.feishu.cn/open-apis/drive/v1/permissions/{doc_token}/members",
                        params={"type": "docx"},
                        json={
                            "member_type": "openid",
                            "member_id": sender_open_id,
                            "perm": "full_access",
                        },
                        headers={"Authorization": f"Bearer {tenant_token}"},
                    )
                sr = share_resp.json()
                if sr.get("code") == 0:
                    share_note = "\n✅ 已自动为你开通访问权限。"
                else:
                    share_note = f"\n⚠️ 自动授权失败（{sr.get('code')}），你可能需要手动在飞书前端搜索此文件。"
        except Exception as _e:
            share_note = f"\n⚠️ 自动授权异常: {_e}"

        return (
            f"✅ 文档创建成功！{share_note}\n"
            f"标题：{title}\n"
            f"Token：{doc_token}\n"
            f"🔗 访问链接：{doc_url}\n"
            f'下一步：调用 feishu_doc_append(document_token="{doc_token}", content="...") 写入正文内容。'
        )
    except Exception as e:
        return f"Failed: {str(e)[:300]}"


def _parse_inline_markdown(text: str) -> list[dict]:
    """Parse inline markdown (bold, italic, strikethrough) into Feishu text_run elements.
    Note: inline `code` is deliberately NOT rendered as inline_code style because
    Feishu's API rejects inline_code inside heading blocks (field validation error).
    Instead, backtick-wrapped text is returned as plain text.
    Empty text_element_style dicts are intentionally omitted to avoid API validation errors.
    """
    import re as _re

    def _make_run(content: str, style: dict | None = None) -> dict:
        run: dict = {"content": content}
        if style:
            run["text_element_style"] = style
        return {"text_run": run}

    elements = []
    # Only handle **bold**, *italic*, ~~strikethrough~~; backticks become plain text
    pattern = r"(\*\*(.+?)\*\*|\*(.+?)\*|~~(.+?)~~|`(.+?)`)"
    pos = 0
    for m in _re.finditer(pattern, text):
        if m.start() > pos:
            elements.append(_make_run(text[pos : m.start()]))
        raw = m.group(0)
        if raw.startswith("**"):
            elements.append(_make_run(m.group(2), {"bold": True}))
        elif raw.startswith("~~"):
            elements.append(_make_run(m.group(4), {"strikethrough": True}))
        elif raw.startswith("`"):
            # Render as plain text to avoid inline_code validation issues in headings
            elements.append(_make_run(m.group(5)))
        else:
            elements.append(_make_run(m.group(3), {"italic": True}))
        pos = m.end()
    if pos < len(text):
        elements.append(_make_run(text[pos:]))
    if not elements:
        elements.append(_make_run(text or " "))
    return elements


def _markdown_to_feishu_blocks(markdown: str) -> list[dict]:
    """Convert Markdown text to Feishu docx v1 block list.

    Supported:
      # / ## / ### / ####  → heading1-4 (block_type 3-6)
      - / * / + text       → bullet      (block_type 12)
      1. text              → ordered     (block_type 13)
      > text               → quote       (block_type 15)
      --- / ***            → divider     (block_type 22)
      ``` ... ```          → code block  (block_type 14)
      plain text           → text        (block_type 2)
      inline **bold** *italic* `code` ~~strike~~  → text_element_style
    """
    import re as _re

    _HEADING_BLOCK = {1: (3, "heading1"), 2: (4, "heading2"), 3: (5, "heading3"), 4: (6, "heading4")}

    def _text_block(bt: int, key: str, line: str) -> dict:
        # Omit "style" entirely to avoid Feishu field validation errors on empty style dicts
        return {
            "block_type": bt,
            key: {"elements": _parse_inline_markdown(line)},
        }

    blocks: list[dict] = []
    lines = markdown.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]

        # ── Code fence ──────────────────────────────────────────────────────
        if line.strip().startswith("```"):
            lang = line.strip()[3:].strip()
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            blocks.append(
                {
                    "block_type": 14,
                    "code": {
                        "elements": [{"text_run": {"content": "\n".join(code_lines)}}],
                        "style": {
                            "language": 1
                            if not lang
                            else {
                                "python": 49,
                                "javascript": 22,
                                "js": 22,
                                "typescript": 56,
                                "ts": 56,
                                "bash": 4,
                                "sh": 4,
                                "sql": 53,
                                "java": 21,
                                "go": 17,
                                "rust": 51,
                                "json": 25,
                                "yaml": 60,
                                "html": 19,
                                "css": 10,
                            }.get(lang.lower(), 1)
                        },
                    },
                }
            )
            i += 1
            continue

        # ── Divider ──────────────────────────────────────────────────────────
        if _re.fullmatch(r"[-*_]{3,}", line.strip()):
            # NOTE: block_type 22 (Feishu native divider) is rejected by the batch children
            # creation API with error 99992402 (field validation failed).  Render as a plain
            # text block containing a visual em-dash separator instead — always accepted.
            blocks.append(
                {
                    "block_type": 2,
                    "text": {"elements": [{"text_run": {"content": "\u2500" * 24}}]},
                }
            )
            i += 1
            continue

        # ── Headings ─────────────────────────────────────────────────────────
        hm = _re.match(r"^(#{1,4})\s+(.*)", line)
        if hm:
            level = min(len(hm.group(1)), 4)
            bt, key = _HEADING_BLOCK[level]
            blocks.append(_text_block(bt, key, hm.group(2)))
            i += 1
            continue

        # ── Bullet list ──────────────────────────────────────────────────────
        if _re.match(r"^[\-\*\+]\s+", line):
            text = _re.sub(r"^[\-\*\+]\s+", "", line)
            blocks.append(_text_block(12, "bullet", text))
            i += 1
            continue

        # ── Ordered list ─────────────────────────────────────────────────────
        if _re.match(r"^\d+\.\s+", line):
            text = _re.sub(r"^\d+\.\s+", "", line)
            blocks.append(_text_block(13, "ordered", text))
            i += 1
            continue

        # ── Blockquote ───────────────────────────────────────────────────────
        if line.startswith("> "):
            blocks.append(_text_block(15, "quote", line[2:]))
            i += 1
            continue

        # ── Empty line → empty text block ────────────────────────────────────
        if line.strip() == "":
            blocks.append(
                {
                    "block_type": 2,
                    "text": {"elements": [{"text_run": {"content": " "}}]},
                }
            )
            i += 1
            continue

        # ── Markdown table separator line (|---|---| ) → skip ───────────────
        if _re.match(r"^\|[\s\-:]+(\|[\s\-:]+)*\|?\s*$", line.strip()):
            i += 1
            continue

        # ── Markdown table row → plain text ──────────────────────────────────
        if line.strip().startswith("|") and line.strip().endswith("|"):
            # Strip pipe separators and render each cell as plain text
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            cell_text = "  |  ".join(c for c in cells if c)
            blocks.append(_text_block(2, "text", cell_text))
            i += 1
            continue

        # ── Plain text (with inline formatting) ──────────────────────────────
        blocks.append(_text_block(2, "text", line))
        i += 1

    return blocks


async def _feishu_doc_append(agent_id: uuid.UUID, arguments: dict) -> str:
    document_token = arguments.get("document_token", "").strip()
    if not document_token:
        url = arguments.get("url", "")
        parsed = _parse_feishu_url(url)
        document_token = parsed.get("document_token", parsed.get("wiki_token", ""))

    content = arguments.get("content", "").strip()
    if not document_token:
        return "Failed: Missing required argument 'document_token'"
    if not content:
        return "Failed: Missing required argument 'content'"

    app_id, app_secret = await _get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "Failed: Feishu app credentials not configured for this agent."

    from app.services.feishu_service import feishu_service

    tenant_token = await feishu_service.get_tenant_access_token(app_id, app_secret)

    # For wiki node tokens, use the obj_token for the docx API
    node_info = await _feishu_wiki_get_node(document_token, tenant_token)
    docx_token = node_info["obj_token"] if (node_info and node_info.get("obj_token")) else document_token

    try:
        import httpx

        async with httpx.AsyncClient(timeout=20) as client:
            meta_resp = (
                await client.get(
                    f"https://open.feishu.cn/open-apis/docx/v1/documents/{docx_token}",
                    headers={"Authorization": f"Bearer {tenant_token}"},
                )
            ).json()
            err = _check_feishu_err(meta_resp)
            if err:
                return err

            body_block_id = meta_resp.get("data", {}).get("document", {}).get("body", {}).get("block_id") or docx_token

            children = _markdown_to_feishu_blocks(content)

            result = (
                await client.post(
                    f"https://open.feishu.cn/open-apis/docx/v1/documents/{docx_token}/blocks/{body_block_id}/children",
                    # Do NOT pass index: -1.  Omitting the field lets Feishu default to
                    # append-at-end, which is always valid.  Passing -1 explicitly can
                    # trigger error 1770001 (invalid param) with certain block type mixes.
                    json={"children": children},
                    headers={"Authorization": f"Bearer {tenant_token}"},
                )
            ).json()

            err = _check_feishu_err(result)
            if err:
                return err

        doc_url = await _get_feishu_tenant_doc_url(tenant_token, docx_token)
        return f"✅ 已写入 {len(children)} 个段落到文档。\n🔗 文档直链（原文发给用户，勿修改）：{doc_url}"
    except Exception as e:
        return f"Failed: {str(e)[:300]}"


__all__ = [
    "_resolve_docx_document_token",
    "_feishu_read_doc",
    "_feishu_create_doc",
    "_feishu_append_doc",
    "_feishu_wiki_get_node",
    "_feishu_doc_search",
    "_feishu_wiki_list",
    "_feishu_doc_read",
    "_feishu_doc_create",
    "_parse_inline_markdown",
    "_markdown_to_feishu_blocks",
    "_feishu_doc_append",
]
