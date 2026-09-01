"""Conversation auto-compaction."""

import sys
from types import ModuleType

from app.services.llm import compactor_shared as _compactor_shared
from app.services.llm.compactor_shared import *  # noqa: F401,F403
from app.services.llm import (
    compactor_runtime as _compactor_runtime,
    compactor_runtime_support as _compactor_runtime_support,
    compactor_serialization as _compactor_serialization,
    compactor_summary as _compactor_summary,
)

_MODULE_EXPORTS: tuple[tuple[ModuleType, tuple[str, ...]], ...] = (
    (
        _compactor_serialization,
        (
            '_elide_materialized_output_bodies',
            'prefilter_message_content',
            'serialize_span_for_summary',
            '_load_summary_sender_attribution',
            'objective_evidence_items_from_rows',
        ),
    ),
    (
        _compactor_summary,
        (
            '_UUID_LIKE_RE',
            '_PATH_RE',
            '_ATTACHMENT_PATH_RE',
            '_URL_RE',
            '_SLASH_COMMAND_RE',
            '_KNOWN_SLASH_COMMANDS',
            '_HEADING_RE',
            '_CURRENT_OBJECTIVE_HEADING_RE',
            '_OPEN_ITEMS_HEADING_RE',
            '_GOAL_LEDGER_HEADING_RE',
            '_GOAL_LEDGER_ACTIVE_RE',
            '_GOAL_LEDGER_ACHIEVED_RE',
            '_GOAL_LEDGER_UNFINISHED_RE',
            '_RELATED_TASK_HEADING_RE',
            '_RELATED_TASK_SECTION_RE',
            '_RELATED_TASK_REQUIRED_FIELDS',
            '_ROLE_BLOCK_RE',
            '_OBJECTIVE_SECTION_RE',
            '_objective_evidence_items',
            '_selected_objective_evidence_items',
            'extract_objective_evidence',
            'objective_evidence_is_truncated',
            'pin_summary_objective',
            'extract_preserved_identifiers',
            'append_missing_identifiers',
            'validate_summary',
            'build_deterministic_summary',
            'append_lossless_archive_reference',
        ),
    ),
    (
        _compactor_runtime_support,
        (
            '_delete_uncommitted_archive',
            '_get_session_lock',
            '_current_anchor_owns_latest_logical_tail',
            '_is_dryrun',
            '_summarize_via_llm',
        ),
    ),
    (
        _compactor_runtime,
        (
            'maybe_compact',
            'maybe_precompact_prompt',
            '_do_compact',
            '_load_active_rows',
            '_load_compaction_state',
            '_load_active_marker',
        ),
    ),
)

_SHARED_SYMBOLS = ('objective_evidence_limit', 'CompactionResult', 'ContextRecoveryMessages', 'prompt_exceeds_preflight_limit', 'should_compact', '_is_round_boundary_after', 'select_compaction_span')

for module, names in _MODULE_EXPORTS:
    for name in names:
        globals()[name] = module.__dict__[name]

def _prepare_compactor_module(module: ModuleType, symbol_names: tuple[str, ...] | list[str]) -> None:
    for symbol_name in symbol_names:
        value = module.__dict__.get(symbol_name)
        if value is None or not hasattr(value, "__module__"):
            continue
        try:
            value.__module__ = __name__
        except (AttributeError, TypeError):
            continue

for module, names in _MODULE_EXPORTS:
    _prepare_compactor_module(module, names)
_prepare_compactor_module(_compactor_shared, _SHARED_SYMBOLS)

_SYNC_MODULES = (
    _compactor_shared,
    _compactor_serialization,
    _compactor_summary,
    _compactor_runtime_support,
    _compactor_runtime,
)

def _sync_root_exports() -> None:
    module = sys.modules[__name__]
    for name, value in tuple(module.__dict__.items()):
        if name.startswith("__"):
            continue
        for target in _SYNC_MODULES:
            if name in target.__dict__:
                target.__dict__[name] = value

class _CompactorModule(ModuleType):
    def __setattr__(self, name: str, value) -> None:
        super().__setattr__(name, value)
        if name.startswith("__"):
            return
        for target in _SYNC_MODULES:
            if name in target.__dict__:
                target.__dict__[name] = value

sys.modules[__name__].__class__ = _CompactorModule
_sync_root_exports()
