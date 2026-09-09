"""OpenAPI reads and selects scenes through the ordinary published scene owner."""

from app.services.openapi_applications import fail
from app.services.scene_activation import published_scenes
from app.services.scene_service import (
    SCENE_STATUS_OK,
    project_scene_manifest,
    resolve_scene_for_activation,
    scene_tool_enabled,
    serialize_published_scene,
)


async def available_scenes(db, employee_id):
    if not await scene_tool_enabled(db, employee_id):
        return []
    manifests = [serialize_published_scene(scene, revision)
                 for scene, revision in await published_scenes(db, employee_id)]
    return [project_scene_manifest(manifest)
            for manifest in sorted(manifests, key=lambda item: (item["name"], item["scene_key"]))
            if manifest["enabled"]]


async def require_available_scene(db, employee_id, scene_key):
    resolved = await resolve_scene_for_activation(db, employee_id, scene_key)
    if resolved.status != SCENE_STATUS_OK or not resolved.manifest:
        fail("scene_unavailable", 404)
    return resolved.manifest


def select_session_scene(session, scene_key):
    if scene_key is None:
        return
    config = dict(session.im_config or {})
    config.pop("scene_disabled", None)
    config["scene_key"] = scene_key
    session.im_config = config
