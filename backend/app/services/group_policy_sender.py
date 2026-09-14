"""One prepared, authoritative actor shared by policy admission and message handling."""

import uuid
from contextvars import ContextVar
from dataclasses import dataclass

from sqlalchemy import select

from app.database import async_session
from app.models.agent import Agent
from app.models.channel_config import ChannelConfig
from app.models.user import User
from app.services.channel_user_service import channel_user_service, ChannelUserResolutionError


@dataclass(frozen=True)
class PreparedSender:
    agent_id: uuid.UUID
    tenant_id: uuid.UUID
    channel: str
    scope: str
    provider_id: uuid.UUID
    user_id: uuid.UUID
    subject: str
    subject_type: str
    info: dict


_sender: ContextVar[PreparedSender | None] = ContextVar("prepared_group_sender", default=None)
current_sender = _sender.get
set_current_sender = _sender.set


async def prepare_sender(agent_id, channel, subject, subject_type, name, info):
    async with async_session() as db:
        agent = await db.get(Agent, agent_id)
        config = await db.scalar(select(ChannelConfig).where(ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == ("microsoft_teams" if channel == "teams" else channel)))
        provider, details = await channel_user_service.resolve_channel_provider(db, agent, channel, info)
        scope, provider_id = details["_installation_scope"], provider.id
        if channel == "dingtalk":
            from app.services.dingtalk_sender import resolve_dingtalk_sender
            user, provider, _ = await resolve_dingtalk_sender(db, agent, config,
                info.get("staff_id") or (subject if subject_type == "staff_id" else ""),
                subject if subject_type == "sender_id" else "", name)
        else:
            if channel == "feishu":
                from app.services.feishu_sender import feishu_sender_info
                credentials = config.app_id, config.app_secret
                await db.commit()
                details.update(await feishu_sender_info(*credentials,
                    subject if subject_type == "open_id" else info.get("open_id", ""),
                    info.get("external_id") or (subject if subject_type == "user_id" else ""),
                    info.get("unionid") or (subject if subject_type == "union_id" else "")))
                external_id = details.get("external_id") or None
            else:
                external_id = subject
                details["name"] = name
            details["name"] = details.get("name") or name
            # Re-read config after provider waits; a different installation/provider
            # must not be admitted with credentials or claims from the old one.
            db.expire_all()
            agent = await db.get(Agent, agent_id)
            current_provider, current_info = await channel_user_service.resolve_channel_provider(db, agent, channel)
            if current_provider.id != provider_id or current_info["_installation_scope"] != scope:
                raise ChannelUserResolutionError("Channel identity configuration changed during preparation")
            user = await channel_user_service.resolve_channel_user(db, agent, channel, external_id, details, provider=current_provider)
        if not user.is_active or (user.identity and not user.identity.is_active):
            raise ChannelUserResolutionError("Prepared channel user is inactive")
        actor = PreparedSender(agent_id, agent.tenant_id, channel, scope, provider_id,
                               user.id, subject, subject_type, details)
        await db.commit()
        return actor


async def resolve_ingress_user(db, agent, channel_type, external_user_id, extra_info=None, provider=None):
    actor = current_sender()
    if actor and actor.agent_id == agent.id and actor.channel == channel_type:
        user = await db.scalar(select(User).where(User.id == actor.user_id, User.tenant_id == agent.tenant_id))
        if not user or not user.is_active:
            raise ChannelUserResolutionError("Prepared channel user is unavailable")
        return user
    return await channel_user_service.resolve_channel_user(db, agent, channel_type, external_user_id, extra_info, provider)
