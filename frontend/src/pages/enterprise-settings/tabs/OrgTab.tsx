import React, { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { useDialog } from '../../../components/Dialog/DialogProvider';
import { useToast } from '../../../components/Toast/ToastProvider';
import LinearCopyButton from '../../../components/LinearCopyButton';
import Avatar from '../../../components/ui/Avatar';
import { fetchJson } from '../utils/fetchJson';
import ProviderForm from './orgTab/ProviderForm';
import DirectorySyncControls from './orgTab/DirectorySyncControls';
import DepartmentTree from './orgTab/DepartmentTree';
import { buildProviderEntries, hasDirectoryCapability } from './orgTab/providerCatalog';

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
        ? t('enterprise.identity.dingtalkSsoLoginToggle')
        : t('enterprise.identity.ssoLoginToggle');
    const ssoLoginHint = idpType === 'dingtalk'
        ? t('enterprise.identity.dingtalkSsoLoginToggleHint')
        : t('enterprise.identity.ssoLoginToggleHint');

    const handleSsoToggle = async () => {
        if (!existingProvider) {
            toast.warning(t('enterprise.identity.saveFirst'));
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
                setSsoError(t('enterprise.identity.ssoIpConflict'));
            } else {
                setSsoError(msg || t('enterprise.identity.ssoToggleFailed'));
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
                        {t('enterprise.identity.ssoSubdomain')}
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
                            {domain ? (domain.startsWith('http') ? domain : `https://${domain}`) : t('enterprise.identity.ssoUrlEmpty')}
                        </div>
                        <LinearCopyButton
                            className="btn btn-ghost btn-sm"
                            style={{ fontSize: '11px', width: 'auto', minWidth: '70px', height: '33px' }}
                            disabled={!domain}
                            textToCopy={domain ? (domain.startsWith('http') ? domain : `https://${domain}`) : ''}
                            label={t('common.copy')}
                            copiedLabel={t('common.copied')}
                        />
                    </div>
                    <div style={{ fontSize: '10px', color: 'var(--text-tertiary)', marginTop: '4px' }}>
                        {t('enterprise.identity.ssoSubdomainHint')}
                    </div>
                </div>
                <div>
                    <label className="form-label" style={{ fontSize: '11px', marginBottom: '4px', color: 'var(--text-secondary)' }}>
                        {t('enterprise.identity.callbackUrl')}
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
                            {callbackUrl || t('enterprise.identity.ssoUrlEmpty')}
                        </div>
                        <LinearCopyButton
                            className="btn btn-ghost btn-sm"
                            style={{ fontSize: '11px', width: 'auto', minWidth: '70px', height: '33px' }}
                            disabled={!callbackUrl}
                            textToCopy={callbackUrl}
                            label={t('common.copy')}
                            copiedLabel={t('common.copied')}
                        />
                    </div>
                    <div style={{ fontSize: '10px', color: 'var(--text-tertiary)', marginTop: '4px' }}>
                        {t('enterprise.identity.callbackUrlHint')}
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

    const [syncingProviders, setSyncingProviders] = useState<Record<string, boolean>>({});
    const [syncResults, setSyncResults] = useState<Record<string, any>>({});
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
        is_active: true,
        config: {} as any,
        app_id: '',
        app_secret: '',
        authorize_url: '',
        token_url: '',
        user_info_url: '',
        scope: 'openid profile email',
        scim_base_url: '',
        field_mapping: {} as Record<string, string>,
        directory_field_mapping: {} as Record<string, string>,
    });

    const currentTenantId = localStorage.getItem('current_tenant_id') || '';

    // Queries
    const { data: providers = [] } = useQuery({
        queryKey: ['identity-providers', currentTenantId],
        queryFn: () => fetchJson<any[]>(`/enterprise/identity-providers${currentTenantId ? `?tenant_id=${currentTenantId}` : ''}`),
    });

    const { data: syncRuns = [] } = useQuery({
        queryKey: ['directory-sync-runs', currentTenantId],
        queryFn: () => fetchJson<any[]>('/enterprise/org/sync-runs'),
        refetchInterval: 3_000,
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
            const payload = { ...data, tenant_id: currentTenantId, is_active: data.is_active !== false };
            if (data.provider_type === 'oauth2' && useOAuth2Form) {
                return fetchJson('/enterprise/identity-providers/oauth2', {
                    method: 'POST',
                    body: JSON.stringify({
                        name: data.name,
                        tenant_id: currentTenantId,
                        is_active: data.is_active !== false,
                        sso_login_enabled: !!data.sso_login_enabled,
                        config: {
                            app_id: data.app_id,
                            app_secret: data.app_secret,
                            authorize_url: data.authorize_url,
                            token_url: data.token_url,
                            user_info_url: data.user_info_url,
                            scope: data.scope,
                            scim_base_url: data.scim_base_url || '',
                            field_mapping: Object.fromEntries(
                                Object.entries(data.field_mapping || {}).filter(
                                    ([, value]) => String(value || '').trim(),
                                ),
                            ),
                            directory: {
                                ...(data.config?.directory || {}),
                                field_mapping: Object.fromEntries(
                                    Object.entries(data.directory_field_mapping || {}).filter(
                                        ([, value]) => String(value || '').trim(),
                                    ),
                                ),
                            },
                            identity_match_policy: data.config?.identity_match_policy,
                        },
                    })
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
                    body: JSON.stringify({
                        name: data.name,
                        is_active: data.is_active,
                        sso_login_enabled: data.sso_login_enabled,
                        config: {
                            app_id: data.app_id,
                            app_secret: data.app_secret,
                            authorize_url: data.authorize_url,
                            token_url: data.token_url,
                            user_info_url: data.user_info_url,
                            scope: data.scope,
                            scim_base_url: data.scim_base_url || '',
                            field_mapping: Object.fromEntries(
                                Object.entries(data.field_mapping || {}).filter(
                                    ([, value]) => String(value || '').trim(),
                                ),
                            ),
                            directory: {
                                ...(data.config?.directory || {}),
                                field_mapping: Object.fromEntries(
                                    Object.entries(data.directory_field_mapping || {}).filter(
                                        ([, value]) => String(value || '').trim(),
                                    ),
                                ),
                            },
                            identity_match_policy: data.config?.identity_match_policy,
                        },
                    })
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
        setSyncingProviders(current => ({ ...current, [providerId]: true }));
        setSyncResults(current => {
            const next = { ...current };
            delete next[providerId];
            return next;
        });
        try {
            let result = await fetchJson<any>(`/enterprise/org/sync?provider_id=${providerId}`, { method: 'POST' });
            setSyncResults(current => ({ ...current, [providerId]: result }));
            while (['pending', 'running'].includes(result.status)) {
                await new Promise(resolve => window.setTimeout(resolve, 1000));
                result = await fetchJson<any>(`/enterprise/org/sync-runs/${result.id}`);
                setSyncResults(current => ({ ...current, [providerId]: result }));
            }
            await qc.invalidateQueries({ queryKey: ['org-departments'] });
            await qc.invalidateQueries({ queryKey: ['org-members'] });
            await qc.invalidateQueries({ queryKey: ['identity-providers'] });
        } catch (e: any) {
            setSyncResults(current => ({ ...current, [providerId]: { status: 'failed', error: e.message } }));
        } finally {
            setSyncingProviders(current => ({ ...current, [providerId]: false }));
        }
    };

    const updateSyncSchedule = async (providerId: string, value: number, unit: string, enabled: boolean) => {
        await fetchJson(`/enterprise/identity-providers/${providerId}`, {
            method: 'PUT',
            body: JSON.stringify({
                sync_enabled: enabled,
                sync_interval_value: value,
                sync_interval_unit: unit,
            }),
        });
        await qc.invalidateQueries({ queryKey: ['identity-providers'] });
    };

    const initOAuth2FromConfig = (config: any) => ({
        app_id: config?.app_id || config?.client_id || '',
        app_secret: config?.app_secret || config?.client_secret || '',
        authorize_url: config?.authorize_url || '',
        token_url: config?.token_url || '',
        user_info_url: config?.user_info_url || '',
        scope: config?.scope || 'openid profile email',
        scim_base_url: config?.scim_base_url || '',
        field_mapping: config?.field_mapping || {},
        directory_field_mapping: config?.directory?.field_mapping || {},
    });

    const withEnterpriseRootMapping = (config: any) => ({
        ...(config || {}),
        directory: {
            ...(config?.directory || {}),
            root_mapping: {
                ...(config?.directory?.root_mapping || {}),
                root_name: config?.directory?.root_mapping?.root_name || tenant?.name || '',
            },
        },
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

    const handleExpand = (type: string, existingProvider?: any) => {
        const expansionKey = existingProvider?.id || `new:${type}`;
        if (expandedType === expansionKey) {
            setExpandedType(null);
            return;
        }
        setExpandedType(expansionKey);
        setEditingId(existingProvider ? existingProvider.id : null);
        setUseOAuth2Form(type === 'oauth2');

        if (existingProvider) {
            setForm({
                ...existingProvider,
                config: withEnterpriseRootMapping(existingProvider.config),
                ...(type === 'oauth2' ? initOAuth2FromConfig(existingProvider.config) : {}),
            });
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
            const nameMap: Record<string, string> = {
                feishu: 'Feishu',
                wecom: 'WeCom',
                dingtalk: 'DingTalk',
                google_workspace: 'Google',
                oauth2: 'OAuth 2.0 / SCIM 2.0',
            };
            setForm({
                provider_type: type,
                name: nameMap[type] || type,
                is_active: true,
                config: withEnterpriseRootMapping(defaults[type] || {}),
                app_id: '', app_secret: '', authorize_url: '', token_url: '', user_info_url: '',
                scope: 'openid profile email',
                scim_base_url: '',
                field_mapping: {},
                directory_field_mapping: {},
            });
        }
        setSelectedDept(null);
        setMemberSearch('');
    };


    const renderOrgBrowser = (p: any) => {
        const persistedResult = syncRuns.find((run: any) => run.provider_id === p.id);
        const localResult = syncResults[p.id];
        const result = !localResult || (
            persistedResult?.created_at
            && new Date(persistedResult.created_at).getTime()
                >= new Date(localResult.created_at || 0).getTime()
        ) ? persistedResult : localResult;
        const providerSyncing = !!syncingProviders[p.id]
            || ['pending', 'running'].includes(result?.status || '');
        return (
            <div style={{ marginTop: '24px', paddingTop: '24px', borderTop: '1px dashed var(--border-subtle)' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: '16px' }}>
                    <div style={{ fontWeight: 500, fontSize: '14px' }}>{t('enterprise.org.orgBrowser')}</div>

                    <DirectorySyncControls
                        provider={p}
                        syncing={providerSyncing}
                        result={result || null}
                        onTrigger={() => void triggerSync(p.id)}
                        onUpdateSchedule={(value, unit, enabled) => updateSyncSchedule(p.id, value, unit, enabled)}
                    />
                </div>


                <div style={{ display: 'flex', gap: '16px' }}>
                    <div style={{ width: '260px', borderRight: '1px solid var(--border-subtle)', paddingRight: '16px', maxHeight: '500px', overflowY: 'auto' }}>
                        <DepartmentTree
                            tenantId={currentTenantId}
                            providerId={p.id}
                            selectedDepartmentId={selectedDept}
                            onSelect={setSelectedDept}
                            reloadKey={`${result?.status || ''}:${result?.finished_at || result?.created_at || ''}`}
                        />
                    </div>

                    <div style={{ flex: 1 }}>
                        <input className="form-input" placeholder={t("enterprise.org.searchMembers")} value={memberSearch} onChange={e => setMemberSearch(e.target.value)} style={{ marginBottom: '12px' }} />
                        <div style={{ display: 'flex', flexDirection: 'column', gap: '4px', maxHeight: '400px', overflowY: 'auto' }}>
                            {members.map((m: any) => (
                                <div key={m.id} style={{ display: 'flex', alignItems: 'center', gap: '10px', padding: '8px', borderRadius: '6px', border: '1px solid var(--border-subtle)' }}>
                                    <Avatar
                                        src={m.avatar_url}
                                        name={m.name}
                                        style={{ width: '32px', height: '32px', background: 'var(--bg-tertiary)', fontSize: '14px', fontWeight: 600 }}
                                    />
                                    <div>
                                        <div style={{ fontWeight: 500, fontSize: '13px' }}>
                                            {m.name}
                                            {m.nickname && m.nickname !== m.name && (
                                                <span style={{ color: 'var(--text-secondary)', fontWeight: 400 }}>
                                                    {`（${m.nickname}）`}
                                                </span>
                                            )}
                                            {m.phone_masked && (
                                                <span style={{ color: 'var(--text-tertiary)', fontWeight: 400 }}>
                                                    {` · ${m.phone_masked}`}
                                                </span>
                                            )}
                                        </div>
                                        <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
                                            {[m.title, m.department_path].filter(Boolean).join(' · ') || '-'}
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
                        {t('enterprise.identity.title')}
                    </h3>
                    <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginTop: '4px' }}>
                        {t('enterprise.identity.description')}
                    </div>
                </div>

                <div style={{ display: 'flex', flexDirection: 'column' }}>
                    {buildProviderEntries(providers, t).map(({ idp, existingProvider, key }, index, entries) => {
                        const isExpanded = expandedType === key;

                        return (
                            <div key={key} style={{ borderBottom: index < entries.length - 1 ? '1px solid var(--border-subtle)' : 'none' }}>
                                <div
                                    style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '16px 20px', cursor: 'pointer', background: isExpanded ? 'var(--bg-secondary)' : 'transparent', transition: 'background 0.2s' }}
                                    onClick={() => handleExpand(idp.type, existingProvider)}
                                >
                                    <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
                                        {idp.icon}
                                        <div>
                                            <div style={{ fontWeight: 500, fontSize: '14px' }}>
                                                {['dingtalk', 'wecom'].includes(idp.type)
                                                    ? idp.name
                                                    : existingProvider?.name || idp.name}
                                            </div>
                                            <div style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>{idp.desc}</div>
                                        </div>
                                    </div>
                                    <div style={{ display: 'flex', alignItems: 'center', gap: '16px' }}>
                                        {existingProvider ? (
                                            <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'flex-end', gap: '8px' }}>
                                                <span className={`badge ${existingProvider.is_active ? 'badge-success' : 'badge-secondary'}`} style={{ fontSize: '10px' }}>
                                                    {existingProvider.is_active
                                                        ? t('enterprise.identity.statusActive')
                                                        : t('enterprise.identity.statusInactive')}
                                                </span>
                                                {existingProvider.last_synced_at && (
                                                    <span style={{ fontSize: '10px', color: 'var(--text-tertiary)' }}>
                                                        {t('enterprise.identity.lastSynced', {
                                                            date: new Date(existingProvider.last_synced_at).toLocaleDateString(),
                                                        })}
                                                    </span>
                                                )}
                                            </div>
                                        ) : (
                                            <span className="badge badge-secondary" style={{ fontSize: '10px' }}>
                                                {t('enterprise.identity.notConfigured')}
                                            </span>
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
                                        {existingProvider && hasDirectoryCapability(existingProvider) && renderOrgBrowser(existingProvider)}
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
