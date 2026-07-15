"""Canonical recipient resolution for Agent-visible delivery operations.

Public callers address natural people only by tenant ``User.id`` and digital
employees only by ``Agent.id``.  Provider/member identifiers remain an
implementation detail returned by this module after tenant, relationship and
effective-status checks have succeeded.

Relationships are authorized by canonical User/Agent IDs. ``OrgMember`` is
consulted only after authorization to resolve a provider-specific delivery
route; provider identifiers never enter the public contract.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import (
    evaluate_agent_relationship_status,
    evaluate_human_relationship_status,
)
from app.models.agent import Agent
from app.models.channel_config import ChannelConfig
from app.models.identity import IdentityProvider
from app.models.org import (
    AgentAgentRelationship,
    AgentRelationship,
    ChannelUserBinding,
    OrgMember,
    RelationshipSuppression,
)
from app.models.user import User
from app.services.channel_user_service import channel_user_service


MESSAGE_OUTBOUND_CHANNELS = frozenset(
    {"feishu", "dingtalk", "wecom", "slack", "teams", "wechat"}
)


def _normalize_channel(value: str | None) -> str | None:
    channel = (value or "").strip().lower()
    if not channel:
        return None
    return "teams" if channel == "microsoft_teams" else channel


class RecipientResolutionError(ValueError):
    """Truthful, machine-readable recipient resolution failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        available_channels: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.available_channels = sorted(set(available_channels or []))

    def as_dict(self) -> dict:
        payload = {"status": "error", "code": self.code, "message": self.message}
        if self.available_channels:
            payload["available_channels"] = self.available_channels
        return payload

    def as_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False)


def parse_canonical_id(value: object, field_name: str) -> uuid.UUID:
    """Parse a complete UUID without accepting names or provider identifiers."""

    if isinstance(value, uuid.UUID):
        return value
    raw = str(value or "").strip()
    try:
        return uuid.UUID(raw)
    except (TypeError, ValueError) as exc:
        raise RecipientResolutionError(
            "invalid_canonical_id",
            f"{field_name} must be a complete platform UUID",
        ) from exc


@dataclass(frozen=True)
class ResolvedAgentRecipient:
    source_agent: Agent
    target_agent: Agent
    relationship: AgentAgentRelationship


@dataclass(frozen=True)
class ResolvedHumanRecipient:
    source_agent: Agent
    user: User
    relationship: AgentRelationship
    member: OrgMember | None


@dataclass(frozen=True)
class ResolvedHumanRoute(ResolvedHumanRecipient):
    channel: str


@dataclass(frozen=True)
class HumanRecipientProfile:
    """Canonical relationship discovery data with executable route choices."""

    user: User
    member: OrgMember | None
    channels: tuple[str, ...]
    provider_names: tuple[str, ...]
    access_status: str
    access_status_reason: str | None


async def _load_source_agent(db: AsyncSession, source_agent_id: uuid.UUID) -> Agent:
    result = await db.execute(
        select(Agent).where(Agent.id == source_agent_id, Agent.is_deleted.is_(False))
    )
    source = result.scalar_one_or_none()
    if not source:
        raise RecipientResolutionError("source_agent_not_found", "Source agent not found")
    return source


async def resolve_agent_recipient(
    db: AsyncSession,
    source_agent_id: uuid.UUID,
    target_agent_id: object,
) -> ResolvedAgentRecipient:
    """Resolve one exact, same-tenant, active A2A relationship by Agent.id."""

    target_id = parse_canonical_id(target_agent_id, "agent_id")
    source = await _load_source_agent(db, source_agent_id)
    target_result = await db.execute(
        select(Agent).where(
            Agent.id == target_id,
            Agent.tenant_id == source.tenant_id,
            Agent.is_deleted.is_(False),
        )
    )
    targets = target_result.scalars().all()
    if len(targets) != 1:
        code = "recipient_not_found" if not targets else "ambiguous_recipient"
        raise RecipientResolutionError(
            code,
            "agent_id does not identify exactly one active digital employee in this tenant",
        )
    target = targets[0]
    relationship_result = await db.execute(
        select(AgentAgentRelationship).where(
            AgentAgentRelationship.agent_id == source.id,
            AgentAgentRelationship.target_agent_id == target.id,
        )
    )
    relationship = relationship_result.scalar_one_or_none()
    if relationship is None:
        raise RecipientResolutionError(
            "recipient_not_related",
            "agent_id does not identify an active related digital employee",
        )
    status = await evaluate_agent_relationship_status(db, relationship)
    if not status.get("access_allowed") or status.get("access_status") != "active":
        raise RecipientResolutionError(
            "relationship_inactive",
            status.get("access_status_reason")
            or "Digital employee relationship is inactive",
        )
    return ResolvedAgentRecipient(source, target, relationship)


async def _active_human_rows(
    db: AsyncSession,
    source: Agent,
    user_id: uuid.UUID,
) -> tuple[User, AgentRelationship]:
    user_result = await db.execute(
        select(User).where(
            User.id == user_id,
            User.tenant_id == source.tenant_id,
            User.is_active.is_(True),
        )
    )
    user = user_result.scalar_one_or_none()
    if not user:
        raise RecipientResolutionError(
            "recipient_not_found",
            "user_id does not identify an active user in the source agent tenant",
        )

    result = await db.execute(
        select(AgentRelationship).where(
            AgentRelationship.agent_id == source.id,
            AgentRelationship.user_id == user.id,
        )
    )
    relationships = result.scalars().all()
    if len(relationships) != 1:
        code = "recipient_not_related" if not relationships else "ambiguous_relationship"
        raise RecipientResolutionError(
            code,
            "user_id does not identify exactly one relationship",
        )
    relationship = relationships[0]
    status = await evaluate_human_relationship_status(
        db, relationship, source_agent=source
    )
    if status["access_status"] != "active":
        raise RecipientResolutionError(
            "relationship_inactive",
            status.get("access_status_reason") or "Human relationship is inactive",
        )
    return user, relationship


async def resolve_platform_user_recipient(
    db: AsyncSession,
    source_agent_id: uuid.UUID,
    user_id: object,
) -> ResolvedHumanRecipient:
    """Resolve an exact canonical user for first-party platform delivery."""

    canonical_id = parse_canonical_id(user_id, "user_id")
    source = await _load_source_agent(db, source_agent_id)
    user, relationship = await _active_human_rows(db, source, canonical_id)
    if user.identity_id is None:
        raise RecipientResolutionError(
            "platform_recipient_unreachable",
            "user_id is an external-channel identity and cannot receive web platform messages",
        )
    member = (
        await db.get(OrgMember, relationship.member_id)
        if relationship.member_id
        else None
    )
    return ResolvedHumanRecipient(source, user, relationship, member)


async def resolve_human_channel_recipient(
    db: AsyncSession,
    source_agent_id: uuid.UUID,
    user_id: object,
    *,
    channel: str | None = None,
) -> ResolvedHumanRoute:
    """Resolve exactly one internal provider route for a canonical user.

    When more than one channel is valid the platform returns the choices and
    leaves the business decision to the Agent.  It never applies a first-row or
    name-based fallback.
    """

    canonical_id = parse_canonical_id(user_id, "user_id")
    requested_channel = _normalize_channel(channel)
    if requested_channel and requested_channel not in MESSAGE_OUTBOUND_CHANNELS:
        raise RecipientResolutionError(
            "channel_unavailable",
            f"{requested_channel} is not an executable outbound message route",
        )
    source = await _load_source_agent(db, source_agent_id)
    user, relationship = await _active_human_rows(db, source, canonical_id)
    config_query = select(ChannelConfig).where(
        ChannelConfig.agent_id == source.id,
        ChannelConfig.is_configured.is_(True),
        ChannelConfig.channel_type.in_(
            [
                "microsoft_teams" if value == "teams" else value
                for value in MESSAGE_OUTBOUND_CHANNELS
            ]
        ),
    )
    if requested_channel:
        config_type = (
            "microsoft_teams" if requested_channel == "teams" else requested_channel
        )
        config_query = config_query.where(ChannelConfig.channel_type == config_type)
    configs = (await db.execute(config_query)).scalars().all()

    routed: list[tuple[AgentRelationship, OrgMember, str]] = []
    for config in configs:
        normalized_channel = _normalize_channel(config.channel_type)
        if normalized_channel in {None, "web", "platform"}:
            continue
        scope = await channel_user_service.resolve_installation_scope(
            db, source, normalized_channel
        )
        bindings = (
            await db.execute(
                select(ChannelUserBinding).where(
                    ChannelUserBinding.tenant_id == source.tenant_id,
                    ChannelUserBinding.user_id == user.id,
                    ChannelUserBinding.installation_scope == scope,
                    ChannelUserBinding.channel_type.in_(
                        [normalized_channel, config.channel_type]
                    ),
                )
            )
        ).scalars().all()

        member_ids: set[uuid.UUID] = set()
        for binding in bindings:
            subject_predicate = {
                "union_id": OrgMember.unionid == binding.subject,
                "open_id": OrgMember.open_id == binding.subject,
                "user_id": OrgMember.external_id == binding.subject,
                "staff_id": OrgMember.external_id == binding.subject,
                "aad_user_id": OrgMember.external_id == binding.subject,
                "external_id": OrgMember.external_id == binding.subject,
            }.get(binding.id_type)
            if subject_predicate is None:
                continue
            member_rows = (
                await db.execute(
                    select(OrgMember).where(
                        OrgMember.user_id == user.id,
                        OrgMember.tenant_id == source.tenant_id,
                        OrgMember.status == "active",
                        OrgMember.provider_id == binding.provider_id,
                        subject_predicate,
                    )
                )
            ).scalars().all()
            member_ids.update(member.id for member in member_rows)

        # Directory-stable IDs are safe across app installations. App-scoped
        # identifiers (open_id and generic external_id) never use this fallback.
        if not member_ids and normalized_channel in {"feishu", "dingtalk", "wecom"}:
            stable_member_rows = (
                await db.execute(
                    select(OrgMember)
                    .join(IdentityProvider, OrgMember.provider_id == IdentityProvider.id)
                    .where(
                        OrgMember.user_id == user.id,
                        OrgMember.tenant_id == source.tenant_id,
                        OrgMember.status == "active",
                        OrgMember.external_id.is_not(None),
                        OrgMember.external_id != "",
                        or_(
                            IdentityProvider.provider_type == normalized_channel,
                            IdentityProvider.provider_type == config.channel_type,
                        ),
                    )
                )
            ).scalars().all()
            member_ids.update(member.id for member in stable_member_rows)

        if len(member_ids) > 1:
            raise RecipientResolutionError(
                "ambiguous_route",
                f"user_id has multiple {normalized_channel} endpoints in this installation",
                available_channels=[normalized_channel],
            )
        if member_ids:
            member = await db.get(OrgMember, next(iter(member_ids)))
            if member is not None:
                routed.append((relationship, member, normalized_channel))
    available = sorted({row[2] for row in routed if row[2]})
    if requested_channel:
        routed = [row for row in routed if row[2] == requested_channel]
        if not routed:
            raise RecipientResolutionError(
                "channel_unavailable",
                f"user_id has no active {requested_channel} route",
                available_channels=available,
            )
    if not routed:
        raise RecipientResolutionError(
            "recipient_unreachable",
            "user_id has no active external-channel route",
            available_channels=available,
        )
    if len(routed) != 1:
        raise RecipientResolutionError(
            "ambiguous_route",
            "More than one active route is available; provide channel explicitly or repair duplicate bindings",
            available_channels=available,
        )
    relationship, member, provider_type = routed[0]
    assert provider_type is not None
    return ResolvedHumanRoute(source, user, relationship, member, provider_type)


async def list_human_recipient_channels(
    db: AsyncSession,
    source_agent_id: uuid.UUID,
    user_id: object,
) -> list[str]:
    """List only routes that the canonical user can execute right now."""

    canonical_id = parse_canonical_id(user_id, "user_id")
    source = await _load_source_agent(db, source_agent_id)
    _user, relationship = await _active_human_rows(db, source, canonical_id)
    profile = (await load_human_recipient_profiles(db, source, [relationship])).get(
        canonical_id
    )
    return list(profile.channels) if profile else []


async def load_human_recipient_profiles(
    db: AsyncSession,
    source_agent: Agent,
    relationships: list[AgentRelationship],
) -> dict[uuid.UUID, HumanRecipientProfile]:
    """Aggregate all directory identities by canonical user, never member ID."""

    user_ids = sorted({relationship.user_id for relationship in relationships})
    if not user_ids:
        return {}
    users = (
        await db.execute(
            select(User).where(
                User.id.in_(user_ids),
                User.tenant_id == source_agent.tenant_id,
            )
        )
    ).scalars().all()
    user_by_id = {user.id: user for user in users}
    member_rows = (
        await db.execute(
            select(OrgMember, IdentityProvider.name, IdentityProvider.provider_type)
            .outerjoin(IdentityProvider, OrgMember.provider_id == IdentityProvider.id)
            .where(
                OrgMember.user_id.in_(user_ids),
                OrgMember.tenant_id == source_agent.tenant_id,
                OrgMember.status == "active",
            )
            .order_by(
                OrgMember.user_id,
                OrgMember.provider_id.is_(None),
                OrgMember.synced_at.asc(),
                OrgMember.id.asc(),
            )
        )
    ).all()
    members_by_user: dict[uuid.UUID, list[tuple[OrgMember, str | None, str | None]]] = {}
    for member, provider_name, provider_type in member_rows:
        members_by_user.setdefault(member.user_id, []).append(
            (member, provider_name, provider_type)
        )

    suppressed_user_ids = set(
        (
            await db.execute(
                select(RelationshipSuppression.target_id).where(
                    RelationshipSuppression.agent_id == source_agent.id,
                    RelationshipSuppression.target_type == "user",
                    RelationshipSuppression.target_id.in_(user_ids),
                )
            )
        ).scalars().all()
    )
    configs = list(
        (
            await db.execute(
                select(ChannelConfig).where(
                    ChannelConfig.agent_id == source_agent.id,
                    ChannelConfig.is_configured.is_(True),
                    ChannelConfig.channel_type.in_(
                        [
                            "microsoft_teams" if value == "teams" else value
                            for value in MESSAGE_OUTBOUND_CHANNELS
                        ]
                    ),
                )
            )
        ).scalars().all()
    )
    scoped_configs: list[tuple[str, ChannelConfig, str]] = []
    for config in configs:
        channel = _normalize_channel(config.channel_type)
        if channel not in MESSAGE_OUTBOUND_CHANNELS:
            continue
        scope = await channel_user_service.resolve_installation_scope(
            db, source_agent, channel
        )
        scoped_configs.append((channel, config, scope))

    scopes = [scope for _channel, _config, scope in scoped_configs]
    bindings = []
    if scopes:
        bindings = list(
            (
                await db.execute(
                    select(ChannelUserBinding).where(
                        ChannelUserBinding.tenant_id == source_agent.tenant_id,
                        ChannelUserBinding.user_id.in_(user_ids),
                        ChannelUserBinding.installation_scope.in_(scopes),
                    )
                )
            ).scalars().all()
        )
    bindings_by_route: dict[tuple[uuid.UUID, str, str], list[ChannelUserBinding]] = {}
    for binding in bindings:
        bindings_by_route.setdefault(
            (binding.user_id, binding.channel_type, binding.installation_scope), []
        ).append(binding)

    profiles: dict[uuid.UUID, HumanRecipientProfile] = {}
    for relationship in relationships:
        user = user_by_id.get(relationship.user_id)
        if user is None:
            continue
        rows = members_by_user.get(user.id, [])
        provider_names = sorted(
            {
                "Platform"
                if (provider_type or "").lower() in {"web", "platform"}
                else provider_name or provider_type
                for _member, provider_name, provider_type in rows
                if provider_name or provider_type
            }
        )
        if not user.is_active:
            access_status = "restricted"
            access_reason = "user_inactive"
        elif user.id in suppressed_user_ids:
            access_status = "suppressed"
            access_reason = "relationship_explicitly_suppressed"
        else:
            access_status = "active"
            access_reason = None

        channels: set[str] = set()
        if access_status == "active":
            if user.identity_id is not None:
                channels.add("platform")
            for channel, config, scope in scoped_configs:
                route_bindings = [
                    *bindings_by_route.get((user.id, channel, scope), []),
                    *bindings_by_route.get((user.id, config.channel_type, scope), []),
                ]
                member_ids: set[uuid.UUID] = set()
                for binding in route_bindings:
                    attr = {
                        "union_id": "unionid",
                        "open_id": "open_id",
                        "user_id": "external_id",
                        "staff_id": "external_id",
                        "aad_user_id": "external_id",
                        "external_id": "external_id",
                    }.get(binding.id_type)
                    if attr is None:
                        continue
                    for member, _provider_name, _provider_type in rows:
                        if (
                            member.provider_id == binding.provider_id
                            and getattr(member, attr, None) == binding.subject
                        ):
                            member_ids.add(member.id)
                if not member_ids and channel in {"feishu", "dingtalk", "wecom"}:
                    for member, _provider_name, provider_type in rows:
                        if (
                            _normalize_channel(provider_type) == channel
                            and bool((member.external_id or "").strip())
                        ):
                            member_ids.add(member.id)
                if len(member_ids) == 1:
                    channels.add(channel)
        profiles[user.id] = HumanRecipientProfile(
            user=user,
            member=rows[0][0] if rows else None,
            channels=tuple(sorted(channels)),
            provider_names=tuple(provider_names),
            access_status=access_status,
            access_status_reason=access_reason,
        )
    return profiles
