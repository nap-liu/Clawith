import base64
import json
import time
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from unittest.mock import AsyncMock
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app.schemas.channel_config import ChannelConfigPublic
from app.services.webhook_security import (
    WebhookVerificationError,
    verify_feishu_webhook,
    verify_teams_service_url,
)


def _channel_config(**overrides):
    values = {
        "id": uuid.uuid4(),
        "agent_id": uuid.uuid4(),
        "channel_type": "feishu",
        "app_id": "cli_app",
        "app_secret": "app-secret",
        "encrypt_key": "encrypt-secret",
        "verification_token": "verify-secret",
        "is_configured": True,
        "is_connected": False,
        "last_tested_at": None,
        "extra_config": {
            "connection_mode": "webhook",
            "nested": {"access_token": "must-not-leak", "region": "cn"},
        },
        "created_at": datetime.now(timezone.utc),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_channel_config_public_never_serializes_credentials():
    result = ChannelConfigPublic.model_validate(_channel_config()).model_dump()
    assert "app_secret" not in result
    assert "encrypt_key" not in result
    assert "verification_token" not in result
    assert "access_token" not in result["extra_config"]["nested"]
    assert result["extra_config"]["nested"]["region"] == "cn"
    assert result["has_app_secret"] is True


def test_teams_service_url_is_bound_to_claim_or_trusted_host():
    assert verify_teams_service_url(
        "https://smba.trafficmanager.net/amer/",
        {"serviceurl": "https://smba.trafficmanager.net/amer"},
    ) == "https://smba.trafficmanager.net/amer"
    assert verify_teams_service_url(
        "https://callback.example.test/v3", {"serviceurl": "https://callback.example.test/v3"}
    ) == "https://callback.example.test/v3"
    with pytest.raises(WebhookVerificationError):
        verify_teams_service_url("https://smba.trafficmanager.net/amer", {})
    with pytest.raises(WebhookVerificationError):
        verify_teams_service_url("http://smba.trafficmanager.net/amer", {})
    with pytest.raises(WebhookVerificationError):
        verify_teams_service_url("https://attacker.example/capture", {})
    with pytest.raises(WebhookVerificationError):
        verify_teams_service_url(
            "https://attacker.example/capture",
            {"serviceurl": "https://smba.trafficmanager.net/amer"},
        )


@pytest.mark.asyncio
async def test_teams_jwt_verifies_signature_audience_issuer_and_endorsement(monkeypatch):
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
    from jose import jwk, jwt
    from app.services import webhook_security

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    public_jwk = jwk.construct(public_pem, algorithm="RS256").to_dict()
    public_jwk.update({"kid": "key-1", "endorsements": ["msteams"]})

    async def fake_jwks():
        return {"keys": [public_jwk]}

    monkeypatch.setattr(webhook_security, "_get_teams_jwks", fake_jwks)
    claims = {
        "aud": "bot-app-id",
        "iss": "https://api.botframework.com",
        "nbf": int(time.time()) - 1,
        "exp": int(time.time()) + 60,
        "serviceurl": "https://smba.trafficmanager.net/amer",
    }
    token = jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": "key-1"})
    verified = await webhook_security.verify_teams_webhook(
        f"Bearer {token}", "bot-app-id", "msteams"
    )
    assert verified["serviceurl"] == claims["serviceurl"]

    with pytest.raises(WebhookVerificationError):
        await webhook_security.verify_teams_webhook(
            f"Bearer {token}", "another-app-id", "msteams"
        )
    with pytest.raises(WebhookVerificationError):
        await webhook_security.verify_teams_webhook(
            f"Bearer {token}", "bot-app-id", "unendorsed-channel"
        )


@pytest.mark.asyncio
async def test_command_replay_claim_is_atomic_and_namespaced(monkeypatch):
    from app.services import webhook_security

    calls = []

    class Redis:
        async def set(self, *args, **kwargs):
            calls.append((args, kwargs))
            return True

    async def fake_redis():
        return Redis()

    monkeypatch.setattr(webhook_security, "get_redis", fake_redis)
    assert await webhook_security.claim_command_event("teams", "cfg-1", "event-1") is True
    assert calls == [
        (("clawith:webhook-command:teams:cfg-1:event-1", "1"), {"nx": True, "ex": 86400})
    ]
    with pytest.raises(WebhookVerificationError):
        await webhook_security.claim_command_event("teams", "cfg-1", None)


def test_feishu_challenge_requires_matching_token():
    config = _channel_config()
    assert verify_feishu_webhook(
        {"challenge": "ok", "token": "verify-secret"}, config
    )["challenge"] == "ok"
    with pytest.raises(WebhookVerificationError):
        verify_feishu_webhook({"challenge": "leak", "token": "wrong"}, config)


def test_feishu_event_requires_id_and_fresh_timestamp():
    config = _channel_config()
    valid = {
        "header": {
            "token": "verify-secret",
            "app_id": "cli_app",
            "event_id": "evt-1",
            "create_time": "1000",
        },
        "event": {},
    }
    assert verify_feishu_webhook(valid, config, now=1000) == valid
    with pytest.raises(WebhookVerificationError):
        verify_feishu_webhook({**valid, "header": {**valid["header"], "create_time": "1"}}, config, now=1000)
    with pytest.raises(WebhookVerificationError):
        verify_feishu_webhook({**valid, "header": {**valid["header"], "event_id": ""}}, config, now=1000)


def test_feishu_encrypted_payload_is_decrypted_then_authenticated():
    config = _channel_config()
    plaintext = json.dumps({"challenge": "ok", "token": "verify-secret"}).encode()
    import hashlib

    key = hashlib.sha256(config.encrypt_key.encode()).digest()
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
    encrypted = base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode()
    assert verify_feishu_webhook({"encrypt": encrypted}, config)["challenge"] == "ok"


def test_trigger_config_redaction_and_reserved_field_rejection():
    from app.api.triggers import (
        _contains_private_config,
        _merge_private_config,
        _private_config,
        _public_config,
    )

    source = {
        "schedule": "daily",
        "_webhook_queue": ["internal"],
        "nested": {"access_token": "secret", "event": "push"},
    }
    assert _public_config(source) == {"schedule": "daily", "nested": {"event": "push"}}
    assert _contains_private_config(source) is True
    assert _contains_private_config({"schedule": "daily"}) is False
    assert _merge_private_config(
        {"nested": {"event": "merge"}}, _private_config(source)
    ) == {
        "_webhook_queue": ["internal"],
        "nested": {"event": "merge", "access_token": "secret"},
    }


def test_confirmation_actor_binding_rejects_forwarded_card():
    from app.services.confirmation_service import (
        ConfirmationActorMismatch,
        _assert_confirmation_actor,
    )

    intended = uuid.uuid4()
    row = SimpleNamespace(user_id=intended)
    _assert_confirmation_actor(row, {"intended_user_id": str(intended)}, intended)
    with pytest.raises(ConfirmationActorMismatch):
        _assert_confirmation_actor(row, {"intended_user_id": str(intended)}, uuid.uuid4())


@pytest.mark.asyncio
async def test_org_admin_is_tenant_scoped_and_private_agents_stay_private():
    from fastapi import HTTPException
    from app.core.permissions import check_agent_access

    class Result:
        def __init__(self, scalar=None, scalars=None):
            self.scalar = scalar
            self._scalars = scalars or []

        def scalar_one_or_none(self):
            return self.scalar

        def scalars(self):
            return SimpleNamespace(all=lambda: self._scalars)

    class DB:
        def __init__(self, agent):
            self.agent = agent
            self.calls = 0

        async def execute(self, _statement):
            self.calls += 1
            return Result(self.agent) if self.calls == 1 else Result(scalars=[])

    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    admin = SimpleNamespace(id=uuid.uuid4(), role="org_admin", tenant_id=tenant_a)
    foreign = SimpleNamespace(
        id=uuid.uuid4(), tenant_id=tenant_b, creator_id=uuid.uuid4(),
        access_mode="company", company_access_level="use",
    )
    with pytest.raises(HTTPException) as exc:
        await check_agent_access(DB(foreign), admin, foreign.id)
    assert exc.value.status_code == 403

    same_tenant = SimpleNamespace(
        id=uuid.uuid4(), tenant_id=tenant_a, creator_id=uuid.uuid4(),
        access_mode="company", company_access_level="use",
    )
    _, level = await check_agent_access(DB(same_tenant), admin, same_tenant.id)
    assert level == "manage"

    private = SimpleNamespace(
        id=uuid.uuid4(), tenant_id=tenant_a, creator_id=uuid.uuid4(),
        access_mode="private", company_access_level=None,
    )
    with pytest.raises(HTTPException) as exc:
        await check_agent_access(DB(private), admin, private.id)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_approval_compare_and_swap_allows_only_one_execution(monkeypatch):
    from sqlalchemy.sql.dml import Update
    from app.models.agent import Agent
    from app.models.audit import ApprovalRequest
    from app.services.autonomy_service import AutonomyService
    from app.services import notification_service

    approval_id, agent_id, creator_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    approval = SimpleNamespace(
        id=approval_id,
        agent_id=agent_id,
        status="pending",
        action_type="tool_call",
        details={"tool": "dangerous_tool", "args": {}},
    )
    agent = SimpleNamespace(id=agent_id, creator_id=creator_id, name="gate-test")
    user = SimpleNamespace(id=creator_id, role="user")

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class DB:
        def __init__(self):
            self.claimed = False
            self.added = []

        async def execute(self, statement):
            if isinstance(statement, Update):
                if self.claimed:
                    return Result(None)
                self.claimed = True
                return Result(approval_id)
            entity = statement.column_descriptions[0].get("entity")
            return Result(approval if entity is ApprovalRequest else agent if entity is Agent else None)

        def add(self, value):
            self.added.append(value)

        async def commit(self):
            return None

        async def rollback(self):
            return None

        async def flush(self):
            return None

        async def get(self, _model, _id):
            return approval

    service = AutonomyService()
    execute_action = AsyncMock(return_value="executed")
    monkeypatch.setattr(service, "_execute_approved_action", execute_action)
    monkeypatch.setattr(notification_service, "send_notification", AsyncMock())
    db = DB()

    await service.resolve_approval(db, approval_id, user, "approve")
    with pytest.raises(ValueError, match="already resolved"):
        await service.resolve_approval(db, approval_id, user, "approve")
    execute_action.assert_awaited_once()


class _WebhookConfigResult:
    def __init__(self, config):
        self.config = config

    def scalar_one_or_none(self):
        return self.config


class _WebhookDB:
    def __init__(self, config):
        self.config = config

    async def execute(self, _statement):
        return _WebhookConfigResult(self.config)


@pytest.mark.asyncio
async def test_feishu_endpoint_rejects_before_event_processing(monkeypatch):
    from app.api import feishu as feishu_api

    config = _channel_config()
    request = SimpleNamespace(
        json=AsyncMock(
            return_value={
                "header": {
                    "token": "wrong-token",
                    "app_id": config.app_id,
                    "event_id": "evt-rejected",
                    "create_time": str(int(time.time())),
                },
                "event": {},
            }
        )
    )
    process_event = AsyncMock()
    monkeypatch.setattr(feishu_api, "process_feishu_event", process_event)

    response = await feishu_api.feishu_event_webhook(
        config.agent_id, request, _WebhookDB(config)
    )
    assert response.status_code == 401
    process_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_teams_endpoint_rejects_before_identity_resolution(monkeypatch):
    from app.api import teams as teams_api
    from app.services import channel_user_service, webhook_security

    config = _channel_config(
        channel_type="microsoft_teams",
        app_id="teams-bot-app",
        encrypt_key=None,
        verification_token=None,
    )
    activity = {
        "type": "message",
        "id": "activity-rejected",
        "channelId": "msteams",
        "serviceUrl": "https://smba.trafficmanager.net/amer/",
        "text": "must never be processed",
        "from": {"id": "aad-user"},
        "conversation": {"id": "conversation"},
    }
    request = SimpleNamespace(
        body=AsyncMock(return_value=json.dumps(activity).encode()),
        headers={},
    )
    verify = AsyncMock(
        side_effect=WebhookVerificationError("missing authorization")
    )
    resolve_user = AsyncMock()
    monkeypatch.setattr(webhook_security, "verify_teams_webhook", verify)
    monkeypatch.setattr(
        channel_user_service.channel_user_service,
        "resolve_channel_user",
        resolve_user,
    )

    response = await teams_api.teams_event_webhook(
        config.agent_id, request, _WebhookDB(config)
    )
    assert response.status_code == 401
    verify.assert_awaited_once()
    resolve_user.assert_not_awaited()
