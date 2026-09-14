"""One short-transaction participant gate shared by all IM transports."""

import uuid
from contextvars import ContextVar

from loguru import logger
from sqlalchemy import String, func, or_, select
from sqlalchemy.dialects.postgresql import insert

from app.database import async_session
from app.models.agent import Agent
from app.models.agent_group import AgentGroup, AgentGroupMember
from app.models.channel_config import ChannelConfig
from app.models.org import ChannelUserBinding
from app.models.identity import IdentityProvider
from app.models.user import User
from app.services.channel_user_service import channel_user_service, ChannelUserResolutionError
from app.services.group_policy_sender import prepare_sender, set_current_sender
from app.services.user_output import sanitize_user_visible_text
from app.services.group_policy_identity import external_group_from_route, stable_group_id, stable_member_id

SUPPORTED_CHANNELS = frozenset({"dingtalk", "feishu", "wecom", "slack", "discord", "teams"})
_incoming_group: ContextVar[tuple | None] = ContextVar("incoming_group_policy", default=None)


def policy_allows(rules, member_ids=(), user_ids=()):
    """Enabled denies win; the presence of any enabled allow closes the roster."""
    members = {str(item) for item in member_ids}
    users = {str(item) for item in user_ids}
    active = [rule for rule in rules if rule.get("enabled", True)]
    def matches(rule):
        return (rule.get("all_members", False)
                or bool(members.intersection(rule.get("member_ids", [])))
                or bool(users.intersection(rule.get("user_ids", []))))
    if any(rule["effect"] == "deny" and matches(rule) for rule in active):
        return False
    allows = [rule for rule in active if rule["effect"] == "allow"]
    return not allows or any(matches(rule) for rule in allows)


async def installation_scope(db, agent, channel):
    config_type = "microsoft_teams" if channel == "teams" else channel
    config = await db.scalar(select(ChannelConfig.id).where(
        ChannelConfig.agent_id == agent.id, ChannelConfig.channel_type == config_type,
        ChannelConfig.is_configured.is_(True),
    ))
    if config is None:
        return None
    return await channel_user_service.resolve_installation_scope(db, agent, channel)


async def observe_group(db, agent, channel, scope, external_group_id, name=""):
    """Discovery never changes manager-owned rules."""
    display_name = sanitize_user_visible_text(str(name or ""))[:200]
    statement = insert(AgentGroup).values(
        id=stable_group_id(agent.id, channel, scope, external_group_id), tenant_id=agent.tenant_id, agent_id=agent.id,
        channel=channel, installation_scope=scope, external_group_id=external_group_id, name=display_name,
    )
    changes = {"last_seen_at": func.now()}
    if display_name:
        changes["name"] = display_name
    return (await db.execute(statement.on_conflict_do_update(
        constraint="uq_agent_groups_identity", set_=changes,
    ).returning(AgentGroup).execution_options(populate_existing=True))).scalar_one()


def scoped_bindings(group, provider_id=None):
    configured = select(ChannelConfig.extra_config["identity_provider_id"].as_string()).where(
        ChannelConfig.agent_id == group.agent_id,
        ChannelConfig.channel_type == ("microsoft_teams" if group.channel == "teams" else group.channel),
    ).scalar_subquery()
    provider_filter = ChannelUserBinding.provider_id == provider_id if provider_id else or_(
        configured.is_(None), func.cast(ChannelUserBinding.provider_id, String) == configured)
    return select(ChannelUserBinding).join(IdentityProvider, IdentityProvider.id == ChannelUserBinding.provider_id).where(
        provider_filter, IdentityProvider.tenant_id == group.tenant_id, IdentityProvider.is_active.is_(True),
        ChannelUserBinding.tenant_id == group.tenant_id,
        ChannelUserBinding.installation_scope == group.installation_scope,
        ChannelUserBinding.channel_type == group.channel,
    )


async def observe_member(db, group, subject, subject_type, name="", actor=None):
    """Observe participation, reusing verified bindings solely for aliases/names."""
    users = {actor.user_id} if actor else set()
    member = await db.scalar(select(AgentGroupMember).where(
        AgentGroupMember.group_id == group.id, AgentGroupMember.subject == subject,
        AgentGroupMember.subject_type == subject_type,
    ))
    related = []
    if users:
        aliases = scoped_bindings(group, actor.provider_id if actor else None).where(ChannelUserBinding.user_id.in_(users)).subquery()
        related = list((await db.scalars(select(AgentGroupMember).join(aliases,
            (aliases.c.subject == AgentGroupMember.subject) & (aliases.c.id_type == AgentGroupMember.subject_type),
        ).where(AgentGroupMember.group_id == group.id).order_by(AgentGroupMember.id))).unique())
        if not member and related:
            member = related[0]
    if not member:
        member = AgentGroupMember(id=stable_member_id(group.id, subject_type, subject), group_id=group.id, subject=subject, subject_type=subject_type)
        db.add(member)
    if users and not name:
        name = await db.scalar(select(User.display_name).where(User.id.in_(users), User.tenant_id == group.tenant_id))
    if name:
        member.name = sanitize_user_visible_text(str(name))[:200]
    member.last_seen_at = func.now()
    await db.flush()
    return {item.id for item in [member, *related]}, users


async def group_ingress_allowed(
    agent_id, channel, external_group_id, *, is_group, name="", conversation_ref=None,
    sender_id="", sender_name="", sender_type=None, sender_info=None, prepared_sender=None,
):
    """Authenticate in adapters, then discover/evaluate before commands or media."""
    _incoming_group.set(None)
    set_current_sender(None)
    if not is_group:
        return True
    subject_type = sender_type or {"feishu": "open_id", "dingtalk": "sender_id", "wecom": "user_id"}.get(channel, "external_id")
    async with async_session() as db:
        agent = await db.scalar(select(Agent).where(Agent.id == agent_id))
        if agent is None or not agent.tenant_id or agent.is_deleted:
            return False
        scope = await installation_scope(db, agent, channel)
        if not scope:
            rosters = await db.scalars(select(AgentGroup.rules).where(
                AgentGroup.agent_id == agent_id, AgentGroup.tenant_id == agent.tenant_id,
                AgentGroup.channel == channel))
            return not any(rule.get("enabled", True) for roster in rosters for rule in roster)
        if not external_group_id or len(str(external_group_id)) > 512:
            rosters = await db.scalars(select(AgentGroup.rules).where(
                AgentGroup.agent_id == agent_id, AgentGroup.tenant_id == agent.tenant_id,
                AgentGroup.channel == channel, AgentGroup.installation_scope == scope))
            return not any(rule.get("enabled", True) for roster in rosters for rule in roster)
        # A complete all-member denial needs no identity lookup or provider wait.
        rules = await db.scalar(select(AgentGroup.rules).where(
            AgentGroup.agent_id == agent_id, AgentGroup.tenant_id == agent.tenant_id,
            AgentGroup.channel == channel, AgentGroup.installation_scope == scope,
            AgentGroup.external_group_id == str(external_group_id),
        ))
        if not any(rule.get("enabled", True) for rule in (rules or [])):
            # Discovery is local metadata only. It must not introduce identity
            # preparation/provider dependencies into an unrestricted conversation.
            group = await observe_group(db, agent, channel, scope, str(external_group_id), name)
            rules = group.rules  # Serialize with a concurrent first policy save.
            if not any(rule.get("enabled", True) for rule in rules):
                if sender_id and len(str(sender_id)) <= 512:
                    await observe_member(db, group, str(sender_id), subject_type, sender_name)
                set_incoming_group(agent, channel, group, external_group_id, conversation_ref)
                await db.commit()
                return True
        deny_all = any(rule.get("enabled", True) and rule.get("all_members") and rule["effect"] == "deny" for rule in (rules or []))
        await db.commit()
        if deny_all:
            return False
    actor = prepared_sender
    if actor and (actor.agent_id != agent_id or actor.channel != channel or actor.scope != scope
                  or actor.subject != str(sender_id) or actor.subject_type != subject_type):
        return False
    if not actor and sender_id and len(str(sender_id)) <= 512:
        try:
            actor = await prepare_sender(agent_id, channel, str(sender_id), subject_type, sender_name, sender_info or {})
        except ChannelUserResolutionError:
            logger.warning("Group sender identity could not be resolved: agent={} channel={}", agent_id, channel)
            return False
    async with async_session() as db:
        agent = await db.get(Agent, agent_id)
        if not agent or agent.is_deleted or await installation_scope(db, agent, channel) != scope:
            return False
        if actor:
            provider, info = await channel_user_service.resolve_channel_provider(db, agent, channel)
            if provider.id != actor.provider_id or info["_installation_scope"] != actor.scope or actor.tenant_id != agent.tenant_id:
                return False
        # Only local observations and the final latest-rule check hold the group lock.
        group = await observe_group(db, agent, channel, scope, str(external_group_id), name)
        members, users = None, set()
        if actor:
            members, users = await observe_member(db, group, str(sender_id), subject_type, sender_name, actor)
        allowed = policy_allows(group.rules, members or [], users)
        if not members and any(rule.get("enabled", True) for rule in group.rules):
            allowed = False
        if allowed:
            set_incoming_group(agent, channel, group, external_group_id, conversation_ref)
            set_current_sender(actor)
        else:
            logger.info("Group member input denied: agent={} group={} revision={}", agent.id, group.id, group.revision)
        await db.commit()
        return allowed


def set_incoming_group(agent, channel, group, external_group_id, conversation_ref):
    route = conversation_ref or {
        "dingtalk": f"dingtalk_group_{external_group_id}", "feishu": f"feishu_group_{external_group_id}",
        "wecom": f"wecom_group_{external_group_id}", "slack": f"slack_{external_group_id}",
        "discord": f"discord_{external_group_id}", "teams": external_group_id,
    }.get(channel)
    _incoming_group.set((str(agent.id), channel, str(group.id), route))


def bind_session_group(session):
    incoming = _incoming_group.get()
    if not session.is_group or not incoming:
        return
    agent_id, channel, group_id, expected_route = incoming
    if str(session.agent_id) == agent_id and session.source_channel == channel and session.external_conv_id == expected_route:
        session.im_config = {**(session.im_config or {}), "group_target_id": group_id}


async def session_group_allowed(db, session, user_id=None):
    """Confirmation checks the clicking participant, never the Session owner."""
    legacy_discord_group = bool(session and session.source_channel == "discord"
        and (session.external_conv_id or "").startswith("discord_")
        and not (session.external_conv_id or "").startswith("discord_dm_"))
    if not session or not (session.is_group or legacy_discord_group) or session.project_id or session.source_channel not in SUPPORTED_CHANNELS:
        return True
    agent = await db.get(Agent, session.agent_id)
    if not agent:
        return False
    try:
        group_id = uuid.UUID(str((session.im_config or {}).get("group_target_id")))
    except (ValueError, TypeError):
        external_id = external_group_from_route(session.source_channel, session.external_conv_id)
        if not external_id:
            rules = await db.scalars(select(AgentGroup.rules).where(
                AgentGroup.agent_id == agent.id, AgentGroup.tenant_id == agent.tenant_id,
                AgentGroup.channel == session.source_channel,
            ))
            return not any(rule.get("enabled", True) for roster in rules for rule in roster)
        group_id = await db.scalar(select(AgentGroup.id).where(
            AgentGroup.agent_id == agent.id, AgentGroup.tenant_id == agent.tenant_id,
            AgentGroup.channel == session.source_channel, AgentGroup.external_group_id == external_id,
            AgentGroup.installation_scope == await installation_scope(db, agent, session.source_channel),
        ))
        if group_id is None:
            return True
    group = await db.scalar(select(AgentGroup).where(
        AgentGroup.id == group_id, AgentGroup.agent_id == agent.id,
        AgentGroup.tenant_id == agent.tenant_id, AgentGroup.channel == session.source_channel,
    ).with_for_update(read=True).execution_options(populate_existing=True))
    if group and not any(rule.get("enabled", True) for rule in group.rules):
        return True
    if not group or group.installation_scope != await installation_scope(db, agent, group.channel):
        return False
    aliases = scoped_bindings(group).where(ChannelUserBinding.user_id == user_id).subquery()
    members = list(await db.scalars(select(AgentGroupMember.id).join(aliases,
        (aliases.c.subject == AgentGroupMember.subject) & (aliases.c.id_type == AgentGroupMember.subject_type),
    ).where(AgentGroupMember.group_id == group.id)))
    canonical_user = await db.scalar(select(User.id).where(User.id == user_id, User.tenant_id == group.tenant_id))
    if not members and not canonical_user and any(rule.get("enabled", True) for rule in group.rules):
        return False
    return policy_allows(group.rules, members, [canonical_user] if canonical_user else [])
