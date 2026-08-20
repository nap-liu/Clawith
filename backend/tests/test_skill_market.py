import asyncio
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.api.files import ImportSkillBody, import_skill_to_agent
from app.api.skill_market import PublishAgentSkillIn, publish_from_agent
from app.api.skills import SkillUpdateIn, _save_skill_to_db, delete_skill, update_skill
from app.database import async_session
from app.models.agent import Agent
from app.models.audit import ApprovalRequest
from app.models.skill import Skill, SkillInstall
from app.models.tenant import Tenant
from app.models.user import Identity, User
from app.services.agent_provisioning import validate_requested_skill_ids
from app.services.agent_tools import execute_tool
from app.services.autonomy_service import autonomy_service
from app.services.skill_market import (
    get_market_skill_detail,
    install_market_skill,
    list_market_skills,
    publish_agent_skill,
    uninstall_market_skill,
    validate_skill_files,
)
from app.services.storage import get_storage_backend, normalize_storage_key


async def _create_tenant_team(label: str) -> tuple[Tenant, User, User, Agent]:
    suffix = uuid.uuid4().hex[:10]
    async with async_session() as db:
        tenant = Tenant(name=f"Market {label}", slug=f"market-{label}-{suffix}")
        db.add(tenant)
        await db.flush()

        owner_identity = Identity(email=f"owner-{label}-{suffix}@example.test")
        guest_identity = Identity(email=f"guest-{label}-{suffix}@example.test")
        db.add_all([owner_identity, guest_identity])
        await db.flush()
        owner = User(
            tenant_id=tenant.id,
            identity_id=owner_identity.id,
            display_name=f"Owner {label}",
            role="member",
        )
        guest = User(
            tenant_id=tenant.id,
            identity_id=guest_identity.id,
            display_name=f"Guest {label}",
            role="member",
        )
        db.add_all([owner, guest])
        await db.flush()
        agent = Agent(
            tenant_id=tenant.id,
            creator_id=owner.id,
            name=f"Market Agent {label}",
            role_description="Skill market behavior test",
            status="idle",
            access_mode="company",
            company_access_level="use",
            autonomy_policy={
                "install_skill_from_market": "L3",
                "publish_skill_to_market": "L3",
            },
        )
        db.add(agent)
        await db.commit()
        return tenant, owner, guest, agent


async def _write_skill(agent_id: uuid.UUID, folder: str, description: str) -> None:
    storage = get_storage_backend()
    prefix = normalize_storage_key(f"{agent_id}/skills/{folder}")
    await storage.write_text(
        f"{prefix}/SKILL.md",
        f"---\nname: {folder}\ndescription: {description}\n---\n\n# {folder}\n",
    )
    await storage.write_text(f"{prefix}/references/example.md", "# Example\n")


@pytest.mark.asyncio(loop_scope="session")
async def test_market_publish_visibility_permissions_install_and_unique_counts(monkeypatch):
    tenant_a, owner_a, guest_a, agent_a = await _create_tenant_team("a")
    tenant_b, owner_b, _guest_b, agent_b = await _create_tenant_team("b")
    async with async_session() as db:
        target_a = Agent(
            tenant_id=tenant_a.id,
            creator_id=owner_a.id,
            name=f"Market Install Target {uuid.uuid4().hex[:8]}",
            role_description="Skill market install target",
            status="idle",
            access_mode="private",
            company_access_level="use",
        )
        db.add(target_a)
        await db.commit()
        target_a_id = target_a.id
    folder = f"market-analysis-{uuid.uuid4().hex[:8]}"
    await _write_skill(agent_a.id, folder, "Analyze market data")
    storage = get_storage_backend()
    target_a_key = normalize_storage_key(f"{target_a_id}/skills/{folder}/SKILL.md")

    async with async_session() as db:
        source_agent = await db.get(Agent, agent_a.id)
        actor = await db.get(User, owner_a.id)
        skill = await publish_agent_skill(
            db,
            agent=source_agent,
            actor=actor,
            path=f"skills/{folder}",
            name="Market Analysis",
            description="Analyze market data",
            category="research",
            visibility="tenant",
        )
        skill_id = skill.id
        await db.commit()

    async with async_session() as db:
        visible_a = await list_market_skills(db, tenant_id=tenant_a.id, query="market analysis")
        visible_b = await list_market_skills(db, tenant_id=tenant_b.id, query="market analysis")
        assert any(item["id"] == str(skill_id) for item in visible_a)
        assert all(item["id"] != str(skill_id) for item in visible_b)
        detail = await get_market_skill_detail(db, skill_id=skill_id, tenant_id=tenant_a.id)
        assert [item["path"] for item in detail["files"]] == ["SKILL.md", "references/example.md"]
        assert detail["files"][1]["content"] == "# Example\n"

        with pytest.raises(HTTPException) as denied:
            await publish_from_agent(
                agent_id=agent_a.id,
                body=PublishAgentSkillIn(
                    path=f"skills/{folder}",
                    name="Unauthorized update",
                    visibility="tenant",
                ),
                current_user=await db.get(User, guest_a.id),
                db=db,
            )
        assert denied.value.status_code == 403

    async def concurrent_install():
        async with async_session() as db:
            result = await install_market_skill(
                db,
                agent=await db.get(Agent, target_a_id),
                skill_id=skill_id,
                actor_user_id=owner_a.id,
            )
            await db.commit()
            return result

    concurrent_results = await asyncio.gather(concurrent_install(), concurrent_install())
    assert all(result["downloads"] == 1 for result in concurrent_results)

    async with async_session() as db:
        second = await install_market_skill(
            db,
            agent=await db.get(Agent, target_a_id),
            skill_id=skill_id,
            actor_user_id=owner_a.id,
        )
        await db.commit()
        count = await db.scalar(select(func.count(SkillInstall.id)).where(SkillInstall.skill_id == skill_id))
        assert second["downloads"] == 1
        assert count == 1

    await _write_skill(agent_a.id, folder, "Analyze market data v2")
    async with async_session() as db:
        updated = await publish_agent_skill(
            db,
            agent=await db.get(Agent, agent_a.id),
            actor=await db.get(User, owner_a.id),
            path=f"skills/{folder}",
            name="Market Analysis",
            description="Analyze market data",
            category="research",
            visibility="public",
        )
        await db.commit()
        assert updated.version == 2

    # If the database commit fails after the file replacement, the previous
    # installed version is restored and the install row remains unchanged.
    previous_target_a = await storage.read_bytes(target_a_key)
    async with async_session() as db:

        async def fail_commit():
            raise RuntimeError("injected commit failure")

        monkeypatch.setattr(db, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="injected commit failure"):
            await install_market_skill(
                db,
                agent=await db.get(Agent, target_a_id),
                skill_id=skill_id,
                actor_user_id=owner_a.id,
            )
    assert await storage.read_bytes(target_a_key) == previous_target_a
    async with async_session() as db:
        unchanged = await db.scalar(
            select(SkillInstall).where(SkillInstall.skill_id == skill_id, SkillInstall.agent_id == target_a_id)
        )
        assert unchanged is not None and unchanged.installed_version == 1

    async with async_session() as db:
        visible_b = await list_market_skills(db, tenant_id=tenant_b.id, query="market analysis")
        assert any(item["id"] == str(skill_id) for item in visible_b)
        installed_b = await install_market_skill(
            db,
            agent=await db.get(Agent, agent_b.id),
            skill_id=skill_id,
            actor_user_id=owner_b.id,
        )
        await db.commit()
        assert installed_b["downloads"] == 2

    installed_key = normalize_storage_key(f"{agent_b.id}/skills/{folder}/SKILL.md")
    assert await storage.is_file(installed_key)

    # Uninstall owns the same storage/DB compensation boundary.
    installed_snapshot = await storage.read_bytes(installed_key)
    async with async_session() as db:

        async def fail_uninstall_commit():
            raise RuntimeError("injected uninstall commit failure")

        monkeypatch.setattr(db, "commit", fail_uninstall_commit)
        with pytest.raises(RuntimeError, match="injected uninstall commit failure"):
            await uninstall_market_skill(
                db,
                agent=await db.get(Agent, agent_b.id),
                skill_id=skill_id,
            )
    assert await storage.read_bytes(installed_key) == installed_snapshot
    async with async_session() as db:
        active_after_failure = await db.scalar(
            select(SkillInstall).where(SkillInstall.skill_id == skill_id, SkillInstall.agent_id == agent_b.id)
        )
        assert active_after_failure is not None and active_after_failure.is_active is True

    async with async_session() as db:
        await uninstall_market_skill(
            db,
            agent=await db.get(Agent, agent_b.id),
            skill_id=skill_id,
        )
        await db.commit()
        install_row = await db.scalar(
            select(SkillInstall).where(SkillInstall.skill_id == skill_id, SkillInstall.agent_id == agent_b.id)
        )
        assert install_row is not None and install_row.is_active is False
        assert await db.scalar(select(func.count(SkillInstall.id)).where(SkillInstall.skill_id == skill_id)) == 2
    assert not await storage.exists(installed_key)

    # The same folder name is valid in another tenant; the market identity is the Skill UUID.
    await _write_skill(agent_b.id, folder, "Tenant B variant")
    async with async_session() as db:
        tenant_b_skill = await publish_agent_skill(
            db,
            agent=await db.get(Agent, agent_b.id),
            actor=await db.get(User, owner_b.id),
            path=f"skills/{folder}",
            name="Tenant B Market Analysis",
            description="Tenant B variant",
            category="research",
            visibility="public",
        )
        await db.commit()
        assert tenant_b_skill.id != skill_id

    async with async_session() as db:
        with pytest.raises(HTTPException) as occupied:
            await install_market_skill(
                db,
                agent=await db.get(Agent, target_a_id),
                skill_id=tenant_b_skill.id,
                actor_user_id=owner_a.id,
            )
        assert occupied.value.status_code == 409

    # Global folders stay reserved, while tenant folders only conflict inside
    # their own tenant. This keeps the legacy path-based Skill APIs unambiguous.
    global_folder = f"global-reserved-{uuid.uuid4().hex[:8]}"
    async with async_session() as db:
        global_skill = Skill(
            name="Global Reserved Skill",
            folder_name=global_folder,
            tenant_id=None,
            visibility="public",
            status="published",
        )
        db.add(global_skill)
        await db.commit()
    await _write_skill(agent_a.id, global_folder, "Must not shadow global")
    async with async_session() as db:
        with pytest.raises(HTTPException) as conflict:
            await publish_agent_skill(
                db,
                agent=await db.get(Agent, agent_a.id),
                actor=await db.get(User, owner_a.id),
                path=f"skills/{global_folder}",
                name="Tenant Shadow",
                description="Must fail",
                category="general",
                visibility="tenant",
            )
        assert conflict.value.status_code == 409


@pytest.mark.asyncio(loop_scope="session")
async def test_legacy_preset_install_delegates_to_market_accounting():
    tenant, owner, _guest, source_agent = await _create_tenant_team("preset-delegation")
    async with async_session() as db:
        target_agent = Agent(
            tenant_id=tenant.id,
            creator_id=owner.id,
            name=f"Preset Install Target {uuid.uuid4().hex[:8]}",
            role_description="Legacy install delegation target",
            status="idle",
            access_mode="private",
            company_access_level="use",
        )
        db.add(target_agent)
        await db.commit()
        target_agent_id = target_agent.id

    folder = f"preset-market-{uuid.uuid4().hex[:8]}"
    await _write_skill(source_agent.id, folder, "Preset market delegation")
    async with async_session() as db:
        skill = await publish_agent_skill(
            db,
            agent=await db.get(Agent, source_agent.id),
            actor=await db.get(User, owner.id),
            path=f"skills/{folder}",
            name="Preset Delegated Skill",
            description="Preset market delegation",
            category="general",
            visibility="tenant",
        )
        await db.commit()
        skill_id = skill.id

    async with async_session() as db:
        result = await import_skill_to_agent(
            agent_id=target_agent_id,
            body=ImportSkillBody(skill_id=str(skill_id)),
            current_user=await db.get(User, owner.id),
            db=db,
        )
        assert result["downloads"] == 1

    async with async_session() as db:
        install = await db.scalar(
            select(SkillInstall).where(
                SkillInstall.skill_id == skill_id,
                SkillInstall.agent_id == target_agent_id,
            )
        )
        assert install is not None and install.is_active is True
    assert await get_storage_backend().is_file(normalize_storage_key(f"{target_agent_id}/skills/{folder}/SKILL.md"))

    async with async_session() as db:
        market_skill = await db.get(Skill, skill_id)
        market_skill.status = "offline"
        await db.commit()
    async with async_session() as db:
        with pytest.raises(HTTPException) as offline:
            await import_skill_to_agent(
                agent_id=target_agent_id,
                body=ImportSkillBody(skill_id=str(skill_id)),
                current_user=await db.get(User, owner.id),
                db=db,
            )
        assert offline.value.status_code == 404


@pytest.mark.asyncio(loop_scope="session")
async def test_agent_market_install_waits_for_l3_approval_then_executes_once():
    tenant_a, owner_a, _guest_a, agent_a = await _create_tenant_team("approval-source")
    _tenant_b, owner_b, _guest_b, agent_b = await _create_tenant_team("approval-target")
    folder = f"approved-skill-{uuid.uuid4().hex[:8]}"
    await _write_skill(agent_a.id, folder, "Approval behavior")

    async with async_session() as db:
        skill = await publish_agent_skill(
            db,
            agent=await db.get(Agent, agent_a.id),
            actor=await db.get(User, owner_a.id),
            path=f"skills/{folder}",
            name="Approved Skill",
            description="Approval behavior",
            category="general",
            visibility="public",
        )
        skill_id = skill.id
        await db.commit()

    # Agent policy cannot downgrade these two mutations below L3.
    async with async_session() as db:
        target_agent = await db.get(Agent, agent_b.id)
        target_agent.autonomy_policy = {
            "install_skill_from_market": "L1",
            "publish_skill_to_market": "L1",
        }
        await db.commit()

    session_id = f"skill-market-{uuid.uuid4()}"
    tool_call_id = f"call-{uuid.uuid4()}"
    result = await execute_tool(
        "install_skill_from_market",
        {"skill_id": str(skill_id)},
        agent_b.id,
        owner_b.id,
        session_id=session_id,
        tool_call_id=tool_call_id,
        skip_autonomy=True,
    )
    assert "requires approval" in result
    duplicate = await execute_tool(
        "install_skill_from_market",
        {"skill_id": str(skill_id)},
        agent_b.id,
        owner_b.id,
        session_id=session_id,
        tool_call_id=tool_call_id,
    )
    assert "requires approval" in duplicate
    target_key = normalize_storage_key(f"{agent_b.id}/skills/{folder}/SKILL.md")
    assert not await get_storage_backend().exists(target_key)

    async with async_session() as db:
        approval = await db.scalar(
            select(ApprovalRequest)
            .where(
                ApprovalRequest.agent_id == agent_b.id,
                ApprovalRequest.action_type == "install_skill_from_market",
                ApprovalRequest.status == "pending",
            )
            .order_by(ApprovalRequest.created_at.desc())
        )
        assert approval is not None
        assert approval.details["args"] == {"skill_id": str(skill_id)}
        assert approval.details["tool_call_id"] == tool_call_id
        assert (
            await db.scalar(
                select(func.count(ApprovalRequest.id)).where(
                    ApprovalRequest.agent_id == agent_b.id,
                    ApprovalRequest.action_type == "install_skill_from_market",
                )
            )
            == 1
        )
        await autonomy_service.resolve_approval(
            db,
            approval.id,
            await db.get(User, owner_b.id),
            "approve",
        )
        await db.commit()

    assert await get_storage_backend().is_file(target_key)
    replay = await execute_tool(
        "install_skill_from_market",
        {"skill_id": str(skill_id)},
        agent_b.id,
        owner_b.id,
        session_id=session_id,
        tool_call_id=tool_call_id,
    )
    assert "already been executed" in replay
    async with async_session() as db:
        assert (
            await db.scalar(
                select(func.count(SkillInstall.id)).where(
                    SkillInstall.skill_id == skill_id,
                    SkillInstall.agent_id == agent_b.id,
                )
            )
            == 1
        )
        assert (await db.get(Skill, skill_id)).tenant_id == tenant_a.id


@pytest.mark.asyncio(loop_scope="session")
async def test_agent_market_publish_approval_is_idempotent_and_rejection_is_final():
    tenant, owner, _guest, agent = await _create_tenant_team("approval-publish")
    async with async_session() as db:
        source = await db.get(Agent, agent.id)
        source.autonomy_policy = {"publish_skill_to_market": "L1"}
        await db.commit()

    folder = f"publish-approved-{uuid.uuid4().hex[:8]}"
    await _write_skill(agent.id, folder, "Publish approval")
    args = {
        "path": f"skills/{folder}",
        "name": "Approval Published Skill",
        "description": "Publish approval",
        "category": "general",
        "visibility": "tenant",
    }
    session_id = f"publish-session-{uuid.uuid4()}"
    tool_call_id = f"publish-call-{uuid.uuid4()}"
    first, duplicate = await asyncio.gather(
        execute_tool(
            "publish_skill_to_market",
            args,
            agent.id,
            owner.id,
            session_id=session_id,
            tool_call_id=tool_call_id,
        ),
        execute_tool(
            "publish_skill_to_market",
            args,
            agent.id,
            owner.id,
            session_id=session_id,
            tool_call_id=tool_call_id,
        ),
    )
    assert "requires approval" in first
    assert "requires approval" in duplicate

    async with async_session() as db:
        approvals = (
            (
                await db.execute(
                    select(ApprovalRequest).where(
                        ApprovalRequest.agent_id == agent.id,
                        ApprovalRequest.action_type == "publish_skill_to_market",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(approvals) == 1
        approval_id = approvals[0].id
        assert not await db.scalar(select(Skill.id).where(Skill.tenant_id == tenant.id, Skill.folder_name == folder))

    async def resolve_publish():
        async with async_session() as db:
            return await autonomy_service.resolve_approval(
                db,
                approval_id,
                await db.get(User, owner.id),
                "approve",
            )

    resolutions = await asyncio.gather(resolve_publish(), resolve_publish(), return_exceptions=True)
    assert sum(isinstance(result, ApprovalRequest) for result in resolutions) == 1
    assert sum(isinstance(result, ValueError) for result in resolutions) == 1

    replay = await execute_tool(
        "publish_skill_to_market",
        args,
        agent.id,
        owner.id,
        session_id=session_id,
        tool_call_id=tool_call_id,
    )
    assert "already been executed" in replay
    async with async_session() as db:
        published = await db.scalar(select(Skill).where(Skill.tenant_id == tenant.id, Skill.folder_name == folder))
        assert published is not None and published.version == 1

    rejected_folder = f"publish-rejected-{uuid.uuid4().hex[:8]}"
    await _write_skill(agent.id, rejected_folder, "Reject approval")
    rejected_args = {**args, "path": f"skills/{rejected_folder}", "name": "Rejected Skill"}
    rejected_call_id = f"publish-call-{uuid.uuid4()}"
    rejected_result = await execute_tool(
        "publish_skill_to_market",
        rejected_args,
        agent.id,
        owner.id,
        session_id=session_id,
        tool_call_id=rejected_call_id,
    )
    assert "requires approval" in rejected_result
    async with async_session() as db:
        rejected_approval = await db.scalar(
            select(ApprovalRequest)
            .where(
                ApprovalRequest.agent_id == agent.id,
                ApprovalRequest.action_type == "publish_skill_to_market",
                ApprovalRequest.status == "pending",
            )
            .order_by(ApprovalRequest.created_at.desc())
        )
        await autonomy_service.resolve_approval(
            db,
            rejected_approval.id,
            await db.get(User, owner.id),
            "reject",
        )
    rejected_replay = await execute_tool(
        "publish_skill_to_market",
        rejected_args,
        agent.id,
        owner.id,
        session_id=session_id,
        tool_call_id=rejected_call_id,
    )
    assert "was rejected" in rejected_replay
    async with async_session() as db:
        assert not await db.scalar(
            select(Skill.id).where(Skill.tenant_id == tenant.id, Skill.folder_name == rejected_folder)
        )


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


@pytest.mark.asyncio(loop_scope="session")
async def test_legacy_crud_cannot_mutate_or_delete_market_skills():
    tenant, owner, _guest, _agent = await _create_tenant_team("legacy-crud")
    async with async_session() as db:
        actor = await db.get(User, owner.id)
        actor.role = "org_admin"
        builtin = Skill(
            tenant_id=None,
            name="Protected Builtin",
            folder_name=f"protected-builtin-{uuid.uuid4().hex[:8]}",
            status="published",
            visibility="public",
            is_builtin=True,
        )
        db.add(builtin)
        await db.commit()
        builtin_id = builtin.id

    async with async_session() as db:
        actor = await db.get(User, owner.id)
    with pytest.raises(HTTPException) as update_blocked:
        await update_skill(
            str(builtin_id),
            SkillUpdateIn(description="bypass"),
            current_user=actor,
        )
    assert update_blocked.value.status_code == 409
    with pytest.raises(HTTPException) as delete_blocked:
        await delete_skill(str(builtin_id), current_user=actor)
    assert delete_blocked.value.status_code == 409
    async with async_session() as db:
        preserved = await db.get(Skill, builtin_id)
        assert preserved is not None and preserved.description != "bypass"
        assert preserved.tenant_id is None
        assert actor.tenant_id == tenant.id


@pytest.mark.asyncio(loop_scope="session")
async def test_global_and_tenant_folder_creation_is_serialized():
    _tenant, owner, _guest, agent = await _create_tenant_team("folder-lock")
    folder = f"folder-lock-{uuid.uuid4().hex[:8]}"
    await _write_skill(agent.id, folder, "Folder lock")

    async def create_tenant_market_skill():
        async with async_session() as db:
            skill = await publish_agent_skill(
                db,
                agent=await db.get(Agent, agent.id),
                actor=await db.get(User, owner.id),
                path=f"skills/{folder}",
                name="Tenant Folder Lock",
                description="Folder lock",
                category="general",
                visibility="tenant",
            )
            await db.commit()
            return skill.id

    results = await asyncio.gather(
        create_tenant_market_skill(),
        _save_skill_to_db(
            folder,
            "Global Folder Lock",
            "Folder lock",
            "general",
            "--",
            [{"path": "SKILL.md", "content": "# Folder lock\n"}],
            tenant_id=None,
        ),
        return_exceptions=True,
    )
    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, HTTPException) for result in results) == 1
    async with async_session() as db:
        assert await db.scalar(select(func.count(Skill.id)).where(Skill.folder_name == folder)) == 1


def test_market_routes_are_exposed_before_dynamic_skill_route():
    from app.main import app

    paths = list(app.openapi()["paths"])
    assert "/api/skills/market" in paths
    assert "/api/skills/market/{skill_id}" in paths
    assert "/api/skills/mine" in paths
    assert "/api/agents/{agent_id}/skills/install" in paths
    assert paths.index("/api/skills/market") < paths.index("/api/skills/{skill_id}")
