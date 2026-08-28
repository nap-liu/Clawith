"""DingTalk interactive-card delivery.

Sends a DingTalk 互动卡片 (standard interactive card) into a durable ChatSession.
Confirmation cards retain the established two-step create + deliver transport.
Proactive group-message cards use DingTalk's atomic createAndDeliver transport:
real-client validation showed that native group mentions are lost on the
otherwise equivalent two-step route. Both derive the target from the stored
ChatSession instead of an inbound message or its short-lived session webhook.

callbackType is STREAM. Confirmation-card button clicks return over the existing
DingTalk Stream connection (see dingtalk_stream.py); proactive message cards contain
no actions and use the same transport without introducing another callback path.
"""

import html
import json
import logging
import re

import httpx

from app.services.dingtalk_token import dingtalk_token_manager
from app.services.im_delivery import ProviderResponseUncertainError

logger = logging.getLogger(__name__)

DINGTALK_OPENAPI = "https://api.dingtalk.com"
_CREATE_URL = f"{DINGTALK_OPENAPI}/v1.0/card/instances"
_DELIVER_URL = f"{DINGTALK_OPENAPI}/v1.0/card/instances/deliver"
_CREATE_AND_DELIVER_URL = f"{DINGTALK_OPENAPI}/v1.0/card/instances/createAndDeliver"
_UPDATE_URL = f"{DINGTALK_OPENAPI}/v1.0/card/instances"  # PUT

_SUMMARY_LIMIT = 100
_AUTO_LAYOUT_CARD_CONFIG = json.dumps(
    {"config": {"autoLayout": True}},
    separators=(",", ":"),
)
_MARKDOWN_LINK_RE = re.compile(r"!?\[([^]]*)\]\([^)]+\)")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_MARKDOWN_MARKER_RE = re.compile(r"(?:^|\s)[#>*+-]+\s*|[`*_~]+")


def _parse_target(external_conv_id: str, is_group: bool) -> tuple[str, str]:
    """Parse the DingTalk delivery target from ChatSession.external_conv_id.

    P2P:   'dingtalk_p2p_{staffId}'        -> ('IM_ROBOT', staffId)
    group: 'dingtalk_group_{convId}'       -> ('IM_GROUP', convId)
    Archived sessions carry a '__archived_*' suffix — strip it.
    """
    raw = external_conv_id or ""
    if "__archived" in raw:
        raw = raw.split("__archived")[0]
    if is_group or raw.startswith("dingtalk_group_"):
        return "IM_GROUP", raw[len("dingtalk_group_") :]
    return "IM_ROBOT", raw[len("dingtalk_p2p_") :]


# risk level -> standard CSS named color for the card title (the template binds
# `risk` to the title color, which takes a standard CSS named color).
_RISK_COLOR = {"high": "red", "medium": "orange", "low": "green"}


# DingTalk button colors are a fixed enum (NOT CSS colors — e.g. "green" renders black).
_VALID_BTN_COLORS = {"blue", "red", "gray"}

_DEFAULT_BUTTONS = [
    {"text": "取消", "value": "cancel", "color": "gray"},
    {"text": "确认", "value": "confirm", "color": "blue"},
]


def build_confirmation_card_data(
    *,
    title: str,
    summary: str,
    action_preview: str = "",
    risk_level: str = "medium",
    status: str = "",
    buttons: list | None = None,
    buttons_disabled: bool = False,
) -> dict:
    """Assemble cardParamMap. NB: DingTalk cardParamMap is Map<String,String> —
    every value MUST be a string (the buttons array is a JSON string).

    `buttons` are the agent-defined buttons [{text, value, color}], any number; None →
    default 确认/取消; [] → no buttons. Each maps to the template's data binding
    {text, color, status(normal|disabled), action(=value, the click-callback value)}.
    """
    btn_status = "disabled" if buttons_disabled else "normal"
    spec = _DEFAULT_BUTTONS if buttons is None else buttons
    card_buttons = [
        {
            "text": (b.get("text") or b.get("value") or ""),
            "color": (b.get("color") if b.get("color") in _VALID_BTN_COLORS else "blue"),
            "status": btn_status,
            "action": (b.get("value") or b.get("text") or ""),
        }
        for b in spec
        if isinstance(b, dict)
    ]
    return {
        "title": title or "需要确认",
        "summary": summary or "",
        "action_preview": action_preview or "",
        "risk": _RISK_COLOR.get((risk_level or "medium").lower(), _RISK_COLOR["medium"]),
        "status": status or "",
        "buttons": json.dumps(card_buttons, ensure_ascii=False),
    }


async def send_confirmation_card(
    *,
    app_id: str,
    app_secret: str,
    card_template_id: str,
    out_track_id: str,
    card_data: dict,
    external_conv_id: str,
    is_group: bool,
) -> str | None:
    """Create + deliver an interactive card. Returns outTrackId on success, else None.

    Best-effort: never raises (a delivery failure must not break confirmation creation).
    """
    return await _create_and_deliver_card(
        app_id=app_id,
        app_secret=app_secret,
        card_template_id=card_template_id,
        out_track_id=out_track_id,
        card_data=card_data,
        external_conv_id=external_conv_id,
        is_group=is_group,
    )


async def send_message_card(
    *,
    app_id: str,
    app_secret: str,
    card_template_id: str,
    out_track_id: str,
    content: str,
    external_conv_id: str,
    at_user_ids: dict[str, str],
) -> str | None:
    """Send one group Markdown card with matching visible and native mentions.

    DingTalk accepts ``atUserIds`` independently from the card data, but the
    mention label is not injected into a template's Markdown field for us.
    Keep both representations aligned at the transport boundary: the card
    renders the human-readable label while the delivery model carries the
    provider IDs used for notification routing.
    """
    if not at_user_ids:
        logger.warning("[DingTalkCard] message card requires at least one mention target")
        return None
    if "@ALL" in at_user_ids:
        mention_text = "@所有人"
    else:
        mention_text = " ".join(f"@{name}" for name in at_user_ids.values() if name)
    escaped_mention = html.escape(mention_text)
    mention_markup = (
        f"<font colorTokenV2=common_blue1_color>{escaped_mention}</font>"
        if escaped_mention
        else ""
    )
    body_content = (content or "").rstrip()
    rendered_content = (
        f"{body_content}\n\n{mention_markup}" if body_content and mention_markup else mention_markup
    )
    summary = _build_message_summary(content, mention_text)
    return await _create_and_deliver_message_card(
        app_id=app_id,
        app_secret=app_secret,
        card_template_id=card_template_id,
        out_track_id=out_track_id,
        card_data={
            "content": rendered_content,
            # DingTalk cardParamMap values must be strings. This built-in field
            # carries non-string public card data and enables responsive width.
            "sys_full_json_obj": _AUTO_LAYOUT_CARD_CONFIG,
        },
        external_conv_id=external_conv_id,
        at_user_ids=at_user_ids,
        summary=summary,
    )


def _build_message_summary(content: str, mention_text: str) -> str:
    """Build a compact plain-text conversation preview for DingTalk clients."""
    plain_content = _MARKDOWN_LINK_RE.sub(r"\1", content or "")
    plain_content = _HTML_TAG_RE.sub("", plain_content)
    plain_content = html.unescape(plain_content)
    plain_content = _MARKDOWN_MARKER_RE.sub(" ", plain_content)
    preview = " ".join(part for part in (plain_content, mention_text) if part)
    preview = " ".join(preview.split())
    if len(preview) <= _SUMMARY_LIMIT:
        return preview
    return f"{preview[: _SUMMARY_LIMIT - 1].rstrip()}…"


async def _create_and_deliver_message_card(
    *,
    app_id: str,
    app_secret: str,
    card_template_id: str,
    out_track_id: str,
    card_data: dict,
    external_conv_id: str,
    at_user_ids: dict[str, str],
    summary: str,
) -> str | None:
    """Atomically create and deliver one proactive group card with native mentions.

    Keep this request deliberately minimal. In real DingTalk clients, adding
    ``imGroupOpenSpaceModel.notification`` or splitting create and deliver into
    separate calls produced a visible card but suppressed the native @ behavior.
    ``userIdType`` must remain explicit because mention keys are DingTalk userIds.
    """
    try:
        token = await dingtalk_token_manager.get_token(app_id, app_secret)
        if not token:
            logger.warning("[DingTalkCard] no access token for app %s — skip card send", app_id[:10])
            return None
        _, space_id = _parse_target(external_conv_id, True)
        if not space_id:
            logger.warning("[DingTalkCard] message-card target is empty")
            return None

        body = {
            "cardTemplateId": card_template_id,
            "outTrackId": out_track_id,
            "cardData": {"cardParamMap": card_data},
            "callbackType": "STREAM",
            "openSpaceId": f"dtv1.card//IM_GROUP.{space_id}",
            "imGroupOpenSpaceModel": {
                "supportForward": False,
                "lastMessageI18n": {
                    "ZH_CN": summary,
                    "EN_US": summary,
                },
            },
            "imGroupOpenDeliverModel": {
                "robotCode": app_id,
                "atUserIds": dict(at_user_ids),
            },
            "userIdType": 1,
        }
        headers = {
            "Content-Type": "application/json",
            "x-acs-dingtalk-access-token": token,
        }
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                _CREATE_AND_DELIVER_URL,
                headers=headers,
                json=body,
            )
        if response.status_code >= 300:
            logger.warning(
                "[DingTalkCard] createAndDeliver failed %s: %s",
                response.status_code,
                response.text[:300],
            )
            return None
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderResponseUncertainError(
                "DingTalk card delivery returned an unreadable response"
            ) from exc
        result = payload.get("result") if isinstance(payload, dict) else None
        deliveries = result.get("deliverResults") if isinstance(result, dict) else None
        if payload.get("success") is not True or not isinstance(deliveries, list):
            logger.warning("[DingTalkCard] createAndDeliver returned no successful delivery")
            return None
        if not deliveries or any(item.get("success") is not True for item in deliveries):
            logger.warning("[DingTalkCard] createAndDeliver group delivery was rejected")
            return None
        logger.info("[DingTalkCard] atomically sent mention card %s to IM_GROUP.%s", out_track_id, space_id)
        return out_track_id
    except Exception:
        logger.exception("[DingTalkCard] atomic create-and-deliver failed")
        raise


async def _create_and_deliver_card(
    *,
    app_id: str,
    app_secret: str,
    card_template_id: str,
    out_track_id: str,
    card_data: dict,
    external_conv_id: str,
    is_group: bool,
) -> str | None:
    """Create and deliver one card without relying on an inbound webhook."""
    try:
        token = await dingtalk_token_manager.get_token(app_id, app_secret)
        if not token:
            logger.warning("[DingTalkCard] no access token for app %s — skip card send", app_id[:10])
            return None
        headers = {"Content-Type": "application/json", "x-acs-dingtalk-access-token": token}

        create_body = {
            "cardTemplateId": card_template_id,
            "outTrackId": out_track_id,
            "cardData": {"cardParamMap": card_data},
            "callbackType": "STREAM",
            "imRobotOpenSpaceModel": {"supportForward": False},
            "imGroupOpenSpaceModel": {"supportForward": False},
        }

        space_type, space_id = _parse_target(external_conv_id, is_group)
        if not space_id:
            logger.warning("[DingTalkCard] card target is empty")
            return None
        deliver_body: dict = {
            "outTrackId": out_track_id,
            "userIdType": 1,
            "openSpaceId": f"dtv1.card//{space_type}.{space_id}",
        }
        if space_type == "IM_GROUP":
            deliver_body["imGroupOpenDeliverModel"] = {"robotCode": app_id}
        else:
            deliver_body["imRobotOpenDeliverModel"] = {"spaceType": "IM_ROBOT"}

        async with httpx.AsyncClient(timeout=15) as client:
            r1 = await client.post(_CREATE_URL, headers=headers, json=create_body)
            if r1.status_code >= 300:
                logger.warning("[DingTalkCard] create failed %s: %s", r1.status_code, r1.text[:300])
                return None
            r2 = await client.post(_DELIVER_URL, headers=headers, json=deliver_body)
            if r2.status_code >= 300:
                logger.warning("[DingTalkCard] deliver failed %s: %s", r2.status_code, r2.text[:300])
                return None
        logger.info("[DingTalkCard] sent card %s to %s.%s", out_track_id, space_type, space_id)
        return out_track_id
    except Exception:
        logger.exception("[DingTalkCard] create-and-deliver failed")
        return None


async def update_confirmation_card(
    *,
    app_id: str,
    app_secret: str,
    out_track_id: str,
    card_data: dict,
) -> bool:
    """Update an existing card (e.g. flip status to executed/cancelled, drop buttons).
    Best-effort; never raises."""
    try:
        token = await dingtalk_token_manager.get_token(app_id, app_secret)
        if not token:
            return False
        headers = {"Content-Type": "application/json", "x-acs-dingtalk-access-token": token}
        body = {"outTrackId": out_track_id, "cardData": {"cardParamMap": card_data}}
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.put(_UPDATE_URL, headers=headers, json=body)
            if r.status_code >= 300:
                logger.warning("[DingTalkCard] update failed %s: %s", r.status_code, r.text[:300])
                return False
        return True
    except Exception:
        logger.exception("[DingTalkCard] update_confirmation_card failed")
        return False
