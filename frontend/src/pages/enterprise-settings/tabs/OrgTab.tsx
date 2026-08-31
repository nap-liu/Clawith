import React, { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { useDialog } from '../../../components/Dialog/DialogProvider';
import { useToast } from '../../../components/Toast/ToastProvider';
import LinearCopyButton from '../../../components/LinearCopyButton';
import { fetchJson } from '../utils/fetchJson';
import ProviderForm from './orgTab/ProviderForm';


// ─── Department Tree ───────────────────────────────
function DeptTree({ departments, parentId, selectedDept, onSelect, level }: {
    departments: any[]; parentId: string | null; selectedDept: string | null;
    onSelect: (id: string | null) => void; level: number;
}) {
    const children = departments.filter((d: any) =>
        parentId === null ? !d.parent_id : d.parent_id === parentId
    );
    if (children.length === 0) return null;
    return (
        <>
            {children.map((d: any) => (
                <div key={d.id}>
                    <div
                        style={{
                            padding: '5px 8px',
                            paddingLeft: `${8 + level * 16}px`,
                            borderRadius: '4px',
                            cursor: 'pointer',
                            fontSize: '13px',
                            marginBottom: '1px',
                            background: selectedDept === d.id ? 'rgba(224,238,238,0.12)' : 'transparent',
                            display: 'flex',
                            justifyContent: 'space-between',
                            alignItems: 'center'
                        }}
                        onClick={() => onSelect(d.id)}
                    >
                        <div>
                            <span style={{ color: 'var(--text-tertiary)', marginRight: '4px', fontSize: '11px' }}>
                                {departments.some((c: any) => c.parent_id === d.id) ? '▾' : '·'}
                            </span>
                            {d.name}
                        </div>
                        {d.member_count !== undefined && (
                            <span style={{ fontSize: '10px', color: 'var(--text-tertiary)' }}>
                                {d.member_count}
                            </span>
                        )}
                    </div>
                    <DeptTree departments={departments} parentId={d.id} selectedDept={selectedDept} onSelect={onSelect} level={level + 1} />
                </div>
            ))}
        </>
    );
}

// ─── SSO Channel Section ────────────────────────────────
function SsoChannelSection({ idpType, existingProvider, tenant, t }: {
    idpType: string; existingProvider: any; tenant: any; t: any;
}) {
    const qc = useQueryClient();
    const dialog = useDialog();
    const toast = useToast();
    const [liveDomain, setLiveDomain] = useState<string>(existingProvider?.sso_domain || tenant?.sso_domain || '');
    const [ssoError, setSsoError] = useState<string>('');
    const [toggling, setToggling] = useState(false);

    useEffect(() => {
        setLiveDomain(existingProvider?.sso_domain || tenant?.sso_domain || '');
    }, [existingProvider?.sso_domain, tenant?.sso_domain]);

    const ssoEnabled = existingProvider ? !!existingProvider.sso_login_enabled : false;
    const domain = liveDomain;
    const callbackUrl = domain ? (domain.startsWith('http') ? `${domain}/api/auth/${idpType}/callback` : `https://${domain}/api/auth/${idpType}/callback`) : '';
    const ssoLoginLabel = idpType === 'dingtalk'
        ? t('enterprise.identity.dingtalkSsoLoginToggle', 'Enable DingTalk Login')
        : t('enterprise.identity.ssoLoginToggle', 'SSO Login');
    const ssoLoginHint = idpType === 'dingtalk'
        ? t('enterprise.identity.dingtalkSsoLoginToggleHint', 'Only enable this if team members should log in with DingTalk. Directory sync works with AppKey/AppSecret even when this is off.')
        : t('enterprise.identity.ssoLoginToggleHint', 'Allow users to log in via this identity provider.');

    const handleSsoToggle = async () => {
        if (!existingProvider) {
            toast.warning(t('enterprise.identity.saveFirst', 'Please save the configuration first to enable SSO.'));
            return;
        }
        const newVal = !ssoEnabled;
        setToggling(true);
        setSsoError('');
        try {
            const result = await fetchJson<any>(`/enterprise/identity-providers/${existingProvider.id}`, {
                method: 'PUT',
                body: JSON.stringify({ sso_login_enabled: newVal }),
            });
            if (result?.sso_domain) setLiveDomain(result.sso_domain);
            qc.invalidateQueries({ queryKey: ['identity-providers'] });
            if (tenant?.id) qc.invalidateQueries({ queryKey: ['tenant', tenant.id] });
        } catch (e: any) {
            const msg = e?.message || '';
            if (msg.includes('IP address') || msg.includes('multi-tenant')) {
                setSsoError(t('enterprise.identity.ssoIpConflict', 'IP 模式下只能有一个企业开启 SSO，当前已有其他企业占用。'));
            } else {
                setSsoError(msg || t('enterprise.identity.ssoToggleFailed', 'Failed to toggle SSO'));
            }
        } finally {
            setToggling(false);
        }
    };

    return (
        <div style={{ marginTop: '20px', paddingTop: '20px', borderTop: '1px dashed var(--border-subtle)' }}>
            {/* SSO Toggle */}
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: ssoError ? '8px' : '16px' }}>
                <div>
                    <div style={{ fontWeight: 500, fontSize: '13px' }}>{ssoLoginLabel}</div>
                    <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>
                        {ssoLoginHint}
                    </div>
                </div>
                <label style={{ position: 'relative', display: 'inline-block', width: '36px', height: '20px', flexShrink: 0, opacity: (existingProvider && !toggling) ? 1 : 0.5 }}>
                    <input
                        type="checkbox"
                        checked={ssoEnabled}
                        onChange={handleSsoToggle}
                        disabled={!existingProvider || toggling}
                        style={{ opacity: 0, width: 0, height: 0 }}
                    />
                    <span style={{
                        position: 'absolute', top: 0, left: 0, right: 0, bottom: 0,
                        borderRadius: '20px', cursor: (existingProvider && !toggling) ? 'pointer' : 'not-allowed',
                        background: ssoEnabled ? 'var(--accent-primary)' : 'var(--border-subtle)',
                        transition: '0.2s',
                    }}>
                        <span style={{
                            position: 'absolute', left: ssoEnabled ? '18px' : '2px', top: '2px',
                            width: '16px', height: '16px', borderRadius: '50%',
                            background: '#fff', transition: '0.2s',
                            boxShadow: '0 1px 2px rgba(0,0,0,0.1)'
                        }} />
                    </span>
                </label>
            </div>
            {ssoError && (
                <div style={{ fontSize: '12px', color: 'var(--error)', marginBottom: '12px', padding: '6px 10px', background: 'rgba(var(--error-rgb,220,38,38),0.08)', borderRadius: '6px' }}>
                    {ssoError}
                </div>
            )}
            <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
                <div>
                    <label className="form-label" style={{ fontSize: '11px', marginBottom: '4px', color: 'var(--text-secondary)' }}>
                        {t('enterprise.identity.ssoSubdomain', 'SSO Login URL')}
                    </label>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                        <div style={{
                            flex: 1, maxWidth: '400px',
                            padding: '8px 12px',
                            background: 'var(--bg-elevated)',
                            border: '1px solid var(--border-subtle)',
                            borderRadius: '6px',
                            fontSize: '12px',
                            color: domain ? 'var(--text-primary)' : 'var(--text-tertiary)',
                            fontFamily: 'monospace',
                            whiteSpace: 'nowrap',
                            overflow: 'hidden',
                            textOverflow: 'ellipsis'
                        }}>
                            {domain ? (domain.startsWith('http') ? domain : `https://${domain}`) : t('enterprise.identity.ssoUrlEmpty', '请先开启 SSO 以生成地址')}
                        </div>
                        <LinearCopyButton
                            className="btn btn-ghost btn-sm"
                            style={{ fontSize: '11px', width: 'auto', minWidth: '70px', height: '33px' }}
                            disabled={!domain}
                            textToCopy={domain ? (domain.startsWith('http') ? domain : `https://${domain}`) : ''}
                            label={t('common.copy', 'Copy')}
                            copiedLabel="Copied"
                        />
                    </div>
                    <div style={{ fontSize: '10px', color: 'var(--text-tertiary)', marginTop: '4px' }}>
                        {t('enterprise.identity.ssoSubdomainHint', 'Share this URL with your team. SSO login buttons will appear when they visit this address.')}
                    </div>
                </div>
                <div>
                    <label className="form-label" style={{ fontSize: '11px', marginBottom: '4px', color: 'var(--text-secondary)' }}>
                        {t('enterprise.identity.callbackUrl', 'Redirect URL (paste this in your app settings)')}
                    </label>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                        <div style={{
                            flex: 1, maxWidth: '400px',
                            padding: '8px 12px',
                            background: 'var(--bg-elevated)',
                            border: '1px solid var(--border-subtle)',
                            borderRadius: '6px',
                            fontSize: '12px',
                            color: callbackUrl ? 'var(--text-primary)' : 'var(--text-tertiary)',
                            fontFamily: 'monospace',
                            whiteSpace: 'nowrap',
                            overflow: 'hidden',
                            textOverflow: 'ellipsis'
                        }}>
                            {callbackUrl || t('enterprise.identity.ssoUrlEmpty', '请先开启 SSO 以生成地址')}
                        </div>
                        <LinearCopyButton
                            className="btn btn-ghost btn-sm"
                            style={{ fontSize: '11px', width: 'auto', minWidth: '70px', height: '33px' }}
                            disabled={!callbackUrl}
                            textToCopy={callbackUrl}
                            label={t('common.copy', 'Copy')}
                            copiedLabel="Copied"
                        />
                    </div>
                    <div style={{ fontSize: '10px', color: 'var(--text-tertiary)', marginTop: '4px' }}>
                        {t('enterprise.identity.callbackUrlHint', "Add this URL as the OAuth redirect URI in your identity provider's app configuration.")}
                    </div>
                </div>
            </div>
        </div>
    );
}


// ─── Org & Identity Tab ─────────────────────────────
export default function OrgTab({ tenant }: { tenant: any }) {
    const { t } = useTranslation();
    const dialog = useDialog();
    const qc = useQueryClient();




    const SsoStatus = () => {
        const [isExpanded, setIsExpanded] = useState(!!tenant?.sso_enabled);
        const [ssoEnabled, setSsoEnabled] = useState(!!tenant?.sso_enabled);
        const [ssoDomain, setSsoDomain] = useState(tenant?.sso_domain || '');
        const [saving, setSaving] = useState(false);
        const [error, setError] = useState('');

        useEffect(() => {
            setSsoEnabled(!!tenant?.sso_enabled);
            setSsoDomain(tenant?.sso_domain || '');
            setIsExpanded(!!tenant?.sso_enabled);
        }, [tenant]);

        const handleSave = async (forceEnabled?: boolean) => {
            if (!tenant?.id) return;
            const targetEnabled = forceEnabled !== undefined ? forceEnabled : ssoEnabled;
            setSaving(true);
            setError('');
            try {
                await fetchJson(`/tenants/${tenant.id}`, {
                    method: 'PUT',
                    body: JSON.stringify({
                        sso_enabled: targetEnabled,
                        sso_domain: targetEnabled ? (ssoDomain.trim() || null) : null,
                    }),
                });
                qc.invalidateQueries({ queryKey: ['tenant', tenant.id] });
            } catch (e: any) {
                setError(e.message || 'Failed to update SSO configuration');
            }
            setSaving(false);
        };

        const handleToggle = (e: React.ChangeEvent<HTMLInputElement>) => {
            const checked = e.target.checked;
            setSsoEnabled(checked);
            setIsExpanded(checked);
            if (!checked) {
                // auto-save when disabling
                handleSave(false);
            }
        };

        return (
            <div className="card" style={{ marginBottom: '24px', overflow: 'hidden' }}>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '16px' }}>
                    <div>
                        <div style={{ fontWeight: 600, fontSize: '14px', marginBottom: '4px' }}>
                            {t('enterprise.identity.ssoTitle', 'Enterprise SSO')}
                        </div>
                        <div style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>
                            {t('enterprise.identity.ssoDisabledHint', 'Seamless enterprise login via Single Sign-On.')}
                        </div>
                    </div>
                    <div>
                        <label style={{ position: 'relative', display: 'inline-block', width: '36px', height: '20px' }}>
                            <input
                                type="checkbox"
                                checked={ssoEnabled}
                                onChange={handleToggle}
                                style={{ opacity: 0, width: 0, height: 0 }}
                            />
                            <span style={{
                                position: 'absolute', top: 0, left: 0, right: 0, bottom: 0,
                                borderRadius: '20px', cursor: 'pointer',
                                background: ssoEnabled ? 'var(--accent-primary)' : 'var(--border-subtle)',
                                transition: '0.2s'
                            }}>
                                <span style={{
                                    position: 'absolute', left: ssoEnabled ? '18px' : '2px', top: '2px',
                                    width: '16px', height: '16px', borderRadius: '50%',
                                    background: '#fff', transition: '0.2s',
                                    boxShadow: '0 1px 2px rgba(0,0,0,0.1)'
                                }} />
                            </span>
                        </label>
                    </div>
                </div>

                {isExpanded && (
                    <div style={{ padding: '0 16px 16px', borderTop: '1px solid var(--border-subtle)', paddingTop: '16px' }}>
                        <div style={{ marginBottom: '16px' }}>
                            <label className="form-label" style={{ fontSize: '12px', marginBottom: '8px' }}>
                                {t('enterprise.identity.ssoDomain', 'Custom Access Domain')}
                            </label>
                            <input
                                className="form-input"
                                value={ssoDomain}
                                onChange={e => setSsoDomain(e.target.value)}
                                placeholder={t('enterprise.identity.ssoDomainPlaceholder', 'e.g. login.example.com')}
                                style={{ fontSize: '13px', width: '100%', maxWidth: '400px' }}
                            />
                            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '6px' }}>
                                {t('enterprise.identity.ssoDomainDesc', 'The custom domain users will use to log in via SSO.')}
                            </div>
                        </div>

                        {error && <div style={{ color: 'var(--error)', fontSize: '12px', marginBottom: '12px' }}>{error}</div>}

                        <div style={{ display: 'flex', gap: '8px' }}>
                            <button className="btn btn-primary btn-sm" onClick={() => handleSave()} disabled={saving || !ssoDomain.trim()}>
                                {saving ? t('common.loading') : t('common.save', 'Save Configuration')}
                            </button>
                        </div>
                    </div>
                )}
            </div>
        );
    };

    const [syncing, setSyncing] = useState<string | null>(null);
    const [syncResult, setSyncResult] = useState<any>(null);
    const [memberSearch, setMemberSearch] = useState('');
    const [selectedDept, setSelectedDept] = useState<string | null>(null);
    const [expandedType, setExpandedType] = useState<string | null>(null);
    const [savingProvider, setSavingProvider] = useState(false);
    const [saveProviderOk, setSaveProviderOk] = useState(false);

    // Identity Providers state
    const [editingId, setEditingId] = useState<string | null>(null);
    const [useOAuth2Form, setUseOAuth2Form] = useState(false);
    const [form, setForm] = useState({
        provider_type: 'feishu',
        name: '',
        config: {} as any,
        app_id: '',
        app_secret: '',
        authorize_url: '',
        token_url: '',
        user_info_url: '',
        scope: 'openid profile email'
    });

    const currentTenantId = localStorage.getItem('current_tenant_id') || '';

    // Queries
    const { data: providers = [] } = useQuery({
        queryKey: ['identity-providers', currentTenantId],
        queryFn: () => fetchJson<any[]>(`/enterprise/identity-providers${currentTenantId ? `?tenant_id=${currentTenantId}` : ''}`),
    });

    const { data: departmentsData = { items: [], total_member: 0 } } = useQuery({
        queryKey: ['org-departments', currentTenantId, editingId],
        queryFn: () => {
            const params = new URLSearchParams();
            if (currentTenantId) params.set('tenant_id', currentTenantId);
            if (editingId) params.set('provider_id', editingId);
            return fetchJson<{ items: any[]; total_member: number }>(`/enterprise/org/departments?${params}`);
        },
        enabled: !!editingId,
    });

    const { data: members = [] } = useQuery({
        queryKey: ['org-members', selectedDept, memberSearch, currentTenantId, editingId],
        queryFn: () => {
            const params = new URLSearchParams();
            if (selectedDept) params.set('department_id', selectedDept);
            if (memberSearch) params.set('search', memberSearch);
            if (currentTenantId) params.set('tenant_id', currentTenantId);
            if (editingId) params.set('provider_id', editingId);
            return fetchJson<any[]>(`/enterprise/org/members?${params}`);
        },
        enabled: !!editingId,
    });

    // Mutations
    const addProvider = useMutation({
        mutationFn: (data: any) => {
            const payload = { ...data, tenant_id: currentTenantId, is_active: true };
            if (data.provider_type === 'oauth2' && useOAuth2Form) {
                return fetchJson('/enterprise/identity-providers/oauth2', {
                    method: 'POST',
                    body: JSON.stringify(payload)
                });
            }
            return fetchJson('/enterprise/identity-providers', { method: 'POST', body: JSON.stringify(payload) });
        },
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ['identity-providers'] });
            setUseOAuth2Form(false);
            setSavingProvider(false);
            setSaveProviderOk(true);
            setTimeout(() => setSaveProviderOk(false), 2500);
        },
        onError: () => setSavingProvider(false),
    });

    const updateProvider = useMutation({
        mutationFn: ({ id, data }: { id: string; data: any }) => {
            if (data.provider_type === 'oauth2' && useOAuth2Form) {
                return fetchJson(`/enterprise/identity-providers/${id}/oauth2`, {
                    method: 'PATCH',
                    body: JSON.stringify(data)
                });
            }
            return fetchJson(`/enterprise/identity-providers/${id}`, { method: 'PUT', body: JSON.stringify(data) });
        },
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ['identity-providers'] });
            setUseOAuth2Form(false);
            setSavingProvider(false);
            setSaveProviderOk(true);
            setTimeout(() => setSaveProviderOk(false), 2500);
        },
        onError: () => setSavingProvider(false),
    });

    const deleteProvider = useMutation({
        mutationFn: (id: string) => fetchJson(`/enterprise/identity-providers/${id}`, { method: 'DELETE' }),
        onSuccess: () => qc.invalidateQueries({ queryKey: ['identity-providers'] }),
    });

    const triggerSync = async (providerId: string) => {
        setSyncing(providerId);
        setSyncResult(null);
        try {
            const result = await fetchJson<any>(`/enterprise/org/sync?provider_id=${providerId}`, { method: 'POST' });
            setSyncResult({ ...result, providerId });
            // Force refetch to ensure UI updates after sync
            await qc.invalidateQueries({ queryKey: ['org-departments'] });
            await qc.invalidateQueries({ queryKey: ['org-members'] });
            await qc.invalidateQueries({ queryKey: ['identity-providers'] });
        } catch (e: any) {
            setSyncResult({ error: e.message, providerId });
        }
        setSyncing(null);
    };

    const initOAuth2FromConfig = (config: any) => ({
        app_id: config?.app_id || config?.client_id || '',
        app_secret: config?.app_secret || config?.client_secret || '',
        authorize_url: config?.authorize_url || '',
        token_url: config?.token_url || '',
        user_info_url: config?.user_info_url || '',
        scope: config?.scope || 'openid profile email'
    });

    const save = () => {
        setSavingProvider(true);
        setSaveProviderOk(false);
        if (editingId) {
            updateProvider.mutate({ id: editingId, data: form });
        } else {
            addProvider.mutate(form);
        }
    };

    const handleGoogleAdminAuthorize = async (providerId: string) => {
        const res = await fetchJson<{ authorization_url: string }>(`/enterprise/identity-providers/${providerId}/google-workspace-sync/authorize-url`);
        const popup = window.open(res.authorization_url, 'google-workspace-sync', 'width=640,height=760');
        if (!popup) {
            window.location.href = res.authorization_url;
            return;
        }

        const onMessage = (event: MessageEvent) => {
            if (event.data?.type === 'google-workspace-sync-authorized') {
                window.removeEventListener('message', onMessage);
                qc.invalidateQueries({ queryKey: ['identity-providers'] });
            }
        };
        window.addEventListener('message', onMessage);
    };

    const IDP_TYPES = [
        { type: 'feishu', name: 'Feishu', desc: 'Feishu / Lark Integration', icon: <img src="/feishu.png" width="20" height="20" alt="Feishu" /> },
        { type: 'wecom', name: 'WeCom', desc: 'WeChat Work Integration', icon: <img src="/wecom.png" width="20" height="20" style={{ borderRadius: '4px' }} alt="WeCom" /> },
        { type: 'dingtalk', name: 'DingTalk', desc: 'DingTalk App Integration', icon: <img src="/dingtalk.png" width="20" height="20" style={{ borderRadius: '4px' }} alt="DingTalk" /> },
        { type: 'google_workspace', name: 'Google', desc: 'Google Admin Directory Sync', icon: <img src="/google.svg" width="20" height="20" alt="Google" /> },
        { type: 'oauth2', name: 'OAuth2', desc: 'Generic OIDC Provider', icon: <div style={{ width: 20, height: 20, background: 'var(--accent-primary)', borderRadius: 4, display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#fff', fontSize: 10, fontWeight: 700 }}>O</div> }
    ];

    const handleExpand = (type: string, existingProvider?: any) => {
        if (expandedType === type) {
            setExpandedType(null);
            return;
        }
        setExpandedType(type);
        setEditingId(existingProvider ? existingProvider.id : null);
        setUseOAuth2Form(type === 'oauth2');

        if (existingProvider) {
            setForm({ ...existingProvider, ...(type === 'oauth2' ? initOAuth2FromConfig(existingProvider.config) : {}) });
        } else {
            const defaults: any = {
                feishu: { app_id: '', app_secret: '', corp_id: '' },
                dingtalk: { app_key: '', app_secret: '', corp_id: '' },
                wecom: { corp_id: '', secret: '', agent_id: '', app_secret: '', bot_id: '', bot_secret: '', verify_token: '', verify_aes_key: '' },
                google_workspace: {
                    client_id: '',
                    client_secret: '',
                },
            };
            const nameMap: Record<string, string> = { feishu: 'Feishu', wecom: 'WeCom', dingtalk: 'DingTalk', google_workspace: 'Google', oauth2: 'OAuth2' };
            setForm({
                provider_type: type,
                name: nameMap[type] || type,
                config: defaults[type] || {},
                app_id: '', app_secret: '', authorize_url: '', token_url: '', user_info_url: '',
                scope: 'openid profile email'
            });
        }
        setSelectedDept(null);
        setMemberSearch('');
    };


    const renderOrgBrowser = (p: any) => {
        return (
            <div style={{ marginTop: '24px', paddingTop: '24px', borderTop: '1px dashed var(--border-subtle)' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: '16px' }}>
                    <div style={{ fontWeight: 500, fontSize: '14px' }}>{t('enterprise.org.orgBrowser', 'Organization Browser')}</div>

                    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: '8px' }}>
                        {['feishu', 'dingtalk', 'google_workspace'].includes(p.provider_type) && (
                            <button className="btn btn-secondary btn-sm" style={{ fontSize: '12px' }} onClick={() => triggerSync(p.id)} disabled={!!syncing}>
                                {syncing === p.id ? 'Syncing...' : 'Sync Directory'}
                            </button>
                        )}
                        {syncResult && (
                            <div style={{ padding: '6px 10px', borderRadius: '4px', fontSize: '11px', background: syncResult.error || (syncResult.errors && syncResult.errors.length > 0) ? 'rgba(255,100,0,0.1)' : 'rgba(0,200,0,0.1)' }}>
                                {syncResult.error
                                    ? `Error: ${syncResult.error}`
                                    : `Sync complete: ${syncResult.departments || 0} depts, ${syncResult.members || 0} members synced.`}
                                {syncResult.errors && syncResult.errors.length > 0 && (
                                    <div style={{ marginTop: '4px', color: 'var(--color-warning, #f90)' }}>
                                        {/* Show first error to help diagnose permission issues */}
                                        {`Warning: ${syncResult.errors[0]}`}
                                        {syncResult.errors.length > 1 && ` (+${syncResult.errors.length - 1} more)`}
                                    </div>
                                )}
                            </div>
                        )}
                    </div>
                </div>


                <div style={{ display: 'flex', gap: '16px' }}>
                    <div style={{ width: '260px', borderRight: '1px solid var(--border-subtle)', paddingRight: '16px', maxHeight: '500px', overflowY: 'auto' }}>
                        <div style={{ padding: '6px 8px', borderRadius: '4px', cursor: 'pointer', fontSize: '13px', display: 'flex', justifyContent: 'space-between', alignItems: 'center', background: !selectedDept ? 'rgba(224,238,238,0.1)' : 'transparent' }} onClick={() => setSelectedDept(null)}>
                            {t('common.all')}
                            {departmentsData.total_member > 0 && <span style={{ fontSize: '10px', color: 'var(--text-tertiary)' }}>({departmentsData.total_member})</span>}
                        </div>
                        <DeptTree departments={departmentsData.items} parentId={null} selectedDept={selectedDept} onSelect={setSelectedDept} level={0} />
                    </div>

                    <div style={{ flex: 1 }}>
                        <input className="form-input" placeholder={t("enterprise.org.searchMembers")} value={memberSearch} onChange={e => setMemberSearch(e.target.value)} style={{ marginBottom: '12px' }} />
                        <div style={{ display: 'flex', flexDirection: 'column', gap: '4px', maxHeight: '400px', overflowY: 'auto' }}>
                            {members.map((m: any) => (
                                <div key={m.id} style={{ display: 'flex', alignItems: 'center', gap: '10px', padding: '8px', borderRadius: '6px', border: '1px solid var(--border-subtle)' }}>
                                    <div style={{ width: '32px', height: '32px', borderRadius: '50%', background: 'var(--bg-tertiary)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: '14px', fontWeight: 600 }}>{m.name?.[0]}</div>
                                    <div>
                                        <div style={{ fontWeight: 500, fontSize: '13px' }}>{m.name}</div>
                                        {m.nickname && m.nickname !== m.name && (
                                            <div style={{ fontSize: '11px', color: 'var(--text-secondary)' }}>
                                                {t('enterprise.org.nickname', 'Nickname')}: {m.nickname}
                                            </div>
                                        )}
                                        <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
                                            {m.provider_type && <span style={{ marginRight: '4px', padding: '1px 4px', borderRadius: '3px', background: 'var(--bg-secondary)', fontSize: '10px' }}>{m.provider_type}</span>}
                                            {m.title || '-'} · {m.department_path || m.department_id || '-'}
                                        </div>
                                    </div>
                                </div>
                            ))}
                            {members.length === 0 && <div style={{ textAlign: 'center', padding: '24px', color: 'var(--text-tertiary)' }}>{t('enterprise.org.noMembers')}</div>}
                        </div>
                    </div>
                </div>
            </div>
        );
    };

    return (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '20px' }}>
            {/* SSO status is now derived from per-channel toggles — no global switch */}

            {/* 1. Identity Providers Section */}
            <div className="card" style={{ padding: '0', overflow: 'hidden' }}>
                <div style={{ padding: '16px 20px', borderBottom: '1px solid var(--border-subtle)', background: 'var(--bg-secondary)' }}>
                    <h3 style={{ margin: 0, fontSize: '15px', fontWeight: 600 }}>
                        {t('enterprise.identity.title', 'Organization & Directory Sync')}
                    </h3>
                    <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginTop: '4px' }}>
                        Configure enterprise directory synchronization and Identity Provider settings.
                    </div>
                </div>

                <div style={{ display: 'flex', flexDirection: 'column' }}>
                    {IDP_TYPES.map((idp, index) => {
                        const existingProvider = providers.find((p: any) => p.provider_type === idp.type);
                        const isExpanded = expandedType === idp.type;

                        return (
                            <div key={idp.type} style={{ borderBottom: index < IDP_TYPES.length - 1 ? '1px solid var(--border-subtle)' : 'none' }}>
                                <div
                                    style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '16px 20px', cursor: 'pointer', background: isExpanded ? 'var(--bg-secondary)' : 'transparent', transition: 'background 0.2s' }}
                                    onClick={() => handleExpand(idp.type, existingProvider)}
                                >
                                    <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
                                        {idp.icon}
                                        <div>
                                            <div style={{ fontWeight: 500, fontSize: '14px' }}>{idp.name}</div>
                                            <div style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>{idp.desc}</div>
                                        </div>
                                    </div>
                                    <div style={{ display: 'flex', alignItems: 'center', gap: '16px' }}>
                                        {existingProvider ? (
                                            <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'flex-end', gap: '8px' }}>
                                                <span className="badge badge-success" style={{ fontSize: '10px' }}>Active</span>
                                                {existingProvider.last_synced_at && (
                                                    <span style={{ fontSize: '10px', color: 'var(--text-tertiary)' }}>
                                                        Synced: {new Date(existingProvider.last_synced_at).toLocaleDateString()}
                                                    </span>
                                                )}
                                            </div>
                                        ) : (
                                            <span className="badge badge-secondary" style={{ fontSize: '10px' }}>Not configured</span>
                                        )}
                                        <div style={{ color: 'var(--text-tertiary)', transform: isExpanded ? 'rotate(180deg)' : 'none', transition: 'transform 0.2s', fontSize: '12px' }}>
                                            ▼
                                        </div>
                                    </div>
                                </div>

                                {isExpanded && (
                                    <div style={{ padding: '0 20px 20px', background: 'var(--bg-secondary)' }}>
                                        <ProviderForm
                                            type={idp.type}
                                            existingProvider={existingProvider}
                                            tenant={tenant}
                                            t={t}
                                            form={form}
                                            setForm={setForm}
                                            save={save}
                                            savingProvider={savingProvider}
                                            saveProviderOk={saveProviderOk}
                                            handleGoogleAdminAuthorize={handleGoogleAdminAuthorize}
                                            editingId={editingId}
                                            dialog={dialog}
                                            deleteProvider={deleteProvider}
                                        />

                                        {/* Per-channel SSO Login URLs & Toggle */}
                                        {['feishu', 'dingtalk', 'google_workspace', 'oauth2'].includes(idp.type) && (
                                            <SsoChannelSection
                                                idpType={idp.type}
                                                existingProvider={existingProvider}
                                                tenant={tenant}
                                                t={t}
                                            />
                                        )}
                                        {existingProvider && idp.type !== 'wecom' && renderOrgBrowser(existingProvider)}
                                    </div>
                                )}
                            </div>
                        );
                    })}
                </div>
            </div>

        </div>
    );
}
