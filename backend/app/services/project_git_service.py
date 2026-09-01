"""Compatibility facade for project repository and workspace operations."""

from __future__ import annotations

import sys
import types

from app.services import project_git_core as _core
from app.services import project_git_diff as _diff
from app.services import project_git_inspection as _inspection
from app.services import project_git_mutations as _mutations
from app.services import project_git_repository as _repository


_IMPLEMENTATION_MODULES = (
    _core,
    _repository,
    _inspection,
    _mutations,
    _diff,
)

_IMPLEMENTATION_EXPORTS: dict[str, object] = {}
for _module in _IMPLEMENTATION_MODULES:
    _IMPLEMENTATION_EXPORTS.update(
        {
            _name: _value
            for _name, _value in vars(_module).items()
            if not _name.startswith("__")
        }
    )

# The implementation files are contiguous slices of the former module. Restore
# its single global namespace so functions in an earlier slice can resolve
# helpers that were defined later in the original file.
for _module in _IMPLEMENTATION_MODULES:
    vars(_module).update(_IMPLEMENTATION_EXPORTS)
globals().update(_IMPLEMENTATION_EXPORTS)

for _name, _value in tuple(globals().items()):
    if callable(_value) and str(getattr(_value, "__module__", "")).startswith(
        "app.services.project_git_"
    ):
        _value.__module__ = __name__


class _ProjectGitServiceModule(types.ModuleType):
    """Keep historical root-module monkeypatch targets effective."""

    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        for implementation in _IMPLEMENTATION_MODULES:
            if hasattr(implementation, name):
                setattr(implementation, name, value)


sys.modules[__name__].__class__ = _ProjectGitServiceModule
