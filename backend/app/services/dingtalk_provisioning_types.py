"""Shared types for DingTalk automatic channel provisioning."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
import uuid

StreamStarter = Callable[[uuid.UUID, str, str], Awaitable[None]]
StreamStopper = Callable[[uuid.UUID], Awaitable[None]]
WelcomeSender = Callable[[str, str, str, str], Awaitable[dict[str, Any]]]

@dataclass(frozen=True)
class PollingWindow:
    expires_at: datetime
    next_poll_at: datetime
    poll_interval_seconds: int
    max_poll_attempts: int


@dataclass(frozen=True)
class WelcomeDeliveryClaim:
    provisioning_id: uuid.UUID
    message_id: uuid.UUID
    attempt_id: str
    dingtalk_user_id: str
    message: str


class DingTalkRegistrationError(RuntimeError):
    """Raised when DingTalk registration API responses are malformed or failed."""
