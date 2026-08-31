from __future__ import annotations

import asyncio
from contextvars import ContextVar

from app.services import agent_tools_outbound_core as outbound_core

MEDIA_DELIVERY_MAX_IN_FLIGHT = 4

if not hasattr(outbound_core, "_outbound_media_slots"):
    outbound_core._outbound_media_slots = asyncio.Semaphore(MEDIA_DELIVERY_MAX_IN_FLIGHT)
if not hasattr(outbound_core, "_outbound_media_connection"):
    outbound_core._outbound_media_connection = ContextVar(
        "outbound_media_connection", default=None
    )

_outbound_media_slots = outbound_core._outbound_media_slots
_outbound_media_connection = outbound_core._outbound_media_connection
_lock_outbound_operation = outbound_core._lock_outbound_operation
_outbound_operation_lock_id = outbound_core._outbound_operation_lock_id
_outbound_operation_lifecycle_lock = outbound_core._outbound_operation_lifecycle_lock
_locked_outbound_media_connection = outbound_core._locked_outbound_media_connection
_outbound_media_db_session = outbound_core._outbound_media_db_session

__all__ = [
    "MEDIA_DELIVERY_MAX_IN_FLIGHT",
    "_outbound_media_slots",
    "_outbound_media_connection",
    "_lock_outbound_operation",
    "_outbound_operation_lock_id",
    "_outbound_operation_lifecycle_lock",
    "_locked_outbound_media_connection",
    "_outbound_media_db_session",
]
