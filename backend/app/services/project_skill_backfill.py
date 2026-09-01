"""Project Skill binding and legacy-backfill operations."""

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
    _SkillBackfillPlan,
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

async def bind_library_skill_to_project_agent(
    db: AsyncSession,
    project: Project,
    *,
    skill_id: uuid.UUID | None,
    project_agent_id: uuid.UUID | None,
    is_enabled: bool,
    scope: dict,
    actor_user_id: uuid.UUID,
    actor_display_name: str,
) -> ProjectCapabilityBinding:
    """Copy one visible library Skill into a project Agent and create its binding."""

    if skill_id is None or project_agent_id is None:
        raise HTTPException(
            status_code=422,
            detail="Project Skills require a Skill and a project digital employee",
        )
    member = (
        await db.execute(
            select(ProjectMemberSnapshot).where(
                ProjectMemberSnapshot.project_id == project.id,
                ProjectMemberSnapshot.tenant_id == project.tenant_id,
                ProjectMemberSnapshot.agent_id == project_agent_id,
                ProjectMemberSnapshot.is_enabled.is_(True),
            )
        )
    ).scalar_one_or_none()
    agent = await db.get(Agent, project_agent_id)
    if (
        member is None
        or agent is None
        or agent.tenant_id != project.tenant_id
        or agent.scope != "project"
        or agent.project_id != project.id
        or agent.is_deleted
    ):
        raise HTTPException(status_code=422, detail="Project Skill target must be an active project digital employee")
    skill = (
        await db.execute(
            select(Skill)
            .where(
                Skill.id == skill_id,
                (Skill.tenant_id == project.tenant_id) | Skill.tenant_id.is_(None),
            )
            .options(selectinload(Skill.files))
        )
    ).scalar_one_or_none()
    if skill is None:
        raise HTTPException(status_code=422, detail="Skill is unavailable in this tenant")
    folder = _validate_folder_name(skill.folder_name)
    files = [
        {"path": _validate_relative_path(file.path), "content": file.content or ""}
        for file in sorted(skill.files, key=lambda item: item.path)
    ]
    try:
        validate_skill_files(files)
    except HTTPException as exc:
        raise HTTPException(status_code=422, detail=str(exc.detail)) from exc
    root = project_repo_path(project.tenant_id, project.id)
    relative_path = f"skills/{folder}"
    metadata = _asset_metadata(
        files,
        path=relative_path,
        version=str(skill.version or 1),
        source="library",
        source_agent_id=skill.publisher_agent_id,
    )
    reusable = await _find_reusable_asset(
        db,
        project,
        capability_id=skill.id,
        metadata=metadata,
    )
    if reusable is not None:
        reusable_binding, reusable_metadata = reusable
        metadata["asset_id"] = reusable_metadata["asset_id"]
        source_path = _binding_asset_path(root, reusable_binding, reusable_metadata)
    else:
        source_path = None
    target_relative = relative_path if is_enabled else _disabled_asset_path(metadata)
    target = resolve_project_agent_path(root, project_agent_id, target_relative)
    if target.exists():
        raise HTTPException(status_code=409, detail=f"Project Skill path already exists: {relative_path}")
    staging = target.with_name(f".{target.name}.{uuid.uuid4().hex}.binding")
    created = False
    try:
        if source_path is None:
            await asyncio.to_thread(_write_files, staging, files)
        else:
            await asyncio.to_thread(_link_files, source_path, staging, files)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, target)
        created = True
        binding = ProjectCapabilityBinding(
            tenant_id=project.tenant_id,
            project_id=project.id,
            capability_type="skill",
            capability_id=skill.id,
            capability_name=skill.name,
            source="inherited",
            inherited_from_agent_id=project_agent_id,
            is_enabled=is_enabled,
            scope=sanitize_template_scope(scope or {}),
            config={_ASSET_CONFIG_KEY: metadata},
        )
        db.add(binding)
        await db.flush()
        await commit_project_changes(
            project,
            f"添加项目技能：{skill.name}",
            [f".agents/{project_agent_id}/{target_relative}"],
            author_name=actor_display_name,
            author_email=project_user_git_email(actor_user_id),
        )
        return binding
    except Exception:
        if created:
            await asyncio.to_thread(shutil.rmtree, target, True)
        raise
    finally:
        await asyncio.to_thread(shutil.rmtree, staging, True)


async def snapshot_source_agent_skills(
    db: AsyncSession,
    project: Project,
    *,
    source_agent_id: uuid.UUID,
    project_agent_id: uuid.UUID,
) -> list[ProjectCapabilityBinding]:
    """Copy valid source-Agent Skills into one project Agent and bind them."""

    source_agent = await db.get(Agent, source_agent_id)
    project_agent = await db.get(Agent, project_agent_id)
    member = (
        await db.execute(
            select(ProjectMemberSnapshot).where(
                ProjectMemberSnapshot.project_id == project.id,
                ProjectMemberSnapshot.tenant_id == project.tenant_id,
                ProjectMemberSnapshot.agent_id == project_agent_id,
                ProjectMemberSnapshot.is_enabled.is_(True),
            )
        )
    ).scalar_one_or_none()
    if (
        source_agent is None
        or source_agent.tenant_id != project.tenant_id
        or source_agent.scope != "standard"
        or source_agent.is_deleted
        or project_agent is None
        or project_agent.tenant_id != project.tenant_id
        or project_agent.scope != "project"
        or project_agent.project_id != project.id
        or project_agent.is_deleted
        or member is None
    ):
        raise HTTPException(status_code=422, detail="Project Skill snapshot source or target is unavailable")

    source_prefix = normalize_storage_key(f"{source_agent_id}/skills")
    storage = get_storage_backend()
    if not await storage.is_dir(source_prefix):
        return []

    project_root = project_repo_path(project.tenant_id, project.id)
    project_agent_workspace(project_root, project_agent_id)
    prepared: list[tuple[str, str, str, list[dict[str, str]], dict, Path | None]] = []
    for entry in await storage.list_dir(source_prefix):
        if not entry.is_dir:
            continue
        folder = _validate_folder_name(entry.name)
        files = await _read_storage_skill(entry.key)
        name, version = _skill_identity(folder, files)
        metadata = _asset_metadata(
            files,
            path=f"skills/{folder}",
            version=version,
            source="agent",
            source_agent_id=source_agent_id,
        )
        reusable = await _find_reusable_asset(
            db,
            project,
            capability_id=None,
            metadata=metadata,
        )
        source_path = None
        if reusable is not None:
            reusable_binding, reusable_metadata = reusable
            metadata["asset_id"] = reusable_metadata["asset_id"]
            source_path = _binding_asset_path(project_root, reusable_binding, reusable_metadata)
        prepared.append((folder, name, version, files, metadata, source_path))

    if not prepared:
        return []

    skills_root = resolve_project_agent_path(project_root, project_agent_id, "skills")
    if skills_root.exists():
        raise HTTPException(status_code=409, detail="Project Agent Skill directory already exists")
    staging = skills_root.with_name(f".{skills_root.name}.{uuid.uuid4().hex}.importing")
    created = False
    try:
        await asyncio.to_thread(_write_prepared_skills, staging, prepared)
        skills_root.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, skills_root)
        created = True
        bindings = []
        for _folder, name, _version, _files, metadata, _source_path in prepared:
            binding = ProjectCapabilityBinding(
                tenant_id=project.tenant_id,
                project_id=project.id,
                capability_type="skill",
                capability_id=None,
                capability_name=name,
                source="inherited",
                inherited_from_agent_id=project_agent_id,
                is_enabled=True,
                scope={},
                config={_ASSET_CONFIG_KEY: metadata},
            )
            db.add(binding)
            bindings.append(binding)
        await db.flush()
        return bindings
    except Exception:
        if created:
            await asyncio.to_thread(shutil.rmtree, skills_root, True)
        raise
    finally:
        await asyncio.to_thread(shutil.rmtree, staging, True)


async def plan_project_skill_backfill(
    db: AsyncSession,
    project: Project,
) -> tuple[dict, list[_SkillBackfillPlan]]:
    """Inspect legacy Skill bindings without changing database or workspace state."""

    bindings = await _skill_bindings(db, project)
    root = project_repo_path(project.tenant_id, project.id)
    ready: list[_SkillBackfillPlan] = []
    items: list[dict] = []
    asset_ids: dict[tuple, str] = {}
    for binding in bindings:
        raw_config = binding.config if isinstance(binding.config, dict) else {}
        raw_metadata = raw_config.get(_ASSET_CONFIG_KEY)
        if isinstance(raw_metadata, dict) and raw_metadata.get("schema_version") == 2:
            try:
                _binding_asset_metadata(binding)
            except ProjectTemplateSnapshotError as exc:
                items.append(_backfill_item(binding, "blocked", str(exc)))
            else:
                items.append(_backfill_item(binding, "current"))
            continue
        if raw_metadata is not None and not (
            isinstance(raw_metadata, dict)
            and raw_metadata.get("schema_version") == 1
            and set(raw_metadata) == _ASSET_KEYS_V1
        ):
            items.append(_backfill_item(binding, "blocked", "Legacy Skill metadata is invalid"))
            continue
        try:
            plan = await _prepare_skill_backfill_plan(
                db,
                project,
                binding,
                raw_metadata,
                root,
                asset_ids,
            )
        except (HTTPException, OSError, ProjectTemplateSnapshotError) as exc:
            detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
            items.append(_backfill_item(binding, "blocked", str(detail)))
            continue
        ready.append(plan)
        items.append(
            _backfill_item(
                binding,
                "ready",
                previous_schema_version=plan.previous_schema_version,
            )
        )
    summary = {
        "project_id": str(project.id),
        "ready_count": sum(item["status"] == "ready" for item in items),
        "current_count": sum(item["status"] == "current" for item in items),
        "blocked_count": sum(item["status"] == "blocked" for item in items),
        "items": items,
    }
    return summary, ready


async def apply_project_skill_backfill(
    db: AsyncSession,
    project: Project,
    *,
    actor_user_id: uuid.UUID,
    actor_display_name: str,
) -> tuple[dict, list[dict]]:
    """Upgrade every safe legacy Skill binding as one compensating operation."""

    summary, plans = await plan_project_skill_backfill(db, project)
    if summary["blocked_count"]:
        raise HTTPException(
            status_code=409,
            detail={"code": "project_skill_backfill_blocked", **summary},
        )
    moved: list[tuple[Path, Path]] = []
    previous_configs = [(plan.binding, dict(plan.binding.config or {})) for plan in plans]
    try:
        for plan in plans:
            if plan.source_path != plan.target_path:
                plan.target_path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(plan.source_path, plan.target_path)
                moved.append((plan.source_path, plan.target_path))
            plan.binding.config = {
                **dict(plan.binding.config or {}),
                _ASSET_CONFIG_KEY: dict(plan.metadata),
            }
        await db.flush()
        if moved:
            await commit_project_changes(
                project,
                f"更新 {len(plans)} 项项目技能",
                _skill_backfill_git_paths(project, moved),
                author_name=actor_display_name,
                author_email=project_user_git_email(actor_user_id),
            )
    except Exception:
        for source, target in reversed(moved):
            if target.exists() and not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                os.replace(target, source)
        for binding, config in previous_configs:
            binding.config = config
        raise
    rollback_entries = [
        {
            "binding_id": str(plan.binding.id),
            "previous_schema_version": plan.previous_schema_version,
            "previous_asset_id": plan.previous_asset_id,
            "applied_asset_id": plan.metadata["asset_id"],
            "other_config_fingerprint": plan.other_config_fingerprint,
        }
        for plan in plans
    ]
    return {**summary, "applied_count": len(plans)}, rollback_entries


async def rollback_project_skill_backfill(
    db: AsyncSession,
    project: Project,
    entries: object,
    *,
    actor_user_id: uuid.UUID,
    actor_display_name: str,
) -> dict:
    """Restore the exact legacy metadata shape when assets remain unchanged."""

    if not isinstance(entries, list):
        raise HTTPException(status_code=409, detail="Project Skill backfill audit data is invalid")
    bindings_by_id = {binding.id: binding for binding in await _skill_bindings(db, project)}
    prepared: list[tuple[ProjectCapabilityBinding, dict, dict | None, Path, Path]] = []
    root = project_repo_path(project.tenant_id, project.id)
    for entry in entries:
        if not isinstance(entry, dict):
            raise HTTPException(status_code=409, detail="Project Skill backfill audit data is invalid")
        try:
            binding_id = uuid.UUID(str(entry.get("binding_id")))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail="Project Skill backfill audit data is invalid") from exc
        binding = bindings_by_id.get(binding_id)
        if binding is None:
            raise HTTPException(status_code=409, detail="A backfilled Project Skill binding is unavailable")
        metadata = _require_skill_asset(binding)
        if metadata["asset_id"] != entry.get("applied_asset_id"):
            raise HTTPException(status_code=409, detail="Project Skill changed after backfill")
        if _other_config_fingerprint(binding.config) != entry.get("other_config_fingerprint"):
            raise HTTPException(status_code=409, detail="Project Skill configuration changed after backfill")
        source = _binding_asset_path(root, binding, metadata)
        files = await asyncio.to_thread(_read_skill_root, source, metadata["path"])
        if _skill_hash(files) != metadata["sha256"]:
            raise HTTPException(status_code=409, detail="Project Skill files changed after backfill")
        previous_schema = entry.get("previous_schema_version")
        if previous_schema == 0:
            previous_metadata = None
            if binding.inherited_from_agent_id is None:
                raise HTTPException(status_code=409, detail="Project Skill member is unavailable")
            target = resolve_project_agent_path(
                root,
                binding.inherited_from_agent_id,
                metadata["path"],
            )
        elif previous_schema == 1:
            previous_metadata = {
                key: value
                for key, value in metadata.items()
                if key != "asset_id"
            }
            previous_metadata["schema_version"] = 1
            old_asset_id = str(entry.get("previous_asset_id") or "")
            if not re.fullmatch(r"[0-9a-f]{64}", old_asset_id):
                raise HTTPException(status_code=409, detail="Project Skill backfill audit data is invalid")
            target = (
                resolve_project_agent_path(root, binding.inherited_from_agent_id, metadata["path"])
                if binding.is_enabled
                else resolve_project_agent_path(
                    root,
                    binding.inherited_from_agent_id,
                    f".disabled-skills/{old_asset_id}/{PurePosixPath(metadata['path']).name}",
                )
            )
        else:
            raise HTTPException(status_code=409, detail="Project Skill backfill audit data is invalid")
        if target != source and target.exists():
            raise HTTPException(status_code=409, detail="Project Skill rollback path is occupied")
        prepared.append((binding, dict(binding.config or {}), previous_metadata, source, target))

    moved: list[tuple[Path, Path]] = []
    try:
        for binding, current_config, previous_metadata, source, target in prepared:
            if source != target:
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, target)
                moved.append((source, target))
            restored_config = dict(current_config)
            if previous_metadata is None:
                restored_config.pop(_ASSET_CONFIG_KEY, None)
            else:
                restored_config[_ASSET_CONFIG_KEY] = previous_metadata
            binding.config = restored_config
        await db.flush()
        if moved:
            await commit_project_changes(
                project,
                f"恢复 {len(prepared)} 项项目技能设置",
                _skill_backfill_git_paths(project, moved),
                author_name=actor_display_name,
                author_email=project_user_git_email(actor_user_id),
            )
    except Exception:
        for source, target in reversed(moved):
            if target.exists() and not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                os.replace(target, source)
        for binding, current_config, _previous_metadata, _source, _target in prepared:
            binding.config = current_config
        raise
    return {
        "project_id": str(project.id),
        "rolled_back_count": len(prepared),
        "binding_ids": [str(binding.id) for binding, *_rest in prepared],
    }


async def register_project_workspace_skill(
    db: AsyncSession,
    project: Project,
    *,
    project_agent_id: uuid.UUID,
    folder_name: str,
    actor_user_id: uuid.UUID,
    actor_display_name: str,
) -> ProjectCapabilityBinding:
    """Register the existing project Agent Skill directory without copying it."""

    folder = _validate_folder_name(folder_name)
    relative_path = f"skills/{folder}"
    root = project_repo_path(project.tenant_id, project.id)
    files = await asyncio.to_thread(_read_local_skill, root, project_agent_id, relative_path)
    existing = next(
        (
            binding
            for binding in await _skill_bindings(db, project)
            if binding.inherited_from_agent_id == project_agent_id
            and (metadata := _binding_asset_metadata(binding)) is not None
            and metadata["path"] == relative_path
        ),
        None,
    )
    existing_metadata = _binding_asset_metadata(existing) if existing is not None else None
    name, version = _skill_identity(
        folder,
        files,
        default_version=existing_metadata["version"] if existing_metadata is not None else "1",
    )
    if existing is None:
        metadata = _asset_metadata(
            files,
            path=relative_path,
            version=version,
            source="workspace",
            source_agent_id=None,
        )
        existing = ProjectCapabilityBinding(
            tenant_id=project.tenant_id,
            project_id=project.id,
            capability_type="skill",
            capability_id=None,
            capability_name=name,
            source="inherited",
            inherited_from_agent_id=project_agent_id,
            is_enabled=True,
            scope={},
            config={_ASSET_CONFIG_KEY: metadata},
        )
        db.add(existing)
    else:
        old_metadata = _require_skill_asset(existing)
        group = await _shared_asset_bindings(db, project, existing, old_metadata)
        for shared_binding, shared_metadata in group:
            shared_binding.capability_name = name
            shared_binding.config = {
                _ASSET_CONFIG_KEY: _asset_metadata(
                    files,
                    path=shared_metadata["path"],
                    version=version,
                    source=shared_metadata["source"],
                    source_agent_id=shared_metadata.get("source_agent_id"),
                    asset_id=shared_metadata["asset_id"],
                )
            }
    await db.flush()
    await commit_project_changes(
        project,
        f"更新项目技能：{name}",
        [f".agents/{project_agent_id}/{relative_path}"],
        author_name=actor_display_name,
        author_email=project_user_git_email(actor_user_id),
    )
    return existing
