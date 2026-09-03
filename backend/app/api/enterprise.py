"""Enterprise management API routes: LLM pool, identity, org, and invitations."""

from app.api.enterprise_api_shared import *  # noqa: F401,F403
from app.api.enterprise_routes_core import *  # noqa: F401,F403
from app.api.enterprise_routes_identity import *  # noqa: F401,F403
from app.api.enterprise_routes_identity_conflicts import *  # noqa: F401,F403
from app.api.enterprise_routes_org import *  # noqa: F401,F403
from app.api.enterprise_routes_invitations import *  # noqa: F401,F403


_COMPAT_SYMBOLS = ('_is_platform_admin_user', '_assert_tenant_scope', '_validate_model_context_budget', 'CheckEmailRequest', 'check_email_exists', 'list_llm_providers', 'LLMTestRequest', '_load_llm_test_api_key', 'test_llm_model', 'list_llm_models', 'add_llm_model', 'clone_llm_model', 'set_default_llm_model', 'remove_llm_model', 'update_llm_model', 'list_enterprise_info', 'update_enterprise_info', 'list_approvals', 'resolve_approval', 'list_audit_logs', 'get_enterprise_stats', 'TenantQuotaUpdate', 'get_tenant_quotas', 'update_tenant_quotas', 'TestEmailRequest', 'send_test_email_endpoint', 'get_email_templates_endpoint', 'EmailTemplatesUpdate', 'update_email_templates_endpoint', 'SettingUpdate', 'get_notification_bar_public', 'get_system_setting', 'update_system_setting', '_sync_tenant_sso_state', '_regenerate_all_sso_domains', 'list_identity_providers', 'IdentityProviderCreate', 'IdentityProviderUpdate', 'OAuth2Config', 'IdentityProviderOAuth2Create', 'normalize_oauth2_config', 'validate_provider_config', '_sanitize_identity_provider_config', '_identity_provider_response', 'create_identity_provider', 'create_oauth2_provider', 'update_oauth2_provider', 'update_identity_provider', 'delete_identity_provider', 'list_org_departments', 'list_org_members', 'trigger_org_sync', 'wecom_org_sync_verify', 'wecom_callback_verify_universal', 'InvitationCodeCreate', '_require_tenant_admin', '_ensure_invitation_email_enabled', 'create_invitation_codes', 'invite_users', 'list_invitation_codes', 'export_invitation_codes_csv', 'deactivate_invitation_code')
for _compat_name in _COMPAT_SYMBOLS:
    _compat_symbol = globals().get(_compat_name)
    if _compat_symbol is not None:
        _compat_symbol.__module__ = __name__
