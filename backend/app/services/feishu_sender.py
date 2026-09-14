"""Prepare the complete Feishu sender profile before admission or media."""

import httpx as _httpx
from loguru import logger
from app.services.channel_user_errors import ChannelUserResolutionError


async def feishu_sender_info(app_id, app_secret, sender_open_id, event_user_id="", event_union_id=""):
    sender_name = ""
    sender_user_id_feishu = event_user_id  # tenant-level user_id, pre-filled from event body
    extra_info: dict | None = {
        "open_id": sender_open_id,
        "external_id": sender_user_id_feishu or None,
        "unionid": event_union_id or None,
    }

    try:
        if not app_id or not app_secret:
            return extra_info
        async with _httpx.AsyncClient() as _client:
            _tok_resp = await _client.post(
                "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal",
                json={"app_id": app_id, "app_secret": app_secret},
            )
            _app_token = _tok_resp.json().get("app_access_token", "")
            if _app_token:
                _user_resp = await _client.get(
                    f"https://open.feishu.cn/open-apis/contact/v3/users/{sender_open_id}",
                    params={"user_id_type": "open_id"},
                    headers={"Authorization": f"Bearer {_app_token}"},
                )
                _user_data = _user_resp.json()
                logger.info(f"[Feishu] Sender resolve: code={_user_data.get('code')}, msg={_user_data.get('msg', '')}")
                if _user_data.get("code") == 0:
                    _user_info = _user_data.get("data", {}).get("user", {})
                    for key, observed in (("user_id", event_user_id), ("union_id", event_union_id)):
                        if observed and _user_info.get(key) and _user_info[key] != observed:
                            raise ChannelUserResolutionError("Feishu event and contact identities disagree")
                    sender_name = _user_info.get("name", "")
                    sender_user_id_feishu = _user_info.get("user_id", "")
                    sender_email = _user_info.get("email", "") or _user_info.get("enterprise_email", "")
                    # Feishu contact API returns 'avatar' as a dict
                    # (keys: avatar_240, avatar_640, avatar_origin), NOT a plain URL.
                    # We must extract a string to avoid a DataError when writing to the DB.
                    _raw_avatar = _user_info.get("avatar")
                    if isinstance(_raw_avatar, dict):
                        _avatar_url = (
                            _raw_avatar.get("avatar_240")
                            or _raw_avatar.get("avatar_640")
                            or _raw_avatar.get("avatar_origin")
                            or ""
                        )
                    else:
                        _avatar_url = _raw_avatar or ""
                    extra_info = {
                        "name": sender_name,
                        "email": sender_email,
                        "mobile": _user_info.get("mobile"),
                        "avatar_url": _avatar_url,
                        "external_id": _user_info.get("user_id") or event_user_id or None,
                        "unionid": _user_info.get("union_id") or event_union_id or None,
                        "open_id": sender_open_id,
                    }
                    logger.info(f"[Feishu] Resolved sender: {sender_name} (user_id={sender_user_id_feishu})")
    except ChannelUserResolutionError:
        raise
    except Exception as e:
        logger.error(f"[Feishu] Failed to resolve sender: {e}")

    return extra_info
