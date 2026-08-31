"""Compatibility facade for the structurally split trigger daemon."""

import sys
import types

from app.services import trigger_daemon_delivery as _delivery
from app.services import trigger_daemon_evaluation as _evaluation
from app.services import trigger_daemon_invocation as _invocation
from app.services import trigger_daemon_loop as _loop
from app.services import trigger_daemon_shared as _shared

_IMPLEMENTATION_MODULES = (
    _shared,
    _evaluation,
    _delivery,
    _invocation,
    _loop,
)

for _module in _IMPLEMENTATION_MODULES:
    globals().update(
        {
            _name: _value
            for _name, _value in vars(_module).items()
            if not _name.startswith("__")
        }
    )

for _module in _IMPLEMENTATION_MODULES:
    for _value in vars(_module).values():
        if callable(_value) and getattr(_value, "__module__", None) == _module.__name__:
            _value.__module__ = __name__


class _TriggerDaemonModule(types.ModuleType):
    """Keep facade monkeypatches visible inside every implementation module."""

    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        for implementation in _IMPLEMENTATION_MODULES:
            if hasattr(implementation, name):
                setattr(implementation, name, value)


sys.modules[__name__].__class__ = _TriggerDaemonModule

__all__ = [name for name in globals() if not name.startswith("__")]
