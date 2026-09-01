"""Durable Subagent sessions, execution, messaging, and parent wake-up."""

from __future__ import annotations

import sys
from types import ModuleType

from app.services import subagent_runtime_lifecycle as _lifecycle
from app.services import subagent_runtime_parent_dispatch as _parent_dispatch
from app.services import subagent_runtime_parent_events as _parent_events
from app.services import subagent_runtime_parent_round as _parent_round
from app.services import subagent_runtime_project_common as _project_common
from app.services import subagent_runtime_project_dispatch as _project_dispatch
from app.services import subagent_runtime_project_enqueue as _project_enqueue
from app.services import subagent_runtime_project_leader as _project_leader
from app.services import subagent_runtime_shared as _shared
from app.services import subagent_runtime_tools as _tools
from app.services import subagent_runtime_worker_claim as _worker_claim
from app.services import subagent_runtime_worker_execute as _worker_execute
from app.services import subagent_runtime_worker_resume as _worker_resume

_EXPORT_MODULES = (
    _shared,
    _lifecycle,
    _tools,
    _worker_claim,
    _worker_resume,
    _worker_execute,
    _parent_events,
    _parent_round,
    _parent_dispatch,
    _project_common,
    _project_leader,
    _project_enqueue,
    _project_dispatch,
)

_SYNC_MODULES = _EXPORT_MODULES
_EXPORTED_NAMES: frozenset[str] = frozenset()


def _load_module_exports() -> None:
    global _EXPORTED_NAMES
    module = sys.modules[__name__]
    exported_names: set[str] = set()
    for source in _EXPORT_MODULES:
        for name, value in source.__dict__.items():
            if name.startswith("__"):
                continue
            module.__dict__[name] = value
            exported_names.add(name)
    _EXPORTED_NAMES = frozenset(exported_names)


def _prepare_export_modules() -> None:
    module = sys.modules[__name__]
    for value in module.__dict__.values():
        source_name = getattr(value, "__module__", "")
        if not isinstance(source_name, str):
            continue
        if not source_name.startswith("app.services.subagent_runtime") or source_name == __name__:
            continue
        try:
            value.__module__ = __name__
        except (AttributeError, TypeError):
            continue


def _sync_root_exports() -> None:
    module = sys.modules[__name__]
    for name in _EXPORTED_NAMES:
        value = module.__dict__[name]
        for target in _SYNC_MODULES:
            target.__dict__[name] = value


class _SubagentRuntimeModule(ModuleType):
    def __setattr__(self, name: str, value) -> None:
        super().__setattr__(name, value)
        if name.startswith("__") or name not in _EXPORTED_NAMES:
            return
        for target in _SYNC_MODULES:
            target.__dict__[name] = value


_load_module_exports()
_prepare_export_modules()
sys.modules[__name__].__class__ = _SubagentRuntimeModule
_sync_root_exports()

__all__ = [name for name in globals() if name != "__all__" and not name.startswith("__")]
