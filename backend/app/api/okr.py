"""OKR REST API — objectives, key results, settings, reports and periods."""

from types import ModuleType

from app.api import (
    okr_shared as _okr_shared,
    okr_models as _okr_models,
    okr_settings_periods as _okr_settings_periods,
    okr_objectives as _okr_objectives,
    okr_reports as _okr_reports,
    okr_outreach as _okr_outreach,
)
from app.api.okr_models import (
    CompanyReportOut,
    CompanyReportRegenerate,
    KeyResultCreate,
    KeyResultOut,
    KeyResultUpdate,
    MemberDailyReportOut,
    MemberDailyReportUpsert,
    ObjectiveCreate,
    ObjectiveOut,
    ObjectiveUpdate,
    OKRSettingsOut,
    OKRSettingsUpdate,
    PeriodOut,
    ProgressUpdate,
    WorkReportOut,
    _kr_to_out,
    _obj_to_out,
    _serialize_company_report,
)
from app.api.okr_objectives import (
    create_key_result,
    create_objective,
    delete_key_result,
    delete_objective,
    list_key_results,
    list_objectives,
    update_key_result,
    update_kr_progress_endpoint,
    update_objective,
)
from app.api.okr_outreach import members_without_okr, trigger_daily_collection, trigger_member_outreach
from app.api.okr_reports import (
    list_company_reports_api,
    list_member_daily_reports,
    list_reports,
    regenerate_company_report,
    upsert_member_daily_report,
)
from app.api.okr_settings_periods import get_okr_settings, list_periods, sync_okr_relationships, update_okr_settings
from app.api.okr_shared import (
    _advance_period,
    _compute_current_period,
    _compute_period_for_date,
    _dashboard_write_forbidden,
    _get_or_create_settings,
    _is_okr_admin,
    _sync_okr_agent_relationships,
    _sync_okr_report_triggers,
    router,
)


def _prepare_okr_module(module: ModuleType, symbol_names: tuple[str, ...]) -> None:
    for symbol_name in symbol_names:
        module.__dict__[symbol_name].__module__ = __name__


_prepare_okr_module(
    _okr_shared,
    (
        "_is_okr_admin",
        "_dashboard_write_forbidden",
        "_sync_okr_agent_relationships",
        "_get_or_create_settings",
        "_sync_okr_report_triggers",
        "_compute_current_period",
        "_compute_period_for_date",
        "_advance_period",
    ),
)
_prepare_okr_module(
    _okr_models,
    (
        "OKRSettingsOut",
        "OKRSettingsUpdate",
        "KeyResultOut",
        "ObjectiveOut",
        "ObjectiveCreate",
        "ObjectiveUpdate",
        "KeyResultCreate",
        "KeyResultUpdate",
        "ProgressUpdate",
        "PeriodOut",
        "WorkReportOut",
        "MemberDailyReportOut",
        "MemberDailyReportUpsert",
        "CompanyReportOut",
        "CompanyReportRegenerate",
        "_kr_to_out",
        "_obj_to_out",
        "_serialize_company_report",
    ),
)
_prepare_okr_module(
    _okr_settings_periods,
    ("get_okr_settings", "update_okr_settings", "sync_okr_relationships", "list_periods"),
)
_prepare_okr_module(
    _okr_objectives,
    (
        "list_objectives",
        "create_objective",
        "update_objective",
        "delete_objective",
        "list_key_results",
        "create_key_result",
        "update_key_result",
        "update_kr_progress_endpoint",
        "delete_key_result",
    ),
)
_prepare_okr_module(
    _okr_reports,
    (
        "list_member_daily_reports",
        "upsert_member_daily_report",
        "list_company_reports_api",
        "regenerate_company_report",
        "list_reports",
    ),
)
_prepare_okr_module(
    _okr_outreach,
    ("members_without_okr", "trigger_member_outreach", "trigger_daily_collection"),
)
