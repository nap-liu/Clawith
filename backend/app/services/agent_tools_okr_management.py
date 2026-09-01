"""OKR mutation tools extracted from agent_tools."""

import uuid

from loguru import logger
from sqlalchemy import select

from app.database import async_session
from app.services.agent_tools_okr_queries import (
    _can_access_existing_okr_target,
    _can_create_okr_target,
    _load_okr_request_context,
)


async def _create_objective(agent_id: uuid.UUID | None, user_id: uuid.UUID | None, arguments: dict) -> str:
    if not agent_id:
        return "OKR tools require agent context."
    try:
        from app.models.agent import Agent as AgentModel
        from app.models.okr import OKRObjective
        from app.models.user import User as UserModel

        async with async_session() as db:
            ctx = await _load_okr_request_context(db, agent_id, user_id)
            ag = ctx["agent"]
            if not ag:
                return "Agent not found."

            title = arguments.get("title")
            period_start = arguments.get("period_start")
            period_end = arguments.get("period_end")
            if not all([title, period_start, period_end]):
                return "Missing required fields: title, period_start, period_end"

            from datetime import date

            p_start = date.fromisoformat(period_start)
            p_end = date.fromisoformat(period_end)

            owner_user_raw = arguments.get("user_id")
            owner_agent_raw = arguments.get("agent_id")
            if bool(owner_user_raw) and bool(owner_agent_raw):
                return "Exactly one of user_id or agent_id may be provided. Omit both for a company Objective."

            owner_user_id: uuid.UUID | None = None
            owner_agent_id: uuid.UUID | None = None
            if owner_user_raw:
                try:
                    owner_user_id = uuid.UUID(str(owner_user_raw))
                except (TypeError, ValueError):
                    return "Invalid user_id format. Use a canonical platform User UUID."
                result = await db.execute(
                    select(UserModel.id).where(
                        UserModel.id == owner_user_id,
                        UserModel.tenant_id == ag.tenant_id,
                        UserModel.is_active.is_(True),
                    )
                )
                if result.scalar_one_or_none() is None:
                    return "user_id was not found as an active User in this tenant."
            elif owner_agent_raw:
                try:
                    owner_agent_id = uuid.UUID(str(owner_agent_raw))
                except (TypeError, ValueError):
                    return "Invalid agent_id format. Use a canonical platform Agent UUID."
                result = await db.execute(
                    select(AgentModel.id).where(
                        AgentModel.id == owner_agent_id,
                        AgentModel.tenant_id == ag.tenant_id,
                        AgentModel.is_deleted.is_(False),
                        AgentModel.is_expired.is_(False),
                    )
                )
                if result.scalar_one_or_none() is None:
                    return "agent_id was not found as an active Agent in this tenant."

            owner_id = owner_user_id or owner_agent_id

            permission_error = _can_create_okr_target(ctx, owner_user_id, owner_agent_id)
            if permission_error:
                return permission_error

            obj = OKRObjective(
                tenant_id=ag.tenant_id,
                title=title,
                description=arguments.get("description"),
                owner_user_id=owner_user_id,
                owner_agent_id=owner_agent_id,
                period_start=p_start,
                period_end=p_end,
                status="active",
            )
            db.add(obj)
            await db.commit()
            owner_info = f"owner={owner_id or 'company'}"
            return f"Successfully created Objective '{obj.title}' (ID: {obj.id}, {owner_info})"
    except Exception as e:
        logger.exception(f"[OKR] create_objective failed")
        return f"Failed to create objective: {str(e)[:200]}"


async def _create_key_result(agent_id: uuid.UUID | None, user_id: uuid.UUID | None, arguments: dict) -> str:
    if not agent_id:
        return "OKR tools require agent context."
    try:
        from app.models.okr import OKRObjective, OKRKeyResult

        async with async_session() as db:
            ctx = await _load_okr_request_context(db, agent_id, user_id)
            if not ctx["agent"]:
                return "Agent not found."

            obj_id_str = arguments.get("objective_id")
            if not obj_id_str:
                return "Missing objective_id"
            try:
                obj_id = uuid.UUID(obj_id_str)
            except ValueError:
                return "Invalid formatted objective_id (must be UUID)"

            # Verify objective exists
            obj_res = await db.execute(
                select(OKRObjective).where(
                    OKRObjective.id == obj_id,
                    OKRObjective.tenant_id == ctx["tenant_id"],
                )
            )
            obj = obj_res.scalar_one_or_none()
            if not obj:
                return f"Objective {obj_id} not found."

            permission_error = _can_access_existing_okr_target(ctx, obj.owner_user_id, obj.owner_agent_id)
            if permission_error:
                return permission_error

            kr = OKRKeyResult(
                objective_id=obj_id,
                title=arguments.get("title"),
                target_value=float(arguments.get("target_value", 100)),
                current_value=0.0,
                unit=arguments.get("unit"),
                focus_ref=arguments.get("focus_ref"),
            )
            db.add(kr)
            await db.commit()
            return f"Successfully created Key Result '{kr.title}' (ID: {kr.id})"
    except Exception as e:
        logger.exception(f"[OKR] create_key_result failed")
        return f"Failed to create key result: {str(e)[:200]}"


async def _update_objective(agent_id: uuid.UUID | None, user_id: uuid.UUID | None, arguments: dict) -> str:
    """Update Objective metadata.

    Permission rules:
    - Regular agents: can only modify Objectives whose canonical agent_id is their own.
    - System agents are constrained by the requesting user's role: admins can modify any OKR,
      non-admins may only modify their own personal OKRs.
    """
    if not agent_id:
        return "OKR tools require agent context."
    try:
        from app.models.okr import OKRObjective

        async with async_session() as db:
            ctx = await _load_okr_request_context(db, agent_id, user_id)
            if not ctx["agent"]:
                return "Agent not found."

            obj_id_str = arguments.get("objective_id")
            if not obj_id_str:
                return "Missing objective_id"
            try:
                obj_id = uuid.UUID(obj_id_str)
            except ValueError:
                return "Invalid formatted objective_id (must be UUID)"

            obj_res = await db.execute(
                select(OKRObjective).where(
                    OKRObjective.id == obj_id,
                    OKRObjective.tenant_id == ctx["tenant_id"],
                )
            )
            obj = obj_res.scalar_one_or_none()
            if not obj:
                return f"Objective {obj_id} not found."

            permission_error = _can_access_existing_okr_target(ctx, obj.owner_user_id, obj.owner_agent_id)
            if permission_error:
                return permission_error

            updates = []
            if "title" in arguments:
                obj.title = arguments["title"]
                updates.append("title")
            if "description" in arguments:
                obj.description = arguments["description"]
                updates.append("description")
            if "status" in arguments:
                obj.status = arguments["status"]
                updates.append("status")
            if "period_start" in arguments:
                from datetime import date

                obj.period_start = date.fromisoformat(arguments["period_start"])
                updates.append("period_start")
            if "period_end" in arguments:
                from datetime import date

                obj.period_end = date.fromisoformat(arguments["period_end"])
                updates.append("period_end")

            if not updates:
                return "No supported fields provided to update."

            await db.commit()
            return f"Successfully updated Objective {obj.id}. Changed fields: {', '.join(updates)}"
    except Exception as e:
        logger.exception(f"[OKR] update_objective failed")
        return f"Failed to update objective: {str(e)[:200]}"


async def _update_any_kr_progress(agent_id: uuid.UUID | None, user_id: uuid.UUID | None, arguments: dict) -> str:
    """OKR Agent exclusive version of update_kr_progress."""
    if not agent_id:
        return "OKR tools require agent context."
    try:
        from app.models.okr import OKRKeyResult, OKRObjective, OKRProgressLog

        async with async_session() as db:
            ctx = await _load_okr_request_context(db, agent_id, user_id)
            if not ctx["agent"]:
                return "Agent not found."

            kr_id_str = arguments.get("kr_id")
            val = arguments.get("value")
            if not kr_id_str or val is None:
                return "Missing kr_id or value"
            try:
                kr_id = uuid.UUID(kr_id_str)
            except ValueError:
                return "Invalid formatted kr_id (must be UUID)"

            kr_res = await db.execute(
                select(OKRKeyResult, OKRObjective)
                .join(OKRObjective, OKRKeyResult.objective_id == OKRObjective.id)
                .where(
                    OKRKeyResult.id == kr_id,
                    OKRObjective.tenant_id == ctx["tenant_id"],
                )
            )
            row = kr_res.first()
            if not row:
                return f"Key Result {kr_id} not found in your organization."

            kr, obj = row
            permission_error = _can_access_existing_okr_target(ctx, obj.owner_user_id, obj.owner_agent_id)
            if permission_error:
                return permission_error

            old_val = kr.current_value
            kr.current_value = float(val)

            # Auto-compute status if not explicitly given
            explicit_status = arguments.get("status")
            if explicit_status:
                kr.status = explicit_status
            else:
                progress = kr.current_value / kr.target_value if kr.target_value != 0 else 0
                if progress >= 1.0:
                    kr.status = "completed"
                elif progress >= 0.7:
                    kr.status = "on_track"
                elif progress >= 0.4:
                    kr.status = "at_risk"
                else:
                    kr.status = "behind"

            from datetime import datetime

            kr.last_updated_at = datetime.utcnow()

            note = arguments.get("note", "Updated by OKR Agent after check-in")
            log_entry = OKRProgressLog(
                kr_id=kr.id,
                previous_value=old_val,
                new_value=kr.current_value,
                source="okr_agent" if ctx["agent_is_system"] else "agent",
                note=note,
            )
            db.add(log_entry)
            await db.commit()

            return f"Successfully updated KR '{kr.title}'. Progress: {old_val} -> {kr.current_value} {kr.unit or ''}. Status: {kr.status}"
    except Exception as e:
        logger.exception(f"[OKR] update_any_kr_progress failed")
        return f"Failed to update kr progress: {str(e)[:200]}"


async def _upsert_member_daily_report(agent_id: uuid.UUID | None, arguments: dict) -> str:
    """OKR Agent exclusive tool for creating or revising a member daily report."""
    if not agent_id:
        return "OKR tools require agent context."

    try:
        from datetime import date as date_cls
        from app.models.agent import Agent as AgentModel
        from app.models.okr import MemberDailyReport
        from app.models.user import User as UserModel
        from app.services.okr_reporting import upsert_member_daily_report as _upsert

        report_date_raw = arguments.get("report_date")
        content = (arguments.get("content") or "").strip()
        target_user_raw = arguments.get("user_id")
        target_agent_raw = arguments.get("agent_id")
        source = (arguments.get("source") or "okr_agent_assisted").strip() or "okr_agent_assisted"

        if not report_date_raw or not content:
            return "Missing report_date or content"
        if bool(target_user_raw) == bool(target_agent_raw):
            return "Exactly one of user_id or agent_id is required."

        try:
            report_date = date_cls.fromisoformat(report_date_raw)
        except ValueError:
            return "Invalid report_date format. Use YYYY-MM-DD."

        async with async_session() as db:
            ag_res = await db.execute(select(AgentModel).where(AgentModel.id == agent_id))
            ag = ag_res.scalar_one_or_none()
            if not ag:
                return "Agent not found."
            if not ag.is_system:
                return "Permission denied: only the OKR Agent can upsert member daily reports."

            target_user_id: uuid.UUID | None = None
            target_agent_id: uuid.UUID | None = None
            if target_user_raw:
                try:
                    target_user_id = uuid.UUID(str(target_user_raw))
                except (TypeError, ValueError):
                    return "Invalid user_id format. Use a canonical platform User UUID."
                target_result = await db.execute(
                    select(UserModel.id).where(
                        UserModel.id == target_user_id,
                        UserModel.tenant_id == ag.tenant_id,
                        UserModel.is_active.is_(True),
                    )
                )
                if target_result.scalar_one_or_none() is None:
                    return "user_id was not found as an active User in this tenant."
            else:
                try:
                    target_agent_id = uuid.UUID(str(target_agent_raw))
                except (TypeError, ValueError):
                    return "Invalid agent_id format. Use a canonical platform Agent UUID."
                target_result = await db.execute(
                    select(AgentModel.id).where(
                        AgentModel.id == target_agent_id,
                        AgentModel.tenant_id == ag.tenant_id,
                        AgentModel.is_deleted.is_(False),
                        AgentModel.is_expired.is_(False),
                    )
                )
                if target_result.scalar_one_or_none() is None:
                    return "agent_id was not found as an active Agent in this tenant."

            existing_res = await db.execute(
                select(MemberDailyReport).where(
                    MemberDailyReport.tenant_id == ag.tenant_id,
                    MemberDailyReport.user_id == target_user_id
                    if target_user_id
                    else MemberDailyReport.agent_id == target_agent_id,
                    MemberDailyReport.report_date == report_date,
                )
            )
            existing = existing_res.scalar_one_or_none()
            previous_content = existing.content if existing else ""

        report = await _upsert(
            tenant_id=ag.tenant_id,
            user_id=target_user_id,
            agent_id=target_agent_id,
            report_date=report_date,
            content=content,
            source=source,
        )

        resolved_id = target_user_id or target_agent_id
        action = "Updated" if previous_content else "Created"
        details = [
            f"{action} daily report for {resolved_id} on {report.report_date.isoformat()}.",
            f"Stored length: {len(report.content)} characters.",
            f"Status: {report.status}.",
        ]
        if previous_content:
            details.append(f"Previous content: {previous_content}")
        details.append(f"Current content: {report.content}")
        return " ".join(details)
    except Exception as e:
        logger.exception("[OKR] upsert_member_daily_report failed")
        return f"Failed to upsert member daily report: {str(e)[:200]}"

__all__ = [name for name in globals() if not name.startswith("__")]
