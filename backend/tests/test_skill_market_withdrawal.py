"""Skill-market withdrawal, validation, and provisioning tests."""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.database import async_session
from app.models.agent import Agent
from app.models.audit import ApprovalRequest
from app.models.skill import Skill, SkillFile, SkillInstall
from app.models.user import User
from app.services.agent_provisioning import validate_requested_skill_ids
from app.services.agent_tools import execute_tool
from app.services.autonomy_service import autonomy_service
from app.services.skill_market import (
    delete_offline_market_skill,
    get_market_skill_detail,
    list_market_skills,
    publish_agent_skill,
    relist_market_skill,
    serialize_market_skill,
    take_skill_offline,
    validate_skill_files,
    withdraw_agent_skill,
)
from app.services.storage import get_storage_backend, normalize_storage_key
from tests.test_skill_market import _create_tenant_team, _write_skill


@pytest.mark.asyncio(loop_scope="session")
async def test_user_and_agent_can_withdraw_with_agent_l3_and_ownership_boundaries():
    tenant, owner, guest, agent = await _create_tenant_team("withdraw")
    folder = f"withdraw-skill-{uuid.uuid4().hex[:8]}"
    await _write_skill(agent.id, folder, "Withdrawal behavior")

    async with async_session() as db:
        skill = await publish_agent_skill(
            db,
            agent=await db.get(Agent, agent.id),
            actor=await db.get(User, owner.id),
            path=f"skills/{folder}",
            name="Withdrawal Skill",
            description="Withdrawal behavior",
            category="general",
            visibility="tenant",
        )
        skill_id = skill.id
        db.add(
            SkillInstall(
                tenant_id=tenant.id,
                skill_id=skill_id,
                agent_id=agent.id,
                installed_version=1,
                installed_by_user_id=owner.id,
                is_active=True,
            )
        )
        await db.commit()

    async with async_session() as db:
        with pytest.raises(HTTPException) as denied:
            await take_skill_offline(
                db,
                skill_id=skill_id,
                actor=await db.get(User, guest.id),
            )
        assert denied.value.status_code == 403
        await db.rollback()

    session_id = f"withdraw-session-{uuid.uuid4()}"
    tool_call_id = f"withdraw-call-{uuid.uuid4()}"
    pending = await execute_tool(
        "withdraw_skill_from_market",
        {"skill_id": str(skill_id)},
        agent.id,
        owner.id,
        session_id=session_id,
        tool_call_id=tool_call_id,
        skip_autonomy=True,
    )
    assert "requires approval" in pending
    async with async_session() as db:
        assert (await db.get(Skill, skill_id)).status == "published"
        approval = await db.scalar(
            select(ApprovalRequest).where(
                ApprovalRequest.agent_id == agent.id,
                ApprovalRequest.action_type == "withdraw_skill_from_market",
                ApprovalRequest.status == "pending",
            )
        )
        assert approval is not None
        await autonomy_service.resolve_approval(
            db,
            approval.id,
            await db.get(User, owner.id),
            "approve",
        )
        await db.commit()

    async with async_session() as db:
        assert (await db.get(Skill, skill_id)).status == "offline"
        assert all(
            item["id"] != str(skill_id)
            for item in await list_market_skills(db, tenant_id=tenant.id)
        )
        owner_detail = await get_market_skill_detail(
            db,
            skill_id=skill_id,
            tenant_id=tenant.id,
            viewer_user_id=owner.id,
        )
        assert owner_detail["status"] == "offline"
        with pytest.raises(HTTPException) as hidden_from_other_user:
            await get_market_skill_detail(
                db,
                skill_id=skill_id,
                tenant_id=tenant.id,
                viewer_user_id=guest.id,
            )
        assert hidden_from_other_user.value.status_code == 404

    replay = await execute_tool(
        "withdraw_skill_from_market",
        {"skill_id": str(skill_id)},
        agent.id,
        owner.id,
        session_id=session_id,
        tool_call_id=tool_call_id,
    )
    assert "already been executed" in replay

    # Relisting preserves the independently managed catalog contents.
    await _write_skill(agent.id, folder, "Withdrawal behavior v2")
    async with async_session() as db:
        republished = await relist_market_skill(
            db,
            skill_id=skill_id,
            actor=await db.get(User, owner.id),
        )
        assert republished.status == "published"
        assert republished.version == 1
        assert serialize_market_skill(republished)["updated_at"] is not None
        await db.commit()

    async with async_session() as db:
        refreshed_detail = await get_market_skill_detail(
            db,
            skill_id=skill_id,
            tenant_id=tenant.id,
        )
        assert "Withdrawal behavior v2" not in refreshed_detail["skill_md"]
        assert "Withdrawal behavior" in refreshed_detail["skill_md"]
        intruder = Agent(
            tenant_id=tenant.id,
            creator_id=owner.id,
            name=f"Withdrawal Intruder {uuid.uuid4().hex[:8]}",
            role_description="Cannot withdraw another Agent's publication",
            status="idle",
            access_mode="private",
            company_access_level="use",
        )
        db.add(intruder)
        await db.flush()
        with pytest.raises(HTTPException) as wrong_agent:
            await withdraw_agent_skill(db, skill_id=skill_id, agent=intruder)
        assert wrong_agent.value.status_code == 403
        await db.rollback()

    async with async_session() as db:
        with pytest.raises(HTTPException) as still_published:
            await delete_offline_market_skill(
                db,
                skill_id=skill_id,
                actor=await db.get(User, owner.id),
            )
        assert still_published.value.status_code == 409
        await db.rollback()

    async with async_session() as db:
        withdrawn = await take_skill_offline(
            db,
            skill_id=skill_id,
            actor=await db.get(User, owner.id),
        )
        assert withdrawn.status == "offline"
        assert serialize_market_skill(withdrawn)["status"] == "offline"
        await db.commit()

    async with async_session() as db:
        with pytest.raises(HTTPException) as wrong_user:
            await delete_offline_market_skill(
                db,
                skill_id=skill_id,
                actor=await db.get(User, guest.id),
            )
        assert wrong_user.value.status_code == 403
        await db.rollback()

    async with async_session() as db:
        deleted = await delete_offline_market_skill(
            db,
            skill_id=skill_id,
            actor=await db.get(User, owner.id),
        )
        assert deleted == {"status": "ok", "skill_id": str(skill_id)}
        await db.commit()
        assert await db.get(Skill, skill_id) is None
        assert await db.scalar(select(func.count(SkillFile.id)).where(SkillFile.skill_id == skill_id)) == 0
        assert await db.scalar(select(func.count(SkillInstall.id)).where(SkillInstall.skill_id == skill_id)) == 0

    source_manifest = normalize_storage_key(f"{agent.id}/skills/{folder}/SKILL.md")
    assert await get_storage_backend().is_file(source_manifest)
    assert b"Withdrawal behavior v2" in await get_storage_backend().read_bytes(source_manifest)


def test_skill_validation_rejects_missing_manifest_and_secret_content():
    with pytest.raises(HTTPException) as missing_manifest:
        validate_skill_files([{"path": "README.md", "content": "hello"}])
    assert missing_manifest.value.status_code == 400

    with pytest.raises(HTTPException) as secret:
        validate_skill_files(
            [
                {
                    "path": "SKILL.md",
                    "content": "api_key = 'abcdefghijklmnop123456'",
                }
            ]
        )
    assert secret.value.status_code == 400


def test_builtin_market_publisher_uses_platform_label():
    skill = Skill(
        name="Builtin Market Skill",
        folder_name="builtin-market-skill",
        status="published",
        visibility="public",
        is_builtin=True,
    )

    result = serialize_market_skill(skill)

    assert result["is_builtin"] is True
    assert result["publisher_name"] == "Platform"


@pytest.mark.asyncio(loop_scope="session")
async def test_agent_creation_skill_ids_are_scoped_and_market_skills_are_rejected():
    tenant_a, _owner_a, _guest_a, _agent_a = await _create_tenant_team("provision-scope-a")
    tenant_b, _owner_b, _guest_b, _agent_b = await _create_tenant_team("provision-scope-b")
    suffix = uuid.uuid4().hex[:8]
    async with async_session() as db:
        own_draft = Skill(
            tenant_id=tenant_a.id,
            name="Own Draft",
            folder_name=f"own-draft-{suffix}",
            status="draft",
            visibility="tenant",
        )
        other_draft = Skill(
            tenant_id=tenant_b.id,
            name="Other Draft",
            folder_name=f"other-draft-{suffix}",
            status="draft",
            visibility="tenant",
        )
        public_market = Skill(
            tenant_id=tenant_b.id,
            name="Public Market",
            folder_name=f"public-market-{suffix}",
            status="published",
            visibility="public",
        )
        global_builtin = Skill(
            tenant_id=None,
            name="Global Builtin",
            folder_name=f"global-builtin-{suffix}",
            status="published",
            visibility="public",
            is_builtin=True,
        )
        db.add_all([own_draft, other_draft, public_market, global_builtin])
        await db.commit()

        allowed = await validate_requested_skill_ids(
            db,
            tenant_id=tenant_a.id,
            skill_ids=[own_draft.id, global_builtin.id],
        )
        assert allowed == {own_draft.id, global_builtin.id}
        with pytest.raises(ValueError, match="unavailable"):
            await validate_requested_skill_ids(
                db,
                tenant_id=tenant_a.id,
                skill_ids=[other_draft.id],
            )
        with pytest.raises(ValueError, match="Skill market"):
            await validate_requested_skill_ids(
                db,
                tenant_id=tenant_a.id,
                skill_ids=[public_market.id],
            )
