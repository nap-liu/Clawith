"""Shared data contracts and department-path helpers for organization sync."""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.org import OrgDepartment, OrgMember
from app.services.canonical_user_resolver import normalize_email, normalize_phone

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def build_department_path_map(departments: list[OrgDepartment]) -> dict[uuid.UUID, str]:
    """Build department name paths by walking the internal department tree."""
    dept_by_id = {dept.id: dept for dept in departments}
    paths: dict[uuid.UUID, str] = {}

    def is_virtual_root(dept: OrgDepartment) -> bool:
        return not dept.parent_id and str(getattr(dept, "external_id", "") or "") == "0"

    def compute_path(dept_id: uuid.UUID, visited: set[uuid.UUID] | None = None) -> str:
        if dept_id in paths:
            return paths[dept_id]
        if visited is None:
            visited = set()
        if dept_id in visited:
            dept = dept_by_id.get(dept_id)
            fallback = (dept.name if dept else "") or ""
            paths[dept_id] = fallback
            return fallback

        visited.add(dept_id)
        dept = dept_by_id.get(dept_id)
        if not dept:
            return ""

        if is_virtual_root(dept):
            paths[dept_id] = ""
            return ""

        name = (dept.name or "").strip()
        if not dept.parent_id or dept.parent_id not in dept_by_id:
            paths[dept_id] = name
            return name

        parent_path = compute_path(dept.parent_id, visited)
        full_path = f"{parent_path}/{name}" if parent_path else name
        paths[dept_id] = full_path
        return full_path

    for dept in departments:
        compute_path(dept.id)

    return paths


async def derive_member_department_paths(
    db: AsyncSession,
    members: list[OrgMember],
) -> dict[uuid.UUID, str]:
    """Resolve member department paths from department_id via the department tree."""
    dept_ids = {member.department_id for member in members if member.department_id}
    if not dept_ids:
        return {}

    departments: dict[uuid.UUID, OrgDepartment] = {}
    pending_ids = set(dept_ids)

    while pending_ids:
        result = await db.execute(
            select(OrgDepartment).where(OrgDepartment.id.in_(pending_ids))
        )
        batch = result.scalars().all()
        if not batch:
            break

        next_pending: set[uuid.UUID] = set()
        for department in batch:
            departments[department.id] = department
            if department.parent_id and department.parent_id not in departments:
                next_pending.add(department.parent_id)
        pending_ids = next_pending

    dept_path_map = build_department_path_map(list(departments.values()))

    return {
        member.id: dept_path_map.get(member.department_id, member.department_path or "")
        for member in members
    }


def normalize_contact_for_match(value: str | None) -> str | None:
    """Normalize synced contact identifiers before matching platform users."""
    if value and "@" in value:
        return normalize_email(value)
    return normalize_phone(value)


def _normalize_contact(value: str | None) -> str | None:
    return normalize_contact_for_match(value)


@dataclass
class ExternalDepartment:
    """Standardized department info from external providers."""

    external_id: str
    name: str
    parent_external_id: str | None = None
    member_count: int = 0
    raw_data: dict = field(default_factory=dict)


@dataclass
class ExternalUser:
    """Standardized user info from external providers."""

    external_id: str  # The unique, platform-stable ID (e.g., userid)
    name: str
    nickname: str = ""
    open_id: str = ""  # OAuth open_id
    unionid: str = ""  # Union ID for cross-app identification
    email: str = ""
    avatar_url: str = ""
    title: str = ""
    department_external_id: str = ""
    department_path: str = ""
    department_ids: list[str] = field(default_factory=list)  # List of dept IDs from provider
    mobile: str = ""
    status: str = "active"
    raw_data: dict = field(default_factory=dict)



