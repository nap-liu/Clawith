"""DingTalk authoritative sender preparation, shared before group admission."""

from loguru import logger
from sqlalchemy import select as _select
from sqlalchemy.orm import selectinload as _selectinload
from app.models.user import User as UserModel


async def resolve_dingtalk_sender(db, agent_obj, _early_cfg, sender_staff_id, sender_id, sender_nick):
    from app.api.dingtalk import _resolve_dingtalk_directory_credentials, _get_dingtalk_user_detail_with_fallback
    directory_staff_id = sender_staff_id if not sender_id or sender_staff_id != sender_id else ""
    # -- Multi-dimension user matching (optimized: local-first, API-last) --
    from app.models.org import OrgMember
    from sqlalchemy import or_ as _or
    from app.models.user import Identity as _IdentityModel

    dt_username = f"dingtalk_{directory_staff_id or sender_staff_id}"
    platform_user = None
    dt_unionid = ""
    dt_mobile = ""
    dt_email = ""
    dt_raw_email = None
    dt_raw_org_email = None
    dt_real_name = ""

    from app.services.channel_user_service import channel_user_service

    # Resolve the tenant enterprise provider selected by this channel.
    _dingtalk_provider, _channel_identity_info = (
        await channel_user_service.resolve_channel_provider(
            db, agent_obj, "dingtalk"
        )
    )
    _directory_credentials = _resolve_dingtalk_directory_credentials(
        _dingtalk_provider, _early_cfg
    )
    if _directory_credentials:
        logger.debug(
            "[DingTalk] Directory credential chain selected: sources={} provider_id={}",
            [item[2] for item in _directory_credentials],
            getattr(_dingtalk_provider, "id", None),
        )
    else:
        logger.warning(
            "[DingTalk] No enterprise provider credentials are available for directory enrichment"
        )

    # Canonical attribution is resolved through the shared provider user.
    # The receiving robot never supplies directory lookup authority.
    dt_user_detail = None
    if _directory_credentials and directory_staff_id:
        await db.commit()
        dt_user_detail = await _get_dingtalk_user_detail_with_fallback(
            _directory_credentials,
            directory_staff_id,
            getattr(_dingtalk_provider, "id", None),
        )
        if dt_user_detail:
            dt_real_name = dt_user_detail.get("name", "")
            dt_unionid = dt_user_detail.get("unionid", "")
            dt_mobile = dt_user_detail.get("mobile") or ""
            dt_raw_email = dt_user_detail.get("email")
            dt_raw_org_email = dt_user_detail.get("org_email")
            dt_email = dt_raw_org_email or dt_raw_email or ""

    # Step 3: Enrich missing identity data, including legacy dingtalk_* users.
    if not dt_mobile and _directory_credentials and directory_staff_id:
        await db.commit()
        dt_user_detail = await _get_dingtalk_user_detail_with_fallback(
            _directory_credentials,
            directory_staff_id,
            getattr(_dingtalk_provider, "id", None),
        )
        if dt_user_detail:
            dt_real_name = dt_real_name or dt_user_detail.get("name", "")
            dt_unionid = dt_unionid or dt_user_detail.get("unionid", "")
            dt_mobile = dt_mobile or dt_user_detail.get("mobile") or ""
            dt_raw_email = dt_user_detail.get("email")
            dt_raw_org_email = dt_user_detail.get("org_email")
            dt_email = dt_email or dt_raw_org_email or dt_raw_email or ""

    # Provider I/O ran without a transaction. Revalidate before any identity write.
    from app.models.agent import Agent
    from app.models.channel_config import ChannelConfig
    from app.services.channel_user_service import ChannelUserResolutionError
    agent_id, config_id = agent_obj.id, _early_cfg.id if _early_cfg else None
    provider_id, scope = _dingtalk_provider.id, _channel_identity_info["_installation_scope"]
    db.expire_all()
    agent_obj = await db.get(Agent, agent_id)
    if config_id:
        _early_cfg = await db.get(ChannelConfig, config_id)
    current_provider, current_info = await channel_user_service.resolve_channel_provider(db, agent_obj, "dingtalk")
    if current_provider.id != provider_id or current_info["_installation_scope"] != scope:
        raise ChannelUserResolutionError("Channel identity configuration changed during preparation")
    _dingtalk_provider = current_provider

    _channel_identity_info.update(
        {
            "external_id": directory_staff_id,
            "sender_id": sender_id,
            "unionid": dt_unionid,
            "mobile": dt_mobile,
            "email": dt_email,
            "raw_email": dt_raw_email,
            "raw_org_email": dt_raw_org_email,
            "raw_mobile": dt_mobile,
            "name": dt_real_name,
            "nickname": sender_nick,
            "directory_name_verified": bool(dt_real_name),
            "identity_verified": bool(dt_mobile or dt_email),
            "source_account_active": dt_user_detail is not None,
        }
    )
    platform_user = await channel_user_service.resolve_channel_user(
        db,
        agent_obj,
        "dingtalk",
        directory_staff_id or None,
        _channel_identity_info,
        provider=_dingtalk_provider,
    )

    _sender_org_member = None

    # Step 1: Match via sender_staff_id in org_members.external_id (企业 userId，最稳定)
    if directory_staff_id and _dingtalk_provider and not platform_user:
        _om_r = await db.execute(
            _select(OrgMember).where(
                OrgMember.provider_id == _dingtalk_provider.id,
                OrgMember.external_id == directory_staff_id,
                OrgMember.status == "active",
            ).order_by(OrgMember.synced_at.desc(), OrgMember.id.desc()).limit(1)
        )
        _sender_org_member = _om_r.scalar_one_or_none()
        if _sender_org_member:
            dt_unionid = _sender_org_member.unionid or ""
            dt_mobile = _sender_org_member.phone or ""
            dt_email = _sender_org_member.email or ""
        if _sender_org_member and _sender_org_member.user_id:
            _u_r = await db.execute(_select(UserModel).where(UserModel.id == _sender_org_member.user_id).options(_selectinload(UserModel.identity)))
            platform_user = _u_r.scalar_one_or_none()
            if platform_user:
                logger.info(f"[DingTalk] Step1: Matched user via staff_id {directory_staff_id}: {platform_user.username}")
                if platform_user.identity:
                    dt_mobile = dt_mobile or platform_user.identity.phone or ""
                    identity_email = platform_user.identity.email or ""
                    if identity_email and not identity_email.endswith(".local"):
                        dt_email = dt_email or identity_email

    # Step 2: Match via username = dingtalk_{staffId} (兼容旧用户)
    if directory_staff_id and not platform_user:
        _u_r = await db.execute(
            _select(UserModel).join(UserModel.identity).where(
                _IdentityModel.username == dt_username,
                UserModel.tenant_id == agent_obj.tenant_id,
            ).options(_selectinload(UserModel.identity))
        )
        platform_user = _u_r.scalar_one_or_none()
        if platform_user:
            logger.info(f"[DingTalk] Step2: Matched user via username {dt_username}")
            if platform_user.identity:
                dt_mobile = dt_mobile or platform_user.identity.phone or ""
                identity_email = platform_user.identity.email or ""
                if identity_email and not identity_email.endswith(".local"):
                    dt_email = dt_email or identity_email

    # 3a: unionId 查 org_members（跨通道匹配 SSO 用户）
    if dt_unionid and _dingtalk_provider and not platform_user:
        _om_r = await db.execute(
            _select(OrgMember).where(
                OrgMember.provider_id == _dingtalk_provider.id,
                OrgMember.status == "active",
                _or(
                    OrgMember.unionid == dt_unionid,
                    OrgMember.external_id == dt_unionid,
                ),
            ).order_by(OrgMember.synced_at.desc(), OrgMember.id.desc()).limit(1)
        )
        _om = _om_r.scalar_one_or_none()
        if _om and _om.user_id:
            _u_r = await db.execute(_select(UserModel).where(UserModel.id == _om.user_id).options(_selectinload(UserModel.identity)))
            platform_user = _u_r.scalar_one_or_none()
            if platform_user:
                logger.info("[DingTalk] Step3a: Matched user via enterprise unionid")

    # 3b: mobile 匹配
    if dt_mobile and not platform_user:
        _u_r = await db.execute(
            _select(UserModel).join(UserModel.identity).where(
                _IdentityModel.phone == dt_mobile,
                UserModel.tenant_id == agent_obj.tenant_id,
            ).options(_selectinload(UserModel.identity))
        )
        mobile_user = _u_r.scalar_one_or_none()
        if mobile_user:
            platform_user = mobile_user
            logger.info(f"[DingTalk] Step3b: Matched user via mobile: {platform_user.username}")

    # 3c: email 匹配
    if dt_email and not platform_user:
        _u_r = await db.execute(
            _select(UserModel).join(UserModel.identity).where(
                _IdentityModel.email == dt_email,
                UserModel.tenant_id == agent_obj.tenant_id,
            ).options(_selectinload(UserModel.identity))
        )
        platform_user = _u_r.scalar_one_or_none()
        if platform_user:
            logger.info(f"[DingTalk] Step3c: Matched user via email: {platform_user.username}")


    # Step 4: No match found — create new user
    if not platform_user:
        # Defensive fallback only: inbound channel identities are not login
        # principals and must never mint synthetic credentials.
        platform_user = UserModel(
            identity_id=None,
            display_name=sender_nick or f"DingTalk {sender_staff_id[:8]}",
            role="member",
            tenant_id=agent_obj.tenant_id if agent_obj else None,
            source="dingtalk",
            is_active=True,
        )
        db.add(platform_user)
        await db.flush()
        logger.info(f"[DingTalk] Step4: Created new user: {dt_username}")
    else:
        # Update source and verified contact data for existing users. The
        # canonical display name is owned by the directory profile; an
        # inbound senderNick must never overwrite it.
        updated = False
        if not platform_user.source or platform_user.source == "web":
            platform_user.source = "dingtalk"
            updated = True
        # 补充 mobile/email（通讯录获取的信息写入已有用户的 Identity）
        if platform_user.identity:
            if dt_mobile and not platform_user.identity.phone:
                _claimed_phone_r = await db.execute(
                    _select(_IdentityModel.id).where(
                        _IdentityModel.phone == dt_mobile,
                        _IdentityModel.id != platform_user.identity.id,
                    ).limit(1)
                )
                if _claimed_phone_r.scalar_one_or_none() is None:
                    platform_user.identity.phone = dt_mobile
                    updated = True
                else:
                    logger.warning(
                        "[DingTalk] Skipped identity phone backfill because the number "
                        "is already claimed by another identity"
                    )
            if dt_email and (not platform_user.identity.email or platform_user.identity.email.endswith((".local",))):
                from sqlalchemy import func as _func

                _claimed_email_r = await db.execute(
                    _select(_IdentityModel.id).where(
                        _func.lower(_IdentityModel.email) == dt_email.lower(),
                        _IdentityModel.id != platform_user.identity.id,
                    ).limit(1)
                )
                if _claimed_email_r.scalar_one_or_none() is None:
                    platform_user.identity.email = dt_email
                    updated = True
                else:
                    logger.warning(
                        "[DingTalk] Skipped identity email backfill because the address "
                        "is already claimed by another identity"
                    )
        if updated:
            await db.flush()

    # -- Ensure org_member record exists (for future Step 1 fast-path) --
    if _dingtalk_provider and directory_staff_id:
        if _sender_org_member:
            _existing_om = _sender_org_member
            if _existing_om.user_id != platform_user.id:
                _existing_om.user_id = platform_user.id
        else:
            _om_check_r = await db.execute(
                _select(OrgMember).where(
                    OrgMember.user_id == platform_user.id,
                    OrgMember.provider_id == _dingtalk_provider.id,
                ).order_by(OrgMember.synced_at.desc(), OrgMember.id.desc()).limit(1)
            )
            _existing_om = _om_check_r.scalar_one_or_none()
        if not _existing_om:
            # Create org_member so next message hits Step 1 directly
            _new_om = OrgMember(
                user_id=platform_user.id,
                provider_id=_dingtalk_provider.id,
                external_id=directory_staff_id,
                unionid=dt_unionid or None,
                phone=dt_mobile or None,
                email=dt_email or None,
                name=dt_real_name or platform_user.display_name or sender_nick or dt_username,
                nickname=sender_nick or None,
                status="active",
                tenant_id=agent_obj.tenant_id,
            )
            db.add(_new_om)
            await db.flush()
            logger.info(f"[DingTalk] Created org_member for user {platform_user.username}, external_id={directory_staff_id}")
        else:
            updated_member = False
            if _existing_om.external_id != directory_staff_id:
                _existing_om.external_id = directory_staff_id
                updated_member = True
            if dt_unionid and not _existing_om.unionid:
                _existing_om.unionid = dt_unionid
                updated_member = True
            if dt_mobile and not _existing_om.phone:
                _existing_om.phone = dt_mobile
                updated_member = True
            if dt_email and not _existing_om.email:
                _existing_om.email = dt_email
                updated_member = True
            if dt_real_name and _existing_om.name != dt_real_name:
                _existing_om.name = dt_real_name
                updated_member = True
            if sender_nick and _existing_om.nickname != sender_nick:
                _existing_om.nickname = sender_nick
                updated_member = True
            if updated_member:
                await db.flush()
                logger.info("[DingTalk] Backfilled enterprise org member identity fields")

    return platform_user, _dingtalk_provider, directory_staff_id
