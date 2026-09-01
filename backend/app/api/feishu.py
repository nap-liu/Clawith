"""Compatibility facade for the structurally split Feishu API."""

import sys
import types

from app.api import feishu_events as _events
from app.api import feishu_files as _files
from app.api import feishu_routes as _routes
from app.api import feishu_shared as _shared
from app.api import feishu_turn as _turn

_IMPLEMENTATION_MODULES = (
    _shared,
    _routes,
    _files,
    _turn,
    _events,
)

_EXPORTS: dict[str, object] = {}
for _module in _IMPLEMENTATION_MODULES:
    _EXPORTS.update(
        {
            _name: _value
            for _name, _value in vars(_module).items()
            if not _name.startswith("__")
        }
    )

globals().update(_EXPORTS)

for _module in _IMPLEMENTATION_MODULES:
    for _value in vars(_module).values():
        if callable(_value) and getattr(_value, "__module__", None) == _module.__name__:
            _value.__module__ = __name__
    vars(_module).update(_EXPORTS)


class _FeishuModule(types.ModuleType):
    """Keep facade monkeypatches visible inside every implementation module."""

    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        if name in _EXPORTS:
            for implementation in _IMPLEMENTATION_MODULES:
                setattr(implementation, name, value)


sys.modules[__name__].__class__ = _FeishuModule

__all__ = [name for name in globals() if not name.startswith("__")]
