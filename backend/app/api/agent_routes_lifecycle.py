"""Mechanically separated Agent API route group."""

from app.api.agent_api_shared import *  # noqa: F401,F403
from app.api.agent_routes_directory import _agent_to_out
from app.core.permissions import is_platform_admin_user


@router.patch("/{agent_id}", response_model=AgentOut)
async def update_agent(
    agent_id: uuid.UUID,
    data: AgentUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update agent settings (creator or admin)."""
    agent, agent_access = await check_agent_access(db, current_user, agent_id)

    is_admin = is_platform_admin_user(current_user) or current_user.role == "org_admin"
    is_agent_admin = current_user.role == "agent_admin" and agent_access == "manage"

    if not is_agent_creator(current_user, agent) and not is_admin and not is_agent_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only creator or admin can update agent settings")

    update_data = data.model_dump(exclude_unset=True)

    # expires_at: admin only
    if "expires_at" in update_data:
        if not is_admin:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only admin can modify agent expiry time")
        from datetime import datetime
        from datetime import timezone as tz
        new_expires = update_data["expires_at"]
        # Allow any value: extend, shorten, or null (permanent).
        # Re-activate the agent if new expiry is in the future or cleared.
        if new_expires is None or new_expires > datetime.now(tz.utc):
            if agent.is_expired:
                agent.is_expired = False
                agent.status = "idle"

    # Native standard Digital Employees share the same validated ordinary
    # settings path as MCP update_agent and update_self_settings.
    clamped_fields = []
    special_fields = {"autonomy_policy", "expires_at"}
    regular_update = {
        field: value for field, value in update_data.items()
        if field not in special_fields
    }
    if agent.scope == "standard" and agent.agent_type == "native":
        from app.services.agent_settings_update import apply_agent_settings_patch

        if "primary_model_id" in regular_update:
            value = regular_update.pop("primary_model_id")
            regular_update["primary_model"] = str(value) if value else None
        if "fallback_model_id" in regular_update:
            value = regular_update.pop("fallback_model_id")
            regular_update["fallback_model"] = str(value) if value else None
        if "temperature" in regular_update:
            regular_update["imagination"] = regular_update.pop("temperature")
        try:
            outcome = await apply_agent_settings_patch(db, agent, regular_update)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
        clamped_fields = outcome.clamps
        for field in special_fields & update_data.keys():
            setattr(agent, field, update_data[field])
        await db.flush()
    else:
        # Remote OpenClaw records retain their existing update semantics; their
        # runtime settings are managed through the OpenClaw-specific endpoint.
        for field, value in update_data.items():
            setattr(agent, field, value)
        await db.flush()

        if "name" in update_data or "avatar_url" in update_data:
            from app.models.participant import Participant
            participant = (
                await db.execute(
                    select(Participant).where(
                        Participant.type == "agent",
                        Participant.ref_id == agent_id,
                    )
                )
            ).scalar_one_or_none()
            if participant:
                if "name" in update_data:
                    participant.display_name = agent.name
                if "avatar_url" in update_data:
                    participant.avatar_url = agent.avatar_url
                await db.flush()

    out_model = await _agent_to_out(db, agent, current_user.id)
    out = out_model.model_dump()
    if clamped_fields:
        out["_clamped_fields"] = clamped_fields
    return out


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a digital employee (creator only)."""
    agent, _access = await check_agent_access(db, current_user, agent_id)
    if not is_agent_creator(current_user, agent) and current_user.role not in ("super_admin", "org_admin", "platform_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only creator or admin can delete agent")

    # System agents (OKR Agent, etc.) cannot be deleted — they are seeded by the
    # platform and required for core features. Disable them via settings instead.
    if agent.is_system:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="System agents cannot be deleted. Disable the related feature (e.g. OKR) in Company Settings instead.",
        )

    parent_session = aliased(ChatSession)
    child_session = aliased(ChatSession)
    subagent_audit = (
        await db.execute(
            select(SubagentRun.id)
            .join(parent_session, parent_session.id == SubagentRun.parent_session_id)
            .join(child_session, child_session.id == SubagentRun.id)
            .where(
                or_(
                    parent_session.agent_id == agent_id,
                    parent_session.peer_agent_id == agent_id,
                    child_session.agent_id == agent_id,
                    child_session.peer_agent_id == agent_id,
                )
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if subagent_audit is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="被 Subagent 审计记录引用的数字员工不能删除。",
        )

    # Stop container and archive files (best effort)
    from app.services.agent_manager import agent_manager
    archive_dir: Path | None = None
    try:
        await agent_manager.remove_container(agent)
    except Exception:
        pass
    try:
        archive_dir = await agent_manager.archive_agent_files(agent.id)
    except Exception:
        pass
    if archive_dir is not None:
        try:
            await _archive_agent_task_history(db, agent.id, archive_dir)
        except Exception:
            pass

    # Delete related records that reference this agent
    # Use savepoints so a failure in one table doesn't poison the whole transaction
    from sqlalchemy import text

    cleanup_tables = [
        "agent_activity_logs",
        "audit_logs",
        "approval_requests",
        "chat_messages",
        "chat_sessions",
        "agent_schedules",
        "agent_triggers",
        "dingtalk_channel_provisioning_sessions",
        "channel_configs",
        "agent_permissions",
        "agent_tools",
        "agent_relationships",
        "gateway_messages",
        "published_pages",
        "notifications",
        "daily_token_usage",
    ]

    for table in cleanup_tables:
        try:
            async with db.begin_nested():
                await db.execute(text(f"DELETE FROM {table} WHERE agent_id = :aid"), {"aid": agent_id})
        except Exception:
            pass

    # Clean up secondary FK columns that also reference agents table
    secondary_fk_cleanups = [
        "DELETE FROM task_logs WHERE task_id IN (SELECT id FROM tasks WHERE agent_id = :aid)",
        "DELETE FROM tasks WHERE agent_id = :aid",
        "DELETE FROM chat_sessions WHERE peer_agent_id = :aid",
        "DELETE FROM gateway_messages WHERE sender_agent_id = :aid",
        "UPDATE chat_messages SET sender_agent_id = NULL WHERE sender_agent_id = :aid",
    ]
    for sql in secondary_fk_cleanups:
        try:
            async with db.begin_nested():
                await db.execute(text(sql), {"aid": agent_id})
        except Exception:
            pass

    # Also clean agent_agent_relationships (has both agent_id and target_agent_id)
    try:
        async with db.begin_nested():
            await db.execute(
                text("DELETE FROM agent_agent_relationships WHERE agent_id = :aid OR target_agent_id = :aid"),
                {"aid": agent_id},
            )
    except Exception:
        pass

    # Also clear plaza posts by this agent
    try:
        async with db.begin_nested():
            await db.execute(text("DELETE FROM plaza_posts WHERE author_id = :aid"), {"aid": str(agent_id)})
    except Exception:
        pass

    # Clean up Participant identity
    try:
        async with db.begin_nested():
            await db.execute(
                text("DELETE FROM participants WHERE type = 'agent' AND ref_id = :aid"),
                {"aid": agent_id},
            )
    except Exception:
        pass

    await db.delete(agent)
    await db.commit()


@router.post("/{agent_id}/start", response_model=AgentOut)
async def start_agent(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Start an agent's container."""
    agent, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only manager can start agent")

    from app.services.agent_manager import agent_manager
    await agent_manager.start_container(db, agent)
    await db.flush()
    return await _agent_to_out(db, agent, current_user.id)


@router.post("/{agent_id}/stop", response_model=AgentOut)
async def stop_agent(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Stop an agent's container."""
    agent, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only manager can stop agent")

    from app.services.agent_manager import agent_manager
    await agent_manager.stop_container(agent)
    await db.flush()
    return await _agent_to_out(db, agent, current_user.id)


__all__ = [name for name in globals() if not name.startswith("__")]
