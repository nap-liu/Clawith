import React from 'react';
import { IconSettings } from '@tabler/icons-react';
import LinearCopyButton from '../../../../components/LinearCopyButton';

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
        const providerBaseUrl = (() => {
            const rawDomain = existingProvider?.sso_domain || tenant?.sso_domain || '';
            if (rawDomain) {
                return rawDomain.startsWith('http') ? rawDomain : `https://${rawDomain}`;
            }
            return window.location.origin;
        })();
        const providerCallbackUrl = `${providerBaseUrl}/api/auth/${type}/callback`;

        return (
            <div style={{ marginTop: '16px', paddingTop: '16px', borderTop: '1px solid var(--border-subtle)' }}>
                {/* Setup Guide moved to the top */}
                {['feishu', 'dingtalk', 'google_workspace'].includes(type) && (
                    <div style={{ background: 'var(--bg-primary)', padding: '16px', borderRadius: '8px', border: '1px solid var(--border-subtle)', marginBottom: '20px', fontSize: '12px' }}>
                        <div style={{ fontWeight: 600, fontSize: '13px', marginBottom: '8px', color: 'var(--text-primary)', display: 'flex', alignItems: 'center', gap: '6px' }}>
                            <IconSettings size={15} stroke={1.8} /> {t('enterprise.org.syncSetupGuide', 'Setup Guide & Required Permissions')}
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
                                        {t('enterprise.org.feishuGuideText', 'Permission JSON (bulk import)')}
                                    </div>
                                    <div style={{ position: 'relative', background: '#282c34', borderRadius: '6px', padding: '12px', paddingRight: '40px', color: '#abb2bf', fontFamily: 'monospace', fontSize: '11px', whiteSpace: 'pre-wrap', overflowX: 'auto' }}>
                                        <LinearCopyButton
                                            className="btn btn-ghost"
                                            style={{ position: 'absolute', top: '8px', right: '8px', fontSize: '10px', color: '#abb2bf', padding: '4px 8px', background: 'rgba(255,255,255,0.1)', cursor: 'pointer', border: 'none', borderRadius: '4px', height: 'fit-content', minWidth: '60px' }}
                                            textToCopy={FEISHU_SYNC_PERM_JSON}
                                            label="Copy"
                                            copiedLabel="Copied✓"
                                        />
                                        {FEISHU_SYNC_PERM_JSON}
                                    </div>
                                    <div style={{ marginTop: '8px', color: 'var(--text-secondary)' }}>
                                        {t('enterprise.org.feishuGuideWarning', 'Note: You must re-publish the app each time you add new permissions.')}
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

                {/* Name field only for oauth2 */}
                {type === 'oauth2' && (
                    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px', marginBottom: '16px' }}>
                        <div className="form-group">
                            <label className="form-label">{t('enterprise.identity.name')}</label>
                            <input className="form-input" value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} />
                        </div>
                    </div>
                )}

                {type === 'oauth2' ? (
                    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px' }}>
                        <div className="form-group">
                            <label className="form-label">Client ID</label>
                            <input className="form-input" value={form.app_id} onChange={e => setForm({ ...form, app_id: e.target.value })} />
                        </div>
                        <div className="form-group">
                            <label className="form-label">Client Secret</label>
                            <input className="form-input" type="password" value={form.app_secret} onChange={e => setForm({ ...form, app_secret: e.target.value })} />
                        </div>
                        <div className="form-group" style={{ gridColumn: '1 / -1' }}>
                            <label className="form-label">Authorize URL</label>
                            <input className="form-input" value={form.authorize_url} onChange={e => setForm({ ...form, authorize_url: e.target.value })} />
                        </div>
                        <div className="form-group">
                            <label className="form-label">Token URL</label>
                            <input className="form-input" value={form.token_url} onChange={e => setForm({ ...form, token_url: e.target.value })} placeholder="optional · auto-derived from Authorize URL" />
                        </div>
                        <div className="form-group">
                            <label className="form-label">UserInfo URL</label>
                            <input className="form-input" value={form.user_info_url} onChange={e => setForm({ ...form, user_info_url: e.target.value })} placeholder="optional · auto-derived from Authorize URL" />
                        </div>
                        <div className="form-group" style={{ gridColumn: '1 / -1' }}>
                            <label className="form-label">Scope</label>
                            <input className="form-input" value={form.scope} onChange={e => setForm({ ...form, scope: e.target.value })} placeholder="e.g. openid,profile,email" />
                        </div>
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
                    </div>


                ) : type === 'dingtalk' ? (
                    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px' }}>
                        <div className="form-group" style={{ gridColumn: '1 / -1' }}>
                            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>{t('enterprise.identity.providerHints.dingtalk')}</div>
                            <div style={{ marginTop: '8px', padding: '10px 12px', borderRadius: '6px', border: '1px solid var(--border-subtle)', background: 'var(--bg-primary)' }}>
                                <div style={{ fontWeight: 500, fontSize: '12px', color: 'var(--text-primary)', marginBottom: '4px' }}>
                                    {t('enterprise.identity.dingtalkSyncOnlyNoticeTitle', 'Directory sync works without DingTalk login')}
                                </div>
                                <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', lineHeight: 1.5 }}>
                                    {t('enterprise.identity.dingtalkSyncOnlyNoticeDesc', 'Use AppKey/AppSecret to sync DingTalk contacts. Turn on SSO Login only when users also need to sign in with DingTalk.')}
                                </div>
                            </div>
                        </div>
                        <div className="form-group">
                            <label className="form-label">App Key</label>
                            <input className="form-input" value={form.config.app_key || ''} onChange={e => setForm({ ...form, config: { ...form.config, app_key: e.target.value } })} placeholder="dingxxxxxxxxxxxx" />
                        </div>
                        <div className="form-group">
                            <label className="form-label">App Secret</label>
                            <input className="form-input" type="password" value={form.config.app_secret || ''} onChange={e => setForm({ ...form, config: { ...form.config, app_secret: e.target.value } })} />
                        </div>
                    </div>
                ) : type === 'google_workspace' ? (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
                        <div style={{ padding: '14px', borderRadius: '8px', border: '1px solid var(--border-subtle)', background: 'var(--bg-primary)' }}>
                            <div style={{ fontWeight: 600, fontSize: '13px', marginBottom: '10px' }}>Google OAuth</div>
                            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px' }}>
                                <div className="form-group" style={{ gridColumn: '1 / -1' }}>
                                    <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
                                        {t('enterprise.identity.providerHints.google_workspace', 'Google Workspace: use one Client ID and Client Secret for both SSO and admin-authorized directory sync.')}
                                    </div>
                                </div>
                                <div className="form-group">
                                    <label className="form-label">Client ID</label>
                                    <input
                                        className="form-input"
                                        value={form.config.client_id || ''}
                                        onChange={e => setForm({ ...form, config: { ...form.config, client_id: e.target.value } })}
                                        placeholder="xxxxxxxx.apps.googleusercontent.com"
                                    />
                                </div>
                                <div className="form-group">
                                    <label className="form-label">Client Secret</label>
                                    <input
                                        className="form-input"
                                        type="password"
                                        value={form.config.client_secret || ''}
                                        onChange={e => setForm({ ...form, config: { ...form.config, client_secret: e.target.value } })}
                                    />
                                </div>
                                <div className="form-group" style={{ gridColumn: '1 / -1' }}>
                                    <label className="form-label">{t('enterprise.identity.callbackUrl', 'Redirect URL (paste this in your app settings)')}</label>
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
                                            label={t('common.copy', 'Copy')}
                                            copiedLabel="Copied"
                                        />
                                    </div>
                                    <div style={{ fontSize: '10px', color: 'var(--text-tertiary)', marginTop: '4px' }}>
                                        {t('enterprise.identity.callbackUrlHint', "Add this URL as the OAuth redirect URI in your identity provider's app configuration.")}
                                    </div>
                                </div>
                                <div className="form-group" style={{ gridColumn: '1 / -1' }}>
                                    <label className="form-label">Directory Sync Authorization</label>
                                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
                                        <button
                                            className="btn btn-secondary btn-sm"
                                            type="button"
                                            onClick={() => existingProvider && handleGoogleAdminAuthorize(existingProvider.id)}
                                            disabled={!existingProvider}
                                        >
                                            {existingProvider?.config?.google_admin_authorized_email ? 'Re-authorize Admin Sync' : 'Authorize Admin Sync'}
                                        </button>
                                        {!existingProvider && (
                                            <span style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
                                                Please save the provider first.
                                            </span>
                                        )}
                                        {existingProvider?.config?.google_admin_authorized_email && (
                                            <span style={{ fontSize: '11px', color: 'var(--text-secondary)' }}>
                                                Authorized as {existingProvider.config.google_admin_authorized_email}
                                            </span>
                                        )}
                                    </div>
                                    <div style={{ fontSize: '10px', color: 'var(--text-tertiary)', marginTop: '4px' }}>
                                        Sign in with a Google Workspace admin account to grant directory read access. The platform will securely store a refresh token and use it for scheduled sync.
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
                            <label className="form-label">App ID</label>
                            <input className="form-input" value={form.config.app_id || ''} onChange={e => setForm({ ...form, config: { ...form.config, app_id: e.target.value } })} placeholder="cli_xxxxxxxxxxxx" />
                        </div>
                        <div className="form-group">
                            <label className="form-label">App Secret</label>
                            <input className="form-input" type="password" value={form.config.app_secret || ''} onChange={e => setForm({ ...form, config: { ...form.config, app_secret: e.target.value } })} />
                        </div>
                    </div>
                ) : null}

                {/* Hide save/delete for WeCom while config is disabled */}
                {type !== 'wecom' && (
                    <div style={{ display: 'flex', gap: '8px', alignItems: 'center', marginTop: '16px' }}>
                        <button className="btn btn-primary btn-sm" onClick={save} disabled={savingProvider}>
                            {savingProvider ? t('common.loading') : t('common.save', 'Save')}
                        </button>
                        {saveProviderOk && (
                            <span style={{ fontSize: '12px', color: 'var(--success)' }}>Saved</span>
                        )}
                        {existingProvider && (
                            <button className="btn btn-ghost btn-sm" style={{ color: 'var(--error)' }} onClick={async () => { const ok = await dialog.confirm(t('common.dialog.deleteConfigConfirm'), { title: t('common.dialog.deleteConfig'), danger: true, confirmLabel: t('common.confirmActions.deleteLabel') }); if (ok) deleteProvider.mutate(existingProvider.id); }}>
                                {t('common.delete', 'Delete')}
                            </button>
                        )}
                    </div>
                )}
                {/* WeCom App IP Whitelist verification URL — hidden while WeCom config is disabled */}
                {type === 'wecom' && false && editingId && (existingProvider?.config?.verify_token || form.config?.verify_token) && (() => {
                    const verifyToken = form.config?.verify_token || existingProvider?.config?.verify_token || '';
                    const aesKey = form.config?.verify_aes_key || existingProvider?.config?.verify_aes_key || '';
                    // Use window.location.origin as the base, but if it's a private/non-standard URL let user know
                    const base = window.location.origin;
                    const callbackUrl = aesKey
                        ? `${base}/api/enterprise/org/wecom-callback/${verifyToken}?aes_key=${aesKey}`
                        : `${base}/api/enterprise/org/wecom-callback/${verifyToken}?aes_key=(configure EncodingAESKey above first)`;
                    return (
                        <div style={{ marginTop: '16px', padding: '12px', background: 'var(--bg-primary)', borderRadius: '6px', border: '1px solid var(--border-subtle)' }}>
                            <div style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-secondary)', marginBottom: '6px' }}>
                                WeCom Receive Message Server URL
                            </div>
                            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginBottom: '8px' }}>
                                Step 1: Go to WeCom App Management (AgentID 1000010) → App Settings → Set Receive Message Server URL.
                                Use this URL. In the Token field, enter your Verify Token. In EncodingAESKey, enter your key below.
                            </div>
                            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                                <code style={{ flex: 1, fontSize: '11px', padding: '6px 10px', background: 'var(--bg-secondary)', borderRadius: '4px', wordBreak: 'break-all', color: 'var(--text-primary)', fontFamily: 'var(--font-mono)' }}>
                                    {callbackUrl}
                                </code>
                                {aesKey && (
                                    <LinearCopyButton
                                        className="btn btn-ghost"
                                        style={{ fontSize: '11px', padding: '4px 8px', whiteSpace: 'nowrap', flexShrink: 0 }}
                                        textToCopy={callbackUrl}
                                        label="Copy"
                                        copiedLabel="Copied"
                                    />
                                )}
                            </div>
                            {!aesKey && (
                                <div style={{ marginTop: '6px', fontSize: '11px', color: 'var(--warning, #f59e0b)' }}>
                                    Configure the Verify Token and EncodingAESKey fields above, then Save to generate the final URL.
                                </div>
                            )}
                            <div style={{ marginTop: '10px', fontSize: '11px', color: 'var(--text-tertiary)' }}>
                                Step 2: After URL verification passes, configure Enterprise Trusted IP with your server IPs in the WeCom console.
                            </div>
                            <div style={{ marginTop: '4px', fontSize: '11px', color: 'var(--text-tertiary)' }}>
                                Step 3: Paste the App Secret (from that same app page) into the App Secret field above.
                            </div>
                        </div>
                    );
                })()}

            </div>
        );
}
