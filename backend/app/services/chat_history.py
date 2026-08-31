"""Compatibility facade for the structurally split chat-history service."""

from __future__ import annotations

import sys
import types

from app.services import chat_history_assistant as _assistant
from app.services import chat_history_context as _context
from app.services import chat_history_ingest as _ingest
from app.services import chat_history_loading as _loading
from app.services import chat_history_shared as _shared
from app.services import chat_history_tools as _tools

_IMPLEMENTATION_MODULES = (
    _shared,
    _loading,
    _ingest,
    _tools,
    _assistant,
    _context,
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


class _ChatHistoryModule(types.ModuleType):
    """Keep facade monkeypatches visible inside every implementation module."""

    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        for implementation in _IMPLEMENTATION_MODULES:
            if hasattr(implementation, name):
                setattr(implementation, name, value)


sys.modules[__name__].__class__ = _ChatHistoryModule

__all__ = [name for name in globals() if not name.startswith("__")]
