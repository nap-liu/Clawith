"""DingTalk enterprise-directory credential and identity enrichment tests."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from loguru import logger
from sqlalchemy import select

from app.api.dingtalk import (
    _get_dingtalk_user_detail_with_fallback,
    _resolve_dingtalk_directory_credentials,
    process_dingtalk_message,
)
from app.database import async_session, engine
from app.models.agent import Agent
from app.models.channel_config import ChannelConfig
from app.models.identity import IdentityProvider
from app.models.llm import LLMModel
from app.models.org import OrgMember
from app.models.tenant import Tenant
from app.models.user import Identity, User


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    yield
    await engine.dispose()


def test_directory_credentials_prefer_enterprise_then_agent_robot():
    provider = SimpleNamespace(config={"app_key": "enterprise-key", "app_secret": "enterprise-secret"})
    channel = SimpleNamespace(app_id="robot-key", app_secret="robot-secret")

    assert _resolve_dingtalk_directory_credentials(provider, channel) == [
        ("enterprise-key", "enterprise-secret", "enterprise"),
        ("robot-key", "robot-secret", "robot_fallback"),
    ]


def test_directory_credentials_fall_back_when_enterprise_config_is_incomplete():
    provider = SimpleNamespace(config={"app_key": "enterprise-key"})
    channel = SimpleNamespace(app_id="robot-key", app_secret="robot-secret")

    assert _resolve_dingtalk_directory_credentials(provider, channel) == [
        ("robot-key", "robot-secret", "robot_fallback")
    ]


def test_directory_credentials_do_not_call_the_same_app_twice():
    provider = SimpleNamespace(config={"app_key": "shared-key", "app_secret": "shared-secret"})
    channel = SimpleNamespace(app_id="shared-key", app_secret="shared-secret")

    assert _resolve_dingtalk_directory_credentials(provider, channel) == [("shared-key", "shared-secret", "enterprise")]


@pytest.mark.asyncio
async def test_directory_lookup_falls_back_and_never_logs_identity_values(monkeypatch):
    calls: list[tuple[str, str, str]] = []
    mobile = "8613800004321"
    email = "private-person@example.test"

    async def fake_get_detail(app_key: str, app_secret: str, staff_id: str):
        calls.append((app_key, app_secret, staff_id))
        if app_key == "enterprise-key":
            return {"unionid": "union-private", "email": email, "mobile": ""}
        return {"mobile": mobile}

    monkeypatch.setattr("app.api.dingtalk._get_dingtalk_user_detail", fake_get_detail)
    messages: list[str] = []
    sink_id = logger.add(lambda message: messages.append(str(message)))
    try:
        detail = await _get_dingtalk_user_detail_with_fallback(
            [
                ("enterprise-key", "enterprise-secret", "enterprise"),
                ("robot-key", "robot-secret", "robot_fallback"),
            ],
            "staff-private",
            uuid.uuid4(),
        )
    finally:
        logger.remove(sink_id)

    assert detail == {
        "unionid": "union-private",
        "email": email,
        "mobile": mobile,
    }
    assert calls == [
        ("enterprise-key", "enterprise-secret", "staff-private"),
        ("robot-key", "robot-secret", "staff-private"),
    ]
    rendered_logs = "".join(messages)
    assert mobile not in rendered_logs
    assert email not in rendered_logs
    assert "union-private" not in rendered_logs
    assert "enterprise-secret" not in rendered_logs
    assert "robot-secret" not in rendered_logs
    assert "source=enterprise" in rendered_logs
    assert "source=robot_fallback" in rendered_logs


@pytest.mark.asyncio
async def test_directory_lookup_stops_when_enterprise_returns_mobile(monkeypatch):
    calls: list[str] = []

    async def fake_get_detail(app_key: str, _app_secret: str, _staff_id: str):
        calls.append(app_key)
        return {"mobile": "8613800009876"}

    monkeypatch.setattr("app.api.dingtalk._get_dingtalk_user_detail", fake_get_detail)

    detail = await _get_dingtalk_user_detail_with_fallback(
        [
            ("enterprise-key", "enterprise-secret", "enterprise"),
            ("robot-key", "robot-secret", "robot_fallback"),
        ],
        "staff-id",
    )

    assert detail == {"mobile": "8613800009876"}
    assert calls == ["enterprise-key"]


@pytest.mark.asyncio
@pytest.mark.parametrize("email_already_claimed", [False, True])
async def test_existing_dingtalk_user_is_enriched_with_enterprise_identity(
    monkeypatch,
    email_already_claimed,
):
    suffix = uuid.uuid4().hex[:10]
    sender_staff_id = f"staff_{suffix}"
    mobile = f"86139{uuid.uuid4().int % 10**8:08d}"
    real_email = f"person-{suffix}@example.test"
    enterprise_key = f"enterprise-key-{suffix}"
    enterprise_secret = f"enterprise-secret-{suffix}"
    robot_key = f"robot-key-{suffix}"
    robot_secret = f"robot-secret-{suffix}"

    async with async_session() as db:
        tenant = Tenant(name=f"DingTalk {suffix}", slug=f"dt-{suffix}")
        db.add(tenant)
        await db.flush()

        placeholder_email = f"dingtalk_{sender_staff_id}@dingtalk.local"
        identity = Identity(
            username=f"dingtalk_{sender_staff_id}",
            email=placeholder_email,
            password_hash="x",
        )
        db.add(identity)
        await db.flush()

        user = User(
            identity_id=identity.id,
            tenant_id=tenant.id,
            display_name="Existing DingTalk User",
            role="member",
            source="dingtalk",
            is_active=True,
        )
        db.add(user)
        await db.flush()

        if email_already_claimed:
            claimed_identity = Identity(
                username=f"email-owner-{suffix}",
                email=real_email,
                password_hash="x",
            )
            db.add(claimed_identity)
            await db.flush()
            db.add(
                User(
                    identity_id=claimed_identity.id,
                    tenant_id=tenant.id,
                    display_name="Existing Email Owner",
                    role="member",
                    source="web",
                    is_active=True,
                )
            )
            await db.flush()

        model = LLMModel(
            tenant_id=tenant.id,
            provider="openai",
            model="test-model",
            api_key_encrypted="unused",
            label=f"Test {suffix}",
            enabled=True,
            context_window=128000,
        )
        db.add(model)
        await db.flush()

        agent = Agent(
            name=f"DingTalk Agent {suffix}",
            creator_id=user.id,
            tenant_id=tenant.id,
            primary_model_id=model.id,
            status="idle",
        )
        db.add(agent)
        await db.flush()

        provider = IdentityProvider(
            tenant_id=tenant.id,
            provider_type="dingtalk",
            name=f"DingTalk Provider {suffix}",
            is_active=True,
            config={"app_key": enterprise_key, "app_secret": enterprise_secret},
        )
        db.add(provider)
        db.add(
            ChannelConfig(
                agent_id=agent.id,
                channel_type="dingtalk",
                app_id=robot_key,
                app_secret=robot_secret,
                is_configured=True,
            )
        )
        await db.commit()
        agent_id = agent.id
        user_id = user.id
        identity_id = identity.id
        provider_id = provider.id

    captured: dict = {}

    async def fake_directory_lookup(credentials, staff_id, selected_provider_id):
        captured["credentials"] = credentials
        captured["staff_id"] = staff_id
        captured["provider_id"] = selected_provider_id
        return {"unionid": f"union-{suffix}", "mobile": mobile, "email": real_email}

    async def fake_call_agent_llm(*_args, **_kwargs):
        return "identity enriched"

    class FakeResponse:
        status_code = 200
        text = "{}"

        def json(self):
            return {"ok": True}

    class FakeAsyncClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(
        "app.api.dingtalk._get_dingtalk_user_detail_with_fallback",
        fake_directory_lookup,
    )
    monkeypatch.setattr("app.services.channel_llm._call_agent_llm", fake_call_agent_llm)
    monkeypatch.setattr("httpx.AsyncClient", FakeAsyncClient)

    await process_dingtalk_message(
        agent_id=agent_id,
        sender_staff_id=sender_staff_id,
        user_text="please enrich my identity",
        conversation_id=f"conversation-{suffix}",
        conversation_type="1",
        session_webhook="https://example.invalid/dingtalk-webhook",
        sender_nick="Existing DingTalk User",
        message_id=f"message-{suffix}",
    )

    assert captured == {
        "credentials": [
            (enterprise_key, enterprise_secret, "enterprise"),
            (robot_key, robot_secret, "robot_fallback"),
        ],
        "staff_id": sender_staff_id,
        "provider_id": provider_id,
    }

    async with async_session() as db:
        identity = await db.get(Identity, identity_id)
        member = (
            await db.execute(
                select(OrgMember).where(
                    OrgMember.provider_id == provider_id,
                    OrgMember.external_id == sender_staff_id,
                )
            )
        ).scalar_one()

    assert identity.phone == mobile
    assert identity.email == (placeholder_email if email_already_claimed else real_email)
    assert member.user_id == user_id
    assert member.phone == mobile
    assert member.email == real_email
    assert member.unionid == f"union-{suffix}"
