"""Project-owned Skill snapshots and portable template packages."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.agent import Agent
from app.models.project import Project, ProjectCapabilityBinding, ProjectMemberSnapshot
from app.models.skill import Skill
from app.services.project_agent_workspace import (
    is_sensitive_project_asset_path,
    project_agent_workspace,
    resolve_project_agent_path,
)
from app.services.project_git_service import commit_project_changes, project_repo_path, project_user_git_email
from app.services.project_template_snapshot import ProjectTemplateSnapshotError, sanitize_template_scope
from app.services.skill_market import MAX_SKILL_FILES, validate_skill_files
from app.services.storage import get_storage_backend, normalize_storage_key
from app.services.project_skill_assets_support import (
    _ASSET_CONFIG_KEY,
    _ASSET_KEYS_V1,
    _ASSET_KEYS,
    _PACKAGE_KEYS,
    _PACKAGE_FILE_KEYS,
    _FRONTMATTER_FIELD,
    _read_storage_skill,
    _read_local_skill,
    _read_skill_root,
    _asset_metadata,
    _backfill_item,
    _other_config_fingerprint,
    _skill_backfill_git_paths,
    _prepare_skill_backfill_plan,
    _discover_legacy_skill,
    _binding_asset_metadata,
    _skill_bindings,
    _disabled_asset_path,
    _binding_asset_path,
    _read_binding_skill,
    _find_reusable_asset,
    _require_skill_asset,
    _asset_group_key,
    _shared_asset_bindings,
    _load_refresh_source,
    _validate_packages,
    _decode_package_files,
    _package_file,
    _skill_hash,
    _skill_identity,
    _skill_description,
    _validate_folder_name,
    _validate_relative_path,
    _write_prepared_skills,
    _write_files,
    _link_files,
)
from app.services.project_skill_backfill import (
    _SkillBackfillPlan,
    bind_library_skill_to_project_agent,
    snapshot_source_agent_skills,
    plan_project_skill_backfill,
    apply_project_skill_backfill,
    rollback_project_skill_backfill,
    register_project_workspace_skill,
)

async def project_skill_manifest(
    db: AsyncSession,
    project: Project,
    *,
    project_root: Path | None = None,
) -> list[dict]:
    """Return owner-facing, per-binding Skill asset metadata."""

    bindings = await _skill_bindings(db, project)
    agent_ids = {binding.inherited_from_agent_id for binding in bindings if binding.inherited_from_agent_id}
    members = {}
    if agent_ids:
        rows = (
            await db.execute(
                select(ProjectMemberSnapshot).where(
                    ProjectMemberSnapshot.project_id == project.id,
                    ProjectMemberSnapshot.tenant_id == project.tenant_id,
                    ProjectMemberSnapshot.agent_id.in_(agent_ids),
                )
            )
        ).scalars()
        members = {row.agent_id: row for row in rows}

    root = project_root or project_repo_path(project.tenant_id, project.id)
    result: list[dict] = []
    for binding in bindings:
        metadata = _binding_asset_metadata(binding)
        if metadata is None or binding.inherited_from_agent_id is None:
            continue
        files = await asyncio.to_thread(_read_binding_skill, root, binding, metadata)
        observed = _asset_metadata(
            files,
            path=metadata["path"],
            version=_skill_identity(
                PurePosixPath(metadata["path"]).name,
                files,
                default_version=metadata["version"],
            )[1],
            source=metadata["source"],
            source_agent_id=metadata.get("source_agent_id"),
            asset_id=metadata["asset_id"],
        )
        member = members.get(binding.inherited_from_agent_id)
        if member is None:
            raise ProjectTemplateSnapshotError("Project Skill binding member is unavailable")
        result.append(
            {
                "binding_id": str(binding.id),
                "asset_id": metadata["asset_id"],
                "member_id": str(member.id),
                "member_agent_id": str(member.agent_id),
                "member_name": member.name_snapshot,
                "member_role": member.role_snapshot,
                "name": binding.capability_name,
                "version": observed["version"],
                "path": metadata["path"],
                "source": metadata["source"],
                "is_enabled": binding.is_enabled,
                "file_count": observed["file_count"],
                "size_bytes": observed["size_bytes"],
                "sha256": observed["sha256"],
            }
        )
    return result


async def observe_project_skill_binding(
    db: AsyncSession,
    project: Project,
    binding: ProjectCapabilityBinding,
) -> dict:
    """Return UI-safe availability and size metadata for one Skill binding."""

    try:
        metadata = _binding_asset_metadata(binding)
    except ProjectTemplateSnapshotError:
        metadata = None
    if metadata is None or binding.inherited_from_agent_id is None:
        return {
            "asset_id": None,
            "availability": "missing",
            "description": "",
            "version": None,
            "file_count": 0,
            "size_bytes": 0,
        }
    member = (
        await db.execute(
            select(ProjectMemberSnapshot).where(
                ProjectMemberSnapshot.project_id == project.id,
                ProjectMemberSnapshot.tenant_id == project.tenant_id,
                ProjectMemberSnapshot.agent_id == binding.inherited_from_agent_id,
            )
        )
    ).scalar_one_or_none()
    if member is None:
        availability = "restricted"
        files: list[dict[str, str]] = []
    else:
        try:
            files = await asyncio.to_thread(
                _read_binding_skill,
                project_repo_path(project.tenant_id, project.id),
                binding,
                metadata,
            )
            observed = _asset_metadata(
                files,
                path=metadata["path"],
                version=_skill_identity(
                    PurePosixPath(metadata["path"]).name,
                    files,
                    default_version=metadata["version"],
                )[1],
                source=metadata["source"],
                source_agent_id=metadata.get("source_agent_id"),
                asset_id=metadata["asset_id"],
            )
            availability = "available"
        except (HTTPException, OSError, ProjectTemplateSnapshotError):
            files = []
            availability = "missing"
    affected_member_count = len(await _shared_asset_bindings(db, project, binding, metadata))
    return {
        "asset_id": metadata["asset_id"],
        "affected_member_count": affected_member_count,
        "availability": availability,
        "description": _skill_description(files),
        "version": observed["version"] if files else metadata["version"],
        "file_count": observed["file_count"] if files else metadata["file_count"],
        "size_bytes": observed["size_bytes"] if files else metadata["size_bytes"],
    }


async def set_project_skill_enabled(
    db: AsyncSession,
    project: Project,
    binding: ProjectCapabilityBinding,
    *,
    enabled: bool,
    actor_user_id: uuid.UUID,
    actor_display_name: str,
) -> None:
    """Move one member projection in or out of its active Skill directory."""

    metadata = _require_skill_asset(binding)
    if binding.is_enabled == enabled:
        return
    root = project_repo_path(project.tenant_id, project.id)
    source = _binding_asset_path(root, binding, metadata)
    target_relative = metadata["path"] if enabled else _disabled_asset_path(metadata)
    if binding.inherited_from_agent_id is None:
        raise HTTPException(status_code=422, detail="Project Skill member is unavailable")
    target = resolve_project_agent_path(root, binding.inherited_from_agent_id, target_relative)
    if not source.is_dir():
        raise HTTPException(status_code=409, detail="Project Skill files are unavailable")
    if target.exists():
        raise HTTPException(status_code=409, detail=f"Project Skill path already exists: {target_relative}")
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, target)
    previous = binding.is_enabled
    binding.is_enabled = enabled
    try:
        await db.flush()
        await commit_project_changes(
            project,
            f"{'启用' if enabled else '停用'}项目技能：{binding.capability_name}",
            [
                f".agents/{binding.inherited_from_agent_id}/{metadata['path']}",
                f".agents/{binding.inherited_from_agent_id}/{_disabled_asset_path(metadata)}",
            ],
            author_name=actor_display_name,
            author_email=project_user_git_email(actor_user_id),
        )
    except Exception:
        binding.is_enabled = previous
        source.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and not source.exists():
            os.replace(target, source)
        raise


async def project_skill_deletion_impact(
    db: AsyncSession,
    project: Project,
    binding: ProjectCapabilityBinding,
) -> dict:
    """Describe every member association removed with one shared Skill asset."""

    metadata = _require_skill_asset(binding)
    group = await _shared_asset_bindings(db, project, binding, metadata)
    agent_ids = {item.inherited_from_agent_id for item, _metadata in group if item.inherited_from_agent_id}
    members = {}
    if agent_ids:
        rows = (
            await db.execute(
                select(ProjectMemberSnapshot).where(
                    ProjectMemberSnapshot.project_id == project.id,
                    ProjectMemberSnapshot.tenant_id == project.tenant_id,
                    ProjectMemberSnapshot.agent_id.in_(agent_ids),
                )
            )
        ).scalars()
        members = {row.agent_id: row for row in rows}
    affected = []
    for item, _item_metadata in group:
        member = members.get(item.inherited_from_agent_id)
        if member is None:
            raise HTTPException(status_code=409, detail="Project Skill has an unavailable member association")
        affected.append(
            {
                "binding_id": str(item.id),
                "member_id": str(member.id),
                "agent_id": str(member.agent_id),
                "name": member.name_snapshot,
                "role": member.role_snapshot,
                "is_active": member.is_enabled,
                "skill_enabled": item.is_enabled,
            }
        )
    return {
        "asset_id": metadata["asset_id"],
        "skill_name": binding.capability_name,
        "version": metadata["version"],
        "affected_member_count": len(affected),
        "affected_members": affected,
    }


async def delete_project_skill_asset(
    db: AsyncSession,
    project: Project,
    binding: ProjectCapabilityBinding,
    *,
    actor_user_id: uuid.UUID,
    actor_display_name: str,
) -> dict:
    """Delete one shared asset and all of its member associations without dangling bindings."""

    metadata = _require_skill_asset(binding)
    impact = await project_skill_deletion_impact(db, project, binding)
    group = await _shared_asset_bindings(db, project, binding, metadata)
    root = project_repo_path(project.tenant_id, project.id)
    moved: list[tuple[Path, Path]] = []
    changed_paths: list[str] = []
    try:
        for item, item_metadata in group:
            source = _binding_asset_path(root, item, item_metadata)
            if not source.is_dir():
                raise HTTPException(status_code=409, detail="Project Skill files are unavailable")
            backup = source.with_name(f".{source.name}.{uuid.uuid4().hex}.deleting")
            os.replace(source, backup)
            moved.append((source, backup))
            relative = item_metadata["path"] if item.is_enabled else _disabled_asset_path(item_metadata)
            changed_paths.append(f".agents/{item.inherited_from_agent_id}/{relative}")
            await db.delete(item)
        await db.flush()
        await commit_project_changes(
            project,
            f"删除项目技能：{binding.capability_name}",
            changed_paths,
            author_name=actor_display_name,
            author_email=project_user_git_email(actor_user_id),
        )
    except Exception:
        for source, backup in reversed(moved):
            if backup.exists() and not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                os.replace(backup, source)
        raise
    for _source, backup in moved:
        await asyncio.to_thread(shutil.rmtree, backup, True)
    return impact


async def refresh_project_skill_asset(
    db: AsyncSession,
    project: Project,
    binding: ProjectCapabilityBinding,
    *,
    actor_user_id: uuid.UUID,
    actor_display_name: str,
) -> dict:
    """Refresh a shared asset from its existing library or source-Agent origin."""

    metadata = _require_skill_asset(binding)
    files, name, version, folder = await _load_refresh_source(db, project, binding, metadata)
    new_metadata = _asset_metadata(
        files,
        path=f"skills/{folder}",
        version=version,
        source=metadata["source"],
        source_agent_id=metadata.get("source_agent_id"),
        asset_id=metadata["asset_id"],
    )
    group = await _shared_asset_bindings(db, project, binding, metadata)
    impact = await project_skill_deletion_impact(db, project, binding)
    changed = any(
        new_metadata[key] != metadata[key] for key in ("version", "path", "sha256", "file_count", "size_bytes")
    ) or name != binding.capability_name
    if not changed:
        return {"changed": False, **impact}

    root = project_repo_path(project.tenant_id, project.id)
    prepared: list[tuple[ProjectCapabilityBinding, dict, Path, Path, Path, Path]] = []
    staging_paths: list[Path] = []
    first_staging: Path | None = None
    try:
        for item, old_metadata in group:
            old_path = _binding_asset_path(root, item, old_metadata)
            relative = new_metadata["path"] if item.is_enabled else _disabled_asset_path(new_metadata)
            if item.inherited_from_agent_id is None:
                raise HTTPException(status_code=409, detail="Project Skill member is unavailable")
            target = resolve_project_agent_path(root, item.inherited_from_agent_id, relative)
            if target != old_path and target.exists():
                raise HTTPException(status_code=409, detail=f"Project Skill path already exists: {relative}")
            staging = target.with_name(f".{target.name}.{uuid.uuid4().hex}.refreshing")
            if first_staging is None:
                await asyncio.to_thread(_write_files, staging, files)
                first_staging = staging
            else:
                await asyncio.to_thread(_link_files, first_staging, staging, files)
            staging_paths.append(staging)
            backup = old_path.with_name(f".{old_path.name}.{uuid.uuid4().hex}.previous")
            prepared.append((item, old_metadata, old_path, target, backup, staging))

        moved: list[tuple[Path, Path]] = []
        installed: list[Path] = []
        old_states = [(item, item.capability_name, dict(item.config or {})) for item, *_rest in prepared]
        try:
            for item, _old_metadata, old_path, target, backup, staging in prepared:
                os.replace(old_path, backup)
                moved.append((old_path, backup))
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staging, target)
                installed.append(target)
                item.capability_name = name
                item.config = {_ASSET_CONFIG_KEY: dict(new_metadata)}
            await db.flush()
            changed_paths = []
            for item, old_metadata, _old_path, _target, _backup, _staging in prepared:
                old_relative = old_metadata["path"] if item.is_enabled else _disabled_asset_path(old_metadata)
                new_relative = new_metadata["path"] if item.is_enabled else _disabled_asset_path(new_metadata)
                changed_paths.extend(
                    [
                        f".agents/{item.inherited_from_agent_id}/{old_relative}",
                        f".agents/{item.inherited_from_agent_id}/{new_relative}",
                    ]
                )
            await commit_project_changes(
                project,
                f"刷新项目技能：{name}",
                changed_paths,
                author_name=actor_display_name,
                author_email=project_user_git_email(actor_user_id),
            )
        except Exception:
            for target in installed:
                await asyncio.to_thread(shutil.rmtree, target, True)
            for old_path, backup in reversed(moved):
                if backup.exists() and not old_path.exists():
                    old_path.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(backup, old_path)
            for item, old_name, old_config in old_states:
                item.capability_name = old_name
                item.config = old_config
            raise
        for _old_path, backup in moved:
            await asyncio.to_thread(shutil.rmtree, backup, True)
    finally:
        for path in staging_paths:
            await asyncio.to_thread(shutil.rmtree, path, True)
    return {"changed": True, **impact, "version": new_metadata["version"]}


async def export_project_skills_for_template(
    db: AsyncSession,
    project: Project,
    *,
    included_binding_ids: list[uuid.UUID],
    project_root: Path | None = None,
) -> list[dict]:
    """Package only explicitly selected project Skill snapshots."""

    if not included_binding_ids:
        return []
    if len(set(included_binding_ids)) != len(included_binding_ids):
        raise ProjectTemplateSnapshotError("Selected project Skill bindings must be unique")
    manifest = await project_skill_manifest(db, project, project_root=project_root)
    by_id = {uuid.UUID(item["binding_id"]): item for item in manifest}
    missing = set(included_binding_ids) - set(by_id)
    if missing:
        raise ProjectTemplateSnapshotError("One or more selected project Skills are unavailable")
    template_agent_ids = list(
        (
            await db.execute(
                select(Agent.id)
                .join(
                    ProjectMemberSnapshot,
                    (ProjectMemberSnapshot.project_id == project.id)
                    & (ProjectMemberSnapshot.agent_id == Agent.id),
                )
                .where(
                    ProjectMemberSnapshot.tenant_id == project.tenant_id,
                    Agent.tenant_id == project.tenant_id,
                    Agent.is_deleted.is_(False),
                )
                .order_by(
                    ProjectMemberSnapshot.is_leader.desc(),
                    ProjectMemberSnapshot.created_at,
                    ProjectMemberSnapshot.id,
                )
            )
        ).scalars()
    )
    agent_index = {agent_id: index for index, agent_id in enumerate(template_agent_ids)}
    bindings_by_id = {binding.id: binding for binding in await _skill_bindings(db, project)}
    root = project_root or project_repo_path(project.tenant_id, project.id)
    packages: list[dict] = []
    for binding_id in included_binding_ids:
        item = by_id[binding_id]
        member_agent_id = uuid.UUID(item["member_agent_id"])
        if member_agent_id not in agent_index:
            raise ProjectTemplateSnapshotError("Selected project Skill does not belong to a packaged digital employee")
        binding = bindings_by_id[binding_id]
        metadata = _binding_asset_metadata(binding)
        if metadata is None:
            raise ProjectTemplateSnapshotError("Selected project Skill metadata is unavailable")
        files = await asyncio.to_thread(_read_binding_skill, root, binding, metadata)
        packages.append(
            {
                "schema_version": 1,
                "name": item["name"],
                "version": item["version"],
                "folder_name": PurePosixPath(item["path"]).name,
                "digital_employee_index": agent_index[member_agent_id],
                "is_enabled": item["is_enabled"],
                "scope": {},
                "sha256": item["sha256"],
                "file_count": item["file_count"],
                "size_bytes": item["size_bytes"],
                "files": [_package_file(file) for file in files],
            }
        )
    return packages


async def instantiate_project_skills_from_template(
    db: AsyncSession,
    project: Project,
    owner_display_name: str,
    owner_user_id: uuid.UUID,
    raw_skill_assets: object,
    created_agent_ids: list[uuid.UUID],
) -> list[ProjectCapabilityBinding]:
    """Restore packaged files and fresh project-local bindings without source Skill rows."""

    packages = _validate_packages(raw_skill_assets, len(created_agent_ids))
    if not packages:
        return []
    root = project_repo_path(project.tenant_id, project.id)
    created_paths: list[Path] = []
    changed_paths: list[str] = []
    bindings: list[ProjectCapabilityBinding] = []
    restored_assets: dict[tuple[str, str, str, str], tuple[Path, str]] = {}
    try:
        for package in packages:
            agent_id = created_agent_ids[package["digital_employee_index"]]
            relative_path = f"skills/{package['folder_name']}"
            identity = (
                package["name"],
                package["version"],
                package["folder_name"],
                package["sha256"],
            )
            reusable = restored_assets.get(identity)
            asset_id = reusable[1] if reusable is not None else str(uuid.uuid4())
            target_relative = (
                relative_path
                if package["is_enabled"]
                else _disabled_asset_path({"asset_id": asset_id, "path": relative_path})
            )
            target = resolve_project_agent_path(root, agent_id, target_relative)
            if target.exists():
                raise ProjectTemplateSnapshotError(f"Project Skill path already exists: {target_relative}")
            staging = target.with_name(f".{target.name}.{uuid.uuid4().hex}.restoring")
            try:
                if reusable is None:
                    await asyncio.to_thread(_write_files, staging, package["decoded_files"])
                else:
                    await asyncio.to_thread(_link_files, reusable[0], staging, package["decoded_files"])
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staging, target)
            finally:
                await asyncio.to_thread(shutil.rmtree, staging, True)
            created_paths.append(target)
            changed_paths.append(f".agents/{agent_id}/{target_relative}")
            restored_assets.setdefault(identity, (target, asset_id))
            metadata = _asset_metadata(
                package["decoded_files"],
                path=relative_path,
                version=package["version"],
                source="template",
                source_agent_id=None,
                asset_id=asset_id,
            )
            binding = ProjectCapabilityBinding(
                tenant_id=project.tenant_id,
                project_id=project.id,
                capability_type="skill",
                capability_id=None,
                capability_name=package["name"],
                source="inherited",
                inherited_from_agent_id=agent_id,
                is_enabled=package["is_enabled"],
                scope=package["scope"],
                config={_ASSET_CONFIG_KEY: metadata},
            )
            db.add(binding)
            bindings.append(binding)
        await db.flush()
        await commit_project_changes(
            project,
            f"从模板恢复 {len(bindings)} 项项目技能",
            changed_paths,
            author_name=owner_display_name,
            author_email=project_user_git_email(owner_user_id),
        )
        return bindings
    except Exception:
        for path in created_paths:
            await asyncio.to_thread(shutil.rmtree, path, True)
        raise
