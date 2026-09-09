"""Heartbeat service — proactive agent awareness loop.

Periodically triggers agents to check their environment and take autonomous
actions. Inspired by OpenClaw's heartbeat
mechanism.

Runs as a background task inside the FastAPI process.
"""

import asyncio
import uuid
from datetime import datetime, timezone, timedelta

from loguru import logger

from app.core.logging_config import new_trace_id
from sqlalchemy import select, update, or_
from app.services.storage import agent_storage_key, get_storage_backend


from app.services.heartbeat_oneshot import run_agent_oneshot as run_agent_oneshot


# Default heartbeat instruction used when HEARTBEAT.md doesn't exist
DEFAULT_HEARTBEAT_INSTRUCTION = """[Heartbeat Check]

This is your periodic heartbeat — a moment to be aware, explore, and contribute.

## Phase 1: Review Context & Discover Interest Points

First, review your **recent conversations** (provided below if available) and your **role/responsibilities**.
Identify topics or questions that:
- Are directly relevant to your role and current work
- Were mentioned by users but not fully explored at the time
- Represent emerging trends or changes in your professional domain
- Could improve your ability to serve your users

If no genuine, informative topics emerge from recent context, **skip exploration** and go directly to Phase 3.
Do NOT search for generic or obvious topics just to fill time. Quality over quantity.

## Phase 2: Targeted Exploration (Conditional)

Only if you identified genuine interest points in Phase 1:

1. Use `web_search` to investigate (maximum 5 searches per heartbeat)
2. Keep searches **tightly scoped** to your role and recent work topics
3. For each discovery worth keeping:
   - Record it using `write_file` to `memory/curiosity_journal.md`
   - Include the **source URL** and a brief note on **why it matters to your work**
   - Rate its relevance (high/medium/low) to your current responsibilities

Format for curiosity_journal.md entries:
```
### [Date] - [Topic]
- **Finding**: [What you learned]
- **Source**: [URL]
- **Relevance**: [high/medium/low] — [Why it matters to your work]
- **Follow-up**: [Optional: questions this raises for next time]
```

## Phase 3: Wrap Up

- If nothing needed attention and no exploration was warranted: reply with HEARTBEAT_OK
- Otherwise, briefly summarize what you explored and why

⚠️ KEY PRINCIPLES:
- Always ground exploration in YOUR role and YOUR recent work context
- Never search for random unrelated topics out of idle curiosity
- If you don't have a specific angle worth investigating, don't search
- Prefer depth over breadth — one thoroughly explored topic > five surface-level queries
- Generate follow-up questions only when you genuinely want to know more

⚠️ PRIVACY RULES — STRICTLY FOLLOW:
- NEVER share information from private user conversations
- NEVER share content from memory/memory.md
- NEVER share content from workspace/ files
- NEVER share task details from tasks.json
- If unsure whether something is private, do NOT share it
"""

PRIVATE_AGENT_HEARTBEAT_APPEND = """

⚠️ PRIVATE AGENT RULE — STRICTLY FOLLOW:
- If you have no user-facing or task-facing work to do, reply with HEARTBEAT_OK.
"""


def _is_in_active_hours(active_hours: str, tz_name: str = "UTC") -> bool:
    """Check if current time is within the agent's active hours.

    Format: "HH:MM-HH:MM" (e.g., "09:00-18:00")
    Uses agent's configured timezone (defaults to UTC).
    """
    try:
        from zoneinfo import ZoneInfo
        start_str, end_str = active_hours.split("-")
        sh, sm = map(int, start_str.strip().split(":"))
        eh, em = map(int, end_str.strip().split(":"))
        try:
            tz = ZoneInfo(tz_name)
        except (KeyError, Exception):
            tz = ZoneInfo("UTC")
        now = datetime.now(tz)
        current_minutes = now.hour * 60 + now.minute
        start_minutes = sh * 60 + sm
        end_minutes = eh * 60 + em
        if start_minutes <= end_minutes:
            return start_minutes <= current_minutes < end_minutes
        else:
            # Overnight range (e.g., "22:00-06:00")
            return current_minutes >= start_minutes or current_minutes < end_minutes
    except Exception:
        return True  # Default to active if parsing fails


async def _heartbeat_context(agent):
    """Snapshot the original heartbeat inputs before claiming an occurrence."""
    from app.core.plaza_feature import PLAZA_NOTIFICATION_TYPES
    from app.database import async_session
    from app.models.activity_log import AgentActivityLog
    from app.models.notification import Notification
    from app.services.agent_context import build_agent_context

    instruction = DEFAULT_HEARTBEAT_INSTRUCTION
    storage = get_storage_backend()
    key = agent_storage_key(agent.id, "HEARTBEAT.md")
    if await storage.exists(key):
        custom = (await storage.read_text(key, encoding="utf-8", errors="replace")).strip()
        if custom:
            instruction = custom + """
⚠️ PRIVACY RULES — STRICTLY FOLLOW:
- NEVER share information from private user conversations
- NEVER share content from memory/memory.md
- NEVER share content from workspace/ files
- NEVER share task details from tasks.json
"""
    if (agent.access_mode or "company") != "company":
        instruction += PRIVATE_AGENT_HEARTBEAT_APPEND
    static, dynamic = await build_agent_context(
        agent.id, agent.name, agent.role_description or "",
    )
    async with async_session() as db:
        activities = list(await db.scalars(select(AgentActivityLog).where(
            AgentActivityLog.agent_id == agent.id,
            AgentActivityLog.action_type.in_(["chat_reply", "tool_call", "task_created", "task_updated"]),
        ).order_by(AgentActivityLog.created_at.desc()).limit(50)))
        notices = list(await db.scalars(select(Notification).where(
            Notification.agent_id == agent.id,
            Notification.is_read.is_(False),
            Notification.type.not_in(PLAZA_NOTIFICATION_TYPES),
        ).order_by(Notification.created_at).limit(10)))
    if activities:
        instruction += "\n\n---\n## Recent Activity Context\n"
        instruction += "Here are your recent interactions and work to help you identify relevant topics:\n"
        instruction += "\n".join(
            f"- [{item.created_at:%m-%d %H:%M}] {item.action_type}: {item.summary[:120]}"
            for item in reversed(activities)
        )
    if notices:
        instruction += "\n\n---\n## Inbox (new messages for you — please review and respond if appropriate)\n"
        instruction += "\n".join(
            f"- [{item.type}] {item.title} from {item.sender_name or ''}: {(item.body or '')[:150]}"
            for item in notices
        )
    return instruction, [static, dynamic], [item.id for item in notices]


async def _heartbeat_tick():
    """Claim each due occurrence and its original input in one transaction."""
    from app.core.okr_feature import is_retired_okr_agent
    from app.database import async_session
    from app.models.agent import Agent
    from app.models.notification import Notification
    from app.models.tenant import Tenant
    from app.services.agent_execution.bridge import dispatch_background
    from app.services.audit_logger import write_audit_log
    from app.services.background_task_admission import create_background_turn
    from app.services.timezone_utils import get_agent_timezone_sync

    new_trace_id()
    now = datetime.now(timezone.utc)
    async with async_session() as db:
        agents = list(await db.scalars(select(Agent).where(
            Agent.heartbeat_enabled.is_(True), Agent.status.in_(["running", "idle"]),
        )))
        tenant_ids = {agent.tenant_id for agent in agents if agent.tenant_id}
        tenants = list(await db.scalars(select(Tenant).where(Tenant.id.in_(tenant_ids)))) if tenant_ids else []
        tenants_by_id = {tenant.id: tenant for tenant in tenants}
    triggered = 0
    for agent in agents:
        try:
            if agent.is_expired:
                continue
            if agent.expires_at and now >= agent.expires_at:
                async with async_session() as db:
                    await db.execute(update(Agent).where(Agent.id == agent.id).values(
                        is_expired=True, heartbeat_enabled=False, status="stopped",
                    ))
                    await db.commit()
                continue
            tz_name = get_agent_timezone_sync(agent, tenants_by_id.get(agent.tenant_id))
            if not _is_in_active_hours(agent.heartbeat_active_hours or "09:00-18:00", tz_name):
                continue
            interval = timedelta(minutes=agent.heartbeat_interval_minutes or 240)
            if agent.last_heartbeat_at and now - agent.last_heartbeat_at < interval:
                continue
            async with async_session() as db:
                if await is_retired_okr_agent(db, agent):
                    continue
            instruction, context, notice_ids = await _heartbeat_context(agent)
            async with async_session() as db:
                current = await db.scalar(select(Agent).where(
                    Agent.id == agent.id,
                    Agent.heartbeat_enabled.is_(True),
                    Agent.status.in_(["running", "idle"]),
                    or_(Agent.last_heartbeat_at.is_(None), Agent.last_heartbeat_at <= now - interval),
                ).with_for_update().execution_options(populate_existing=True))
                if current is None or current.is_expired:
                    continue
                if current.expires_at and now >= current.expires_at:
                    continue
                anchor = await create_background_turn(
                    db, agent=current, kind="heartbeat", reference_id=uuid.uuid4(),
                    user_prompt=instruction, execution_user_id=current.creator_id,
                    settings={"prepared_turn_context": context, "max_tool_rounds_override": 20},
                )
                current.last_heartbeat_at = now
                if notice_ids:
                    await db.execute(update(Notification).where(
                        Notification.id.in_(notice_ids), Notification.agent_id == current.id,
                    ).values(is_read=True))
                anchor_id = anchor.id
                await db.commit()
            await dispatch_background("app.services.background_turns:run_background_turn", anchor_id)
            triggered += 1
            await write_audit_log("heartbeat_fire", {
                "agent_name": agent.name, "turn_anchor_id": str(anchor_id),
            }, agent_id=agent.id)
        except Exception:
            logger.exception("Heartbeat admission failed for agent {}", agent.id)
    if triggered:
        await write_audit_log("heartbeat_tick", {
            "eligible_agents": len(agents), "triggered": triggered,
        })


async def start_heartbeat():
    """Start the background heartbeat loop."""
    while True:
        await _heartbeat_tick()
        await asyncio.sleep(60)
