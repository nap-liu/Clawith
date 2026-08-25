"""Observable DingTalk interactive-card request behavior."""

from __future__ import annotations

import httpx
import pytest

from app.services import dingtalk_card

pytestmark = pytest.mark.asyncio


class _Response:
    def __init__(self, payload=None, status_code=200):
        self.status_code = status_code
        self.text = "ok"
        self._payload = payload or {
            "success": True,
            "result": {
                "outTrackId": "message.track-id",
                "deliverResults": [
                    {"spaceType": "IM_GROUP", "spaceId": "group-id", "success": True}
                ],
            },
        }

    def json(self):
        return self._payload


class _Client:
    def __init__(self, calls: list[dict], response_payload=None):
        self.calls = calls
        self.response_payload = response_payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, url, *, headers, json):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return _Response(self.response_payload)


@pytest.mark.parametrize(
    ("at_user_ids", "mention_text"),
    [
        ({"staff-zhangsan": "张三", "staff-lisi": "李四"}, "@张三 @李四"),
        ({"@ALL": "@ALL"}, "@所有人"),
    ],
)
async def test_message_card_renders_labels_and_delivers_native_mentions(
    monkeypatch,
    at_user_ids,
    mention_text,
):
    calls: list[dict] = []

    async def fake_token(*_args, **_kwargs):
        return "app-access-token"

    monkeypatch.setattr(dingtalk_card.dingtalk_token_manager, "get_token", fake_token)
    monkeypatch.setattr(
        dingtalk_card.httpx,
        "AsyncClient",
        lambda **_kwargs: _Client(calls),
    )

    result = await dingtalk_card.send_message_card(
        app_id="ding-app",
        app_secret="ding-secret",
        card_template_id="message-template.schema",
        out_track_id="message.track-id",
        content="## 发布通知\n今晚十点发布",
        external_conv_id="dingtalk_group_open-conversation-id",
        at_user_ids=at_user_ids,
    )

    assert result == "message.track-id"
    assert len(calls) == 1
    assert calls[0]["url"].endswith("/v1.0/card/instances/createAndDeliver")
    assert calls[0]["json"] == {
        "cardTemplateId": "message-template.schema",
        "outTrackId": "message.track-id",
        "cardData": {
            "cardParamMap": {
                "content": (
                    "## 发布通知\n今晚十点发布\n\n"
                    "<font colorTokenV2=common_blue1_color>"
                    f"{mention_text}</font>"
                )
            },
        },
        "callbackType": "STREAM",
        "openSpaceId": "dtv1.card//IM_GROUP.open-conversation-id",
        "imGroupOpenSpaceModel": {
            "supportForward": False,
            "lastMessageI18n": {
                "ZH_CN": f"发布通知 今晚十点发布 {mention_text}",
                "EN_US": f"发布通知 今晚十点发布 {mention_text}",
            },
        },
        "imGroupOpenDeliverModel": {
            "robotCode": "ding-app",
            "atUserIds": at_user_ids,
        },
        "userIdType": 1,
    }


async def test_message_card_escapes_visible_mention_labels(monkeypatch):
    calls: list[dict] = []

    async def fake_token(*_args, **_kwargs):
        return "app-access-token"

    monkeypatch.setattr(dingtalk_card.dingtalk_token_manager, "get_token", fake_token)
    monkeypatch.setattr(
        dingtalk_card.httpx,
        "AsyncClient",
        lambda **_kwargs: _Client(calls),
    )

    await dingtalk_card.send_message_card(
        app_id="ding-app",
        app_secret="ding-secret",
        card_template_id="message-template.schema",
        out_track_id="message.track-id",
        content="通知",
        external_conv_id="dingtalk_group_open-conversation-id",
        at_user_ids={"staff-id": '<张&李>"'},
    )

    body = calls[0]["json"]
    assert body["cardData"]["cardParamMap"]["content"].endswith(
        "<font colorTokenV2=common_blue1_color>"
        '@&lt;张&amp;李&gt;&quot;</font>'
    )
    assert body["imGroupOpenDeliverModel"]["atUserIds"] == {
        "staff-id": '<张&李>"'
    }


async def test_confirmation_card_keeps_shared_transport_without_mentions(monkeypatch):
    calls: list[dict] = []

    async def fake_token(*_args, **_kwargs):
        return "app-access-token"

    monkeypatch.setattr(dingtalk_card.dingtalk_token_manager, "get_token", fake_token)
    monkeypatch.setattr(
        dingtalk_card.httpx,
        "AsyncClient",
        lambda **_kwargs: _Client(calls),
    )

    result = await dingtalk_card.send_confirmation_card(
        app_id="ding-app",
        app_secret="ding-secret",
        card_template_id="confirmation-template.schema",
        out_track_id="confirmation.track-id",
        card_data={"title": "确认", "summary": "请确认"},
        external_conv_id="dingtalk_group_open-conversation-id",
        is_group=True,
    )

    assert result == "confirmation.track-id"
    assert calls[1]["json"]["imGroupOpenDeliverModel"] == {
        "robotCode": "ding-app",
    }


async def test_message_card_rejects_empty_mention_map_before_provider_call(monkeypatch):
    async def fail_token(*_args, **_kwargs):
        raise AssertionError("provider token must not be requested")

    monkeypatch.setattr(dingtalk_card.dingtalk_token_manager, "get_token", fail_token)

    result = await dingtalk_card.send_message_card(
        app_id="ding-app",
        app_secret="ding-secret",
        card_template_id="message-template.schema",
        out_track_id="message.track-id",
        content="通知",
        external_conv_id="dingtalk_group_open-conversation-id",
        at_user_ids={},
    )

    assert result is None


async def test_message_card_rejects_failed_atomic_group_delivery(monkeypatch):
    calls: list[dict] = []

    async def fake_token(*_args, **_kwargs):
        return "app-access-token"

    monkeypatch.setattr(dingtalk_card.dingtalk_token_manager, "get_token", fake_token)
    monkeypatch.setattr(
        dingtalk_card.httpx,
        "AsyncClient",
        lambda **_kwargs: _Client(
            calls,
            {
                "success": True,
                "result": {
                    "outTrackId": "message.track-id",
                    "deliverResults": [
                        {
                            "spaceType": "IM_GROUP",
                            "spaceId": "group-id",
                            "success": False,
                            "errorMsg": "provider rejected",
                        }
                    ],
                },
            },
        ),
    )

    result = await dingtalk_card.send_message_card(
        app_id="ding-app",
        app_secret="ding-secret",
        card_template_id="message-template.schema",
        out_track_id="message.track-id",
        content="通知",
        external_conv_id="dingtalk_group_open-conversation-id",
        at_user_ids={"staff-zhangsan": "张三"},
    )

    assert len(calls) == 1
    assert result is None


async def test_message_card_transport_timeout_propagates_without_retry(monkeypatch):
    calls = []

    async def fake_token(*_args, **_kwargs):
        return "app-access-token"

    class _TimeoutClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, *, headers, json):
            calls.append({"url": url, "headers": headers, "json": json})
            raise httpx.ReadTimeout("provider response timed out")

    monkeypatch.setattr(dingtalk_card.dingtalk_token_manager, "get_token", fake_token)
    monkeypatch.setattr(
        dingtalk_card.httpx,
        "AsyncClient",
        lambda **_kwargs: _TimeoutClient(),
    )

    with pytest.raises(httpx.ReadTimeout):
        await dingtalk_card.send_message_card(
            app_id="ding-app",
            app_secret="ding-secret",
            card_template_id="message-template.schema",
            out_track_id="message.track-id",
            content="通知",
            external_conv_id="dingtalk_group_open-conversation-id",
            at_user_ids={"staff-zhangsan": "张三"},
        )

    assert len(calls) == 1


async def test_message_card_unreadable_response_is_uncertain_without_retry(monkeypatch):
    calls = []

    async def fake_token(*_args, **_kwargs):
        return "app-access-token"

    class _UnreadableResponse:
        status_code = 200
        text = "not-json"

        def json(self):
            raise ValueError("invalid provider JSON")

    class _UnreadableClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, *, headers, json):
            calls.append({"url": url, "headers": headers, "json": json})
            return _UnreadableResponse()

    monkeypatch.setattr(dingtalk_card.dingtalk_token_manager, "get_token", fake_token)
    monkeypatch.setattr(
        dingtalk_card.httpx,
        "AsyncClient",
        lambda **_kwargs: _UnreadableClient(),
    )

    with pytest.raises(dingtalk_card.ProviderResponseUncertainError):
        await dingtalk_card.send_message_card(
            app_id="ding-app",
            app_secret="ding-secret",
            card_template_id="message-template.schema",
            out_track_id="message.track-id",
            content="通知",
            external_conv_id="dingtalk_group_open-conversation-id",
            at_user_ids={"staff-zhangsan": "张三"},
        )

    assert len(calls) == 1
