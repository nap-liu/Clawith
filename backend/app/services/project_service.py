"""Compatibility facade for tenant-safe project orchestration services."""

from __future__ import annotations

import uuid

from app.services import project_service_access as _access
from app.services import project_service_creation as _creation
from app.services import project_service_delivery as _delivery
from app.services import project_service_membership as _membership
from app.services import project_service_serialization as _serialization
from app.services import project_service_shared as _shared


for _module in (
    _shared,
    _access,
    _membership,
    _creation,
    _serialization,
    _delivery,
):
    globals().update(
        {
            _name: _value
            for _name, _value in vars(_module).items()
            if not _name.startswith("__")
        }
    )

for _name, _value in tuple(globals().items()):
    if callable(_value) and str(getattr(_value, "__module__", "")).startswith(
        "app.services.project_service_"
    ):
        _value.__module__ = __name__


_deliver_project_a2a_impl = _delivery.deliver_project_a2a


async def deliver_project_a2a(run_id: uuid.UUID) -> None:
    """Preserve root-module monkeypatch compatibility for A2A delivery."""

    _delivery.async_session = async_session
    _delivery.reconcile_project_run_terminal_state = reconcile_project_run_terminal_state
    await _deliver_project_a2a_impl(run_id)
