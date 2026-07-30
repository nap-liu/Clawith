"""Persistence, versioning, and guarded management for scenes."""

import json
import uuid

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import user_can_manage_agent_id
from app.database import async_session
from app.models.agent import Agent
from app.models.audit import AuditLog
from app.models.chat_session import ChatSession
from app.models.scene import AgentScene, AgentSceneRevision
from app.models.tool import AgentTool, Tool
from app.models.user import User
from app.schemas.scene import (
    SceneConfig,
    ScenePublishRequest,
    SceneRollbackRequest,
    SceneSaveRequest,
    SceneToolSaveRequest,
    validate_scene_key,
)

SCENE_TOOL_NAME = "manage_scene"


async def scene_tool_enabled(db: AsyncSession, agent_id: uuid.UUID) -> bool:
    result = await db.execute(
        select(AgentTool.id)
        .join(Tool, Tool.id == AgentTool.tool_id)
        .where(
            AgentTool.agent_id == agent_id,
            AgentTool.enabled.is_(True),
            Tool.name == SCENE_TOOL_NAME,
            Tool.enabled.is_(True),
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


def _config_dict(config: SceneConfig) -> dict:
    return SceneConfig.model_validate(config.model_dump()).model_dump(mode="json")


def _draft_dict(data: SceneSaveRequest) -> dict:
    return {
        "name": data.name,
        "enabled": data.enabled,
        **_config_dict(data),
    }


def _merge_tool_save_request(
    current: dict | None,
    patch: SceneToolSaveRequest,
) -> SceneSaveRequest:
    """Build a full draft while preserving omitted tool fields by default."""
    current = current or {}
    provided = patch.model_fields_set
    force_overwrite = patch.force_overwrite

    name = patch.name if "name" in provided else current.get("name")
    if not name:
        raise ValueError("name is required when creating a scene")

    if force_overwrite:
        values = {
            "name": name,
            "enabled": patch.enabled if "enabled" in provided else True,
            "expected_revision": patch.expected_revision,
            "welcome_message": patch.welcome_message if "welcome_message" in provided else "",
            "system_prompts": patch.system_prompts if "system_prompts" in provided else [],
            "quick_actions": patch.quick_actions if "quick_actions" in provided else [],
        }
    else:
        values = {
            "name": name,
            "enabled": patch.enabled if "enabled" in provided else current.get("enabled", True),
            "expected_revision": patch.expected_revision,
            "welcome_message": (
                patch.welcome_message
                if "welcome_message" in provided
                else current.get("welcome_message", "")
            ),
            "system_prompts": (
                patch.system_prompts
                if "system_prompts" in provided
                else current.get("system_prompts", [])
            ),
            "quick_actions": (
                patch.quick_actions
                if "quick_actions" in provided
                else current.get("quick_actions", [])
            ),
        }
    return SceneSaveRequest.model_validate(values)


def _published_values(scene: AgentScene, revision: AgentSceneRevision | None) -> tuple[str, bool, dict]:
    raw = revision.config if revision else {}
    config = SceneConfig.model_validate(raw).model_dump(mode="json")
    return (
        str(raw.get("name") or scene.name),
        bool(raw.get("enabled", scene.enabled)),
        config,
    )


def serialize_scene(scene: AgentScene, revision: AgentSceneRevision | None) -> dict:
    published_name, published_enabled, published_config = _published_values(scene, revision)
    draft = scene.draft_config
    if draft is not None:
        config = SceneConfig.model_validate(draft).model_dump(mode="json")
        name = str(draft.get("name") or scene.name)
        enabled = bool(draft.get("enabled", scene.enabled))
    else:
        config = published_config
        name = published_name
        enabled = published_enabled
    return {
        "id": str(scene.id),
        "scene_key": scene.scene_key,
        "name": name,
        "enabled": enabled,
        "revision": scene.current_revision,
        "has_unpublished_changes": draft is not None,
        **config,
        "updated_at": scene.updated_at.isoformat() if scene.updated_at else None,
    }


def serialize_published_scene(scene: AgentScene, revision: AgentSceneRevision) -> dict:
    name, enabled, config = _published_values(scene, revision)
    return {
        "id": str(scene.id),
        "scene_key": scene.scene_key,
        "name": name,
        "enabled": enabled,
        "revision": revision.revision,
        "has_unpublished_changes": False,
        **config,
        "updated_at": scene.updated_at.isoformat() if scene.updated_at else None,
    }


async def list_scenes(db: AsyncSession, agent_id: uuid.UUID) -> list[dict]:
    result = await db.execute(
        select(AgentScene).where(AgentScene.agent_id == agent_id).order_by(AgentScene.created_at, AgentScene.scene_key)
    )
    scenes = result.scalars().all()
    output = []
    for scene in scenes:
        revision = await get_revision(db, scene.id, scene.current_revision)
        output.append(serialize_scene(scene, revision))
    return output


async def get_scene(
    db: AsyncSession,
    agent_id: uuid.UUID,
    scene_key: str,
    *,
    enabled_only: bool = False,
) -> tuple[AgentScene, AgentSceneRevision | None] | None:
    key = validate_scene_key(scene_key)
    conditions = [AgentScene.agent_id == agent_id, AgentScene.scene_key == key]
    result = await db.execute(select(AgentScene).where(*conditions))
    scene = result.scalar_one_or_none()
    if not scene:
        return None
    revision = await get_revision(db, scene.id, scene.current_revision) if scene.current_revision > 0 else None
    if enabled_only:
        if not revision or not serialize_published_scene(scene, revision)["enabled"]:
            return None
    elif not revision and scene.draft_config is None:
        return None
    return scene, revision


async def get_revision(
    db: AsyncSession,
    scene_id: uuid.UUID,
    revision: int,
) -> AgentSceneRevision | None:
    result = await db.execute(
        select(AgentSceneRevision).where(
            AgentSceneRevision.scene_id == scene_id,
            AgentSceneRevision.revision == revision,
        )
    )
    return result.scalar_one_or_none()


async def save_scene(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    scene_key: str,
    data: SceneSaveRequest,
    created_by_user_id: uuid.UUID | None = None,
    created_by_agent_id: uuid.UUID | None = None,
) -> dict:
    key = validate_scene_key(scene_key)
    result = await db.execute(
        select(AgentScene)
        .where(AgentScene.agent_id == agent_id, AgentScene.scene_key == key)
        .with_for_update()
    )
    scene = result.scalar_one_or_none()
    if scene is None:
        if data.expected_revision not in (None, 0):
            raise HTTPException(status_code=409, detail="Scene revision changed; refresh and retry")
        scene = AgentScene(
            tenant_id=tenant_id,
            agent_id=agent_id,
            scene_key=key,
            name=data.name,
            enabled=data.enabled,
            current_revision=0,
        )
        db.add(scene)
        await db.flush()
    elif data.expected_revision is not None and scene.current_revision != data.expected_revision:
        raise HTTPException(status_code=409, detail="Scene revision changed; refresh and retry")

    scene.draft_config = _draft_dict(data)
    await db.flush()
    await db.refresh(scene)
    revision = await get_revision(db, scene.id, scene.current_revision) if scene.current_revision > 0 else None
    return serialize_scene(scene, revision)


async def publish_scene(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    scene_key: str,
    data: ScenePublishRequest,
    created_by_user_id: uuid.UUID | None = None,
    created_by_agent_id: uuid.UUID | None = None,
) -> dict:
    result = await db.execute(
        select(AgentScene)
        .where(
            AgentScene.agent_id == agent_id,
            AgentScene.scene_key == validate_scene_key(scene_key),
        )
        .with_for_update()
    )
    scene = result.scalar_one_or_none()
    if not scene:
        raise HTTPException(status_code=404, detail="Scene not found")
    if scene.current_revision != data.expected_revision:
        raise HTTPException(status_code=409, detail="Scene revision changed; refresh and retry")
    if scene.draft_config is None:
        raise HTTPException(status_code=409, detail="No saved draft to publish")

    payload = dict(scene.draft_config)
    scene.name = str(payload.get("name") or scene.name)
    scene.enabled = bool(payload.get("enabled", scene.enabled))
    scene.current_revision += 1
    revision = AgentSceneRevision(
        scene_id=scene.id,
        revision=scene.current_revision,
        config=payload,
        created_by_user_id=created_by_user_id,
        created_by_agent_id=created_by_agent_id,
    )
    scene.draft_config = None
    db.add(revision)
    await db.flush()
    await db.refresh(scene)
    return serialize_scene(scene, revision)


async def delete_scene(db: AsyncSession, agent_id: uuid.UUID, scene_key: str) -> None:
    result = await db.execute(
        select(AgentScene).where(
            AgentScene.agent_id == agent_id,
            AgentScene.scene_key == validate_scene_key(scene_key),
        )
    )
    scene = result.scalar_one_or_none()
    if not scene:
        raise HTTPException(status_code=404, detail="Scene not found")
    await db.delete(scene)
    await db.flush()


async def list_revisions(db: AsyncSession, agent_id: uuid.UUID, scene_key: str) -> list[dict]:
    found = await get_scene(db, agent_id, scene_key)
    if not found:
        raise HTTPException(status_code=404, detail="Scene not found")
    scene, _ = found
    result = await db.execute(
        select(AgentSceneRevision)
        .where(AgentSceneRevision.scene_id == scene.id)
        .order_by(AgentSceneRevision.revision.desc())
    )
    return [
        {
            "revision": item.revision,
            "created_at": item.created_at.isoformat() if item.created_at else None,
            "created_by_user_id": str(item.created_by_user_id) if item.created_by_user_id else None,
            "created_by_agent_id": str(item.created_by_agent_id) if item.created_by_agent_id else None,
        }
        for item in result.scalars().all()
    ]


async def get_scene_revision_detail(
    db: AsyncSession,
    agent_id: uuid.UUID,
    scene_key: str,
    revision: int,
) -> dict:
    found = await get_scene(db, agent_id, scene_key)
    if not found:
        raise HTTPException(status_code=404, detail="Scene not found")
    scene, _ = found
    source = await get_revision(db, scene.id, revision)
    if not source:
        raise HTTPException(status_code=404, detail="Scene revision not found")
    result = serialize_published_scene(scene, source)
    result["updated_at"] = source.created_at.isoformat() if source.created_at else None
    return result


async def rollback_scene(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    scene_key: str,
    target_revision: int,
    expected_revision: int,
    created_by_user_id: uuid.UUID | None = None,
    created_by_agent_id: uuid.UUID | None = None,
) -> dict:
    result = await db.execute(
        select(AgentScene)
        .where(
            AgentScene.agent_id == agent_id,
            AgentScene.scene_key == validate_scene_key(scene_key),
        )
        .with_for_update()
    )
    scene = result.scalar_one_or_none()
    if not scene:
        raise HTTPException(status_code=404, detail="Scene not found")
    if scene.current_revision != expected_revision:
        raise HTTPException(status_code=409, detail="Scene revision changed; refresh and retry")
    source = await get_revision(db, scene.id, target_revision)
    if not source:
        raise HTTPException(status_code=404, detail="Target revision not found")

    scene.current_revision += 1
    name, enabled, config = _published_values(scene, source)
    payload = {"name": name, "enabled": enabled, **config}
    scene.name = name
    scene.enabled = enabled
    scene.draft_config = None
    revision = AgentSceneRevision(
        scene_id=scene.id,
        revision=scene.current_revision,
        config=payload,
        created_by_user_id=created_by_user_id,
        created_by_agent_id=created_by_agent_id,
    )
    db.add(revision)
    await db.flush()
    await db.refresh(scene)
    return serialize_scene(scene, revision)


async def _audit_scene_tool(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    agent_id: uuid.UUID,
    operation: str,
    allowed: bool,
    reason: str,
    scene_key: str | None = None,
) -> None:
    db.add(
        AuditLog(
            user_id=user_id,
            agent_id=agent_id,
            action="scene_tool_allowed" if allowed else "scene_tool_denied",
            details={
                "operation": operation,
                "scene_key": scene_key,
                "reason": reason,
            },
        )
    )


async def execute_scene_management_tool(
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID | None,
    session_id: str,
    arguments: dict,
) -> str:
    """Execute scene management only for the real manager in this P2P session."""
    operation = str(arguments.get("operation") or "").strip().lower()
    scene_key = arguments.get("scene_key")

    async with async_session() as db:
        denial = ""
        try:
            parsed_session_id = uuid.UUID(str(session_id))
        except (TypeError, ValueError):
            parsed_session_id = None
            denial = "A valid current chat session is required"

        session = None
        if parsed_session_id:
            result = await db.execute(select(ChatSession).where(ChatSession.id == parsed_session_id))
            session = result.scalar_one_or_none()
            if not session:
                denial = "Current chat session was not found"
            elif session.agent_id != agent_id:
                denial = "Current chat session does not belong to this configuration owner"
            elif session.is_group or session.source_channel in {"agent", "trigger"}:
                denial = "Scene management is only allowed in a direct human conversation"
            elif not user_id or session.user_id != user_id:
                denial = "The calling user is not the human owner of this session"

        agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
        agent = agent_result.scalar_one_or_none()
        user = None
        if user_id:
            user_result = await db.execute(select(User).where(User.id == user_id))
            user = user_result.scalar_one_or_none()
        if not denial and (not agent or not user or not user.is_active):
            denial = "The configuration owner or active calling user was not found"
        if not denial and agent.tenant_id != user.tenant_id and user.role != "platform_admin":
            denial = "Cross-tenant scene management is not allowed"
        if not denial and not await scene_tool_enabled(db, agent_id):
            denial = "The scene management tool is not enabled"
        if not denial and not await user_can_manage_agent_id(db, user_id, agent):
            denial = "Only users with administrator permission may change scenes"

        if denial:
            await _audit_scene_tool(
                db,
                user_id=user_id,
                agent_id=agent_id,
                operation=operation,
                allowed=False,
                reason=denial,
                scene_key=str(scene_key) if scene_key else None,
            )
            await db.commit()
            return f"❌ Scene management denied: {denial}."

        try:
            if operation == "list":
                result_payload = await list_scenes(db, agent_id)
            elif operation == "get":
                found = await get_scene(db, agent_id, validate_scene_key(str(scene_key or "")))
                if not found:
                    raise HTTPException(status_code=404, detail="Scene not found")
                scene, revision = found
                result_payload = serialize_scene(scene, revision)
                result_payload["revisions"] = await list_revisions(db, agent_id, scene.scene_key)
            elif operation == "save":
                key = validate_scene_key(str(scene_key or ""))
                if "expected_revision" not in arguments:
                    raise ValueError(
                        "expected_revision is required for save; use the revision returned "
                        "by get, or 0 when the scene has never been published"
                    )
                patch_payload = SceneToolSaveRequest.model_validate(
                    {
                        field: arguments[field]
                        for field in (
                            "name",
                            "enabled",
                            "expected_revision",
                            "welcome_message",
                            "system_prompts",
                            "quick_actions",
                            "force_overwrite",
                        )
                        if field in arguments
                    }
                )
                current_found = await get_scene(db, agent_id, key)
                current_payload = (
                    serialize_scene(current_found[0], current_found[1])
                    if current_found
                    else None
                )
                payload = _merge_tool_save_request(current_payload, patch_payload)
                result_payload = await save_scene(
                    db,
                    agent_id=agent_id,
                    tenant_id=agent.tenant_id,
                    scene_key=key,
                    data=payload,
                    created_by_agent_id=agent_id,
                )
            elif operation == "publish":
                key = validate_scene_key(str(scene_key or ""))
                payload = ScenePublishRequest.model_validate(
                    {"expected_revision": arguments.get("expected_revision")}
                )
                result_payload = await publish_scene(
                    db,
                    agent_id=agent_id,
                    scene_key=key,
                    data=payload,
                    created_by_agent_id=agent_id,
                )
            elif operation == "delete":
                key = validate_scene_key(str(scene_key or ""))
                await delete_scene(db, agent_id, key)
                result_payload = {"ok": True, "scene_key": key}
            elif operation == "rollback":
                key = validate_scene_key(str(scene_key or ""))
                payload = SceneRollbackRequest.model_validate(
                    {
                        "target_revision": arguments.get("target_revision"),
                        "expected_revision": arguments.get("expected_revision"),
                    }
                )
                result_payload = await rollback_scene(
                    db,
                    agent_id=agent_id,
                    scene_key=key,
                    target_revision=payload.target_revision,
                    expected_revision=payload.expected_revision,
                    created_by_agent_id=agent_id,
                )
            else:
                return "❌ Unsupported scene operation. Use list, get, save, publish, delete, or rollback."
        except (ValidationError, ValueError) as exc:
            return f"❌ Invalid scene configuration: {exc}"
        except HTTPException as exc:
            return f"❌ Scene operation failed: {exc.detail}"

        await _audit_scene_tool(
            db,
            user_id=user_id,
            agent_id=agent_id,
            operation=operation,
            allowed=True,
            reason="manager session verified",
            scene_key=str(scene_key) if scene_key else None,
        )
        await db.commit()
        return json.dumps(result_payload, ensure_ascii=False)
