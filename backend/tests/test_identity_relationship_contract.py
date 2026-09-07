"""Canonical identities and relationship constraints on PostgreSQL."""

import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.api.relationships import AgentRelationshipIn, RelationshipIn
from app.database import async_session, engine
from app.models import registry  # noqa: F401
from app.models.agent import Agent
from app.models.identity import IdentityProvider
from app.models.org import (
    AgentAgentRelationship, AgentRelationship, ChannelUserBinding, OrgMember,
    RelationshipSuppression,
)
from app.models.tenant import Tenant
from app.models.user import User
from app.services.channel_user_service import ChannelUserService


@pytest.fixture
async def directory():
    await engine.dispose()
    try:
        async with async_session() as db:
            tenant = Tenant(name="Identity contract", slug=f"identity-{uuid.uuid4().hex}")
            db.add(tenant)
            await db.flush()
            owner = User(tenant_id=tenant.id, display_name="Owner")
            db.add(owner)
            await db.flush()
            yield db, owner
    finally:
        await engine.dispose()


async def test_channel_binding_uses_full_scoped_subject_contract(directory):
    db, owner = directory
    service = ChannelUserService()
    providers = [IdentityProvider(
        tenant_id=owner.tenant_id, provider_type="feishu", name=f"Provider {index}",
        config={} if index == 0 else {"installation_scope": "installation:b"},
    ) for index in range(2)]
    db.add_all(providers)
    await db.flush()
    long_id = "ou_" + "0123456789abcdef" * 20
    assert service._binding_subjects(providers[0], "feishu", None, {"open_id": long_id}) == [
        ("open_id", long_id),
    ]
    assert service._installation_scope(providers[0]) == f"provider:{providers[0].id}"
    assert service._installation_scope(providers[1]) == "installation:b"
    fields = dict(
        tenant_id=owner.tenant_id, user_id=owner.id, provider_id=providers[0].id,
        installation_scope="installation:a", channel_type="feishu", id_type="open_id", subject=long_id,
    )
    for variant in ({}, {"provider_id": None}, {"provider_id": providers[1].id},
                    {"installation_scope": "other"}, {"channel_type": "slack"},
                    {"id_type": "union_id"}, {"subject": long_id + "x"}):
        binding = ChannelUserBinding(**{**fields, **variant})
        db.add(binding)
        await db.flush()
        assert await db.scalar(select(ChannelUserBinding.subject).where(
            ChannelUserBinding.id == binding.id,
        )) == variant.get("subject", long_id)
        with pytest.raises(IntegrityError):
            async with db.begin_nested():
                db.add(ChannelUserBinding(**{**fields, **variant}))
                await db.flush()
    member = OrgMember(
        tenant_id=owner.tenant_id, provider_id=providers[0].id, user_id=owner.id,
        name="Opaque identifiers", open_id=long_id, unionid=long_id, external_id=long_id,
    )
    db.add(member)
    await db.flush()
    assert (await db.execute(select(OrgMember.open_id, OrgMember.unionid, OrgMember.external_id).where(
        OrgMember.id == member.id,
    ))).one() == (long_id, long_id, long_id)


async def test_lazy_channel_user_is_external_only_without_login_identity(directory):
    db, owner = directory
    user = await ChannelUserService()._create_channel_user(
        db, "feishu", None, {"open_id": "ou_external_only", "name": "Channel Person"}, owner.tenant_id,
    )
    user_id, tenant_id = user.id, owner.tenant_id
    db.expire_all()
    persisted = (await db.execute(select(
        User.identity_id, User.tenant_id, User.display_name, User.registration_source,
    ).where(User.id == user_id))).one()
    assert persisted == (None, tenant_id, "Channel Person", "feishu_channel")


async def test_relationship_models_are_unique_by_canonical_target(directory):
    db, owner = directory
    agents = [Agent(tenant_id=owner.tenant_id, creator_id=owner.id, name=f"Agent {i}") for i in range(2)]
    db.add_all(agents)
    await db.flush()
    for model, target in (
        (AgentRelationship, {"user_id": owner.id}),
        (AgentAgentRelationship, {"target_agent_id": agents[1].id}),
        (RelationshipSuppression, {"target_type": "user", "target_id": owner.id}),
    ):
        db.add(model(agent_id=agents[0].id, **target))
        await db.flush()
        with pytest.raises(IntegrityError):
            async with db.begin_nested():
                db.add(model(agent_id=agents[0].id, **target))
                await db.flush()
    with pytest.raises(DBAPIError, match="agent_relationship tenant mismatch"):
        async with db.begin_nested():
            db.add(AgentRelationship(agent_id=agents[0].id, user_id=None))
            await db.flush()


def test_relationship_inputs_reject_legacy_or_ambiguous_ids():
    identifier = uuid.uuid4()
    assert RelationshipIn(user_id=identifier).user_id == identifier
    assert AgentRelationshipIn(agent_id=identifier).agent_id == identifier
    with pytest.raises(ValidationError):
        RelationshipIn.model_validate({"member_id": str(identifier)})
    with pytest.raises(ValidationError):
        RelationshipIn.model_validate({"user_id": f"platform-user:{identifier}"})
    with pytest.raises(ValidationError):
        AgentRelationshipIn.model_validate({"target_agent_id": str(identifier)})
