"""Authentication and replay guards for inbound channel webhooks."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from jose import jwt
from jose.exceptions import JOSEError

from app.core.events import get_redis


class WebhookVerificationError(ValueError):
    """The inbound request could not be authenticated."""


_TEAMS_OPENID_URL = "https://login.botframework.com/v1/.well-known/openidconfiguration"
_TEAMS_ISSUER = "https://api.botframework.com"
_teams_jwks: dict | None = None
_teams_jwks_expires_at = 0.0


async def _get_teams_jwks() -> dict:
    global _teams_jwks, _teams_jwks_expires_at
    now = time.monotonic()
    if _teams_jwks is not None and now < _teams_jwks_expires_at:
        return _teams_jwks
    async with httpx.AsyncClient(timeout=10) as client:
        metadata_response = await client.get(_TEAMS_OPENID_URL)
        metadata_response.raise_for_status()
        jwks_uri = metadata_response.json().get("jwks_uri")
        if not jwks_uri:
            raise WebhookVerificationError("Teams OpenID metadata has no jwks_uri")
        jwks_response = await client.get(jwks_uri)
        jwks_response.raise_for_status()
        _teams_jwks = jwks_response.json()
        _teams_jwks_expires_at = now + 3600
        return _teams_jwks


async def verify_teams_webhook(
    authorization: str | None, app_id: str | None, channel_id: str | None = None
) -> dict:
    """Verify a Bot Framework bearer JWT for this configured bot audience."""
    if not app_id:
        raise WebhookVerificationError("Teams bot app_id is not configured")
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise WebhookVerificationError("Missing Teams bearer token")
    try:
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256" or not header.get("kid"):
            raise WebhookVerificationError("Unsupported Teams JWT header")
        jwks = await _get_teams_jwks()
        key = next((candidate for candidate in jwks.get("keys", []) if candidate.get("kid") == header["kid"]), None)
        if key is None:
            # A key rotation can happen before the one-hour cache expires.
            global _teams_jwks_expires_at
            _teams_jwks_expires_at = 0
            jwks = await _get_teams_jwks()
            key = next((candidate for candidate in jwks.get("keys", []) if candidate.get("kid") == header["kid"]), None)
        if key is None:
            raise WebhookVerificationError("Unknown Teams signing key")
        endorsements = key.get("endorsements") or []
        if endorsements and (not channel_id or channel_id not in endorsements):
            raise WebhookVerificationError("Teams signing key does not endorse this channel")
        return jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=app_id,
            issuer=_TEAMS_ISSUER,
            options={"require_aud": True, "require_exp": True, "leeway": 300},
        )
    except WebhookVerificationError:
        raise
    except (JOSEError, httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        raise WebhookVerificationError("Invalid Teams bearer token") from exc


def verify_teams_service_url(service_url: str | None, claims: dict) -> str:
    """Bind the outbound bearer-token destination to the authenticated activity."""
    if not service_url:
        raise WebhookVerificationError("Teams activity has no serviceUrl")
    parsed = urlparse(service_url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host or parsed.username or parsed.password:
        raise WebhookVerificationError("Invalid Teams serviceUrl")
    claimed = claims.get("serviceurl") or claims.get("serviceUrl")
    if not claimed:
        raise WebhookVerificationError("Teams bearer token has no serviceUrl claim")
    if claimed.rstrip("/") != service_url.rstrip("/"):
        raise WebhookVerificationError("Teams serviceUrl does not match bearer token")
    return service_url.rstrip("/")


def _decrypt_feishu_payload(encrypted: str, encrypt_key: str) -> dict:
    try:
        key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
        ciphertext = base64.b64decode(encrypted, validate=True)
        decryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        plaintext = unpadder.update(padded) + unpadder.finalize()
        value = json.loads(plaintext)
    except Exception as exc:
        raise WebhookVerificationError("Invalid Feishu encrypted payload") from exc
    if not isinstance(value, dict):
        raise WebhookVerificationError("Invalid Feishu payload")
    return value


def verify_feishu_webhook(body: dict, config, *, now: int | None = None) -> dict:
    """Decrypt (when enabled), authenticate and timestamp-check a Feishu callback."""
    if not isinstance(body, dict):
        raise WebhookVerificationError("Invalid Feishu payload")
    if "encrypt" in body:
        if not config.encrypt_key:
            raise WebhookVerificationError("Feishu encrypt_key is not configured")
        body = _decrypt_feishu_payload(str(body["encrypt"]), config.encrypt_key)

    header = body.get("header") if isinstance(body.get("header"), dict) else {}
    supplied_token = header.get("token") or body.get("token")
    if not config.verification_token or not supplied_token or not hmac.compare_digest(
        str(supplied_token), str(config.verification_token)
    ):
        raise WebhookVerificationError("Invalid Feishu verification token")
    supplied_app_id = header.get("app_id")
    if supplied_app_id and config.app_id and not hmac.compare_digest(str(supplied_app_id), str(config.app_id)):
        raise WebhookVerificationError("Feishu app_id mismatch")

    if "challenge" not in body:
        event_id = header.get("event_id")
        if not event_id:
            raise WebhookVerificationError("Feishu event_id is required")
        try:
            created = int(header.get("create_time"))
        except (TypeError, ValueError) as exc:
            raise WebhookVerificationError("Feishu create_time is required") from exc
        current = int(time.time()) if now is None else now
        if created < current - 600 or created > current + 60:
            raise WebhookVerificationError("Feishu event timestamp is outside the replay window")
    return body


async def claim_command_event(provider: str, config_id: object, event_id: str | None) -> bool:
    """Atomically claim a command event across replicas for 24 hours."""
    if not event_id:
        raise WebhookVerificationError("Provider event id is required")
    redis = await get_redis()
    key = f"clawith:webhook-command:{provider}:{config_id}:{event_id}"
    return bool(await redis.set(key, "1", nx=True, ex=86400))
