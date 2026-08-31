"""DingTalk registration device-flow client and payload helpers."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.config import get_settings
from app.models.channel_config import ChannelConfig
from app.models.dingtalk_provisioning import DingTalkChannelProvisioningSession
from app.services.dingtalk_credentials import dingtalk_credential_fingerprint
from app.services.dingtalk_provisioning_constants import (
    DINGTALK_PROVISIONING_OPERATION_INITIAL,
)
from app.services.dingtalk_provisioning_types import (
    DingTalkRegistrationError,
    PollingWindow,
)

def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _extract_payload(data: dict[str, Any]) -> dict[str, Any]:
    nested = data.get("data")
    if isinstance(nested, dict):
        merged = dict(nested)
        for key, value in data.items():
            if key not in {"data"} and key not in merged:
                merged[key] = value
        return merged
    return data


def _as_string(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _configured_dingtalk_fingerprint(config: ChannelConfig | None) -> str | None:
    if (
        config is None
        or not config.is_configured
        or not _as_string(config.app_id)
        or not _as_string(config.app_secret)
    ):
        return None
    return dingtalk_credential_fingerprint(config.app_id, config.app_secret)


def _session_operation(session: DingTalkChannelProvisioningSession) -> str:
    operation = _as_string((session.registration_result or {}).get("operation"))
    return operation or DINGTALK_PROVISIONING_OPERATION_INITIAL


def _session_baseline_fingerprint(session: DingTalkChannelProvisioningSession) -> str:
    return _as_string((session.registration_result or {}).get("baseline_fp"))


def _merge_registration_result(
    session: DingTalkChannelProvisioningSession,
    **values: Any,
) -> None:
    result = dict(session.registration_result or {})
    result.update(values)
    session.registration_result = result


def _bounded_polling_window(
    *,
    expires_in: int | float | str | None,
    interval: int | float | str | None,
    now: datetime | None = None,
) -> PollingWindow:
    settings = get_settings()
    base = _as_utc(now or _now())

    try:
        raw_ttl = int(float(expires_in if expires_in is not None else 7200))
    except (TypeError, ValueError):
        raw_ttl = 7200
    ttl = max(1, min(raw_ttl, settings.DINGTALK_PROVISIONING_MAX_TTL_SECONDS))

    try:
        raw_interval = int(math.ceil(float(interval if interval is not None else 3)))
    except (TypeError, ValueError):
        raw_interval = 3
    poll_interval = max(
        settings.DINGTALK_PROVISIONING_MIN_INTERVAL_SECONDS,
        min(raw_interval, settings.DINGTALK_PROVISIONING_MAX_INTERVAL_SECONDS),
    )

    # Keep the persisted count as observability metadata. The authorization
    # deadline is the only business stop condition; a second, shorter attempt
    # cap would make a still-valid DingTalk link fail early.
    max_attempts = max(1, math.ceil(ttl / poll_interval))
    return PollingWindow(
        expires_at=base + timedelta(seconds=ttl),
        next_poll_at=base + timedelta(seconds=poll_interval),
        poll_interval_seconds=poll_interval,
        max_poll_attempts=max_attempts,
    )


class DingTalkRegistrationClient:
    """Client for DingTalk app registration device flow."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        source: str | None = None,
        timeout_seconds: float = 15,
    ):
        settings = get_settings()
        self.base_url = (base_url or settings.DINGTALK_REGISTRATION_BASE_URL).rstrip("/")
        self.source = source or settings.DINGTALK_REGISTRATION_SOURCE
        self.timeout_seconds = timeout_seconds

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(url, json=payload)
        data = response.json()
        if response.status_code >= 400:
            raise DingTalkRegistrationError(f"DingTalk registration HTTP {response.status_code}: {path}")
        errcode = data.get("errcode")
        if errcode not in (None, 0, "0"):
            errmsg = data.get("errmsg") or data.get("message") or "unknown error"
            raise DingTalkRegistrationError(f"DingTalk registration API error {errcode}: {errmsg}")
        return _extract_payload(data)

    async def begin(self) -> dict[str, Any]:
        init_data = await self._post("/app/registration/init", {"source": self.source})
        nonce = _as_string(init_data.get("nonce"))
        if not nonce:
            raise DingTalkRegistrationError("DingTalk registration init response missing nonce")

        begin_data = await self._post("/app/registration/begin", {"nonce": nonce})
        device_code = _as_string(begin_data.get("device_code"))
        authorization_url = _as_string(begin_data.get("verification_uri_complete"))
        if not device_code:
            raise DingTalkRegistrationError("DingTalk registration begin response missing device_code")
        if not authorization_url:
            raise DingTalkRegistrationError("DingTalk registration begin response missing verification_uri_complete")
        return {
            "device_code": device_code,
            "verification_uri_complete": authorization_url,
            "verification_uri": _as_string(begin_data.get("verification_uri")) or None,
            "expires_in": begin_data.get("expires_in", 7200),
            "interval": begin_data.get("interval", 3),
        }

    async def poll(self, device_code: str) -> dict[str, Any]:
        data = await self._post("/app/registration/poll", {"device_code": device_code})
        status = _as_string(data.get("status")).upper()
        return {
            "status": status or "FAIL",
            "client_id": _as_string(data.get("client_id")) or _as_string(data.get("clientId")),
            "client_secret": _as_string(data.get("client_secret")) or _as_string(data.get("clientSecret")),
            "agent_id": _as_string(data.get("agent_id")) or _as_string(data.get("agentId")),
            "message": _as_string(data.get("fail_reason"))
            or _as_string(data.get("failReason"))
            or _as_string(data.get("errmsg"))
            or _as_string(data.get("message")),
        }


def _default_registration_client() -> DingTalkRegistrationClient:
    return DingTalkRegistrationClient()

