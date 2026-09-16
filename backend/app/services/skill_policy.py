"""Shared catalog ownership and tenant visibility rules."""

from fastapi import HTTPException
from sqlalchemy import exists, or_, select

from app.models.skill import Skill, SkillTenantPolicy


def is_platform_admin(actor):
    identity = vars(actor).get("identity")
    return actor.role == "platform_admin" or bool(getattr(identity, "is_platform_admin", False))


def can_manage_skill(skill, actor):
    if is_platform_admin(actor):
        return True
    return skill.tenant_id is not None and skill.tenant_id == actor.tenant_id and (
        actor.role == "org_admin" or skill.publisher_user_id == actor.id
    )


def require_skill_manager(skill, actor):
    if not can_manage_skill(skill, actor):
        raise HTTPException(403, "Skill management access required")


def tenant_skill_visible(tenant_id):
    return ~exists(select(SkillTenantPolicy.skill_id).where(
        SkillTenantPolicy.skill_id == Skill.id,
        SkillTenantPolicy.tenant_id == tenant_id,
        SkillTenantPolicy.hidden.is_(True),
    ))


def managed_catalog_scope(actor):
    if is_platform_admin(actor):
        return True
    if actor.role == "org_admin" and actor.tenant_id:
        return or_(Skill.tenant_id.is_(None), Skill.tenant_id == actor.tenant_id)
    return (Skill.publisher_user_id == actor.id) & (Skill.tenant_id == actor.tenant_id)
