import React, { useEffect, useState } from 'react';
import { IconSettings } from '@tabler/icons-react';
import LinearCopyButton from '../../../../components/LinearCopyButton';
import OrderedFieldListEditor from '../../../../components/OrderedFieldListEditor';
import ProviderFieldMappingEditor, {
    type ProviderDiscoveredField,
} from '../../../../components/ProviderFieldMappingEditor';
import ToggleSwitch from '../../../../components/ToggleSwitch';
import Button from '../../../../components/ui/Button';
import TextInput from '../../../../components/ui/TextInput';
import { fetchJson } from '../../utils/fetchJson';

const IDENTITY_MATCH_FIELDS = ['phone', 'email'] as const;
type IdentityMatchField = typeof IDENTITY_MATCH_FIELDS[number];
const OAUTH_FIELD_MAPPING_KEYS = ['user_id', 'name', 'email', 'mobile', 'avatar'] as const;
const OAUTH_FIELD_DEFAULTS: Record<string, string> = {
    user_id: 'sub',
    name: 'name',
    email: 'email',
    mobile: 'phone_number',
    avatar: 'picture',
};
const SCIM_FIELD_DEFAULTS: Record<string, string> = {
    name: '/displayName',
    email: '/emails',
    mobile: '/phoneNumbers',
    avatar: '/photos',
    title: '/title',
    employee_number: '/urn:ietf:params:scim:schemas:extension:enterprise:2.0:User/employeeNumber',
    organization: '/urn:ietf:params:scim:schemas:extension:enterprise:2.0:User/organization',
    division: '/urn:ietf:params:scim:schemas:extension:enterprise:2.0:User/division',
    department: '/urn:ietf:params:scim:schemas:extension:enterprise:2.0:User/department',
};

function normalizeIdentityMatchFields(fields: unknown): IdentityMatchField[] {
    if (!Array.isArray(fields)) return [...IDENTITY_MATCH_FIELDS];
    const normalized = fields.filter(
        (field, index): field is IdentityMatchField => (
            IDENTITY_MATCH_FIELDS.includes(field as IdentityMatchField)
            && fields.indexOf(field) === index
        ),
    );
    return normalized.length > 0 ? normalized : [...IDENTITY_MATCH_FIELDS];
}

const FEISHU_SYNC_PERM_JSON = `{
  "scopes": {
    "tenant": [
      "contact:contact.base:readonly",
      "contact:department.base:readonly",
      "contact:user.base:readonly",
      "contact:user.employee_id:readonly"
    ],
    "user": []
  }
}`;

type ProviderFormProps = {
    type: string;
    existingProvider?: any;
    tenant: any;
    t: any;
    form: any;
    setForm: React.Dispatch<React.SetStateAction<any>>;
    save: () => void;
    savingProvider: boolean;
    saveProviderOk: boolean;
    handleGoogleAdminAuthorize: (providerId: string) => Promise<void>;
    editingId: string | null;
    dialog: any;
    deleteProvider: any;
};

export default function ProviderForm({
    type,
    existingProvider,
    tenant,
    t,
    form,
    setForm,
    save,
    savingProvider,
    saveProviderOk,
    handleGoogleAdminAuthorize,
    editingId,
    dialog,
    deleteProvider,
}: ProviderFormProps) {
        const [discoveredFields, setDiscoveredFields] = useState<Record<string, ProviderDiscoveredField[]>>({});
        const [discoveringCapability, setDiscoveringCapability] = useState<string | null>(null);
        const [fieldDiscoveryError, setFieldDiscoveryError] = useState<Record<string, string>>({});
        const [directoryDiscoveryAccount, setDirectoryDiscoveryAccount] = useState('');
        useEffect(() => {
            setDiscoveredFields({});
            setFieldDiscoveryError({});
            setDirectoryDiscoveryAccount('');
        }, [existingProvider?.id]);

        const discoverFieldPaths = async (capability: 'login' | 'directory') => {
            if (!existingProvider?.id) {
                setFieldDiscoveryError((current) => ({
                    ...current,
                    [capability]: t('enterprise.identity.fieldDiscovery.saveFirst'),
                }));
                return;
            }
            setDiscoveringCapability(capability);
            setFieldDiscoveryError((current) => ({ ...current, [capability]: '' }));
            try {
                const params = new URLSearchParams({ capability });
                if (capability === 'directory' && directoryDiscoveryAccount.trim()) {
                    params.set('target_account', directoryDiscoveryAccount.trim());
                }
                const result = await fetchJson<{
                    source?: string;
                    fields?: ProviderDiscoveredField[];
                    paths?: string[];
                }>(
                    `/enterprise/identity-providers/${existingProvider.id}/discover-field-paths?${params}`,
                    { method: 'POST' },
                );
                const fields = result.fields || (result.paths || []).map((path) => ({
                    path,
                    sample_value: '',
                }));
                if (capability === 'login' && result.source === 'authorization_required') {
                    setDiscoveredFields((current) => ({ ...current, login: [] }));
                    return;
                }
                setDiscoveredFields((current) => ({
                    ...current,
                    [capability]: fields,
                }));
            } catch (error) {
                setFieldDiscoveryError((current) => ({
                    ...current,
                    [capability]: t('enterprise.identity.fieldDiscovery.failed'),
                }));
            } finally {
                setDiscoveringCapability(null);
            }
        };

        const providerBaseUrl = (() => {
            const rawDomain = existingProvider?.sso_domain || tenant?.sso_domain || '';
            if (rawDomain) {
                return rawDomain.startsWith('http') ? rawDomain : `https://${rawDomain}`;
            }
            return window.location.origin;
        })();
        const providerCallbackUrl = `${providerBaseUrl}/api/auth/${type}/callback`;
        const identityMatchFields = normalizeIdentityMatchFields(
            form.config?.identity_match_policy?.ordered_fields,
        );
        const enterpriseRootName = form.config?.directory?.root_mapping?.root_name
            ?? tenant?.name
            ?? '';
        const updateEnterpriseRootName = (rootName: string) => {
            setForm((current: any) => ({
                ...current,
                config: {
                    ...(current.config || {}),
                    directory: {
                        ...(current.config?.directory || {}),
                        root_mapping: {
                            ...(current.config?.directory?.root_mapping || {}),
                            root_name: rootName,
                        },
                    },
                },
            }));
        };
        const updateIdentityMatchFields = (orderedFields: IdentityMatchField[]) => {
            setForm((current: any) => ({
                ...current,
                config: {
                    ...(current.config || {}),
                    identity_match_policy: {
                        version: 1,
                        ordered_fields: orderedFields,
                        match_mode: 'normalized_exact',
                        on_lower_priority_conflict: 'bind_highest_priority_and_flag',
                        allow_name_match: false,
                    },
                },
            }));
        };

        return (
            <div style={{ marginTop: '16px', paddingTop: '16px', borderTop: '1px solid var(--border-subtle)' }}>
                {/* Setup Guide moved to the top */}
                {['feishu', 'dingtalk', 'google_workspace', 'wecom'].includes(type) && (
                    <div style={{ background: 'var(--bg-primary)', padding: '16px', borderRadius: '8px', border: '1px solid var(--border-subtle)', marginBottom: '20px', fontSize: '12px' }}>
                        <div style={{ fontWeight: 600, fontSize: '13px', marginBottom: '8px', color: 'var(--text-primary)', display: 'flex', alignItems: 'center', gap: '6px' }}>
                            <IconSettings size={15} stroke={1.8} /> {t('enterprise.org.syncSetupGuide')}
                        </div>
                        <div style={{ color: 'var(--text-secondary)', lineHeight: 1.6 }}>
                            {type === 'feishu' && (
                                <>
                                    {Array.from({ length: 7 }).map((_, i) => (
                                        <div key={i} style={{ marginBottom: '6px' }}>
                                            {i + 1}. {t(`enterprise.org.syncGuide.feishu.step${i + 1}`)}
                                        </div>
                                    ))}
                                    <div style={{ marginTop: '16px', marginBottom: '8px' }}>
                                        {t('enterprise.org.feishuGuideText')}
                                    </div>
                                    <div style={{ position: 'relative', background: '#282c34', borderRadius: '6px', padding: '12px', paddingRight: '40px', color: '#abb2bf', fontFamily: 'monospace', fontSize: '11px', whiteSpace: 'pre-wrap', overflowX: 'auto' }}>
                                        <LinearCopyButton
                                            className="btn btn-ghost"
                                            style={{ position: 'absolute', top: '8px', right: '8px', fontSize: '10px', color: '#abb2bf', padding: '4px 8px', background: 'rgba(255,255,255,0.1)', cursor: 'pointer', border: 'none', borderRadius: '4px', height: 'fit-content', minWidth: '60px' }}
                                            textToCopy={FEISHU_SYNC_PERM_JSON}
                                            label={t('common.copy')}
                                            copiedLabel={t('common.copied')}
                                        />
                                        {FEISHU_SYNC_PERM_JSON}
                                    </div>
                                    <div style={{ marginTop: '8px', color: 'var(--text-secondary)' }}>
                                        {t('enterprise.org.feishuGuideWarning')}
                                    </div>
                                </>
                            )}
                            {type === 'dingtalk' && (
                                <>
                                    {Array.from({ length: 6 }).map((_, i) => (
                                        <div key={i} style={{ marginBottom: '6px' }}>
                                            {i + 1}. {t(`enterprise.org.syncGuide.dingtalk.step${i + 1}`)}
                                        </div>
                                    ))}
                                </>
                            )}
                            {type === 'google_workspace' && (
                                <>
                                    {Array.from({ length: 5 }).map((_, i) => (
                                        <div key={i} style={{ marginBottom: '6px' }}>
                                            {i + 1}. {t(`enterprise.org.syncGuide.google_workspace.step${i + 1}`)}
                                        </div>
                                    ))}
                                </>
                            )}
                            {type === 'wecom' && (
                                <>
                                    {Array.from({ length: 5 }).map((_, i) => (
                                        <div key={i} style={{ marginBottom: '6px' }}>
                                            {i + 1}. {t(`enterprise.org.syncGuide.wecom.step${i + 1}`)}
                                        </div>
                                    ))}
                                </>
                            )}
                        </div>
                    </div>
                )}

                {/* Connection name for generic standards-based providers. */}
                {type === 'oauth2' && (
                    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px', marginBottom: '16px' }}>
                        <div className="form-group">
                            <label className="form-label">{t('enterprise.identity.name')}</label>
                            <TextInput value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} />
                        </div>
                    </div>
                )}

                {type === 'oauth2' ? (
                    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px' }}>
                        <div style={{ gridColumn: '1 / -1', fontSize: '11px', color: 'var(--text-tertiary)' }}>
                            {t('enterprise.identity.providerHints.oauth2')}
                        </div>
                        <div className="form-group">
                            <label className="form-label">{t('enterprise.identity.clientId')}</label>
                            <TextInput value={form.app_id} onChange={e => setForm({ ...form, app_id: e.target.value })} />
                        </div>
                        <div className="form-group">
                            <label className="form-label">{t('enterprise.identity.clientSecret')}</label>
                            <TextInput type="password" value={form.app_secret} onChange={e => setForm({ ...form, app_secret: e.target.value })} />
                        </div>
                        <div className="form-group" style={{ gridColumn: '1 / -1' }}>
                            <label className="form-label">{t('enterprise.identity.authorizeUrl')}</label>
                            <TextInput value={form.authorize_url} onChange={e => setForm({ ...form, authorize_url: e.target.value })} placeholder={t('enterprise.identity.authorizeUrlPlaceholder')} />
                        </div>
                        <div className="form-group">
                            <label className="form-label">{t('enterprise.identity.tokenUrl')}</label>
                            <TextInput value={form.token_url} onChange={e => setForm({ ...form, token_url: e.target.value })} placeholder={t('enterprise.identity.tokenUrlPlaceholder')} />
                        </div>
                        <div className="form-group">
                            <label className="form-label">{t('enterprise.identity.userInfoUrl')}</label>
                            <TextInput value={form.user_info_url} onChange={e => setForm({ ...form, user_info_url: e.target.value })} placeholder={t('enterprise.identity.userInfoUrlPlaceholder')} />
                        </div>
                        <div className="form-group" style={{ gridColumn: '1 / -1' }}>
                            <label className="form-label">{t('enterprise.identity.scope')}</label>
                            <TextInput value={form.scope} onChange={e => setForm({ ...form, scope: e.target.value })} placeholder={t('enterprise.identity.scopePlaceholder')} />
                        </div>
                        <div className="form-group" style={{ gridColumn: '1 / -1' }}>
                            <label className="form-label">{t('enterprise.identity.scimBaseUrl')}</label>
                            <TextInput value={form.scim_base_url || ''} onChange={e => setForm({ ...form, scim_base_url: e.target.value })} placeholder={t('enterprise.identity.scimBaseUrlPlaceholder')} />
                            <div style={{ fontSize: '10px', color: 'var(--text-tertiary)', marginTop: '4px' }}>
                                {t('enterprise.identity.scimBaseUrlHint')}
                            </div>
                        </div>
                        <ProviderFieldMappingEditor
                            title={t('enterprise.identity.loginFieldMapping')}
                            hint={t('enterprise.identity.loginFieldMappingHint')}
                            fields={OAUTH_FIELD_MAPPING_KEYS.map((field) => ({
                                key: field,
                                label: t(`enterprise.identity.${field === 'user_id' ? 'userId' : field}Field`),
                                defaultPath: OAUTH_FIELD_DEFAULTS[field],
                            }))}
                            value={form.field_mapping || {}}
                            onChange={(fieldMapping) => setForm({ ...form, field_mapping: fieldMapping })}
                            discoveredFields={discoveredFields.login || []}
                            onDiscover={() => void discoverFieldPaths('login')}
                            discovering={discoveringCapability === 'login'}
                            discoverLabel={t('enterprise.identity.fieldDiscovery.action')}
                            discoveringLabel={t('enterprise.identity.fieldDiscovery.running')}
                            discoveryError={fieldDiscoveryError.login}
                        />
                        <ProviderFieldMappingEditor
                            title={t('enterprise.identity.directoryFieldMapping')}
                            hint={t('enterprise.identity.directoryFieldMappingHint')}
                            fields={Object.entries(SCIM_FIELD_DEFAULTS).map(([field, defaultPath]) => ({
                                key: field,
                                label: t(`enterprise.identity.directoryFields.${field}`),
                                defaultPath,
                            }))}
                            value={form.directory_field_mapping || {}}
                            onChange={(fieldMapping) => setForm({
                                ...form,
                                directory_field_mapping: fieldMapping,
                            })}
                            discoveredFields={discoveredFields.directory || []}
                            onDiscover={() => void discoverFieldPaths('directory')}
                            discovering={discoveringCapability === 'directory'}
                            discoverLabel={t('enterprise.identity.fieldDiscovery.action')}
                            discoveringLabel={t('enterprise.identity.fieldDiscovery.running')}
                            discoveryError={fieldDiscoveryError.directory}
                            discoveryTarget={directoryDiscoveryAccount}
                            onDiscoveryTargetChange={setDirectoryDiscoveryAccount}
                            discoveryTargetPlaceholder={t('enterprise.identity.fieldDiscovery.targetAccountPlaceholder')}
                            discoveryTargetLabel={t('enterprise.identity.fieldDiscovery.targetAccount')}
                        />
                    </div>
                ) : type === 'wecom' ? (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '0' }}>
                        {/* Prerequisites notice — all strings via i18n */}
                        <div style={{
                            padding: '16px',
                            borderRadius: '8px',
                            border: '1px solid var(--border-subtle)',
                            background: 'var(--bg-primary)',
                            fontSize: '13px',
                            lineHeight: 1.7,
                            color: 'var(--text-secondary)',
                        }}>
                            <div style={{ fontWeight: 600, fontSize: '13px', color: 'var(--text-primary)', marginBottom: '10px' }}>
                                {t('enterprise.identity.wecomNotice.title')}
                            </div>
                            <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
                                <div>
                                    <div style={{ fontWeight: 500, color: 'var(--text-primary)', marginBottom: '3px' }}>
                                        {t('enterprise.identity.wecomNotice.syncTitle')}
                                    </div>
                                    <div style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>
                                        {t('enterprise.identity.wecomNotice.syncDesc')}
                                    </div>
                                </div>
                                <div style={{ borderTop: '1px solid var(--border-subtle)', paddingTop: '10px' }}>
                                    <div style={{ fontWeight: 500, color: 'var(--text-primary)', marginBottom: '3px' }}>
                                        {t('enterprise.identity.wecomNotice.ssoTitle')}
                                    </div>
                                    <div style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>
                                        {t('enterprise.identity.wecomNotice.ssoDesc')}
                                    </div>
                                </div>
                                <div style={{ borderTop: '1px solid var(--border-subtle)', paddingTop: '10px' }}>
                                    <div style={{ fontWeight: 500, color: 'var(--text-primary)', marginBottom: '3px' }}>
                                        {t('enterprise.identity.wecomNotice.messagingTitle')}
                                    </div>
                                    <div style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>
                                        {t('enterprise.identity.wecomNotice.messagingDesc')}
                                    </div>
                                </div>
                            </div>
                            <div style={{ marginTop: '14px', paddingTop: '12px', borderTop: '1px solid var(--border-subtle)', fontSize: '12px', color: 'var(--text-tertiary)', lineHeight: 1.6 }}>
                                {t('enterprise.identity.wecomNotice.footerText')}
                            </div>
                        </div>
                        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px', marginTop: '16px' }}>
                            <div className="form-group">
                                <label className="form-label">{t('enterprise.identity.corpId')}</label>
                                <input className="form-input" value={form.config.corp_id || ''} onChange={e => setForm({ ...form, config: { ...form.config, corp_id: e.target.value } })} />
                            </div>
                            <div className="form-group">
                                <label className="form-label">{t('enterprise.identity.directorySecret')}</label>
                                <input className="form-input" type="password" value={form.config.secret || ''} onChange={e => setForm({ ...form, config: { ...form.config, secret: e.target.value } })} />
                            </div>
                        </div>
                    </div>


                ) : type === 'dingtalk' ? (
                    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px' }}>
                        <div className="form-group" style={{ gridColumn: '1 / -1' }}>
                            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>{t('enterprise.identity.providerHints.dingtalk')}</div>
                            <div style={{ marginTop: '8px', padding: '10px 12px', borderRadius: '6px', border: '1px solid var(--border-subtle)', background: 'var(--bg-primary)' }}>
                                <div style={{ fontWeight: 500, fontSize: '12px', color: 'var(--text-primary)', marginBottom: '4px' }}>
                                    {t('enterprise.identity.dingtalkSyncOnlyNoticeTitle')}
                                </div>
                                <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', lineHeight: 1.5 }}>
                                    {t('enterprise.identity.dingtalkSyncOnlyNoticeDesc')}
                                </div>
                            </div>
                        </div>
                        <div className="form-group">
                            <label className="form-label">{t('enterprise.identity.appKey')}</label>
                            <input className="form-input" value={form.config.app_key || ''} onChange={e => setForm({ ...form, config: { ...form.config, app_key: e.target.value } })} placeholder="dingxxxxxxxxxxxx" />
                        </div>
                        <div className="form-group">
                            <label className="form-label">{t('enterprise.identity.appSecret')}</label>
                            <input className="form-input" type="password" value={form.config.app_secret || ''} onChange={e => setForm({ ...form, config: { ...form.config, app_secret: e.target.value } })} />
                        </div>
                    </div>
                ) : type === 'google_workspace' ? (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
                        <div style={{ padding: '14px', borderRadius: '8px', border: '1px solid var(--border-subtle)', background: 'var(--bg-primary)' }}>
                            <div style={{ fontWeight: 600, fontSize: '13px', marginBottom: '10px' }}>{t('enterprise.identity.googleOAuthTitle')}</div>
                            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px' }}>
                                <div className="form-group" style={{ gridColumn: '1 / -1' }}>
                                    <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
                                        {t('enterprise.identity.providerHints.google_workspace')}
                                    </div>
                                </div>
                                <div className="form-group">
                                    <label className="form-label">{t('enterprise.identity.clientId')}</label>
                                    <input
                                        className="form-input"
                                        value={form.config.client_id || ''}
                                        onChange={e => setForm({ ...form, config: { ...form.config, client_id: e.target.value } })}
                                        placeholder="xxxxxxxx.apps.googleusercontent.com"
                                    />
                                </div>
                                <div className="form-group">
                                    <label className="form-label">{t('enterprise.identity.clientSecret')}</label>
                                    <input
                                        className="form-input"
                                        type="password"
                                        value={form.config.client_secret || ''}
                                        onChange={e => setForm({ ...form, config: { ...form.config, client_secret: e.target.value } })}
                                    />
                                </div>
                                <div className="form-group" style={{ gridColumn: '1 / -1' }}>
                                    <label className="form-label">{t('enterprise.identity.callbackUrl')}</label>
                                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                                        <div style={{
                                            flex: 1,
                                            padding: '8px 12px',
                                            background: 'var(--bg-elevated)',
                                            border: '1px solid var(--border-subtle)',
                                            borderRadius: '6px',
                                            fontSize: '12px',
                                            color: 'var(--text-primary)',
                                            fontFamily: 'monospace',
                                            whiteSpace: 'nowrap',
                                            overflow: 'hidden',
                                            textOverflow: 'ellipsis'
                                        }}>
                                            {providerCallbackUrl}
                                        </div>
                                        <LinearCopyButton
                                            className="btn btn-ghost btn-sm"
                                            style={{ fontSize: '11px', width: 'auto', minWidth: '70px', height: '33px' }}
                                            textToCopy={providerCallbackUrl}
                                            label={t('common.copy')}
                                            copiedLabel={t('common.copied')}
                                        />
                                    </div>
                                    <div style={{ fontSize: '10px', color: 'var(--text-tertiary)', marginTop: '4px' }}>
                                        {t('enterprise.identity.callbackUrlHint')}
                                    </div>
                                </div>
                                <div className="form-group" style={{ gridColumn: '1 / -1' }}>
                                    <label className="form-label">{t('enterprise.identity.googleDirectoryAuthorization')}</label>
                                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
                                        <button
                                            className="btn btn-secondary btn-sm"
                                            type="button"
                                            onClick={() => existingProvider && handleGoogleAdminAuthorize(existingProvider.id)}
                                            disabled={!existingProvider}
                                        >
                                            {existingProvider?.config?.google_admin_authorized_email
                                                ? t('enterprise.identity.googleReauthorizeAdminSync')
                                                : t('enterprise.identity.googleAuthorizeAdminSync')}
                                        </button>
                                        {!existingProvider && (
                                            <span style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
                                                {t('enterprise.identity.googleSaveFirst')}
                                            </span>
                                        )}
                                        {existingProvider?.config?.google_admin_authorized_email && (
                                            <span style={{ fontSize: '11px', color: 'var(--text-secondary)' }}>
                                                {t('enterprise.identity.googleAuthorizedAs', {
                                                    email: existingProvider.config.google_admin_authorized_email,
                                                })}
                                            </span>
                                        )}
                                    </div>
                                    <div style={{ fontSize: '10px', color: 'var(--text-tertiary)', marginTop: '4px' }}>
                                        {t('enterprise.identity.googleDirectoryAuthorizationHint')}
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                ) : type === 'feishu' ? (
                    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px' }}>
                        <div className="form-group" style={{ gridColumn: '1 / -1' }}>
                            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>{t('enterprise.identity.providerHints.feishu')}</div>
                        </div>
                        <div className="form-group">
                            <label className="form-label">{t('enterprise.identity.appId')}</label>
                            <input className="form-input" value={form.config.app_id || ''} onChange={e => setForm({ ...form, config: { ...form.config, app_id: e.target.value } })} placeholder="cli_xxxxxxxxxxxx" />
                        </div>
                        <div className="form-group">
                            <label className="form-label">{t('enterprise.identity.appSecret')}</label>
                            <input className="form-input" type="password" value={form.config.app_secret || ''} onChange={e => setForm({ ...form, config: { ...form.config, app_secret: e.target.value } })} />
                        </div>
                    </div>
                ) : null}

                <div style={{ marginTop: '16px', maxWidth: '520px' }}>
                    <label className="form-label">
                        {t('enterprise.identity.enterpriseRootMapping.title')}
                    </label>
                    <TextInput
                        aria-label={t('enterprise.identity.enterpriseRootMapping.title')}
                        value={enterpriseRootName}
                        onChange={(event) => updateEnterpriseRootName(event.target.value)}
                        placeholder={t('enterprise.identity.enterpriseRootMapping.placeholder')}
                    />
                    <div style={{ fontSize: '10px', color: 'var(--text-tertiary)', marginTop: '4px' }}>
                        {t('enterprise.identity.enterpriseRootMapping.hint')}
                    </div>
                </div>

                <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginTop: '16px' }}>
                    <ToggleSwitch
                        checked={form.is_active !== false}
                        onChange={(checked) => setForm({ ...form, is_active: checked })}
                        ariaLabel={t('enterprise.identity.providerEnabled')}
                    />
                    <div>
                        <div style={{ fontSize: '12px', fontWeight: 500 }}>
                            {t('enterprise.identity.providerEnabled')}
                        </div>
                        <div style={{ fontSize: '10px', color: 'var(--text-tertiary)' }}>
                            {t('enterprise.identity.providerEnabledHint')}
                        </div>
                    </div>
                </div>

                <div style={{ marginTop: '16px', maxWidth: '520px' }}>
                    <label className="form-label">
                        {t('enterprise.identity.identityMatchPriority.title')}
                    </label>
                    <OrderedFieldListEditor
                        values={identityMatchFields}
                        options={IDENTITY_MATCH_FIELDS.map((field) => ({
                            value: field,
                            label: t(`enterprise.identity.identityMatchPriority.fields.${field}`),
                        }))}
                        onChange={updateIdentityMatchFields}
                        addLabel={t('enterprise.identity.identityMatchPriority.addField')}
                        fieldAriaLabel={(position) => t(
                            'enterprise.identity.identityMatchPriority.fieldAriaLabel',
                            { position },
                        )}
                        moveUpLabel={t('enterprise.identity.identityMatchPriority.moveUp')}
                        moveDownLabel={t('enterprise.identity.identityMatchPriority.moveDown')}
                        removeLabel={t('enterprise.identity.identityMatchPriority.remove')}
                    />
                    <div style={{ fontSize: '10px', color: 'var(--text-tertiary)', marginTop: '4px' }}>
                        {t('enterprise.identity.identityMatchPriority.hint')}
                    </div>
                </div>

                <div style={{ display: 'flex', gap: '8px', alignItems: 'center', marginTop: '16px' }}>
                    <Button type="button" variant="primary" className="btn-sm" onClick={save} disabled={savingProvider}>
                        {savingProvider ? t('common.loading') : t('common.save')}
                    </Button>
                    {saveProviderOk && (
                        <span style={{ fontSize: '12px', color: 'var(--success)' }}>
                            {t('enterprise.identity.savedStatus')}
                        </span>
                    )}
                    {existingProvider && (
                        <Button type="button" variant="ghost" className="btn-sm" style={{ color: 'var(--error)' }} onClick={async () => { const ok = await dialog.confirm(t('common.dialog.deleteConfigConfirm'), { title: t('common.dialog.deleteConfig'), danger: true, confirmLabel: t('common.confirmActions.deleteLabel') }); if (ok) deleteProvider.mutate(existingProvider.id); }}>
                            {t('common.delete')}
                        </Button>
                    )}
                </div>
            </div>
        );
}
