"""Automatic selection shares scene publication and the existing turn snapshot."""

import uuid

from fastapi import HTTPException
from sqlalchemy import select

from app.models.agent import Agent
from app.models.scene import AgentScene, AgentSceneRevision
from app.models.user import User
from app.services.scene_targets import current_target_session, decode_target, encode_target, session_target_identity


async def published_scenes(db, agent_id):
    return (await db.execute(select(AgentScene, AgentSceneRevision).join(
        AgentSceneRevision,
        (AgentSceneRevision.scene_id == AgentScene.id)
        & (AgentSceneRevision.revision == AgentScene.current_revision),
    ).where(AgentScene.agent_id == agent_id, AgentScene.enabled.is_(True)))).all()


async def validate_auto_activation(db, agent_id, config, *, viewer_id=None, scene_id=None, publishing=False):
    """Validate against live owned conversations; serialize conflicting publications."""
    auto = config.get("auto_activation") or {}
    if auto.get("enabled") and not auto.get("targets"):
        raise HTTPException(422, detail="sceneAuto.selectRequired")
    if not auto.get("targets") or not auto.get("enabled") or not config.get("enabled", True):
        return
    query = select(Agent).where(Agent.id == agent_id)
    agent = await db.scalar(query.with_for_update() if publishing else query)
    if agent is None:
        raise HTTPException(404, detail="sceneAuto.targetUnavailable")
    targets = auto.get("targets") or []
    viewer = await db.get(User, viewer_id) if viewer_id else None
    refs = set()
    for target in targets:
        identity = decode_target(target["target_ref"], agent)
        session = await current_target_session(db, agent, identity, viewer=viewer)
        if session is None or session.context_terminated_reason:
            raise HTTPException(422, detail="sceneAuto.targetUnavailable")
        ref = encode_target(identity)
        if ref in refs:
            raise HTTPException(422, detail="sceneAuto.duplicateTarget")
        refs.add(ref)
        target.update(target_ref=ref, source_channel=session.source_channel, is_group=session.is_group)
    if not publishing or not auto.get("enabled") or not config.get("enabled", True):
        return
    for scene, revision in await published_scenes(db, agent_id):
        other = revision.config.get("auto_activation") or {}
        if scene.id != scene_id and other.get("enabled"):
            if refs.intersection(item["target_ref"] for item in other.get("targets", [])):
                raise HTTPException(409, detail="sceneAuto.targetConflict")


async def resolve_session_scene(db, agent_id: uuid.UUID, session, *, explicit_key=None):
    # The scene service also calls publication validation here, so defer this
    # import to avoid a module initialization cycle.
    from app.services.scene_service import resolve_scene_for_activation, scene_tool_enabled, serialize_published_scene

    preference = (session.im_config or {}) if session else {}
    selected = explicit_key or preference.get("scene_key")
    if selected:
        resolved = await resolve_scene_for_activation(db, agent_id, selected)
        return resolved.manifest
    if session is None or preference.get("scene_disabled") or session.context_terminated_reason:
        return None
    if not await scene_tool_enabled(db, agent_id):
        return None
    candidates = [(scene, revision) for scene, revision in await published_scenes(db, agent_id)
                  if (revision.config.get("auto_activation") or {}).get("enabled")]
    if not candidates:
        return None
    agent = await db.get(Agent, agent_id)
    if agent is None:
        return None
    identity = await session_target_identity(db, agent, session)
    if identity is None:
        return None
    current = await current_target_session(db, agent, identity)
    if current is None or current.id != session.id:
        return None
    ref = encode_target(identity)
    for scene, revision in candidates:
        auto = revision.config.get("auto_activation") or {}
        if auto.get("enabled") and any(item["target_ref"] == ref for item in auto.get("targets", [])):
            return {**serialize_published_scene(scene, revision), "activation_source": "automatic", "welcome_message": ""}
    return None


async def snapshot_project_scene(db, agent_id, parent, metadata):
    if not parent.project_id or not parent.is_group:
        return
    from app.services.scene_service import scene_message_meta

    manifest = await resolve_session_scene(db, agent_id, parent)
    for key in ("scene_key", "scene_revision", "activation_source"):
        metadata.pop(key, None)
    metadata.update(scene_message_meta(manifest))
