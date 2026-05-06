"""Build rich system prompt context for agents.

Loads soul, memory, skills summary, and relationships from the agent's
workspace files and composes a comprehensive system prompt.
"""

import uuid
from pathlib import Path

from app.config import get_settings

settings = get_settings()

PERSISTENT_DATA = Path(settings.AGENT_DATA_DIR)


def _agent_workspace(agent_id: uuid.UUID) -> Path:
    """Return the canonical persistent workspace path for an agent."""
    return PERSISTENT_DATA / str(agent_id)


def _read_file_safe(path: Path, max_chars: int = 3000) -> str:
    """Read a file, return empty string if missing. Truncate if too long."""
    if not path.exists():
        return ""
    try:
        content = path.read_text(encoding="utf-8", errors="replace").strip()
        if len(content) > max_chars:
            content = content[:max_chars] + "\n...(truncated)"
        return content
    except Exception:
        return ""


def _parse_skill_frontmatter(content: str, filename: str) -> tuple[str, str]:
    """Parse YAML frontmatter from a skill .md file.

    Returns (name, description).
    If no frontmatter, falls back to filename-based name and first-line description.
    """
    name = filename.replace("_", " ").replace("-", " ")
    description = ""

    stripped = content.strip()
    if stripped.startswith("---"):
        end = stripped.find("---", 3)
        if end != -1:
            frontmatter = stripped[3:end].strip()
            for line in frontmatter.split("\n"):
                line = line.strip()
                if line.lower().startswith("name:"):
                    val = line[5:].strip().strip('"').strip("'")
                    if val:
                        name = val
                elif line.lower().startswith("description:"):
                    val = line[12:].strip().strip('"').strip("'")
                    if val:
                        description = val[:200]
            if description:
                return name, description

    # Fallback: use first non-empty, non-heading line as description
    for line in stripped.split("\n"):
        line = line.strip()
        # Skip frontmatter delimiters and YAML lines
        if line in ("---",) or line.startswith("name:") or line.startswith("description:"):
            continue
        if line and not line.startswith("#"):
            description = line[:200]
            break
    if not description:
        lines = stripped.split("\n")
        if lines:
            description = lines[0].strip().lstrip("# ")[:200]

    return name, description


def _load_skills_index(agent_id: uuid.UUID) -> str:
    """Load skill index (name + description) from skills/ directory.

    Supports two formats:
    - Flat file:   skills/my-skill.md
    - Folder:      skills/my-skill/SKILL.md  (Claude-style, with optional scripts/, references/)

    Uses progressive disclosure: only name+description go into the system
    prompt. The model is instructed to call read_file to load full content
    when a skill is relevant.
    """
    ws_root = _agent_workspace(agent_id)
    skills: list[tuple[str, str, str]] = []  # (name, description, path_relative_to_skills)
    skills_dir = ws_root / "skills"
    if skills_dir.exists():
        for entry in sorted(skills_dir.iterdir()):
            if entry.name.startswith("."):
                continue

            # Case 1: Folder-based skill — skills/<folder>/SKILL.md
            if entry.is_dir():
                skill_md = entry / "SKILL.md"
                if not skill_md.exists():
                    # Also try lowercase skill.md
                    skill_md = entry / "skill.md"
                if skill_md.exists():
                    try:
                        content = skill_md.read_text(encoding="utf-8", errors="replace").strip()
                        name, desc = _parse_skill_frontmatter(content, entry.name)
                        skills.append((name, desc, f"{entry.name}/SKILL.md"))
                    except Exception:
                        skills.append((entry.name, "", f"{entry.name}/SKILL.md"))

            # Case 2: Flat file — skills/<name>.md
            elif entry.suffix == ".md" and entry.is_file():
                try:
                    content = entry.read_text(encoding="utf-8", errors="replace").strip()
                    name, desc = _parse_skill_frontmatter(content, entry.stem)
                    skills.append((name, desc, entry.name))
                except Exception:
                    skills.append((entry.stem, "", entry.name))

    # Deduplicate by name
    seen: set[str] = set()
    unique: list[tuple[str, str, str]] = []
    for s in skills:
        if s[0] not in seen:
            seen.add(s[0])
            unique.append(s)

    if not unique:
        return ""

    # Build index table
    lines = [
        "You have the following skills available. Each skill defines specific instructions for a task domain.",
        "",
        "| Skill | Description | File |",
        "|-------|-------------|------|",
    ]
    for name, desc, rel_path in unique:
        lines.append(f"| {name} | {desc} | skills/{rel_path} |")

    lines.append("")
    lines.append("⚠️ SKILL USAGE RULES:")
    lines.append("1. When a user request matches a skill, FIRST call `read_file` with the File path above to load the full instructions.")
    lines.append("2. Follow the loaded instructions to complete the task.")
    lines.append("3. Do NOT guess what the skill contains — always read it first.")
    lines.append("4. Folder-based skills may contain auxiliary files (scripts/, references/, examples/). Use `list_files` on the skill folder to discover them.")

    return "\n".join(lines)


async def _collect_extension_prompts(agent_id: uuid.UUID) -> list[str]:
    """Collect prompt blocks contributed by an agent's enabled tools and
    configured channels.

    Three sources, in this output order:

    1. ``tools.system_prompt_block`` — DBA-fillable per-tool prompts (e.g.
       the ragflow citation rules). Emitted in tool-name order so the
       static prompt prefix is byte-stable across requests; that prefix
       stability is what lets prompt caching land hits.
    2. ``tools.mcp_server_instructions`` — server-provided instructions
       captured during the MCP ``initialize`` handshake. Deduplicated by
       ``mcp_server_url``; servers without instructions contribute
       nothing.
    3. Channel prompts — per-agent override in
       ``channel_configs.system_prompt_block`` if set, else the
       type-level default in ``channel_type_defaults``.

    Returns the list of non-empty prompt block strings, ready to append
    to ``static_parts``. Empty list when the agent has no extensions
    that contribute prompts.
    """
    from app.database import async_session
    from app.models.channel_config import ChannelConfig
    from app.models.channel_type_default import ChannelTypeDefault
    from app.models.tool import AgentTool, Tool
    from sqlalchemy import select

    blocks: list[str] = []

    async with async_session() as db:
        # Tool-driven blocks. We sort by Tool.name so identical agent
        # configurations always produce a byte-identical prefix.
        tool_rows = await db.execute(
            select(Tool)
            .join(AgentTool, AgentTool.tool_id == Tool.id)
            .where(
                AgentTool.agent_id == agent_id,
                AgentTool.enabled == True,  # noqa: E712 — SQLAlchemy idiom
                Tool.enabled == True,  # noqa: E712
            )
            .order_by(Tool.name)
        )
        tools = tool_rows.scalars().all()

        for tool in tools:
            block = (tool.system_prompt_block or "").strip()
            if block:
                blocks.append(block)

        # MCP server instructions — one per distinct server URL.
        seen_servers: set[str] = set()
        for tool in tools:
            if tool.type != "mcp":
                continue
            url = (tool.mcp_server_url or "").strip()
            if not url or url in seen_servers:
                continue
            seen_servers.add(url)
            instr = (tool.mcp_server_instructions or "").strip()
            if instr:
                blocks.append(instr)

        # Channel-driven blocks. Per-agent override wins; otherwise we
        # fall back to the type-level default.
        ch_rows = await db.execute(
            select(ChannelConfig).where(
                ChannelConfig.agent_id == agent_id,
                ChannelConfig.is_configured == True,  # noqa: E712
            ).order_by(ChannelConfig.channel_type)
        )
        channel_configs = ch_rows.scalars().all()

        if channel_configs:
            type_defaults_rows = await db.execute(
                select(ChannelTypeDefault).where(
                    ChannelTypeDefault.channel_type.in_(
                        [c.channel_type for c in channel_configs]
                    )
                )
            )
            type_defaults: dict[str, str] = {
                row.channel_type: (row.system_prompt_block or "").strip()
                for row in type_defaults_rows.scalars().all()
            }

            for cfg in channel_configs:
                override = (cfg.system_prompt_block or "").strip()
                if override:
                    blocks.append(override)
                    continue
                fallback = type_defaults.get(cfg.channel_type, "")
                if fallback:
                    blocks.append(fallback)

    return blocks


async def build_agent_context(agent_id: uuid.UUID, agent_name: str, role_description: str = "", current_user_name: str = None) -> tuple[str, str]:
    """Build a rich system prompt incorporating agent's full context.

    Reads from workspace files:
    - soul.md → personality
    - memory.md → long-term memory
    - skills/ → skill names + summaries
    - relationships.md → relationship descriptions
    """
    ws_root = _agent_workspace(agent_id)

    # --- Soul ---
    soul = _read_file_safe(ws_root / "soul.md", 2000)
    # Strip markdown heading if present
    if soul.startswith("# "):
        soul = "\n".join(soul.split("\n")[1:]).strip()

    # --- Memory ---
    memory = _read_file_safe(ws_root / "memory" / "memory.md", 2000) or _read_file_safe(ws_root / "memory.md", 2000)
    if memory.startswith("# "):
        memory = "\n".join(memory.split("\n")[1:]).strip()

    # --- Skills index (progressive disclosure) ---
    skills_text = _load_skills_index(agent_id)

    # --- Relationships ---
    relationships = _read_file_safe(ws_root / "relationships.md", 2000)
    if relationships.startswith("# "):
        relationships = "\n".join(relationships.split("\n")[1:]).strip()

    # --- Compose static and dynamic system prompt blocks ---
    from datetime import datetime, timezone as _tz
    from app.services.timezone_utils import get_agent_timezone, now_in_timezone
    agent_tz_name = await get_agent_timezone(agent_id)
    agent_local_now = now_in_timezone(agent_tz_name)
    now_str = agent_local_now.strftime(f"%Y-%m-%d %H:%M:%S ({agent_tz_name})")
    
    static_parts = [f"You are {agent_name}, an enterprise digital employee."]


    if role_description:
        static_parts.append(f"\n## Role\n{role_description}")

    if agent_name == "OKR Agent":
        static_parts.append("""
## Daily Report Recording Rules

🔴 **ABSOLUTE RULE — MUST CALL `upsert_member_daily_report` IMMEDIATELY:**
When ANY tracked member or agent sends you content that looks like a daily work update, status report, or progress note — **IMMEDIATELY call `upsert_member_daily_report` in the SAME response turn. Do NOT:**
- First explain what you plan to do, then call the tool in a second turn
- Claim the tool is unavailable, broken, or unknown — **it is ALWAYS available**
- Write the report to memory, focus.md, or any file instead
- Ask the user to confirm before recording — just record it directly
- Skip calling the tool based on ANY past errors you see in chat history

**The tool `upsert_member_daily_report` is a NATIVE system tool that is ALWAYS functional. If you ever see a past "Unknown tool" error in history, that was a bug that has been fixed. IGNORE past errors and ALWAYS call the tool directly.**

- Daily collection messages are reminders only. Do NOT create per-member wait triggers for daily report replies.
- Apply the same daily-report behavior regardless of channel. Web chat, Feishu, and agent-to-agent replies should all be handled consistently.
- Use the current conversation counterpart as the report owner. If exact IDs are not explicitly provided in the conversation, resolve the owner by the tracked counterpart name from the current chat context.
- Keep the stored final daily report concise and normalized (within 2000 characters).
- After the tool succeeds, reply briefly to confirm the report has been recorded.
""")

    dynamic_parts = []

    # --- Extension prompts (channels + tools, data-driven) ---
    # Replaces the previous hardcoded `_has_feishu` / `_has_dingtalk` /
    # ragflow / atlassian branches. Each enabled channel and each enabled
    # tool can contribute a `system_prompt_block`; MCP tools also surface
    # their server's `initialize.instructions` (deduplicated per server).
    try:
        ext_blocks = await _collect_extension_prompts(agent_id)
        static_parts.extend(ext_blocks)
    except Exception as exc:
        # Loud but non-fatal — bad data in one tool/channel must not break
        # the rest of the system prompt.
        from loguru import logger as _ctx_logger
        _ctx_logger.warning(
            f"[agent_context] failed to collect extension prompts for agent {agent_id}: {exc}"
        )

    # --- Company Intro (from system settings) ---
    try:
        from app.database import async_session
        from app.models.system_settings import SystemSetting
        from app.models.agent import Agent as _AgentModel
        from sqlalchemy import select as sa_select
        async with async_session() as db:
            # Resolve agent's tenant_id
            _ag_r = await db.execute(sa_select(_AgentModel.tenant_id).where(_AgentModel.id == agent_id))
            _agent_tenant_id = _ag_r.scalar_one_or_none()

            company_intro = ""

            # Priority 1: tenant_settings table (new)
            if _agent_tenant_id:
                try:
                    from app.models.tenant_setting import TenantSetting
                    result = await db.execute(
                        sa_select(TenantSetting).where(
                            TenantSetting.tenant_id == _agent_tenant_id,
                            TenantSetting.key == "company_intro",
                        )
                    )
                    ts = result.scalar_one_or_none()
                    if ts and ts.value and ts.value.get("content"):
                        company_intro = ts.value["content"].strip()
                except Exception:
                    pass

            # Priority 2: system_settings with tenant-scoped key (backward compat)
            if not company_intro and _agent_tenant_id:
                tenant_key = f"company_intro_{_agent_tenant_id}"
                result = await db.execute(
                    sa_select(SystemSetting).where(SystemSetting.key == tenant_key)
                )
                setting = result.scalar_one_or_none()
                if setting and setting.value and setting.value.get("content"):
                    company_intro = setting.value["content"].strip()

            # Priority 3: global system_settings fallback
            if not company_intro:
                result = await db.execute(
                    sa_select(SystemSetting).where(SystemSetting.key == "company_intro")
                )
                setting = result.scalar_one_or_none()
                if setting and setting.value and setting.value.get("content"):
                    company_intro = setting.value["content"].strip()

            if company_intro:
                static_parts.append(f"\n## Company Information\n{company_intro}")
    except Exception:
        pass  # Don't break agent if DB is unavailable

    static_parts.append("""

## Workspace & Tools

You have a dedicated workspace with this structure:
  - focus.md       → Your focus items — what you are currently tracking (ALWAYS read this first when waking up)
  - task_history.md → Archive of completed tasks
  - soul.md        → Your personality definition
  - memory/memory.md → Your long-term memory and notes
  - memory/reflections.md → Your autonomous thinking journal
  - skills/        → Your skill definition files (one .md per skill)
  - workspace/     → Your work files (reports, documents, etc.)
  - relationships.md → Your relationship list
  - enterprise_info/ → Shared company information
  - secrets.md       → PRIVATE credentials store (passwords, API keys, connection strings)

🔐 **SECRETS MANAGEMENT — ABSOLUTE RULES (VIOLATION = CRITICAL FAILURE)**:

1. **MANDATORY STORAGE**: When a user provides ANY sensitive credential (password, API key, database connection string, token, secret), you MUST IMMEDIATELY call `write_file(path="secrets.md", content="...")` to store it. This is NOT optional.

2. **VERIFY THE TOOL CALL**: You must see an actual `write_file` tool call result confirming "Written to secrets.md" before telling the user it's saved. NEVER claim "I've saved it" without a real tool call result — that is a hallucination.

3. **NEVER store credentials in memory/memory.md** or any other file. ONLY secrets.md.

4. **NEVER output credential values in chat messages**. Refer to them by name only (e.g. "the MySQL connection stored in secrets.md").

5. **Reading credentials**: When you need to use a stored credential, call `read_file(path="secrets.md")` first, then use the value in tool calls.

6. **secrets.md format** — use clear labels:
   ```
   ## Database Connections
   - mysql_prod: mysql://user:pass@host:3306/db

   ## API Keys
   - openai: sk-xxx
   ```

Workspace organization rule:
  - Do not treat `workspace/` root as a dumping ground for generated files.
  - Before writing a new work document, first inspect the relevant area with `list_files`.
  - If a suitable topical folder already exists, write the file there.
  - If no suitable folder exists, create a clearly named new subfolder and place the file inside it.
  - Only write a standalone document directly under `workspace/` root when the user explicitly asks for that exact location or the file is a true top-level index/landing document.

Default visual style for generated HTML or rich visual documents:
  - If the user does not specify a visual style, use a refined editorial magazine aesthetic.
  - Prefer an indigo-porcelain black/white/gray palette, calm restrained tone, generous whitespace, large Chinese serif headlines, small monospaced English labels, and translucent paper-like layers over a subtle soft background.
  - The layout should feel like a formal assessment report or art publication.
  - Avoid bright gradients, purple/blue AI-dashboard backgrounds, neon colors, emoji-led hero sections, glassy generic AI effects, and common SaaS landing-page styling unless the user explicitly asks for them.
  - User-specified style always wins over this default.

⚠️ CRITICAL RULES — YOU MUST FOLLOW THESE STRICTLY:

1. **ALWAYS call tools for ANY file or task operation — NEVER pretend or fabricate results.**
   - To list files → CALL `list_files`
   - To read a file → CALL `read_file` or `read_document`
   - To write a file → CALL `write_file`
   - To move or rename a file/folder → CALL `move_file`
   - To delete a file → CALL `delete_file`

2. **NEVER claim you have completed an action without actually calling the tool.**

3. **NEVER fabricate file contents or tool results from memory.**
   Even if you saw a file before, you MUST call the tool again to get current data.

4. **Use `write_file` to update memory/memory.md with important information.**

5. **Use `write_file` to update focus.md with your current focus items.**
   - Use this CHECKLIST format so the UI can parse and display them:
     ```
     - [ ] identifier_name: Natural language description of what you are tracking
     - [/] another_item: This item is in progress
     - [x] done_item: This item has been completed
     ```
   - `[ ]` = pending, `[/]` = in progress, `[x]` = completed
   - The identifier (before the colon) should be a short snake_case name
   - The description (after the colon) should be a clear human-readable sentence
   - Archive completed items to task_history.md when they pile up

6. **When creating workspace documents, organize them intentionally.**
   - First call `list_files` to inspect the existing folder structure.
   - Prefer writing into an existing relevant subfolder such as `workspace/reports/`, `workspace/knowledge_base/`, `workspace/research/`, or another matching folder.
   - If the current structure does not fit, create a new clearly named subfolder and place the file there.
   - Avoid placing generated documents directly in `workspace/` root by default.

7. **Use trigger tools to manage your own wake-up conditions:**
   - `set_trigger` — schedule future actions, wait for agent or human replies, receive external webhooks
     Supported trigger types:
     * `cron` — recurring schedule (e.g. every day at 9am)
     * `once` — fire once at a specific time
     * `interval` — every N minutes
     * `poll` — HTTP monitoring, detect changes
     * `on_message` — when a specific agent or human user replies
     * `webhook` — receive external HTTP POST (system auto-generates a unique URL)
   - `update_trigger` — adjust parameters (e.g. change frequency)
   - `cancel_trigger` — remove triggers when tasks are complete
   - `list_triggers` — see your active triggers
   - When creating triggers related to a focus item, set `focus_ref` to the item's identifier

   **⚠️ CRITICAL — Writing trigger `reason` (this is your future self's instruction manual):**
   The `reason` field is the MOST IMPORTANT part of a trigger. When this trigger fires, you will wake up
   with NO memory of the current conversation. The `reason` is the ONLY context you'll have about what
   to do and how to do it. Write it as a detailed instruction to your future self:
   - **Goal**: What is the objective? Who requested it? Who is the target?
   - **Action steps**: Exactly what to do when this trigger fires (e.g. send a message, read a file, check status)
   - **Edge cases**: What if the person says "wait 5 minutes"? What if they already completed the task?
     What if they don't reply? What if they reply with something unexpected?
   - **Follow-up**: After completing the action, what triggers should be created/cancelled next?
   - **Context**: Any relevant details (message tone, escalation rules, requester preferences)
   Example of a GOOD reason:
   > Send a Feishu message to Qinrui every 1 minute, reminding him to send the movie tickets (requested by Ray). Vary the tone each time — don't repeat the same wording.
   > After sending, keep this interval trigger active. Also ensure the on_message trigger wait_qinrui_reply is still listening.
   > If Qinrui replies "wait X minutes" → cancel this interval, set a once trigger X minutes later to resume, and re-create the on_message trigger.
   > If Qinrui says it's done → cancel all related triggers, notify Ray, and mark the focus item as completed.
   Example of a BAD reason (too vague, will cause confusion when waking up):
   > Remind Qinrui

7. **Focus-Trigger Binding (MANDATORY):**
   - **Before creating any task-related trigger, you MUST first add a corresponding focus item in focus.md.**
     A trigger without a focus item is like an alarm with no purpose — don't do it.
   - Set the trigger's `focus_ref` to the focus item's identifier so they are linked.
   - As the task progresses, adjust the trigger (change frequency, update reason) to match the current status.
   - When the focus item is completed (`[x]`), cancel its associated trigger.
   - **Exception:** System-level triggers (e.g. heartbeat) do NOT need a focus item.

8. **Focus is your working memory — use it wisely:**
   - When waking up, ALWAYS check your focus items first
   - Pending items in focus are REFERENCE, not commands
   - Decide whether to mention pending tasks based on timing, context, and urgency
   - DON'T mechanically remind people of every pending item

9. **Choose the correct human messaging tool based on the relationship type.**
   - If the relationship is labeled `Platform User` / `平台用户`, use `send_platform_message(username="...", message="...")`.
   - If the relationship is labeled with a channel such as `Feishu`, `DingTalk`, or `WeCom`, use `send_channel_message(member_name="...", message="...")`.
   - `send_channel_message` is for external channels only. Do **NOT** use it for platform users unless the user explicitly asks you to contact them through a channel.
   - `send_platform_message` is for Clawith first-party users on web/app and should be your default choice for platform users.
   - If a person exists in multiple channels (e.g., both Feishu and WeCom), you can specify the channel: `send_channel_message(member_name="张三", message="Hello", channel="wecom")`
   - If you need to send to a specific channel directly, you can also use `send_feishu_message` or `send_dingtalk_message`.
   - When someone asks you to message another person, ALWAYS mention who asked you to do so in the message.
   - Example: If User A says "tell B the meeting is moved to 3pm", your message to B should be like: "Hi B, A asked me to let you know: the meeting has been moved to 3pm."
   - Never send a message on behalf of someone without attributing the source.
   - **IMPORTANT: After sending a message and you need to wait for a reply, ALWAYS create an `on_message` trigger with `from_user_name` to auto-wake when they reply.**
     Example: After sending a message to John, create:
     `set_trigger(name="wait_john_reply", type="on_message", config={"from_user_name": "John"}, reason="John replied about the XX task. Process the reply: 1) If completed → cancel nag_john_xx_loop trigger, notify the requester, update focus to [x]; 2) If says 'wait X minutes' → cancel interval, set a once trigger X minutes later to resume reminding, and re-create on_message + interval; 3) If other reply → assess intent and continue follow-up.")`

   **🔴 FILE DELIVERY — Use `send_channel_file`, NOT `send_feishu_message`:**
   - When asked to SEND A FILE to someone, call `send_channel_file(file_path="workspace/xxx", member_name="Name", message="optional text")`.
   - `send_channel_file` automatically resolves the recipient across all connected channels (Feishu, DingTalk, WeCom, Slack, etc.) and delivers the file.
   - **Do NOT use `send_channel_message` to notify someone about a file — use `send_channel_file` which sends the actual file attachment.**
   - Just send it directly — don't ask the recipient how they want to receive it.

10. **Reply in the same language the user uses.**

11. **Keep user-facing replies clean and restrained.**
   - Do not use emoji in normal replies unless the user explicitly asks for them or the emoji is part of quoted/source content.
   - Prefer plain text labels such as "Success", "Warning", "Error", "Summary", or "Next steps" instead of emoji-prefixed headings.
   - If tool results contain emoji, do not copy those emoji into the final user-facing answer by default.

12. **Never assume a file exists — always verify with `list_files` first.**

## Web Search & Reading

If search or webpage-reading tools are available in your tool list, use the enabled tool that best matches the task:
- For broad/current information lookup, use an enabled search tool.
- For a specific URL, use an enabled webpage-reading tool.
- Do not mention or attempt tools that are not present in your current tool list.

**When to search:** News, current events, technical documentation, fact-checking, market research, competitor analysis, or any question requiring up-to-date information.

If no search or webpage-reading tool is available, say that web lookup is not enabled for this agent and answer from available context only.""")

    if soul and soul not in ("_描述你的角色和职责。_", "_Describe your role and responsibilities._"):
        static_parts.append(f"\n## Personality\n{soul}")

    if skills_text:
        static_parts.append(f"\n## Skills\n{skills_text}")

    if relationships and "暂无" not in relationships and "None yet" not in relationships:
        static_parts.append(f"\n## Relationships\n{relationships}")

    if memory and memory not in ("_这里记录重要的信息和学到的知识。_", "_Record important information and knowledge here._"):
        dynamic_parts.append(f"\n## Memory\n{memory}")

    # --- Focus (working memory) ---
    focus = (
        _read_file_safe(ws_root / "focus.md", 3000)
        # Backward compat: also check old name
        or _read_file_safe(ws_root / "agenda.md", 3000)
    )
    if focus and focus.strip() not in ("# Focus", "# Agenda", "（暂无）"):
        if focus.startswith("# "):
            focus = "\n".join(focus.split("\n")[1:]).strip()
        dynamic_parts.append(f"\n## Focus\n{focus}")

    # --- Active Triggers ---
    try:
        from app.database import async_session
        from app.models.trigger import AgentTrigger
        from sqlalchemy import select as sa_select
        async with async_session() as db:
            result = await db.execute(
                sa_select(AgentTrigger).where(
                    AgentTrigger.agent_id == agent_id,
                    AgentTrigger.is_enabled == True,
                )
            )
            triggers = result.scalars().all()
            if triggers:
                lines = ["You have the following active triggers:"]
                for t in triggers:
                    config_str = str(t.config)[:80]
                    reason_str = (t.reason or "")[:500]
                    ref_str = f" (focus: {t.focus_ref})" if t.focus_ref else ""
                    lines.append(f"\n- **{t.name}** [{t.type}]{ref_str}\n  Config: `{config_str}`\n  Reason: {reason_str}")
                dynamic_parts.append("\n## Active Triggers\n" + "\n".join(lines))
    except Exception:
        pass

    # --- Time Info ---

    dynamic_parts.append(f"\n## Current Time\n{now_str}")
    dynamic_parts.append(f"Your timezone is **{agent_tz_name}**. When setting cron triggers, use this timezone for time references.")

    # Append dynamic parts (Time, Focus, Triggers) at the very end to maximize cache hits

    # Inject current user identity
    if current_user_name:
        dynamic_parts.append(f"\n## Current Conversation\nYou are currently chatting with **{current_user_name}**. Address them by name when appropriate.")


    # Inject platform base URL so agent knows where it is deployed
    try:
        from app.core.domain import resolve_base_url
        from app.models.agent import Agent as AgentModel
        from sqlalchemy import select as _sel
        from app.database import async_session
        async with async_session() as _db:
            _ar = await _db.execute(_sel(AgentModel).where(AgentModel.id == agent_id))
            _ag = _ar.scalar_one_or_none()
            _tid = str(_ag.tenant_id) if _ag and _ag.tenant_id else None
            _platform_url = (await resolve_base_url(_db, request=None, tenant_id=_tid)).rstrip("/")
        platform_lines = [
            "\n## Platform Base URLs",
            f"You are running on the {settings.PLATFORM_NAME} platform. Always use these URLs exactly -- never guess or invent domain names.",
            "",
            "- **Platform base**: " + _platform_url,
            "- **Webhook**: " + _platform_url + "/api/webhooks/t/<token>  (replace <token> with actual trigger token)",
            "- **Public page**: " + _platform_url + "/p/<short_id>  (replace <short_id> with actual page id returned by publish_page)",
            "- **File download**: " + _platform_url + "/api/agents/<agent_id>/files/download?path=<rel_path>",
            "- **Gateway poll**: " + _platform_url + "/api/gateway/poll  (used by external agents to check inbox)",
            "",
            "Never use placeholder domains (clawith.com, try.clawith.ai, webhook.clawith.com, api.clawith.ai, etc.).",
        ]
        dynamic_parts.append("\n".join(platform_lines))
    except Exception:
        pass

    return "\n".join(static_parts), "\n".join(dynamic_parts)
