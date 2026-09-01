"""OKR outreach and daily collection routes."""

import uuid
from datetime import date

from fastapi import Depends, HTTPException
from sqlalchemy import select

from app.api.auth import get_current_user
from app.api.okr_shared import (
    _compute_current_period,
    _get_or_create_settings,
    async_session,
    router,
)
from app.models.okr import OKRKeyResult, OKRObjective


@router.get("/members-without-okr")
async def members_without_okr(user=Depends(get_current_user)):
    """Return tracked members (those in OKR Agent's relationship list) who lack
    OKRs in the current period.  Also returns:
    - okr_agent_id        : UUID of the OKR Agent for the chat-link button
    - company_okr_exists  : bool — whether a company-level objective exists
    - tracked_user_ids    : UUIDs of all tracked platform users (for UI filtering)
    - tracked_agent_ids   : UUIDs of all tracked agents (for UI filtering)
    """
    from app.models.agent import Agent
    from app.models.org import AgentRelationship, AgentAgentRelationship
    from app.models.user import User

    async with async_session() as db:
        settings = await _get_or_create_settings(db, user.tenant_id)
        if not settings.enabled:
            raise HTTPException(403, "OKR is not enabled for this tenant")

        ps, pe = _compute_current_period(
            settings.period_frequency, settings.period_length_days
        )
        await db.commit()

    async with async_session() as db:
        # ── Check if a company-level OKR exists this period ──────────────────
        co_result = await db.execute(
            select(OKRObjective.id).where(
                OKRObjective.tenant_id == user.tenant_id,
                OKRObjective.owner_user_id.is_(None),
                OKRObjective.owner_agent_id.is_(None),
                OKRObjective.period_start >= ps,
                OKRObjective.period_end <= pe,
                OKRObjective.status != "archived",
            ).limit(1)
        )
        company_okr_exists: bool = co_result.scalar_one_or_none() is not None

        covered_users = set((await db.execute(
            select(OKRObjective.owner_user_id).where(
                OKRObjective.tenant_id == user.tenant_id,
                OKRObjective.period_start >= ps,
                OKRObjective.period_end <= pe,
                OKRObjective.status != "archived",
                OKRObjective.owner_user_id.isnot(None),
            )
        )).scalars().all())
        covered_agents = set((await db.execute(
            select(OKRObjective.owner_agent_id).where(
                OKRObjective.tenant_id == user.tenant_id,
                OKRObjective.period_start >= ps,
                OKRObjective.period_end <= pe,
                OKRObjective.status != "archived",
                OKRObjective.owner_agent_id.isnot(None),
            )
        )).scalars().all())

        # ── Get the OKR Agent from Settings ──────────────────────────────────
        settings = await _get_or_create_settings(db, user.tenant_id)
        okr_agent_id_val: uuid.UUID | None = settings.okr_agent_id
        okr_agent_id_str: str | None = str(okr_agent_id_val) if okr_agent_id_val else None

        # ── Fetch tracked members from OKR Agent's relationship list ──────────
        tracked_user_ids: list[str] = []
        tracked_agent_ids: list[str] = []
        members_without_okr: list[dict] = []

        if okr_agent_id_val:
            user_rows = (await db.execute(
                select(User.id, User.display_name, User.avatar_url)
                .join(AgentRelationship, AgentRelationship.user_id == User.id)
                .where(
                    AgentRelationship.agent_id == okr_agent_id_val,
                    User.tenant_id == user.tenant_id,
                    User.is_active == True,  # noqa: E712
                )
            )).fetchall()
            for row in user_rows:
                tracked_user_ids.append(str(row.id))
                if row.id not in covered_users:
                    members_without_okr.append({
                        "user_id": str(row.id), "agent_id": None,
                        "display_name": row.display_name or "", "avatar_url": row.avatar_url or "",
                    })

            # ── Agent members via AgentAgentRelationship ───────────────────────
            agent_rel_result = await db.execute(
                select(Agent.id, Agent.name, Agent.avatar_url)
                .join(AgentAgentRelationship, AgentAgentRelationship.target_agent_id == Agent.id)
                .where(
                    AgentAgentRelationship.agent_id == okr_agent_id_val,
                    Agent.is_system == False,  # noqa: E712
                    Agent.status.notin_(["stopped", "error"]),
                )
            )
            for row in agent_rel_result.fetchall():
                tracked_agent_ids.append(str(row.id))
                if row.id not in covered_agents:
                    members_without_okr.append({
                        "user_id": None,
                        "agent_id": str(row.id),
                        "display_name": row.name or "",
                        "avatar_url": row.avatar_url or "",
                    })

        # Fallback: OKR Agent not seeded, OR no relationships yet (sync not done)
        # In either case show ALL members so the panel is useful before first sync.
        if not okr_agent_id_val or (not tracked_user_ids and not tracked_agent_ids):
            agent_result = await db.execute(
                select(Agent.id, Agent.name, Agent.avatar_url).where(
                    Agent.tenant_id == user.tenant_id,
                    Agent.is_system == False,  # noqa: E712
                    Agent.status.notin_(["stopped", "error"]),
                )
            )
            for row in agent_result.fetchall():
                tracked_agent_ids.append(str(row.id))
                if row.id not in covered_agents:
                    members_without_okr.append({
                        "user_id": None, "agent_id": str(row.id),
                        "display_name": row.name or "",
                        "avatar_url": row.avatar_url or "",
                    })

            user_result = await db.execute(
                select(User.id, User.display_name, User.avatar_url).where(
                    User.tenant_id == user.tenant_id,
                )
            )
            for row in user_result.fetchall():
                tracked_user_ids.append(str(row.id))
                if row.id not in covered_users:
                    members_without_okr.append({
                        "user_id": str(row.id), "agent_id": None,
                        "display_name": row.display_name or "",
                        "avatar_url": row.avatar_url or "",
                    })

    # ── Check for recent oneshot failure notifications ──────────────────────
    last_outreach_error = None
    if okr_agent_id_val:
        from app.models.notification import Notification
        async with async_session() as db2:
            notif_result = await db2.execute(
                select(Notification)
                .where(
                    Notification.user_id == user.id,
                    Notification.ref_id == okr_agent_id_val,
                    Notification.type == "system",
                    Notification.title.contains("task failed"),
                )
                .order_by(Notification.created_at.desc())
                .limit(1)
            )
            notif = notif_result.scalar_one_or_none()
            if notif:
                last_outreach_error = {
                    "message": notif.body,
                    "timestamp": notif.created_at.isoformat() if notif.created_at else "",
                    "is_read": notif.is_read,
                }

    # ── Check for channel members whose channel is not configured on the OKR Agent ──
    channel_warnings: list[dict] = []
    if okr_agent_id_val and members_without_okr:
        # Collect unique channel types referenced by members without OKR
        from app.models.channel_config import ChannelConfig as _CC
        member_channels: dict[str, list[str]] = {}  # channel_name -> [member_names]
        for m in members_without_okr:
            ch = m.get("channel") or m.get("source_label")
            if ch and ch not in ("Platform User", "Web"):
                member_channels.setdefault(ch, []).append(m.get("display_name", "?"))

        if member_channels:
            # Map display channel names to channel_type enum values
            _channel_name_to_type = {
                "feishu": "feishu", "Feishu": "feishu",
                "dingtalk": "dingtalk", "DingTalk": "dingtalk",
                "wecom": "wecom", "WeCom": "wecom",
                "slack": "slack", "Slack": "slack",
                "discord": "discord", "Discord": "discord",
                "wechat": "wechat", "WeChat": "wechat",
            }
            needed_types = set()
            for ch_name in member_channels:
                ct = _channel_name_to_type.get(ch_name)
                if ct:
                    needed_types.add(ct)

            if needed_types:
                async with async_session() as db3:
                    configured_result = await db3.execute(
                        select(_CC.channel_type).where(
                            _CC.agent_id == okr_agent_id_val,
                            _CC.channel_type.in_(list(needed_types)),
                            _CC.is_configured == True,  # noqa: E712
                        )
                    )
                    configured_types = {row[0] for row in configured_result.fetchall()}

                missing_types = needed_types - configured_types
                # Build warnings for each missing channel
                _type_to_display = {v: k for k, v in _channel_name_to_type.items() if k[0].isupper()}
                for mt in missing_types:
                    display_name = _type_to_display.get(mt, mt)
                    # Find member names on this channel
                    affected = []
                    for ch_name, names in member_channels.items():
                        if _channel_name_to_type.get(ch_name) == mt:
                            affected.extend(names)
                    channel_warnings.append({
                        "channel_type": mt,
                        "channel_display": display_name,
                        "affected_members": affected,
                        "count": len(affected),
                    })

    return {
        "period_start": ps.isoformat(),
        "period_end": pe.isoformat(),
        "company_okr_exists": company_okr_exists,
        "okr_agent_id": okr_agent_id_str,
        "members_without_okr": members_without_okr,
        "tracked_user_ids": tracked_user_ids,
        "tracked_agent_ids": tracked_agent_ids,
        "total": len(members_without_okr),
        "last_outreach_error": last_outreach_error,
        "channel_warnings": channel_warnings,
    }


@router.post("/trigger-member-outreach")
async def trigger_member_outreach(user=Depends(get_current_user)):
    """Admin-initiated trigger: instruct the OKR Agent to contact all tracked
    members who haven't set their OKRs for the current period.

    Data flow:
      1. Backend queries tracked members (from AgentRelationship) who lack OKRs.
      2. Backend injects up to 3 recent chat messages per member as context.
      3. Builds a structured prompt and fires run_agent_oneshot as a background task.
      4. The OKR Agent LLM loop sends personalised messages via the correct channel,
         then reports success/failure back to the triggering admin.

    Returns immediately with status=accepted.
    """
    import asyncio
    from app.models.agent import Agent
    from app.models.org import AgentRelationship, AgentAgentRelationship
    from app.models.audit import ChatMessage
    from app.models.user import User

    async with async_session() as db:
        settings = await _get_or_create_settings(db, user.tenant_id)
        if not settings.enabled:
            raise HTTPException(403, "OKR is not enabled for this tenant")

        ps, pe = _compute_current_period(settings.period_frequency, settings.period_length_days)

        # ── Find the OKR Agent from Settings ─────────────────────────────────
        if not settings.okr_agent_id:
            raise HTTPException(
                404,
                "未找到 OKR 数字员工，请确认已启用 OKR 并完成初始化。",
            )
        okr_agent_result = await db.execute(select(Agent).where(Agent.id == settings.okr_agent_id))
        okr_agent = okr_agent_result.scalar_one_or_none()
        if not okr_agent:
            raise HTTPException(
                404,
                "未找到 OKR 数字员工，请确认已启用 OKR 并完成初始化。",
            )

        covered_users = set((await db.execute(
            select(OKRObjective.owner_user_id).where(
                OKRObjective.tenant_id == user.tenant_id,
                OKRObjective.period_start >= ps,
                OKRObjective.period_end <= pe,
                OKRObjective.status != "archived",
                OKRObjective.owner_user_id.isnot(None),
            )
        )).scalars().all())
        covered_agents = set((await db.execute(
            select(OKRObjective.owner_agent_id).where(
                OKRObjective.tenant_id == user.tenant_id,
                OKRObjective.period_start >= ps,
                OKRObjective.period_end <= pe,
                OKRObjective.status != "archived",
                OKRObjective.owner_agent_id.isnot(None),
            )
        )).scalars().all())

        # ── Fetch company OKRs + KRs for this period to share as context ─────
        company_okr_result = await db.execute(
            select(OKRObjective).where(
                OKRObjective.tenant_id == user.tenant_id,
                OKRObjective.owner_user_id.is_(None),
                OKRObjective.owner_agent_id.is_(None),
                OKRObjective.period_start >= ps,
                OKRObjective.period_end <= pe,
                OKRObjective.status != "archived",
            ).order_by(OKRObjective.created_at)
        )
        company_okrs = company_okr_result.scalars().all()

        # Fetch KRs for each company OKR
        company_okr_krs: dict[uuid.UUID, list] = {}
        for co in company_okrs:
            kr_result = await db.execute(
                select(OKRKeyResult)
                .where(OKRKeyResult.objective_id == co.id)
                .order_by(OKRKeyResult.created_at)
            )
            company_okr_krs[co.id] = kr_result.scalars().all()

        user_result = await db.execute(
            select(User)
            .join(AgentRelationship, AgentRelationship.user_id == User.id)
            .where(
                AgentRelationship.agent_id == okr_agent.id,
                User.tenant_id == user.tenant_id,
                User.is_active == True,  # noqa: E712
            )
        )
        tracked_users = user_result.scalars().all()

        # ── Fetch tracked agent members from AgentAgentRelationship ──────────
        agent_rel_result = await db.execute(
            select(Agent).join(
                AgentAgentRelationship,
                AgentAgentRelationship.target_agent_id == Agent.id,
            ).where(
                AgentAgentRelationship.agent_id == okr_agent.id,
                Agent.is_system == False,  # noqa: E712
                Agent.status.notin_(["stopped", "error"]),
            )
        )
        tracked_agents = agent_rel_result.scalars().all()

        # ── Build prompt context for each member without OKR ─────────────────
        # Also resolve admin username for the final summary message
        admin_result = await db.execute(
            select(User.display_name).where(User.id == user.id)
        )
        admin_row = admin_result.first()
        admin_username = (admin_row.display_name if admin_row else None) or str(user.id)

        await db.commit()

    # ── Assemble the list of members to contact ───────────────────────────────
    # (DB session is closed — all data fetched above)
    members_to_contact: list[str] = []
    index = 1

    async def _recent_msgs(target_user_id: uuid.UUID) -> list[tuple]:
        async with async_session() as history_db:
            result = await history_db.execute(
                select(ChatMessage.role, ChatMessage.content, ChatMessage.created_at)
                .where(
                    ChatMessage.agent_id == okr_agent.id,
                    ChatMessage.sender_user_id == target_user_id,
                )
                .order_by(ChatMessage.created_at.desc())
                .limit(3)
            )
            return list(reversed(result.all()))

    for platform_user in tracked_users:
        if platform_user.id in covered_users:
            continue
        msgs = await _recent_msgs(platform_user.id)

        # Format history
        if msgs:
            history_lines = []
            for role, content, created_at in msgs:
                ts = created_at.strftime("%m-%d %H:%M") if created_at else ""
                speaker = "You" if role == "assistant" else platform_user.display_name
                history_lines.append(f"  [{ts}] {speaker}: {content[:120]}")
            history_str = "\n".join(history_lines)
        else:
            history_str = "  (No previous conversation — treat this as first contact)"

        member_block = (
            f"--- Member {index}: {platform_user.display_name} ---\n"
            f"  user_id: {platform_user.id}\n"
            f"  Send using the exact user_id; choose the appropriate reachable route.\n"
            f"  Recent chat history (last 3 messages):\n"
            f"{history_str}"
        )
        members_to_contact.append(member_block)
        index += 1

    for agent_member in tracked_agents:
        if agent_member.id in covered_agents:
            continue
        # Embed the actual create_objective call template with the real UUID so the LLM
        # cannot accidentally substitute a placeholder or nil UUID.
        member_block = (
            f"--- Member {index}: {agent_member.name} [Agent] ---\n"
            f"  agent_id: {agent_member.id}\n"
            f"  STEP 1 → send_message_to_agent(agent_id=\"{agent_member.id}\",\n"
            f"             message=\"[OKR Agent] 请根据公司 OKR，描述您在本周期（{ps.isoformat()} ~ {pe.isoformat()}）"
            f"的主要目标（Objective）和关键结果（Key Results）。\")\n"
            f"  STEP 2 → Read the reply carefully from the tool result.\n"
            f"  STEP 3 → Call this EXACTLY (use the UUID below verbatim, do NOT invent one):\n"
            f"    create_objective(title=\"<their objective>\", agent_id=\"{agent_member.id}\",\n"
            f"                    period_start=\"{ps.isoformat()}\", period_end=\"{pe.isoformat()}\")\n"
            f"  STEP 4 → For EACH Key Result they mentioned:\n"
            f"    create_key_result(objective_id=\"<id from STEP 3 result>\",\n"
            f"                     title=\"<KR title>\", target_value=<number>, unit=\"<unit if stated>\")"
        )
        members_to_contact.append(member_block)
        index += 1

    if not members_to_contact:
        return {
            "status": "no_action",
            "message": "All tracked members already have OKRs set for this period. No outreach needed.",
            "okr_agent_id": str(okr_agent.id),
        }

    # ── Compose the final task prompt ─────────────────────────────────────────
    period_label = f"{ps.strftime('%Y-%m-%d')} to {pe.strftime('%Y-%m-%d')}"
    members_block = "\n\n".join(members_to_contact)

    # Build company OKR + KR context summary
    if company_okrs:
        company_okr_lines = []
        for i, co in enumerate(company_okrs, 1):
            company_okr_lines.append(f"  {i}. **{co.title}**")
            if co.description:
                company_okr_lines.append(f"     说明: {co.description[:120]}")
            krs = company_okr_krs.get(co.id, [])
            for j, kr in enumerate(krs, 1):
                target_str = f"（目标值: {kr.target_value} {kr.unit or ''}）" if kr.target_value else ""
                company_okr_lines.append(f"     KR{j}: {kr.title}{target_str}")
        company_okrs_block = "\n".join(company_okr_lines)
    else:
        company_okrs_block = "  (No company OKRs set yet for this period)"

    # Count agent vs human members for adaptive max_rounds
    n_agents = sum(1 for m in members_to_contact if "[Agent]" in m)
    n_humans = len(members_to_contact) - n_agents
    # human: 2 rounds (compose + send); agent: 6 rounds (send + reply + objective + 3 KRs)
    safe_max_rounds = n_humans * 2 + n_agents * 6 + 3

    task_prompt = f"""[ADMIN TRIGGER — OKR Member Outreach — ONE-SHOT TASK]

Current OKR period: {period_label}
Admin who triggered this: {admin_username}

━━━ COMPANY OBJECTIVES (share this context with each member) ━━━
{company_okrs_block}

━━━ YOUR TASK ━━━
Contact the {len(members_to_contact)} member(s) below who have NOT set their OKRs for this period.
• For [Agent] members: collect their OKR and record it immediately (see STEP 1-4 per member).
• For human members: send a warm reminder that includes the company OKR context above.

━━━ TOOL RULES (MANDATORY — DO NOT DEVIATE) ━━━
• For members tagged [Agent]:
  → Follow the STEP 1-4 sequence in their block exactly.
  → Use ONLY send_message_to_agent — never channel tools for agents.
• For human members:
  → Use the exact user_id in their block. Choose a valid reachable route based on tool results.
  → Never resolve or execute by display name or provider-specific identity.
  → Humans are fire-and-forget — do NOT wait for their reply.

━━━ STEP-BY-STEP ━━━
1. Process each member in order, following per-member instructions.
2. If a send or create fails: log the failure and continue.
3. STOP completely after processing all members — do not respond further.

━━━ MEMBERS TO CONTACT ({len(members_to_contact)} total) ━━━

{members_block}

━━━ BEGIN NOW ━━━
"""

    # ── Launch background task ────────────────────────────────────────────────
    from app.services.heartbeat import run_agent_oneshot

    asyncio.create_task(
        run_agent_oneshot(
            agent_id=okr_agent.id,
            prompt=task_prompt,
            triggered_by_user_id=user.id,
            max_rounds=safe_max_rounds,
        )
    )

    return {
        "status": "accepted",
        "message": (
            f"OKR Agent outreach task triggered for {len(members_to_contact)} member(s). "
            "You can check the conversation details in the OKR Agent's chat history."
        ),
        "okr_agent_id": str(okr_agent.id),
        "members_count": len(members_to_contact),
    }


@router.post("/trigger-daily-collection")
async def trigger_daily_collection(user=Depends(get_current_user)):
    """Admin-triggered daily collection for tracked OKR relationships only."""
    if getattr(user, "role", None) not in ("org_admin", "platform_admin"):
        raise HTTPException(403, "Only org admins can trigger daily collection")
    from app.services.okr_daily_collection import trigger_daily_collection_for_tenant

    try:
        result = await trigger_daily_collection_for_tenant(user.tenant_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    if result["total_targets"] == 0:
        return {
            "status": "no_action",
            "message": "OKR Agent has no tracked relationships to collect from.",
            "okr_agent_id": result["okr_agent_id"],
            "member_count": 0,
        }

    return {
        "status": "accepted",
        "message": (
            f"Daily OKR collection sent to {result['sent_humans']} human target(s) and "
            f"{result['sent_agents']} agent target(s). Reply triggers are now active."
        ),
        "okr_agent_id": result["okr_agent_id"],
        "member_count": result["total_targets"],
    }
