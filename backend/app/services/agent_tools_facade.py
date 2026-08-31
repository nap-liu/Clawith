"""Shared compatibility helpers for structurally split agent tool modules."""

from __future__ import annotations

import sys
from types import ModuleType

_FACADE_STATE_KEY = "_agent_tools_facade_state"


class _AgentToolsFacadeModule(ModuleType):
    def __setattr__(self, name: str, value) -> None:
        super().__setattr__(name, value)
        if name.startswith("__"):
            return
        state = self.__dict__.get(_FACADE_STATE_KEY)
        if not state or name not in state["names"]:
            return
        for target in state["modules"]:
            target.__dict__[name] = value


def _facade_state(module_name: str) -> dict[str, object]:
    module = sys.modules[module_name]
    state = module.__dict__.get(_FACADE_STATE_KEY)
    if state is None:
        state = {"modules": [], "names": set()}
        module.__dict__[_FACADE_STATE_KEY] = state
    return state


def export_module_symbols(
    module_name: str,
    export_modules: tuple[ModuleType, ...],
    symbol_names: tuple[str, ...] | list[str],
) -> frozenset[str]:
    module = sys.modules[module_name]
    requested = frozenset(symbol_names)
    exported: set[str] = set()
    for source in export_modules:
        for name, value in source.__dict__.items():
            if name not in requested or name.startswith("__"):
                continue
            module.__dict__[name] = value
            exported.add(name)
    return frozenset(exported)


def prepare_exported_callables(module_name: str, symbol_names: tuple[str, ...] | list[str]) -> None:
    module = sys.modules[module_name]
    for name in symbol_names:
        value = module.__dict__.get(name)
        if not callable(value):
            continue
        try:
            value.__module__ = module_name
        except (AttributeError, TypeError):
            continue


def register_sync_targets(
    module_name: str,
    sync_modules: tuple[ModuleType, ...],
    symbol_names: tuple[str, ...] | list[str],
) -> None:
    module = sys.modules[module_name]
    state = _facade_state(module_name)
    modules: list[ModuleType] = state["modules"]  # type: ignore[assignment]
    names: set[str] = state["names"]  # type: ignore[assignment]
    for target in sync_modules:
        if target not in modules:
            modules.append(target)
    for name in symbol_names:
        names.add(name)
        if name in module.__dict__:
            for target in sync_modules:
                target.__dict__[name] = module.__dict__[name]
    if module.__class__ is not _AgentToolsFacadeModule:
        module.__class__ = _AgentToolsFacadeModule

