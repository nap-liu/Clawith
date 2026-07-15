"""Pure contract tests for canonical channel and relationship identities."""

from pathlib import Path
import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import Text, UniqueConstraint

from app.api.relationships import AgentRelationshipIn, RelationshipIn
from app.models.identity import IdentityProvider
from app.models.org import (
    AgentAgentRelationship,
    AgentRelationship,
    ChannelUserBinding,
    RelationshipSuppression,
)
from app.services.channel_user_service import ChannelUserService


def _unique_columns(model: type) -> set[tuple[str, ...]]:
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in model.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }


def test_channel_binding_uses_full_scoped_subject_contract():
    service = ChannelUserService()
    tenant_id = uuid.uuid4()
    provider_a = IdentityProvider(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        provider_type="feishu",
        name="Feishu A",
        config={},
    )
    provider_b = IdentityProvider(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        provider_type="feishu",
        name="Feishu B",
        config={"installation_scope": "installation:b"},
    )
    long_open_id = "ou_" + "0123456789abcdef" * 20

    assert service._binding_subjects(
        provider_a,
        "feishu",
        None,
        {"open_id": long_open_id},
    ) == [("open_id", long_open_id)]
    assert service._installation_scope(provider_a) == f"provider:{provider_a.id}"
    assert service._installation_scope(provider_b) == "installation:b"

    assert isinstance(ChannelUserBinding.__table__.c.subject.type, Text)
    assert isinstance(AgentRelationship.metadata.tables["org_members"].c.open_id.type, Text)
    assert isinstance(AgentRelationship.metadata.tables["org_members"].c.unionid.type, Text)
    assert isinstance(AgentRelationship.metadata.tables["org_members"].c.external_id.type, Text)
    assert (
        "tenant_id",
        "installation_scope",
        "id_type",
        "subject",
    ) in _unique_columns(ChannelUserBinding)


@pytest.mark.asyncio
async def test_lazy_channel_user_is_external_only_without_login_identity():
    class FakeSession:
        def __init__(self):
            self.added = []

        def add(self, value):
            self.added.append(value)

        async def flush(self):
            for value in self.added:
                if value.id is None:
                    value.id = uuid.uuid4()

    session = FakeSession()
    tenant_id = uuid.uuid4()
    user = await ChannelUserService()._create_channel_user(
        session,
        "feishu",
        None,
        {"open_id": "ou_external_only", "name": "Channel Person"},
        tenant_id,
    )

    assert user.identity_id is None
    assert user.identity is None
    assert user.tenant_id == tenant_id
    assert user.display_name == "Channel Person"
    assert user.registration_source == "feishu_channel"


def test_relationship_models_are_unique_by_canonical_target():
    assert ("agent_id", "user_id") in _unique_columns(AgentRelationship)
    assert ("agent_id", "target_agent_id") in _unique_columns(AgentAgentRelationship)
    assert AgentRelationship.__table__.c.user_id.nullable is False
    assert AgentRelationship.__table__.c.member_id.nullable is True
    assert ("agent_id", "target_type", "target_id") in _unique_columns(
        RelationshipSuppression
    )


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


def test_identity_migration_reports_conflicts_and_fails_closed():
    migration = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "071_identity_relationships_v1.py"
    ).read_text()

    assert 'revision: str = "identity_relationships_v1"' in migration
    assert 'down_revision: Union[str, None] = "speech_recognition_configs"' in migration
    assert "identity_relationship_migration_conflicts" in migration
    assert "ambiguous_channel_subject" in migration
    assert "to_jsonb(provider)" not in migration
    assert "to_jsonb(member)" not in migration
    assert "provider.provider_type IN" in migration
    assert "relationship_suppressions" in migration
    assert "enforce_agent_user_relationship_tenant" in migration
    assert "enforce_agent_agent_relationship_tenant" in migration
    assert "'relationship', to_jsonb" in migration
    assert "DELETE FROM agent_relationships WHERE user_id IS NULL" in migration
    assert "ON CONFLICT (tenant_id, installation_scope, id_type, subject) DO NOTHING" in migration
