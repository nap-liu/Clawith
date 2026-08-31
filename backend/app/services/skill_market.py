"""Minimal first-party Skill market service.

The existing ``Skill``/``SkillFile`` registry is the catalog. This module owns
the single publish/search/install path shared by REST APIs and Agent tools.
"""

from __future__ import annotations

import re
import uuid
from pathlib import PurePosixPath

from fastapi import HTTPException, status
from sqlalchemy import and_, case, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload

from app.config import get_settings
from app.models.agent import Agent
from app.models.skill import Skill, SkillFile, SkillInstall
from app.models.user import User
from app.services.storage import get_storage_backend, normalize_storage_key
from app.services.workspace_locking import serialize_workspace_write

_settings = get_settings()
MAX_SKILL_SIZE = int(getattr(_settings, "MAX_SKILL_SIZE", 512_000) or 512_000)
MAX_SKILL_FILES = 500
_SECRET_RE = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|password|client[_-]?secret)\s*[:=]\s*['\"]?[A-Za-z0-9_./+\-=]{16,}"
)
_PRIVATE_KEY_MARKER = "-----BEGIN PRIVATE KEY-----"


def _market_scope(tenant_id: uuid.UUID | None):
    return and_(
        Skill.status == "published",
        or_(Skill.visibility == "public", Skill.tenant_id == tenant_id),
    )


def _normalize_skill_folder(path: str) -> str:
    normalized = str(path or "").replace("\\", "/").strip().strip("/")
    parts = PurePosixPath(normalized).parts
    if len(parts) == 1:
        folder = parts[0]
    elif len(parts) == 2 and parts[0] == "skills":
        folder = parts[1]
    else:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Skill path must be skills/<folder>")
    if folder in {"", ".", ".."} or len(folder) > 100 or "/" in folder:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid Skill folder")
    return folder


def _validate_relative_file_path(path: str) -> str:
    normalized = str(path or "").replace("\\", "/").lstrip("/")
    pure = PurePosixPath(normalized)
    if not normalized or pure.is_absolute() or ".." in pure.parts:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid Skill file path: {path}")
    return pure.as_posix()


async def lock_skill_folder(db: AsyncSession, folder_name: str) -> None:
    """Serialize global/tenant creation decisions for one logical folder."""
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:folder_key, 0))").bindparams(
            folder_key=f"skill-folder:{folder_name}"
        )
    )


def validate_skill_files(files: list[dict[str, str]]) -> None:
    if not files or not any(item["path"] == "SKILL.md" for item in files):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Skill must contain SKILL.md at its root")
    if len(files) > MAX_SKILL_FILES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"Skill exceeds file count limit ({MAX_SKILL_FILES})",
        )

    total_size = 0
    for item in files:
        path = _validate_relative_file_path(item["path"])
        content = item.get("content", "")
        total_size += len(content.encode("utf-8"))
        if "\x00" in content:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Binary content is not supported: {path}")
        if _PRIVATE_KEY_MARKER in content or _SECRET_RE.search(content):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Potential secret detected in {path}")
    if total_size > MAX_SKILL_SIZE:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"Skill exceeds size limit ({MAX_SKILL_SIZE // 1024}KB)",
        )


async def _read_storage_tree(prefix: str) -> list[dict[str, str]]:
    storage = get_storage_backend()
    if not await storage.is_dir(prefix):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Skill folder not found")

    files: list[dict[str, str]] = []

    async def walk(key: str) -> None:
        for entry in await storage.list_dir(key):
            if entry.is_dir:
                await walk(entry.key)
                continue
            rel_path = entry.key.removeprefix(prefix.rstrip("/") + "/")
            rel_path = _validate_relative_file_path(rel_path)
            raw = await storage.read_bytes(entry.key)
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"Skill files must be UTF-8 text: {rel_path}",
                ) from exc
            files.append({"path": rel_path, "content": content})

    await walk(prefix)
    files.sort(key=lambda item: item["path"])
    validate_skill_files(files)
    return files


async def publish_agent_skill(
    db: AsyncSession,
    *,
    agent: Agent,
    actor: User,
    path: str,
    name: str | None,
    description: str | None,
    category: str,
    visibility: str,
) -> Skill:
    """Publish a validated snapshot of an Agent workspace Skill.

    The Agent workspace folder is the source of truth. ``Skill``/``SkillFile``
    rows are only the market copy, so publishing, taking offline, or deleting a
    listing never mutates the source folder.
    """
    if not agent.tenant_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Agent has no tenant")
    if visibility not in {"tenant", "public"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "visibility must be tenant or public")

    folder = _normalize_skill_folder(path)
    prefix = normalize_storage_key(f"{agent.id}/skills/{folder}")
    files = await _read_storage_tree(prefix)
    await lock_skill_folder(db, folder)

    global_conflict = await db.scalar(
        select(Skill.id).where(
            Skill.tenant_id.is_(None),
            Skill.folder_name == folder,
        )
    )
    if global_conflict:
        raise HTTPException(status.HTTP_409_CONFLICT, "A global Skill already uses this folder name")

    result = await db.execute(
        select(Skill)
        .where(Skill.tenant_id == agent.tenant_id, Skill.folder_name == folder)
        .options(selectinload(Skill.files))
    )
    skill = result.scalar_one_or_none()
    if skill:
        can_update = (
            actor.role in {"platform_admin", "org_admin"}
            or skill.publisher_user_id == actor.id
            or skill.publisher_agent_id == agent.id
        )
        if not can_update:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the publisher or an admin can update this Skill")
        for existing_file in list(skill.files):
            await db.delete(existing_file)
        skill.version = (skill.version or 1) + (1 if skill.status in {"published", "offline"} else 0)
    else:
        skill = Skill(
            tenant_id=agent.tenant_id,
            folder_name=folder,
            is_builtin=False,
            is_default=False,
            version=1,
        )
        db.add(skill)

    skill.name = (name or folder.replace("-", " ").title()).strip()[:100]
    skill.description = (description or "").strip()
    skill.category = (category or "general").strip()[:50]
    skill.visibility = visibility
    skill.status = "published"
    skill.publisher_user_id = actor.id
    skill.publisher_agent_id = agent.id
    # A new Skill needs its required metadata before the first INSERT; the flush
    # also gives it an ID before the child file rows are constructed.
    await db.flush()

    for item in files:
        db.add(SkillFile(skill_id=skill.id, path=item["path"], content=item["content"]))
    await db.flush()
    # ``updated_at`` is generated by PostgreSQL on updates. Load it eagerly so
    # REST callers can serialize a republished snapshot without async lazy IO.
    await db.refresh(skill)
    return skill


def _download_count_subquery():
    return (
        select(func.count(SkillInstall.id)).where(SkillInstall.skill_id == Skill.id).correlate(Skill).scalar_subquery()
    )


async def list_market_skills(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID | None,
    query: str = "",
    limit: int = 50,
) -> list[dict]:
    publisher_user = aliased(User)
    publisher_agent = aliased(Agent)
    downloads = _download_count_subquery()
    stmt = (
        select(
            Skill,
            downloads.label("downloads"),
            publisher_user.display_name.label("publisher_user_name"),
            publisher_agent.name.label("publisher_agent_name"),
        )
        .outerjoin(publisher_user, publisher_user.id == Skill.publisher_user_id)
        .outerjoin(publisher_agent, publisher_agent.id == Skill.publisher_agent_id)
        .where(_market_scope(tenant_id))
    )

    normalized_query = query.strip()
    if normalized_query:
        pattern = f"%{normalized_query}%"
        stmt = stmt.where(
            or_(
                Skill.name.ilike(pattern),
                Skill.description.ilike(pattern),
                Skill.category.ilike(pattern),
            )
        ).order_by(
            case((func.lower(Skill.name) == normalized_query.lower(), 0), else_=1),
            downloads.desc(),
            Skill.updated_at.desc(),
        )
    else:
        stmt = stmt.order_by(downloads.desc(), Skill.updated_at.desc())

    rows = (await db.execute(stmt.limit(max(1, min(limit, 100))))).all()
    return [
        serialize_market_skill(
            skill,
            downloads=count,
            publisher_name=publisher_agent_name or publisher_user_name,
        )
        for skill, count, publisher_user_name, publisher_agent_name in rows
    ]


def serialize_market_skill(
    skill: Skill,
    *,
    downloads: int = 0,
    publisher_name: str | None = None,
    skill_md: str | None = None,
    files: list[dict[str, str]] | None = None,
) -> dict:
    data = {
        "id": str(skill.id),
        "name": skill.name,
        "description": skill.description,
        "category": skill.category,
        "icon": skill.icon,
        "folder_name": skill.folder_name,
        "visibility": skill.visibility,
        "status": skill.status,
        "version": skill.version,
        "downloads": int(downloads or 0),
        "is_builtin": skill.is_builtin,
        "publisher_name": publisher_name or ("Platform" if skill.is_builtin else "Unknown"),
        "publisher_user_id": str(skill.publisher_user_id) if skill.publisher_user_id else None,
        "publisher_agent_id": str(skill.publisher_agent_id) if skill.publisher_agent_id else None,
        "updated_at": skill.updated_at.isoformat() if skill.updated_at else None,
    }
    if skill_md is not None:
        data["skill_md"] = skill_md
    if files is not None:
        data["files"] = files
    return data


async def get_visible_market_skill(
    db: AsyncSession,
    *,
    skill_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    with_files: bool = False,
) -> Skill:
    stmt = select(Skill).where(Skill.id == skill_id, _market_scope(tenant_id))
    if with_files:
        stmt = stmt.options(selectinload(Skill.files))
    skill = (await db.execute(stmt)).scalar_one_or_none()
    if not skill:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Skill not found")
    return skill


async def get_market_skill_detail(
    db: AsyncSession,
    *,
    skill_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    viewer_user_id: uuid.UUID | None = None,
) -> dict:
    # Offline copies disappear from the market immediately, while their owner
    # may still preview them under "My publications" before relisting/deleting.
    visibility = _market_scope(tenant_id)
    if viewer_user_id:
        visibility = or_(
            visibility,
            and_(
                Skill.status == "offline",
                Skill.publisher_user_id == viewer_user_id,
                Skill.tenant_id == tenant_id,
            ),
        )
    skill = (
        await db.execute(
            select(Skill)
            .where(Skill.id == skill_id, visibility)
            .options(selectinload(Skill.files))
        )
    ).scalar_one_or_none()
    if not skill:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Skill not found")
    downloads = await db.scalar(select(func.count(SkillInstall.id)).where(SkillInstall.skill_id == skill.id))
    publisher_name = None
    if skill.publisher_agent_id:
        publisher_name = await db.scalar(select(Agent.name).where(Agent.id == skill.publisher_agent_id))
    if not publisher_name and skill.publisher_user_id:
        publisher_name = await db.scalar(select(User.display_name).where(User.id == skill.publisher_user_id))
    files = [{"path": item.path, "content": item.content} for item in sorted(skill.files, key=lambda item: item.path)]
    skill_md = next((item["content"] for item in files if item["path"] == "SKILL.md"), "")
    return serialize_market_skill(
        skill,
        downloads=int(downloads or 0),
        publisher_name=publisher_name,
        skill_md=skill_md,
        files=files,
    )


async def _snapshot_storage_tree(prefix: str) -> dict[str, bytes]:
    storage = get_storage_backend()
    snapshot: dict[str, bytes] = {}
    if not await storage.is_dir(prefix):
        return snapshot

    async def walk(key: str) -> None:
        for entry in await storage.list_dir(key):
            if entry.is_dir:
                await walk(entry.key)
            else:
                rel_path = entry.key.removeprefix(prefix.rstrip("/") + "/")
                snapshot[_validate_relative_file_path(rel_path)] = await storage.read_bytes(entry.key)

    await walk(prefix)
    return snapshot


async def _replace_storage_tree(prefix: str, files: list[dict[str, str]]) -> None:
    storage = get_storage_backend()
    previous = await _snapshot_storage_tree(prefix)
    try:
        if await storage.is_dir(prefix):
            await storage.delete_tree(prefix)
        for item in files:
            key = normalize_storage_key(f"{prefix}/{_validate_relative_file_path(item['path'])}")
            await storage.write_text(key, item["content"], encoding="utf-8")
    except Exception:
        if await storage.is_dir(prefix):
            await storage.delete_tree(prefix)
        for rel_path, content in previous.items():
            await storage.write_bytes(normalize_storage_key(f"{prefix}/{rel_path}"), content)
        raise


async def _restore_storage_tree(prefix: str, snapshot: dict[str, bytes]) -> None:
    storage = get_storage_backend()
    if await storage.is_dir(prefix):
        await storage.delete_tree(prefix)
    for rel_path, content in snapshot.items():
        await storage.write_bytes(normalize_storage_key(f"{prefix}/{rel_path}"), content)


@serialize_workspace_write
async def install_market_skill(
    db: AsyncSession,
    *,
    agent: Agent,
    skill_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
    actor_agent_id: uuid.UUID | None = None,
) -> dict:
    if not agent.tenant_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Agent has no tenant")
    skill = await get_visible_market_skill(
        db,
        skill_id=skill_id,
        tenant_id=agent.tenant_id,
        with_files=True,
    )
    files = [{"path": item.path, "content": item.content} for item in skill.files]
    validate_skill_files(files)

    # Serialize every writer targeting the same Agent folder. Different public
    # Skills may share a tenant-scoped folder name, but they must never race on
    # the same physical directory.
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:install_key, 0))"),
        {"install_key": f"{agent.id}:{skill.folder_name}"},
    )
    existing_install = await db.scalar(
        select(SkillInstall).where(SkillInstall.skill_id == skill.id, SkillInstall.agent_id == agent.id)
    )
    occupied_install = await db.scalar(
        select(SkillInstall)
        .join(Skill, Skill.id == SkillInstall.skill_id)
        .where(
            SkillInstall.agent_id == agent.id,
            SkillInstall.skill_id != skill.id,
            SkillInstall.is_active.is_(True),
            Skill.folder_name == skill.folder_name,
        )
    )
    if occupied_install:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Another installed Skill already owns folder '{skill.folder_name}'",
        )
    target_prefix = normalize_storage_key(f"{agent.id}/skills/{skill.folder_name}")
    if await get_storage_backend().is_dir(target_prefix) and not (existing_install and existing_install.is_active):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Agent already has a local Skill folder named '{skill.folder_name}'",
        )

    previous = await _snapshot_storage_tree(target_prefix)
    try:
        await _replace_storage_tree(target_prefix, files)

        values = {
            "id": uuid.uuid4(),
            "tenant_id": agent.tenant_id,
            "skill_id": skill.id,
            "agent_id": agent.id,
            "installed_version": skill.version,
            "installed_by_user_id": actor_user_id,
            "installed_by_agent_id": actor_agent_id,
            "is_active": True,
        }
        stmt = pg_insert(SkillInstall).values(**values)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_skill_installs_skill_agent",
            set_={
                "installed_version": skill.version,
                "installed_by_user_id": actor_user_id,
                "installed_by_agent_id": actor_agent_id,
                "is_active": True,
                "updated_at": func.now(),
            },
        )
        await db.execute(stmt)
        await db.flush()
        downloads = await db.scalar(select(func.count(SkillInstall.id)).where(SkillInstall.skill_id == skill.id))
        await db.commit()
    except Exception:
        await db.rollback()
        await _restore_storage_tree(target_prefix, previous)
        raise

    return {
        "status": "ok",
        "skill_id": str(skill.id),
        "skill_name": skill.name,
        "folder_name": skill.folder_name,
        "installed_version": skill.version,
        "files_written": len(files),
        "downloads": int(downloads or 0),
    }


@serialize_workspace_write
async def uninstall_market_skill(
    db: AsyncSession,
    *,
    agent: Agent,
    skill_id: uuid.UUID,
) -> dict:
    skill = await db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Skill not found")
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:install_key, 0))"),
        {"install_key": f"{agent.id}:{skill.folder_name}"},
    )
    install = await db.scalar(
        select(SkillInstall).where(
            SkillInstall.skill_id == skill_id,
            SkillInstall.agent_id == agent.id,
        )
    )
    if not install:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Skill is not installed")

    prefix = normalize_storage_key(f"{agent.id}/skills/{skill.folder_name}")
    previous = await _snapshot_storage_tree(prefix)
    try:
        storage = get_storage_backend()
        if await storage.is_dir(prefix):
            await storage.delete_tree(prefix)
        install.is_active = False
        await db.flush()
        await db.commit()
    except Exception:
        await db.rollback()
        await _restore_storage_tree(prefix, previous)
        raise
    return {"status": "ok", "skill_id": str(skill_id)}


async def list_my_published_skills(db: AsyncSession, *, user: User) -> list[dict]:
    downloads = _download_count_subquery()
    rows = (
        await db.execute(
            select(Skill, downloads.label("downloads"))
            .where(Skill.publisher_user_id == user.id)
            .order_by(Skill.updated_at.desc())
        )
    ).all()
    return [serialize_market_skill(skill, downloads=count, publisher_name=user.display_name) for skill, count in rows]


async def take_skill_offline(db: AsyncSession, *, skill_id: uuid.UUID, actor: User) -> Skill:
    skill = await db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Skill not found")
    await lock_skill_folder(db, skill.folder_name)
    await db.refresh(skill, attribute_names=["status", "folder_name", "tenant_id", "publisher_user_id"])
    if actor.role != "platform_admin" and skill.tenant_id != actor.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Skill not found")
    if actor.role not in {"platform_admin", "org_admin"} and skill.publisher_user_id != actor.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the publisher or an admin can take this Skill offline")
    skill.status = "offline"
    await db.flush()
    # PostgreSQL updates ``updated_at`` server-side. Refresh before returning
    # so synchronous response serialization never attempts async lazy IO.
    await db.refresh(skill)
    return skill


async def relist_market_skill(db: AsyncSession, *, skill_id: uuid.UUID, actor: User) -> Skill:
    """Refresh an offline market copy from its source Agent and publish it again."""
    skill = await db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Skill not found")
    await lock_skill_folder(db, skill.folder_name)
    await db.refresh(
        skill,
        attribute_names=[
            "status",
            "folder_name",
            "tenant_id",
            "publisher_user_id",
            "publisher_agent_id",
            "is_builtin",
            "name",
            "description",
            "category",
            "visibility",
        ],
    )
    if skill.is_builtin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Builtin Skills cannot be relisted here")
    if actor.role != "platform_admin" and skill.tenant_id != actor.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Skill not found")
    if actor.role not in {"platform_admin", "org_admin"} and skill.publisher_user_id != actor.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the publisher or an admin can relist this Skill")
    if skill.status != "offline":
        raise HTTPException(status.HTTP_409_CONFLICT, "Only an offline Skill can be relisted")
    if not skill.publisher_agent_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "This Skill has no source Agent to relist from")

    source_agent = await db.get(Agent, skill.publisher_agent_id)
    if not source_agent or source_agent.tenant_id != skill.tenant_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "The source Agent is no longer available")

    return await publish_agent_skill(
        db,
        agent=source_agent,
        actor=actor,
        path=f"skills/{skill.folder_name}",
        name=skill.name,
        description=skill.description,
        category=skill.category,
        visibility=skill.visibility,
    )


async def delete_offline_market_skill(
    db: AsyncSession,
    *,
    skill_id: uuid.UUID,
    actor: User,
) -> dict:
    """Permanently remove an offline market record, not installed copies."""
    skill = await db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Skill not found")
    await lock_skill_folder(db, skill.folder_name)
    await db.refresh(
        skill,
        attribute_names=[
            "status",
            "folder_name",
            "tenant_id",
            "publisher_user_id",
            "is_builtin",
        ],
    )
    if skill.is_builtin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Builtin Skills cannot be deleted from the market")
    if actor.role != "platform_admin" and skill.tenant_id != actor.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Skill not found")
    if actor.role not in {"platform_admin", "org_admin"} and skill.publisher_user_id != actor.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the publisher or an admin can delete this Skill")
    if skill.status != "offline":
        raise HTTPException(status.HTTP_409_CONFLICT, "Only an offline Skill can be deleted")

    deleted_id = str(skill.id)
    await db.delete(skill)
    await db.flush()
    return {"status": "ok", "skill_id": deleted_id}


async def withdraw_agent_skill(
    db: AsyncSession,
    *,
    skill_id: uuid.UUID,
    agent: Agent,
) -> Skill:
    """Withdraw a market Skill only when it was published by this Agent."""
    skill = await db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Skill not found")
    await lock_skill_folder(db, skill.folder_name)
    await db.refresh(
        skill,
        attribute_names=["status", "folder_name", "tenant_id", "publisher_agent_id"],
    )
    if skill.tenant_id != agent.tenant_id or skill.publisher_agent_id != agent.id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "An Agent can only withdraw a Skill it published",
        )
    if skill.status != "published":
        raise HTTPException(status.HTTP_409_CONFLICT, "Skill is not currently published")
    skill.status = "offline"
    await db.flush()
    await db.refresh(skill)
    return skill
