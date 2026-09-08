"""Manage versioned, channel-neutral scenes."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.database import get_db
from app.models.user import User
from app.schemas.scene import ScenePublishRequest, SceneRollbackRequest, SceneSaveRequest, validate_scene_key
from app.services.scene_service import (
    delete_scene,
    get_scene,
    get_scene_revision_detail,
    list_revisions,
    list_scenes,
    publish_scene,
    rollback_scene,
    save_scene,
    scene_tool_enabled,
    serialize_scene,
    serialize_scene_manifest,
)
from app.services.scene_targets import conversation_options
from app.api.tools_agent import get_agent_tools_with_config
from app.services.scene_tool_settings import scene_mcp_override_options

router = APIRouter(prefix="/agents/{agent_id}/scenes", tags=["scenes"])


def _validated_scene_key(scene_key: str) -> str:
    try:
        return validate_scene_key(scene_key)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def _require_scene_access(
    db: AsyncSession,
    current_user: User,
    agent_id: uuid.UUID,
    *,
    manage: bool,
):
    agent, access_level = await check_agent_access(db, current_user, agent_id)
    if manage and access_level != "manage":
        raise HTTPException(status_code=403, detail="Administrator permission required")
    if not await scene_tool_enabled(db, agent_id):
        raise HTTPException(status_code=404, detail="Scene configuration is not enabled")
    return agent


@router.get("")
async def get_scenes(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_scene_access(db, current_user, agent_id, manage=True)
    return await list_scenes(db, agent_id)


@router.get("/conversation-options")
async def get_conversation_options(
    agent_id: uuid.UUID,
    q: str = Query(default="", max_length=200),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent = await _require_scene_access(db, current_user, agent_id, manage=True)
    if agent.tenant_id != current_user.tenant_id:
        raise HTTPException(404, detail="sceneAuto.targetUnavailable")
    return await conversation_options(db, agent, current_user, q=q, offset=offset)


@router.get("/tool-options")
async def get_scene_tool_options(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_scene_access(db, current_user, agent_id, manage=True)
    tools = await get_agent_tools_with_config(agent_id, current_user, db)
    return {"tools": tools, "mcp_server_overrides": await scene_mcp_override_options(db, agent_id, tools)}


@router.get("/{scene_key}")
async def get_scene_detail(
    agent_id: uuid.UUID,
    scene_key: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_scene_access(db, current_user, agent_id, manage=True)
    found = await get_scene(db, agent_id, _validated_scene_key(scene_key))
    if not found:
        raise HTTPException(status_code=404, detail="Scene not found")
    scene, revision = found
    result = serialize_scene(scene, revision)
    result["revisions"] = await list_revisions(db, agent_id, scene.scene_key)
    return result


@router.get("/{scene_key}/revisions/{revision}")
async def get_scene_revision(
    agent_id: uuid.UUID,
    scene_key: str,
    revision: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_scene_access(db, current_user, agent_id, manage=True)
    if revision < 1:
        raise HTTPException(status_code=422, detail="Revision must be greater than zero")
    return await get_scene_revision_detail(
        db,
        agent_id,
        _validated_scene_key(scene_key),
        revision,
    )


@router.put("/{scene_key}")
async def put_scene(
    agent_id: uuid.UUID,
    scene_key: str,
    data: SceneSaveRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent = await _require_scene_access(db, current_user, agent_id, manage=True)
    return await save_scene(
        db,
        agent_id=agent_id,
        tenant_id=agent.tenant_id,
        scene_key=_validated_scene_key(scene_key),
        data=data,
        created_by_user_id=current_user.id,
    )


@router.post("/{scene_key}/publish")
async def publish_scene_revision(
    agent_id: uuid.UUID,
    scene_key: str,
    data: ScenePublishRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_scene_access(db, current_user, agent_id, manage=True)
    return await publish_scene(
        db,
        agent_id=agent_id,
        scene_key=_validated_scene_key(scene_key),
        data=data,
        created_by_user_id=current_user.id,
    )


@router.delete("/{scene_key}")
async def remove_scene(
    agent_id: uuid.UUID,
    scene_key: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_scene_access(db, current_user, agent_id, manage=True)
    await delete_scene(db, agent_id, _validated_scene_key(scene_key))
    return {"ok": True}


@router.post("/{scene_key}/rollback")
async def rollback_scene_revision(
    agent_id: uuid.UUID,
    scene_key: str,
    data: SceneRollbackRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_scene_access(db, current_user, agent_id, manage=True)
    return await rollback_scene(
        db,
        agent_id=agent_id,
        scene_key=_validated_scene_key(scene_key),
        target_revision=data.target_revision,
        expected_revision=data.expected_revision,
        created_by_user_id=current_user.id,
    )


@router.get("/{scene_key}/manifest")
async def get_scene_manifest(
    agent_id: uuid.UUID,
    scene_key: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_scene_access(db, current_user, agent_id, manage=False)
    key = _validated_scene_key(scene_key)
    found = await get_scene(db, agent_id, key, enabled_only=True)
    if not found:
        return {
            "scene_key": key,
            "name": "",
            "enabled": False,
            "revision": 0,
            "has_unpublished_changes": False,
            "welcome_message": "",
            "system_prompts": [],
            "quick_actions": [],
        }
    scene, revision = found
    if revision is None:
        raise HTTPException(status_code=404, detail="Published scene not found")
    return serialize_scene_manifest(scene, revision)
