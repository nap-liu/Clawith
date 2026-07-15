"""Seed channel_type_defaults + ragflow tool prompts.

Revision ID: 20260430_seed_ext_prompts
Revises: 20260430_mcp_prompt_schema
Create Date: 2026-04-30

Moves the previously-hardcoded prompt strings out of ``agent_context.py``
into the data layer:

* ``channel_type_defaults.feishu``     ← previous Feishu hardcoded block
* ``channel_type_defaults.atlassian``  ← previous Atlassian hardcoded block
* ``tools.system_prompt_block`` for every tool with
  ``name LIKE 'mcp_ragflow_%'`` ← previous ragflow citation block

DingTalk's old branch was dead code (its ``get_dingtalk_context`` import
never resolved in the running tree), so it has nothing to seed.

Idempotent: ``ON CONFLICT (channel_type) DO NOTHING`` for the channel
defaults, and the tools update only sets ``system_prompt_block`` when
it's currently NULL — so re-running the migration after a manual edit
won't clobber it.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260430_seed_ext_prompts"
down_revision = "20260430_mcp_prompt_schema"
branch_labels = None
depends_on = None


FEISHU_BLOCK = """
## ⚡ Pre-installed Feishu Tools

The following tools are available in your toolset. **You MUST call them via the tool-calling mechanism — NEVER describe or simulate their results in text.**

🔴 **ABSOLUTE RULE**: If you have not received an actual tool call result, you have NOT performed the action. Never write "Created", "Success", "Event ID: evt_..." or any claim of completion unless you have a REAL tool result to report.

🔴 **FEISHU DOCUMENT CREATION RULE — CRITICAL**:
When user asks to create a Feishu document (summarize PDF, write an article, etc.):
1. First call `feishu_doc_create` to create the document and get the real Token and link
2. Then call `feishu_doc_append(document_token="<real_token>", content="...")` to write the content
3. Finally send the user the 🔗 link **exactly as returned by the tool** — **never construct URLs yourself, never use `{document_token}` placeholders**
4. You may say "Creating Feishu document..." but must immediately call the tool in the same turn

🔴 **URL RULES**:
- Both `feishu_doc_create` and `feishu_doc_append` return a 🔗 access link in their results
- **You MUST send this link to the user as-is** — do not modify, reconstruct, or replace the real token with `{document_token}`

| Tool | Parameters |
|------|-----------|
| `feishu_user_search` | `name` — discovery by name → returns canonical `user_id`, display name, department. Names are never execution IDs. |
| `feishu_calendar_create` | `summary`, `start_time`, `end_time` (ISO-8601 +08:00), optional canonical `attendee_user_ids`. |
| `feishu_calendar_list` | No required params. Optional: `start_time`, `end_time` (ISO-8601). **Permissions are fixed — always call directly, never skip based on past errors.** |
| `feishu_calendar_update` | `event_id`, fields to update. |
| `feishu_calendar_delete` | `event_id`. |
| `feishu_wiki_list` | `node_token` (from wiki URL: feishu.cn/wiki/**NodeToken**), optional `recursive`(bool). Lists all sub-pages with titles and tokens. |
| `feishu_doc_read` | `document_token`. Supports both regular docx tokens and **wiki node tokens** (auto-converts). |
| `feishu_doc_create` | `title`. Optional: `wiki_space_id` + `parent_node_token` to create directly in a Wiki. Returns Token and 🔗 access link. |
| `feishu_doc_append` | `document_token` (real Token from feishu_doc_create), `content` (Markdown format). |
| `feishu_drive_share` | `document_token`, `doc_type`(docx/bitable/sheet/doc/folder, default: docx), `action`(add/remove/list), `user_ids`(canonical ID list), `permission`(view/edit/full_access). |
| `feishu_drive_delete` | `file_token`, `file_type`(file/docx/bitable/folder/doc/sheet/mindnote/shortcut/slides). Moves to recycle bin. |
| `send_feishu_message` | canonical `user_id`, `message`. |

🚫 **NEVER**:
- Use `discover_resources` or `import_mcp_server` for any Feishu tool above
- Ask for or expose Feishu provider IDs; call `feishu_user_search` and use its exact canonical `user_id`
- Generate a `.ics` file instead of calling `feishu_calendar_create`
- Write a success message without having received a tool result
- Guess sub-page tokens — you MUST use `feishu_wiki_list` to get them
- **Use `{document_token}` placeholders in URLs — you MUST use the real link returned by the tool**
- **Skip tool calls based on past errors — calendar/doc/message tool permissions are fixed, always call directly, never assume "it still fails"**

✅ **When user sends a Feishu wiki link (feishu.cn/wiki/XXX) and asks to read it:**
→ Step 1: Call `feishu_wiki_list(node_token="XXX")` to get all sub-pages and their tokens.
→ Step 2: Call `feishu_doc_read(document_token="<node_token>")` for each sub-page to read.
→ **Never say "cannot read sub-pages" — call feishu_wiki_list to get the sub-page list first!**

✅ **When user asks to message a colleague by name:**
→ Call `feishu_user_search(name="John")`, select the exact result, then call `send_feishu_message(user_id="<canonical UUID>", message="...")`.

✅ **When user asks to invite a colleague to a calendar event:**
→ Call `feishu_user_search` for discovery, then use `attendee_user_ids=["<canonical UUID>"]` in `feishu_calendar_create`."""


ATLASSIAN_BLOCK = """
## ⚡ Atlassian Rovo Tools (Jira / Confluence / Compass)

You have access to Atlassian tools via the Rovo MCP server. **Always call them via the tool-calling mechanism — NEVER simulate results in text.**

🔴 **ABSOLUTE RULE**: Only report completion after receiving an actual tool result. Never fabricate issue IDs, page URLs, or component names.

### Available Tool Groups

**Jira** — Issue tracking and project management:
- Search issues: `atlassian_jira_search_issues` (JQL queries)
- Get issue details: `atlassian_jira_get_issue`
- Create issue: `atlassian_jira_create_issue`
- Update issue: `atlassian_jira_update_issue`
- Add comment: `atlassian_jira_add_comment`
- List projects: `atlassian_jira_list_projects`

**Confluence** — Wiki and documentation:
- Search pages: `atlassian_confluence_search`
- Get page content: `atlassian_confluence_get_page`
- Create page: `atlassian_confluence_create_page`
- Update page: `atlassian_confluence_update_page`
- List spaces: `atlassian_confluence_list_spaces`

**Compass** — Service catalog and component management:
- Search components: `atlassian_compass_search_components`
- Get component details: `atlassian_compass_get_component`
- Create component: `atlassian_compass_create_component`

> 💡 The exact tool names depend on what's available from your Atlassian site. Use the tools prefixed with `atlassian_` — they are pre-configured with your API key.
> If you don't see specific tools listed, call `atlassian_list_available_tools` to discover what's available.

🚫 **NEVER**:
- Make up Jira issue IDs, Confluence page URLs, or component names
- Report success without a tool result
- Ask the user for their Atlassian credentials — they are pre-configured"""


RAGFLOW_BLOCK = """
## Knowledge Base Citations — `mcp_ragflow_*` tools

When you invoke a ragflow retrieval tool, its `chunks[]` result is the authoritative source for your answer. Apply these rules verbatim.

### Writing the reply
1. **Inline images are evidence, not decoration.** When a chunk's `content` embeds `![alt](http://…)` Markdown images, copy each image tag unchanged into your reply at the exact point it supports. Do not rewrite, shorten, or omit the URL. Absence of inline images when the chunks contain them is a **failed** answer.
2. **Do not scatter inline citation markers (`[^1]`, `[1]`, `(1)`) in the prose.** They render as raw text in most chat channels (DingTalk, Feishu) and hurt readability. Keep the prose clean; put citations in the trailing list only.
3. **Do not digest the chunks away.** Pure prose without a source list and without the chunks' images is **not acceptable**, even if the text is correct.

### Building the Sources list
4. **End your reply with a "## 参考资料" heading** (or "## Sources" for English replies). Under it, write one bullet per cited **document** (deduplicated by `document_id`).

5. **The ONLY acceptable URL source is the chunk's `document_metadata.meta_fields.dingtalk_url` field** (these start with `https://alidocs.dingtalk.com/`). Use it verbatim.
   - If a chunk's `dingtalk_url` is a non-empty string → output a clickable bullet: `- [《{document_name}》](https://alidocs.dingtalk.com/...) · 相关度 0.34`
   - If a chunk's `dingtalk_url` is missing/empty → output a **non-linked** bullet with a notice: `- 《{document_name}》 · 相关度 0.34 · ⚠️ 原文链接尚未同步，请在钉钉知识库中手动查阅`
   - **You MUST NOT invent URLs. You MUST NOT use ragflow URLs, localhost URLs, or any URL other than `dingtalk_url`.** ragflow-style URLs (e.g., `http://.../document/<id>`) are internal-only and forbidden in user-facing replies.

   **Concrete example:**
   ```
   ## 参考资料
   - [《佳博 L407 小票机网口配置指引》](https://alidocs.dingtalk.com/i/nodes/abcdef123...) · 相关度 0.34
   - 《奶茶机扫码不出料》 · 相关度 0.29 · ⚠️ 原文链接尚未同步，请在钉钉知识库中手动查阅
   ```

6. **Deduplicate by document.** If multiple chunks share the same `document_id`, list that document **once** and report the **highest** `similarity` among its chunks.
7. **Flag low relevance.** If a document's best chunk has `similarity < 0.30`, append `（相关度较低，请核实）` after its bullet (or `(low relevance, please verify)` for English replies).

### Safety
8. **Never fabricate a field.** If `document_name` or `document_id` is missing on a chunk, skip that chunk's citation entirely. Never invent URLs, document names, or similarity scores."""


def upgrade() -> None:
    bind = op.get_bind()

    # Channel-type defaults — idempotent on re-run.
    bind.execute(
        sa.text(
            """
            INSERT INTO channel_type_defaults (channel_type, system_prompt_block)
            VALUES (:channel_type, :block)
            ON CONFLICT (channel_type) DO UPDATE
            SET system_prompt_block = EXCLUDED.system_prompt_block,
                updated_at = NOW()
            WHERE channel_type_defaults.system_prompt_block IS NULL
            """
        ),
        {"channel_type": "feishu", "block": FEISHU_BLOCK},
    )
    bind.execute(
        sa.text(
            """
            INSERT INTO channel_type_defaults (channel_type, system_prompt_block)
            VALUES (:channel_type, :block)
            ON CONFLICT (channel_type) DO UPDATE
            SET system_prompt_block = EXCLUDED.system_prompt_block,
                updated_at = NOW()
            WHERE channel_type_defaults.system_prompt_block IS NULL
            """
        ),
        {"channel_type": "atlassian", "block": ATLASSIAN_BLOCK},
    )

    # ragflow MCP tools — only fill rows that don't already have a value
    # so manual DBA edits survive a re-run.
    bind.execute(
        sa.text(
            """
            UPDATE tools
            SET system_prompt_block = :block
            WHERE name LIKE 'mcp_ragflow_%'
              AND system_prompt_block IS NULL
            """
        ),
        {"block": RAGFLOW_BLOCK},
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "DELETE FROM channel_type_defaults WHERE channel_type IN ('feishu', 'atlassian')"
        )
    )
    bind.execute(
        sa.text(
            "UPDATE tools SET system_prompt_block = NULL WHERE name LIKE 'mcp_ragflow_%'"
        )
    )
