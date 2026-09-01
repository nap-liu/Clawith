"""OKR query, reporting, and access helpers extracted from agent_tools."""

import uuid

from loguru import logger
from sqlalchemy import select

from app.database import async_session


def _compute_okr_period_bounds(frequency: str, length_days: int | None):
    """Return the current OKR period using the tenant's configured cadence."""
    from datetime import date, timedelta

    today = date.today()
    if frequency == "monthly":
        start = today.replace(day=1)
        if today.month == 12:
            end = today.replace(month=12, day=31)
        else:
            end = today.replace(month=today.month + 1, day=1) - timedelta(days=1)
    elif frequency == "custom" and length_days:
        epoch = date(1970, 1, 1)
        days_since_epoch = (today - epoch).days
        period_index = days_since_epoch // length_days
        start = epoch + timedelta(days=period_index * length_days)
        end = start + timedelta(days=length_days - 1)
    else:
        quarter = (today.month - 1) // 3 + 1
        start = date(today.year, (quarter - 1) * 3 + 1, 1)
        if quarter == 4:
            end = date(today.year, 12, 31)
        else:
            end = date(today.year, quarter * 3 + 1, 1) - timedelta(days=1)
    return start, end


async def _get_okr(agent_id: uuid.UUID | None, arguments: dict) -> str:
    """Return the full OKR board for the current period as formatted text.

    Includes company-level O+KR and every member's individual O+KR.
    This is a read-only tool available to all agents.
    """
    import json
    import httpx

    # Resolve tenant_id from the calling agent
    if not agent_id:
        return "OKR tools require agent context."

    try:
        from app.database import async_session
        from app.models.agent import Agent
        from app.models.okr import OKRObjective, OKRKeyResult, OKRSettings
        from app.models.user import User
        from sqlalchemy import select as _select
        from datetime import date, timedelta

        async with async_session() as db:
            # Look up the agent's tenant
            agent_result = await db.execute(_select(Agent).where(Agent.id == agent_id))
            agent = agent_result.scalar_one_or_none()
            if not agent:
                return "Agent not found."

            tenant_id = agent.tenant_id

            # Get OKR settings to determine period
            settings_result = await db.execute(_select(OKRSettings).where(OKRSettings.tenant_id == tenant_id))
            settings = settings_result.scalar_one_or_none()

            if not settings or not settings.enabled:
                return "OKR is not enabled for your organization."

            # Compute period bounds
            period_start = arguments.get("period_start")
            period_end = arguments.get("period_end")
            if period_start and period_end:
                ps = date.fromisoformat(period_start)
                pe = date.fromisoformat(period_end)
            else:
                ps, pe = _compute_okr_period_bounds(
                    settings.period_frequency,
                    settings.period_length_days,
                )

            # Fetch all active objectives
            obj_result = await db.execute(
                _select(OKRObjective)
                .where(
                    OKRObjective.tenant_id == tenant_id,
                    OKRObjective.period_start >= ps,
                    OKRObjective.period_end <= pe,
                    OKRObjective.status != "archived",
                )
                .order_by(OKRObjective.created_at)
            )
            objectives = obj_result.scalars().all()

            if not objectives:
                return f"No OKRs found for the current period ({ps} – {pe})."

            # Fetch all KRs
            obj_ids = [o.id for o in objectives]
            kr_result = await db.execute(
                _select(OKRKeyResult).where(OKRKeyResult.objective_id.in_(obj_ids)).order_by(OKRKeyResult.created_at)
            )
            all_krs = kr_result.scalars().all()

            krs_by_obj: dict = {}
            for kr in all_krs:
                krs_by_obj.setdefault(str(kr.objective_id), []).append(kr)

            # Resolve readable owner names so the OKR Agent can reason about
            # members by display name instead of raw UUIDs.
            user_owner_ids = [o.owner_user_id for o in objectives if o.owner_user_id]
            agent_owner_ids = [o.owner_agent_id for o in objectives if o.owner_agent_id]

            user_names: dict[uuid.UUID, str] = {}
            if user_owner_ids:
                u_result = await db.execute(_select(User.id, User.display_name).where(User.id.in_(user_owner_ids)))
                user_names = {row.id: (row.display_name or "") for row in u_result.fetchall()}

            agent_names: dict[uuid.UUID, str] = {}
            if agent_owner_ids:
                a_result = await db.execute(_select(Agent.id, Agent.name).where(Agent.id.in_(agent_owner_ids)))
                agent_names = {row.id: (row.name or "") for row in a_result.fetchall()}

            def _resolve_owner_label(obj: OKRObjective) -> str:
                if not obj.owner_user_id and not obj.owner_agent_id:
                    return "Company"
                if obj.owner_user_id:
                    name = user_names.get(obj.owner_user_id) or "Unknown user"
                    return f"{name} | user_id:{obj.owner_user_id}"
                name = agent_names.get(obj.owner_agent_id) or "Unknown agent"
                return f"{name} | agent_id:{obj.owner_agent_id}"

        # Format output
        lines = [f"# OKR Board — {ps} to {pe}\n"]

        company_objs = [o for o in objectives if not o.owner_user_id and not o.owner_agent_id]
        member_objs = [o for o in objectives if o.owner_user_id or o.owner_agent_id]

        if company_objs:
            lines.append("## Company Objectives")
            for o in company_objs:
                krs = krs_by_obj.get(str(o.id), [])
                pct = 0
                if krs:
                    pct = int(sum(min(k.current_value / k.target_value, 1) for k in krs) / len(krs) * 100)
                lines.append(f"\n**O: {o.title}** [{pct}%]  objective_id={o.id}")
                for kr in krs:
                    lines.append(
                        f"  - KR ({kr.status}): {kr.title}  "
                        f"[{kr.current_value}/{kr.target_value} {kr.unit or ''}]  "
                        f" kr_id={kr.id}"
                    )

        if member_objs:
            lines.append("\n## Member Objectives")
            for o in member_objs:
                owner_label = _resolve_owner_label(o)
                krs = krs_by_obj.get(str(o.id), [])
                lines.append(f"\n**{owner_label}** | O: {o.title}  objective_id={o.id}")
                for kr in krs:
                    lines.append(
                        f"  - KR ({kr.status}): {kr.title}  "
                        f"[{kr.current_value}/{kr.target_value} {kr.unit or ''}]  "
                        f" kr_id={kr.id}"
                    )

        return "\n".join(lines)

    except Exception as e:
        logger.exception(f"[OKR] get_okr failed for agent {agent_id}")
        return f"Failed to retrieve OKR data: {str(e)[:200]}"


async def _get_my_okr(agent_id: uuid.UUID | None, arguments: dict) -> str:
    """Return the calling agent's own Objectives and KRs.

    Includes objective_id and kr_id values so the agent can update existing OKRs
    instead of accidentally creating duplicate ones.
    """
    if not agent_id:
        return "OKR tools require agent context."

    try:
        from app.database import async_session
        from app.models.agent import Agent
        from app.models.okr import OKRObjective, OKRKeyResult, OKRSettings
        from sqlalchemy import select as _select
        from datetime import date, timedelta

        async with async_session() as db:
            agent_result = await db.execute(_select(Agent).where(Agent.id == agent_id))
            agent = agent_result.scalar_one_or_none()
            if not agent:
                return "Agent not found."

            settings_result = await db.execute(_select(OKRSettings).where(OKRSettings.tenant_id == agent.tenant_id))
            settings = settings_result.scalar_one_or_none()
            if not settings or not settings.enabled:
                return "OKR is not enabled for your organization."

            ps, pe = _compute_okr_period_bounds(
                settings.period_frequency,
                settings.period_length_days,
            )

            obj_result = await db.execute(
                _select(OKRObjective).where(
                    OKRObjective.tenant_id == agent.tenant_id,
                    OKRObjective.owner_agent_id == agent_id,
                    OKRObjective.period_start >= ps,
                    OKRObjective.period_end <= pe,
                    OKRObjective.status != "archived",
                )
            )
            objectives = obj_result.scalars().all()

            if not objectives:
                return (
                    f"You have no OKRs set for the current period ({ps} – {pe}). "
                    "Contact the OKR Agent to set up your Objectives and Key Results."
                )

            obj_ids = [o.id for o in objectives]
            kr_result = await db.execute(
                _select(OKRKeyResult).where(OKRKeyResult.objective_id.in_(obj_ids)).order_by(OKRKeyResult.created_at)
            )
            all_krs = kr_result.scalars().all()

            krs_by_obj: dict = {}
            for kr in all_krs:
                krs_by_obj.setdefault(str(kr.objective_id), []).append(kr)

        lines = [
            f"# My OKRs — {ps} to {pe}\n",
            "If you need to revise an existing OKR, reuse the IDs below:",
            "- change Objective title/description/status with update_objective(objective_id=...)",
            "- change KR title/target/unit/focus/status with update_kr_content(kr_id=...)",
            "- change KR numeric progress with update_kr_progress(kr_id=...)",
            "",
        ]
        for o in objectives:
            krs = krs_by_obj.get(str(o.id), [])
            lines.append(f"**O: {o.title}**  objective_id={o.id}")
            if o.description:
                lines.append(f"  {o.description}")
            for kr in krs:
                lines.append(
                    f"  - [{kr.status}] {kr.title}  "
                    f"Progress: {kr.current_value}/{kr.target_value} {kr.unit or ''}  "
                    f"  kr_id={kr.id}"
                )
        return "\n".join(lines)

    except Exception as e:
        logger.exception(f"[OKR] get_my_okr failed for agent {agent_id}")
        return f"Failed to retrieve your OKR: {str(e)[:200]}"


async def _load_okr_request_context(
    db,
    agent_id: uuid.UUID,
    user_id: uuid.UUID | None,
) -> dict:
    from app.models.agent import Agent as AgentModel
    from app.models.user import User as UserModel

    ag_res = await db.execute(select(AgentModel).where(AgentModel.id == agent_id))
    agent = ag_res.scalar_one_or_none()
    requester = None
    if user_id:
        user_res = await db.execute(select(UserModel).where(UserModel.id == user_id))
        requester = user_res.scalar_one_or_none()

    return {
        "agent": agent,
        "tenant_id": getattr(agent, "tenant_id", None),
        "agent_is_system": bool(agent and agent.is_system),
        "requester": requester,
        "requester_user_id": user_id,
        "requester_is_admin": bool(requester and requester.role in ("org_admin", "platform_admin")),
    }


def _okr_permission_denied(message: str) -> str:
    return f"Permission denied: {message}"


def _can_access_existing_okr_target(
    ctx: dict,
    owner_user_id: uuid.UUID | None,
    owner_agent_id: uuid.UUID | None,
) -> str | None:
    if ctx["agent_is_system"]:
        if ctx["requester_is_admin"]:
            return None
        if owner_user_id != ctx["requester_user_id"] or owner_agent_id is not None:
            return _okr_permission_denied(
                "non-admin requests may only create or modify the requester's own personal OKRs. "
                "Do not create or edit company OKRs or other members' OKRs."
            )
        return None

    if owner_agent_id != ctx["agent"].id or owner_user_id is not None:
        return _okr_permission_denied("you can only create or modify your own agent OKRs.")
    return None


def _can_create_okr_target(
    ctx: dict,
    owner_user_id: uuid.UUID | None,
    owner_agent_id: uuid.UUID | None,
) -> str | None:
    if ctx["agent_is_system"]:
        if ctx["requester_is_admin"]:
            return None
        if owner_user_id != ctx["requester_user_id"] or owner_agent_id is not None:
            return _okr_permission_denied(
                "non-admin requests may only create the requester's own personal OKRs. "
                "Creating company OKRs or other members' OKRs requires an org admin."
            )
        return None

    if owner_agent_id != ctx["agent"].id or owner_user_id is not None:
        return _okr_permission_denied("you can only create OKRs for yourself.")
    return None


async def _update_kr_progress(agent_id: uuid.UUID | None, user_id: uuid.UUID | None, arguments: dict) -> str:
    """Update a KR's current_value. Only the owning agent may call this.

    Automatically writes an OKRProgressLog entry for history tracking.
    """
    if not agent_id:
        return "OKR tools require agent context."

    kr_id_str = arguments.get("kr_id", "").strip()
    value = arguments.get("value")
    note = arguments.get("note")

    if not kr_id_str:
        return "Missing required argument 'kr_id'. Call get_my_okr first to get your KR IDs."
    if value is None:
        return "Missing required argument 'value'."

    try:
        kr_id = uuid.UUID(kr_id_str)
    except ValueError:
        return f"Invalid kr_id format: {kr_id_str}"

    try:
        from app.models.okr import OKRObjective, OKRKeyResult, OKRProgressLog
        from sqlalchemy import select as _select
        from datetime import datetime

        async with async_session() as db:
            ctx = await _load_okr_request_context(db, agent_id, user_id)
            if not ctx["agent"]:
                return "Agent not found."

            result = await db.execute(
                _select(OKRKeyResult, OKRObjective)
                .join(OKRObjective, OKRKeyResult.objective_id == OKRObjective.id)
                .where(
                    OKRKeyResult.id == kr_id,
                    OKRObjective.tenant_id == ctx["tenant_id"],
                )
            )
            row = result.first()
            if not row:
                return f"Key Result {kr_id_str} not found in your organization."

            kr, obj = row
            permission_error = _can_access_existing_okr_target(ctx, obj.owner_user_id, obj.owner_agent_id)
            if permission_error:
                return permission_error

            prev_value = kr.current_value
            kr.current_value = float(value)
            kr.last_updated_at = datetime.utcnow()

            # Auto-determine status based on progress ratio
            ratio = kr.current_value / kr.target_value if kr.target_value else 0
            if ratio >= 1.0:
                kr.status = "completed"
            elif ratio >= 0.7:
                kr.status = "on_track"
            elif ratio >= 0.4:
                kr.status = "at_risk"
            else:
                kr.status = "behind"

            log = OKRProgressLog(
                kr_id=kr_id,
                previous_value=prev_value,
                new_value=float(value),
                source="self_report",
                note=note,
            )
            db.add(log)
            await db.commit()

        return f"KR updated: {kr.title}\n  {prev_value} → {value} {kr.unit or ''} (status: {kr.status})"

    except Exception as e:
        logger.exception(f"[OKR] update_kr_progress failed for agent {agent_id}")
        return f"Failed to update KR progress: {str(e)[:200]}"


async def _update_kr_content(agent_id: uuid.UUID | None, user_id: uuid.UUID | None, arguments: dict) -> str:
    """Update metadata/content fields of one of the caller's own KRs."""
    if not agent_id:
        return "OKR tools require agent context."

    kr_id_str = arguments.get("kr_id", "").strip()
    if not kr_id_str:
        return "Missing required argument 'kr_id'. Call get_my_okr first to get your KR IDs."

    try:
        kr_id = uuid.UUID(kr_id_str)
    except ValueError:
        return f"Invalid kr_id format: {kr_id_str}"

    supported_fields = {
        "title": arguments.get("title"),
        "target_value": arguments.get("target_value"),
        "unit": arguments.get("unit"),
        "focus_ref": arguments.get("focus_ref"),
        "status": arguments.get("status"),
    }
    provided_updates = {key: value for key, value in supported_fields.items() if value is not None}
    if not provided_updates:
        return "No KR content fields provided. You can update: title, target_value, unit, focus_ref, status."

    try:
        from app.models.okr import OKRObjective, OKRKeyResult
        from sqlalchemy import select as _select

        async with async_session() as db:
            ctx = await _load_okr_request_context(db, agent_id, user_id)
            if not ctx["agent"]:
                return "Agent not found."

            result = await db.execute(
                _select(OKRKeyResult, OKRObjective)
                .join(OKRObjective, OKRKeyResult.objective_id == OKRObjective.id)
                .where(
                    OKRKeyResult.id == kr_id,
                    OKRObjective.tenant_id == ctx["tenant_id"],
                )
            )
            row = result.first()
            if not row:
                return f"Key Result {kr_id_str} not found in your organization."

            kr, obj = row
            permission_error = _can_access_existing_okr_target(ctx, obj.owner_user_id, obj.owner_agent_id)
            if permission_error:
                return permission_error

            changed_fields: list[str] = []
            if "title" in provided_updates:
                kr.title = str(provided_updates["title"]).strip()
                changed_fields.append("title")
            if "target_value" in provided_updates:
                kr.target_value = float(provided_updates["target_value"])
                changed_fields.append("target_value")
            if "unit" in provided_updates:
                kr.unit = str(provided_updates["unit"]).strip() or None
                changed_fields.append("unit")
            if "focus_ref" in provided_updates:
                kr.focus_ref = str(provided_updates["focus_ref"]).strip() or None
                changed_fields.append("focus_ref")
            if "status" in provided_updates:
                kr.status = str(provided_updates["status"]).strip()
                changed_fields.append("status")

            await db.commit()

        return f"KR content updated: {kr.title}\nChanged fields: {', '.join(changed_fields)}"

    except Exception as e:
        logger.exception(f"[OKR] update_kr_content failed for agent {agent_id}")
        return f"Failed to update KR content: {str(e)[:200]}"


async def _collect_okr_progress(agent_id: uuid.UUID | None) -> str:
    """Batch-collect KR progress from legacy team member focus files.

    Delegates to okr_scheduler.collect_all_focus_updates(). The calling agent
    must be the OKR Agent — we look up its tenant from the DB.
    """
    if not agent_id:
        return "OKR tools require agent context."

    try:
        from app.models.agent import Agent as AgentModel
        from app.services.okr_scheduler import collect_all_focus_updates

        async with async_session() as db:
            agent_result = await db.execute(select(AgentModel).where(AgentModel.id == agent_id))
            agent = agent_result.scalar_one_or_none()
            if not agent:
                return "Agent not found."

        return await collect_all_focus_updates(
            tenant_id=agent.tenant_id,
            okr_agent_id=agent_id,
        )

    except Exception as e:
        logger.exception(f"[OKR] collect_okr_progress failed for agent {agent_id}")
        return f"Failed to collect OKR progress: {str(e)[:200]}"


async def _generate_okr_report(agent_id: uuid.UUID | None, arguments: dict) -> str:
    """Generate a daily or weekly OKR report.

    Writes to WorkReport table and returns the markdown content for posting.
    """
    if not agent_id:
        return "OKR tools require agent context."

    report_type = arguments.get("report_type", "daily").lower()
    if report_type not in ("daily", "weekly"):
        return "Invalid report_type. Must be 'daily' or 'weekly'."

    try:
        from app.models.agent import Agent as AgentModel
        from app.services.okr_scheduler import generate_daily_report, generate_weekly_report

        async with async_session() as db:
            agent_result = await db.execute(select(AgentModel).where(AgentModel.id == agent_id))
            agent = agent_result.scalar_one_or_none()
            if not agent:
                return "Agent not found."

        if report_type == "daily":
            return await generate_daily_report(
                tenant_id=agent.tenant_id,
                okr_agent_id=agent_id,
            )
        else:
            return await generate_weekly_report(
                tenant_id=agent.tenant_id,
                okr_agent_id=agent_id,
            )

    except Exception as e:
        logger.exception(f"[OKR] generate_okr_report failed for agent {agent_id}")
        return f"Failed to generate OKR report: {str(e)[:200]}"


async def _generate_monthly_okr_report(agent_id: uuid.UUID | None) -> str:
    """Generate the monthly OKR summary report for the agent's tenant.

    Writes a WorkReport (report_type='monthly') and returns the Markdown
    content. The OKR Agent should forward this to admins via send_platform_message.
    Also triggered automatically by the monthly_okr_report system cron trigger.
    """
    if not agent_id:
        return "OKR tools require agent context."

    try:
        from app.models.agent import Agent as AgentModel
        from app.services.okr_scheduler import generate_monthly_report

        async with async_session() as db:
            agent_result = await db.execute(select(AgentModel).where(AgentModel.id == agent_id))
            agent = agent_result.scalar_one_or_none()
            if not agent:
                return "Agent not found."

        return await generate_monthly_report(
            tenant_id=agent.tenant_id,
            okr_agent_id=agent_id,
        )

    except Exception as e:
        logger.exception(f"[OKR] generate_monthly_okr_report failed for agent {agent_id}")
        return f"Failed to generate monthly OKR report: {str(e)[:200]}"


async def _get_okr_settings_tool(agent_id: uuid.UUID | None) -> str:
    """Return OKR settings for the agent's tenant as a formatted string.

    The OKR Agent uses this to determine report schedule and period config
    without needing to make HTTP calls to its own API.
    """
    if not agent_id:
        return "OKR tools require agent context."

    try:
        from app.models.agent import Agent as AgentModel
        from app.services.okr_scheduler import get_okr_settings_for_agent
        import json as _json

        async with async_session() as db:
            agent_result = await db.execute(select(AgentModel).where(AgentModel.id == agent_id))
            agent = agent_result.scalar_one_or_none()
            if not agent:
                return "Agent not found."

        settings = await get_okr_settings_for_agent(agent.tenant_id)
        return _json.dumps(settings, indent=2, ensure_ascii=False)

    except Exception as e:
        logger.exception(f"[OKR] get_okr_settings failed for agent {agent_id}")
        return f"Failed to get OKR settings: {str(e)[:200]}"

__all__ = [name for name in globals() if not name.startswith("__")]
