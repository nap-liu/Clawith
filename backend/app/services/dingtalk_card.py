"""DingTalk interactive-card delivery for confirmation cards.

Sends a DingTalk 互动卡片 (standard interactive card) into the conversation that
triggered a confirmation, and updates it after the user resolves. Mirrors the
two-step create+deliver the `dingtalk_stream` SDK's CardReplier does, but derives
the delivery target from the stored ChatSession instead of an incoming message
(the confirmation is created deep in the LLM loop, far from the chat handler).

callbackType is STREAM — button clicks come back over the existing DingTalk Stream
connection (see dingtalk_stream.py), carrying outTrackId == the confirmation id.
"""

import json
import logging

import httpx

from app.services.dingtalk_token import dingtalk_token_manager

logger = logging.getLogger(__name__)

DINGTALK_OPENAPI = "https://api.dingtalk.com"
_CREATE_URL = f"{DINGTALK_OPENAPI}/v1.0/card/instances"
_DELIVER_URL = f"{DINGTALK_OPENAPI}/v1.0/card/instances/deliver"
_UPDATE_URL = f"{DINGTALK_OPENAPI}/v1.0/card/instances"  # PUT


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
        return "IM_GROUP", raw[len("dingtalk_group_"):]
    return "IM_ROBOT", raw[len("dingtalk_p2p_"):]


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
        logger.exception("[DingTalkCard] send_confirmation_card failed")
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
