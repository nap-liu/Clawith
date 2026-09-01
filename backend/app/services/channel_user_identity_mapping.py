"""Channel identity mapping and directory lookup methods."""

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.identity import IdentityProvider
from app.models.org import ChannelUserBinding, OrgMember
from app.services.channel_user_errors import ChannelUserResolutionError
from app.services.directory_identity_claims import VerifiedDirectoryClaims


class ChannelUserIdentityMappingMethods:
    def _normalize_channel_type(self, channel_type: str) -> str:
        raw = (channel_type or "").strip().lower()
        return self.CHANNEL_TYPE_ALIASES.get(raw, raw)

    def _legacy_provider_types_for_channel(self, channel_type: str) -> list[str]:
        normalized = self._normalize_channel_type(channel_type)
        legacy = [normalized]
        if normalized == "teams":
            legacy.append("microsoft_teams")
        elif normalized == "microsoft_teams":
            legacy.append("teams")
        return legacy

    def _get_channel_ids(
        self,
        channel_type: str,
        external_user_id: str | None,
        extra_info: dict[str, Any],
    ) -> tuple[str | None, str | None, str | None]:
        normalized_channel = self._normalize_channel_type(channel_type)
        unionid = (extra_info.get("unionid") or extra_info.get("union_id") or "").strip() or None
        open_id = (extra_info.get("open_id") or "").strip() or None
        external_id = (extra_info.get("external_id") or external_user_id or "").strip() or None

        if normalized_channel == "feishu":
            # Feishu external_id must remain tenant-stable user_id only.
            # Never backfill it from open_id.
            external_id = (extra_info.get("external_id") or "").strip() or None
        elif normalized_channel == "dingtalk":
            open_id = open_id or None
        elif normalized_channel == "wecom":
            unionid = None
            open_id = open_id or None
        else:
            unionid = None
            open_id = None

        return unionid, open_id, external_id

    def _installation_scope(
        self,
        provider: IdentityProvider,
        extra_info: dict[str, Any] | None = None,
    ) -> str:
        """Return the non-secret installation namespace for channel subjects."""
        explicit = str((extra_info or {}).get("_installation_scope") or "").strip()
        if explicit:
            return explicit
        configured = str((provider.config or {}).get("installation_scope") or "").strip()
        return configured or f"provider:{provider.id}"

    async def _resolve_installation_scope(
        self,
        db: AsyncSession,
        agent: Agent,
        channel_type: str,
        extra_info: dict[str, Any],
    ) -> str:
        """Build a stable non-secret namespace for the receiving bot installation."""
        explicit = str(extra_info.get("_installation_scope") or "").strip()
        if explicit:
            return explicit

        from app.models.channel_config import ChannelConfig

        normalized = self._normalize_channel_type(channel_type)
        config_type = "microsoft_teams" if normalized == "teams" else normalized
        config = (
            await db.execute(
                select(ChannelConfig).where(
                    ChannelConfig.agent_id == agent.id,
                    ChannelConfig.channel_type == config_type,
                )
            )
        ).scalar_one_or_none()
        if config is None:
            return f"agent:{agent.id}:channel:{normalized}"

        extra_config = config.extra_config if isinstance(config.extra_config, dict) else {}
        issuer = str(
            extra_info.get("_issuer")
            or config.app_id
            or extra_config.get("app_id")
            or extra_config.get("workspace_id")
            or extra_config.get("team_id")
            or extra_config.get("corp_id")
            or extra_config.get("robot_code")
            or extra_config.get("phone_number_id")
            or ""
        ).strip()
        if issuer:
            issuer_hash = hashlib.sha256(issuer.encode("utf-8")).hexdigest()[:32]
            return f"agent:{agent.id}:channel:{normalized}:issuer:{issuer_hash}"
        return f"channel-config:{config.id}"

    async def resolve_installation_scope(
        self,
        db: AsyncSession,
        agent: Agent,
        channel_type: str,
    ) -> str:
        """Return the same installation namespace used by inbound bindings."""
        return await self._resolve_installation_scope(db, agent, channel_type, {})

    def _binding_subjects(
        self,
        provider: IdentityProvider,
        channel_type: str,
        external_user_id: str | None,
        extra_info: dict[str, Any],
    ) -> list[tuple[str, str]]:
        unionid, open_id, external_id = self._get_channel_ids(
            channel_type, external_user_id, extra_info
        )
        normalized = self._normalize_channel_type(channel_type)
        external_type = {
            "feishu": "user_id",
            "dingtalk": "staff_id",
            "wecom": "user_id",
        }.get(normalized, "external_id")
        candidates = [
            ("union_id", unionid),
            ("open_id", open_id),
            (external_type, external_id),
        ]
        seen: set[tuple[str, str]] = set()
        subjects: list[tuple[str, str]] = []
        for id_type, subject in candidates:
            full_subject = str(subject or "").strip()
            key = (id_type, full_subject)
            if full_subject and key not in seen:
                seen.add(key)
                subjects.append(key)
        return subjects

    def _fresh_dingtalk_claims(
        self,
        provider: IdentityProvider,
        external_user_id: str | None,
        extra_info: dict[str, Any],
    ) -> VerifiedDirectoryClaims | None:
        if (
            provider.provider_type != "dingtalk"
            or provider.tenant_id is None
            or extra_info.get("identity_verified") is not True
        ):
            return None
        external_id = str(
            extra_info.get("external_id") or external_user_id or ""
        ).strip()
        if not external_id:
            return None
        return VerifiedDirectoryClaims(
            tenant_id=provider.tenant_id,
            provider_id=provider.id,
            external_id=external_id,
            observed_at=datetime.now(timezone.utc),
            raw_email=(
                extra_info.get("raw_email")
                if "raw_email" in extra_info
                else extra_info.get("email")
            ),
            raw_org_email=extra_info.get("raw_org_email"),
            raw_mobile=(
                extra_info.get("raw_mobile")
                if "raw_mobile" in extra_info
                else extra_info.get("mobile")
            ),
            source="dingtalk_user_get",
        )

    def _merge_channel_info_into_member(
        self,
        org_member: OrgMember,
        channel_type: str,
        extra_info: dict[str, Any],
    ) -> None:
        identity_seed = org_member.external_id or org_member.open_id or org_member.id.hex
        generated_name = f"{channel_type.capitalize()} User {identity_seed[:8]}"
        incoming_name = (extra_info.get("name") or "").strip()
        directory_name_verified = extra_info.get("directory_name_verified") is True
        if incoming_name and (
            directory_name_verified
            or not org_member.name
            or org_member.name == generated_name
        ):
            org_member.name = incoming_name
        incoming_nickname = (extra_info.get("nickname") or "").strip()
        if incoming_nickname and org_member.nickname != incoming_nickname:
            org_member.nickname = incoming_nickname
        contact_verified = extra_info.get("identity_verified") is True
        display_email = extra_info.get("raw_org_email") or extra_info.get("raw_email")
        if contact_verified and display_email and not org_member.email:
            org_member.email = display_email
        display_mobile = extra_info.get("raw_mobile")
        if contact_verified and display_mobile and not org_member.phone:
            org_member.phone = display_mobile
        if extra_info.get("avatar_url") and not org_member.avatar_url:
            org_member.avatar_url = extra_info["avatar_url"]
        if extra_info.get("title") and not org_member.title:
            org_member.title = extra_info["title"]

    async def _find_org_member(
        self,
        db: AsyncSession,
        provider_id: uuid.UUID,
        channel_type: str,
        external_user_id: str | None,
        extra_info: dict[str, Any] | None = None,
    ) -> OrgMember | None:
        """Find OrgMember by external identity.

        For Feishu: try unionid first, then open_id, then external_id
        For DingTalk: try unionid first, then external_id
        For WeCom: try external_id (userid)
        For WeChat: try external_id (from_user_id)

        Returns None if OrgMember not found or org sync is not enabled for this channel.
        """
        try:
            extra_info = extra_info or {}
            unionid, open_id, external_id = self._get_channel_ids(
                channel_type, external_user_id, extra_info
            )

            # Build OR conditions for matching
            conditions = [OrgMember.provider_id == provider_id, OrgMember.status == "active"]

            # Channel-specific matching priority
            normalized_channel = self._normalize_channel_type(channel_type)
            if normalized_channel == "feishu":
                # Feishu identifiers have distinct semantics:
                # unionid/open_id come from extra_info; external_id is user_id only.
                lookup_conditions = []
                if unionid:
                    lookup_conditions.append(OrgMember.unionid == unionid)
                if open_id:
                    lookup_conditions.append(OrgMember.open_id == open_id)
                if external_id:
                    lookup_conditions.append(OrgMember.external_id == external_id)
                if not lookup_conditions:
                    return None
                conditions.append(lookup_conditions[0])
                for cond in lookup_conditions[1:]:
                    conditions[-1] = conditions[-1] | cond
            elif normalized_channel == "dingtalk":
                # DingTalk: unionid is stable across apps, then external_id
                lookup_conditions = []
                if unionid:
                    lookup_conditions.append(OrgMember.unionid == unionid)
                if external_id:
                    lookup_conditions.append(OrgMember.external_id == external_id)
                if not lookup_conditions:
                    return None
                conditions.append(lookup_conditions[0])
                for cond in lookup_conditions[1:]:
                    conditions[-1] = conditions[-1] | cond
            elif normalized_channel == "wecom":
                # WeCom: external_id (userid) is the primary identifier
                if not external_id:
                    return None
                conditions.append(OrgMember.external_id == external_id)
            else:
                # Generic channels: provider is already channel-scoped, so external_id
                # can be used directly without namespacing.
                if not external_id:
                    return None
                conditions.append(OrgMember.external_id == external_id)

            query = (
                select(OrgMember)
                .where(*conditions)
                .order_by(
                    OrgMember.synced_at.asc(),
                    OrgMember.id.asc(),
                )
            )
            result = await db.execute(query)
            rows = result.scalars().all()
            if not rows:
                return None
            if normalized_channel not in {"feishu", "dingtalk", "wecom"}:
                # Historical generic-channel OrgMembers were tenant/provider
                # scoped, not installation scoped. They are an exact bridge
                # only when that tenant has one installation for the channel;
                # with multiple workspaces/bots the same external subject may
                # refer to different people, so fail closed for repair instead
                # of merging or creating a silent duplicate.
                from app.models.channel_config import ChannelConfig

                config_types = [normalized_channel]
                if normalized_channel == "teams":
                    config_types.append("microsoft_teams")
                installation_count = (
                    await db.execute(
                        select(func.count(ChannelConfig.id))
                        .select_from(ChannelConfig)
                        .join(Agent, Agent.id == ChannelConfig.agent_id)
                        .where(
                            Agent.tenant_id == rows[0].tenant_id,
                            ChannelConfig.channel_type.in_(config_types),
                        )
                    )
                ).scalar_one()
                if installation_count != 1:
                    row_user_ids = {row.user_id for row in rows if row.user_id is not None}
                    bound_user_ids = set(
                        (
                            await db.execute(
                                select(ChannelUserBinding.user_id).where(
                                    ChannelUserBinding.provider_id == provider_id,
                                    ChannelUserBinding.user_id.in_(row_user_ids),
                                    ChannelUserBinding.channel_type.in_(config_types),
                                    ChannelUserBinding.id_type == "external_id",
                                    ChannelUserBinding.subject == external_id,
                                )
                            )
                        ).scalars().all()
                    ) if row_user_ids else set()
                    if row_user_ids and row_user_ids.issubset(bound_user_ids):
                        # These are modern shells owned by other installation
                        # scopes, not unresolved legacy data. Ignore them so
                        # the current installation can create its own isolated
                        # canonical user and binding for the same subject.
                        return None
                    raise ChannelUserResolutionError(
                        "Legacy channel subject has no unique installation scope; migration_required"
                    )
            if len(rows) > 1:
                user_ids = {row.user_id for row in rows}
                if None in user_ids or len(user_ids) != 1:
                    raise ChannelUserResolutionError(
                        "Directory subject maps to multiple OrgMember records; migration_required"
                    )
            return rows[0]
        except ChannelUserResolutionError:
            raise
        except Exception as e:
            raise ChannelUserResolutionError(
                f"Directory identity lookup failed closed for {channel_type}"
            ) from e
