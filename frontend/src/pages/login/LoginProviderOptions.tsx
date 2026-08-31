import type { TFunction } from 'i18next';

const ssoMeta: Record<string, { label: string; icon: string }> = {
    feishu: { label: 'Feishu', icon: '/feishu.png' },
    dingtalk: { label: 'DingTalk', icon: '/dingtalk.png' },
    wecom: { label: 'WeCom', icon: '/wecom.png' },
    google: { label: 'Google', icon: '/google.svg' },
    google_workspace: { label: 'Google', icon: '/google.svg' },
};

interface SsoLoginOptionsProps {
    loginTenantId: string;
    tenant: any;
    providers: any[];
    loading: boolean;
    error: string;
    onStart: (providerType: string) => void | Promise<void>;
    t: TFunction;
}

export function SsoLoginOptions({
    loginTenantId,
    tenant,
    providers,
    loading,
    error,
    onStart,
    t,
}: SsoLoginOptionsProps) {
    return (
        <div style={{ marginBottom: '24px' }}>
            <div style={{
                padding: '16px', borderRadius: '12px', background: 'rgba(59,130,246,0.08)',
                border: '1px solid rgba(59,130,246,0.15)', marginBottom: '16px',
                textAlign: 'center'
            }}>
                <div style={{ fontSize: '13px', fontWeight: 600, color: 'var(--accent-primary)', marginBottom: '4px' }}>
                    {(tenant?.id === loginTenantId && tenant?.name) || t('auth.enterpriseLogin', 'Enterprise login')}
                </div>
                <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
                    {t('auth.ssoNotice', 'Enterprise SSO is enabled for this domain.')}
                </div>
            </div>

            {loading && (
                <div style={{ textAlign: 'center', color: 'var(--text-tertiary)', fontSize: '12px' }}>
                    {t('auth.ssoLoading', 'Loading SSO providers...')}
                </div>
            )}

            {!loading && providers.length > 0 && (
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))', gap: '12px' }}>
                    {providers.map(p => {
                        const meta = ssoMeta[p.provider_type] || { label: p.name || p.provider_type, icon: '' };
                        return (
                            <button
                                key={p.provider_type}
                                className="login-submit"
                                style={{
                                    background: 'var(--bg-secondary)',
                                    color: 'var(--text-primary)',
                                    display: 'flex',
                                    alignItems: 'center',
                                    justifyContent: 'center',
                                    gap: '10px',
                                    border: '1px solid var(--border-subtle)',
                                }}
                                onClick={() => onStart(p.provider_type)}
                            >
                                {meta.icon ? (
                                    <img src={meta.icon} alt={meta.label} width={18} height={18} />
                                ) : (
                                    <span style={{ width: 18, height: 18, borderRadius: 4, background: 'var(--bg-tertiary)', display: 'inline-flex', alignItems: 'center', justifyContent: 'center', fontSize: 10 }}>
                                        {(meta.label || '').slice(0, 1).toUpperCase()}
                                    </span>
                                )}
                                {meta.label || p.name || p.provider_type}
                            </button>
                        );
                    })}
                </div>
            )}

            {!loading && providers.length === 0 && (
                <div style={{ textAlign: 'center', color: 'var(--text-tertiary)', fontSize: '12px' }}>
                    {error || t('auth.ssoNoProviders', 'No SSO providers configured.')}
                </div>
            )}

            <div style={{
                display: 'flex', alignItems: 'center', gap: '12px',
                margin: '20px 0', color: 'var(--text-tertiary)', fontSize: '11px'
            }}>
                <div style={{ flex: 1, height: '1px', background: 'var(--border-subtle)' }} />
                {t('auth.or', 'or')}
                <div style={{ flex: 1, height: '1px', background: 'var(--border-subtle)' }} />
            </div>
        </div>
    );
}

interface OAuthLoginOptionsProps {
    providers: any[];
    loading: boolean;
    error: string;
    onStart: (providerType: string) => void | Promise<void>;
    t: TFunction;
}

export function OAuthLoginOptions({ providers, loading, error, onStart, t }: OAuthLoginOptionsProps) {
    return (
        <div style={{ marginBottom: '24px' }}>
            {loading && (
                <div style={{ textAlign: 'center', color: 'var(--text-tertiary)', fontSize: '12px' }}>
                    Loading social login providers...
                </div>
            )}

            {!loading && providers.length > 0 && (
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))', gap: '12px' }}>
                    {providers.map(p => {
                        const meta = ssoMeta[p.provider_type] || { label: p.name || p.provider_type, icon: '' };
                        return (
                            <button
                                key={p.provider_type}
                                className="login-submit"
                                type="button"
                                style={{
                                    background: 'var(--bg-secondary)',
                                    color: 'var(--text-primary)',
                                    display: 'flex',
                                    alignItems: 'center',
                                    justifyContent: 'center',
                                    gap: '10px',
                                    border: '1px solid var(--border-subtle)',
                                }}
                                onClick={() => onStart(p.provider_type)}
                            >
                                {meta.icon ? (
                                    <img
                                        src={meta.icon}
                                        width={18}
                                        height={18}
                                        alt=""
                                        aria-hidden="true"
                                    />
                                ) : (
                                    <span style={{ width: 18, height: 18, borderRadius: 4, background: 'var(--bg-tertiary)', display: 'inline-flex', alignItems: 'center', justifyContent: 'center', fontSize: 10 }}>
                                        {(meta.label || '').slice(0, 1).toUpperCase()}
                                    </span>
                                )}
                                Continue with {meta.label || p.name || p.provider_type}
                            </button>
                        );
                    })}
                </div>
            )}

            {!loading && providers.length === 0 && error && (
                <div style={{ textAlign: 'center', color: 'var(--text-tertiary)', fontSize: '12px' }}>
                    {error}
                </div>
            )}

            {!loading && providers.length > 0 && (
                <div style={{
                    display: 'flex', alignItems: 'center', gap: '12px',
                    margin: '20px 0', color: 'var(--text-tertiary)', fontSize: '11px'
                }}>
                    <div style={{ flex: 1, height: '1px', background: 'var(--border-subtle)' }} />
                    {t('auth.or', 'or')}
                    <div style={{ flex: 1, height: '1px', background: 'var(--border-subtle)' }} />
                </div>
            )}
        </div>
    );
}
