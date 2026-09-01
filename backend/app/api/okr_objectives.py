"""OKR objective and key-result routes."""

import uuid
from datetime import date, datetime

from fastapi import Depends, HTTPException
from sqlalchemy import delete, select

from app.api.auth import get_current_user
from app.api.okr_models import (
    KeyResultCreate,
    KeyResultOut,
    KeyResultUpdate,
    ObjectiveCreate,
    ObjectiveOut,
    ObjectiveUpdate,
    ProgressUpdate,
    _kr_to_out,
    _obj_to_out,
)
from app.api.okr_shared import (
    _compute_current_period,
    _dashboard_write_forbidden,
    _get_or_create_settings,
    _is_okr_admin,
    async_session,
    router,
)
from app.models.okr import OKRKeyResult, OKRObjective, OKRProgressLog


@router.get("/objectives", response_model=list[ObjectiveOut])
async def list_objectives(
    period_start: str | None = None,
    period_end: str | None = None,
    user=Depends(get_current_user),
):
    """List all Objectives for the current tenant within a period.

    If period_start / period_end are not supplied, defaults to the current
    OKR period computed from the tenant's OKR settings.
    Includes owner_name resolved from User.display_name or Agent.name.
    """
    from app.models.agent import Agent
    from app.models.user import User

    async with async_session() as db:
        if not period_start or not period_end:
            settings = await _get_or_create_settings(db, user.tenant_id)
            ps, pe = _compute_current_period(
                settings.period_frequency, settings.period_length_days
            )
            await db.commit()
        else:
            ps = date.fromisoformat(period_start)
            pe = date.fromisoformat(period_end)

        result = await db.execute(
            select(OKRObjective)
            .where(
                OKRObjective.tenant_id == user.tenant_id,
                OKRObjective.period_start >= ps,
                OKRObjective.period_end <= pe,
                OKRObjective.status != "archived",
            )
            .order_by(OKRObjective.created_at)
        )
        objectives = result.scalars().all()

        # Fetch all KRs for these objectives in one query
        obj_ids = [o.id for o in objectives]
        krs_result = await db.execute(
            select(OKRKeyResult)
            .where(OKRKeyResult.objective_id.in_(obj_ids))
            .order_by(OKRKeyResult.created_at)
        )
        all_krs = krs_result.scalars().all()

        # Group KRs by objective
        krs_by_obj: dict[uuid.UUID, list[OKRKeyResult]] = {}
        for kr in all_krs:
            krs_by_obj.setdefault(kr.objective_id, []).append(kr)

        # Batch-resolve owner names: collect distinct user/agent IDs
        user_owner_ids = [
            o.owner_user_id for o in objectives if o.owner_user_id
        ]
        agent_owner_ids = [
            o.owner_agent_id for o in objectives if o.owner_agent_id
        ]

        user_names: dict[uuid.UUID, str] = {}
        if user_owner_ids:
            u_result = await db.execute(
                select(User.id, User.display_name).where(User.id.in_(user_owner_ids))
            )
            user_names = {row.id: (row.display_name or "") for row in u_result.fetchall()}

        agent_names: dict[uuid.UUID, str] = {}
        if agent_owner_ids:
            a_result = await db.execute(
                select(Agent.id, Agent.name).where(Agent.id.in_(agent_owner_ids))
            )
            agent_names = {row.id: (row.name or "") for row in a_result.fetchall()}

        def _resolve_name(obj: OKRObjective) -> str | None:
            if obj.owner_user_id:
                return user_names.get(obj.owner_user_id)
            if obj.owner_agent_id:
                return agent_names.get(obj.owner_agent_id)
            return None

        return [
            _obj_to_out(o, krs_by_obj.get(o.id, []), owner_name=_resolve_name(o))
            for o in objectives
        ]


@router.post("/objectives", response_model=ObjectiveOut)
async def create_objective(body: ObjectiveCreate, user=Depends(get_current_user)):
    """Create a new Objective."""
    from app.models.agent import Agent
    from app.models.user import User

    if not _is_okr_admin(user):
        raise _dashboard_write_forbidden()

    async with async_session() as db:
        owner_user_id = uuid.UUID(body.user_id) if body.user_id else None
        owner_agent_id = uuid.UUID(body.agent_id) if body.agent_id else None
        if owner_user_id:
            found = await db.scalar(
                select(User.id).where(
                    User.id == owner_user_id,
                    User.tenant_id == user.tenant_id,
                    User.is_active.is_(True),
                )
            )
            if not found:
                raise HTTPException(422, "user_id is not an active tenant user")
        if owner_agent_id:
            found = await db.scalar(
                select(Agent.id).where(
                    Agent.id == owner_agent_id,
                    Agent.tenant_id == user.tenant_id,
                    Agent.is_deleted.is_(False),
                    Agent.is_expired.is_(False),
                    Agent.status.notin_(["stopped", "error"]),
                )
            )
            if not found:
                raise HTTPException(422, "agent_id is not an active tenant agent")

        obj = OKRObjective(
            tenant_id=user.tenant_id,
            title=body.title,
            description=body.description,
            owner_user_id=owner_user_id,
            owner_agent_id=owner_agent_id,
            period_start=date.fromisoformat(body.period_start),
            period_end=date.fromisoformat(body.period_end),
        )
        db.add(obj)
        await db.commit()
        await db.refresh(obj)
        return _obj_to_out(obj)


@router.patch("/objectives/{objective_id}", response_model=ObjectiveOut)
async def update_objective(
    objective_id: uuid.UUID,
    body: ObjectiveUpdate,
    user=Depends(get_current_user),
):
    """Update an Objective's title, description or status."""
    if not _is_okr_admin(user):
        raise _dashboard_write_forbidden()

    async with async_session() as db:
        result = await db.execute(
            select(OKRObjective).where(
                OKRObjective.id == objective_id,
                OKRObjective.tenant_id == user.tenant_id,
            )
        )
        obj = result.scalar_one_or_none()
        if not obj:
            raise HTTPException(404, "Objective not found")

        if body.title is not None:
            obj.title = body.title
        if body.description is not None:
            obj.description = body.description
        if body.status is not None:
            obj.status = body.status

        await db.commit()
        await db.refresh(obj)
        return _obj_to_out(obj)


@router.delete("/objectives/{objective_id}")
async def delete_objective(
    objective_id: uuid.UUID,
    user=Depends(get_current_user),
):
    """Soft delete an Objective (set status to archived)."""
    if not _is_okr_admin(user):
        raise _dashboard_write_forbidden()

    async with async_session() as db:
        result = await db.execute(
            select(OKRObjective).where(
                OKRObjective.id == objective_id,
                OKRObjective.tenant_id == user.tenant_id,
            )
        )
        obj = result.scalar_one_or_none()
        if not obj:
            raise HTTPException(404, "Objective not found")

        # Soft delete
        obj.status = "archived"
        await db.commit()

        return {"status": "success"}


@router.get(
    "/objectives/{objective_id}/key-results", response_model=list[KeyResultOut]
)
async def list_key_results(
    objective_id: uuid.UUID, user=Depends(get_current_user)
):
    """List all KRs for the given Objective."""
    async with async_session() as db:
        # Verify objective belongs to this tenant
        obj_result = await db.execute(
            select(OKRObjective).where(
                OKRObjective.id == objective_id,
                OKRObjective.tenant_id == user.tenant_id,
            )
        )
        if not obj_result.scalar_one_or_none():
            raise HTTPException(404, "Objective not found")

        result = await db.execute(
            select(OKRKeyResult)
            .where(OKRKeyResult.objective_id == objective_id)
            .order_by(OKRKeyResult.created_at)
        )
        return [_kr_to_out(kr) for kr in result.scalars().all()]


@router.post(
    "/objectives/{objective_id}/key-results", response_model=KeyResultOut
)
async def create_key_result(
    objective_id: uuid.UUID,
    body: KeyResultCreate,
    user=Depends(get_current_user),
):
    """Create a new Key Result under the specified Objective."""
    if not _is_okr_admin(user):
        raise _dashboard_write_forbidden()

    async with async_session() as db:
        # Verify objective belongs to this tenant
        obj_result = await db.execute(
            select(OKRObjective).where(
                OKRObjective.id == objective_id,
                OKRObjective.tenant_id == user.tenant_id,
            )
        )
        if not obj_result.scalar_one_or_none():
            raise HTTPException(404, "Objective not found")

        kr = OKRKeyResult(
            objective_id=objective_id,
            title=body.title,
            target_value=body.target_value,
            unit=body.unit,
            focus_ref=body.focus_ref,
        )
        db.add(kr)
        await db.commit()
        await db.refresh(kr)
        return _kr_to_out(kr)


@router.patch("/key-results/{kr_id}", response_model=KeyResultOut)
async def update_key_result(
    kr_id: uuid.UUID,
    body: KeyResultUpdate,
    user=Depends(get_current_user),
):
    """Update a Key Result's fields or current progress value.

    When current_value changes, an OKRProgressLog entry is created
    automatically to maintain the complete progress history.
    """
    if not _is_okr_admin(user):
        raise _dashboard_write_forbidden()

    async with async_session() as db:
        result = await db.execute(
            select(OKRKeyResult, OKRObjective)
            .join(OKRObjective, OKRKeyResult.objective_id == OKRObjective.id)
            .where(
                OKRKeyResult.id == kr_id,
                OKRObjective.tenant_id == user.tenant_id,
            )
        )
        row = result.first()
        if not row:
            raise HTTPException(404, "Key Result not found")
        kr, _ = row

        prev_value = kr.current_value

        if body.title is not None:
            kr.title = body.title
        if body.target_value is not None:
            kr.target_value = body.target_value
        if body.current_value is not None:
            kr.current_value = body.current_value
        if body.unit is not None:
            kr.unit = body.unit
        if body.focus_ref is not None:
            kr.focus_ref = body.focus_ref
        if body.status is not None:
            kr.status = body.status

        # Log progress change when current_value was updated
        if body.current_value is not None and body.current_value != prev_value:
            log = OKRProgressLog(
                kr_id=kr_id,
                previous_value=prev_value,
                new_value=body.current_value,
                source="manual",
            )
            db.add(log)

        await db.commit()
        await db.refresh(kr)
        return _kr_to_out(kr)


@router.post("/key-results/{kr_id}/progress", response_model=KeyResultOut)
async def update_kr_progress_endpoint(
    kr_id: uuid.UUID,
    body: ProgressUpdate,
    user=Depends(get_current_user),
):
    """Convenience endpoint for updating only the current progress value.

    Used by the update_kr_progress agent tool and the OKR Agent.
    Records an OKRProgressLog entry with the provided note.
    """
    if not _is_okr_admin(user):
        raise _dashboard_write_forbidden()

    async with async_session() as db:
        result = await db.execute(
            select(OKRKeyResult, OKRObjective)
            .join(OKRObjective, OKRKeyResult.objective_id == OKRObjective.id)
            .where(
                OKRKeyResult.id == kr_id,
                OKRObjective.tenant_id == user.tenant_id,
            )
        )
        row = result.first()
        if not row:
            raise HTTPException(404, "Key Result not found")
        kr, _ = row

        prev_value = kr.current_value
        kr.current_value = body.value
        kr.last_updated_at = datetime.utcnow()

        # Update status: use explicit override or auto-compute from progress ratio
        if body.status and body.status in ("on_track", "at_risk", "behind", "completed"):
            kr.status = body.status
        elif kr.target_value:
            ratio = body.value / kr.target_value
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
            new_value=body.value,
            source="manual",
            note=body.note,
        )
        db.add(log)
        await db.commit()
        await db.refresh(kr)
        return _kr_to_out(kr)


@router.delete("/key-results/{kr_id}")
async def delete_key_result(
    kr_id: uuid.UUID,
    user=Depends(get_current_user),
):
    """Hard delete a key result."""
    from app.models.okr import OKRProgressLog

    if not _is_okr_admin(user):
        raise _dashboard_write_forbidden()

    async with async_session() as db:
        result = await db.execute(
            select(OKRKeyResult, OKRObjective)
            .join(OKRObjective, OKRKeyResult.objective_id == OKRObjective.id)
            .where(
                OKRKeyResult.id == kr_id,
                OKRObjective.tenant_id == user.tenant_id,
            )
        )
        row = result.first()
        if not row:
            raise HTTPException(404, "Key Result not found")
        kr, _ = row

        # Manual cascade delete logs
        await db.execute(delete(OKRProgressLog).where(OKRProgressLog.kr_id == kr_id))
        await db.execute(delete(OKRKeyResult).where(OKRKeyResult.id == kr_id))

        await db.commit()
        return {"status": "success"}
