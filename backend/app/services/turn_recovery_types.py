"""Shared recovery identity and configuration types."""

import os
import uuid
from dataclasses import dataclass

from loguru import logger

DEFAULT_RECOVERY_MAX_AGE_HOURS = 2.0


@dataclass
class RecoveryStats:
    scanned: int = 0
    resumed: int = 0
    skipped: int = 0
    failed: int = 0


class _RecoveryFenceLost(RuntimeError):
    """The durable owner or route changed while recovery was running."""


@dataclass(frozen=True)
class _RecoveryOrigin:
    session_found: bool
    source_channel: str | None
    external_conv_id: str | None
    turn_anchor_id: uuid.UUID | None = None
    turn_generation: int = 0


def _recovery_max_age_hours() -> float:
    raw = os.environ.get("TURN_RECOVERY_MAX_AGE_HOURS")
    if raw is None or raw.strip() == "":
        return DEFAULT_RECOVERY_MAX_AGE_HOURS
    try:
        value = float(raw)
    except ValueError:
        logger.warning(f"[turn_recovery] invalid TURN_RECOVERY_MAX_AGE_HOURS={raw!r}; using default")
        return DEFAULT_RECOVERY_MAX_AGE_HOURS
    return max(value, 0.0)


