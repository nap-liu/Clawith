import type { TFunction } from 'i18next';

const ssoMeta: Record<string, { labelKey: string; fallback: string; icon: string }> = {
    feishu: { labelKey: 'enterprise.identity.providers.feishu.name', fallback: 'Feishu', icon: '/feishu.png' },
    dingtalk: { labelKey: 'enterprise.identity.providers.dingtalk.name', fallback: 'DingTalk', icon: '/dingtalk.png' },
    wecom: { labelKey: 'enterprise.identity.providers.wecom.name', fallback: 'WeCom', icon: '/wecom.png' },
    google: { labelKey: 'enterprise.identity.providers.google_workspace.name', fallback: 'Google', icon: '/google.svg' },
    google_workspace: { labelKey: 'enterprise.identity.providers.google_workspace.name', fallback: 'Google', icon: '/google.svg' },
};

interface SsoLoginOptionsProps {
    providers: any[];
    loading: boolean;
    error: string;
    onStart: (providerType: string) => void | Promise<void>;
    showDivider: boolean;
    t: TFunction;
}

export function SsoLoginOptions({
    providers,
    loading,
    error,
    onStart,
    showDivider,
    t,
}: SsoLoginOptionsProps) {
    return (
        <div style={{ marginBottom: '24px' }}>
            {loading && (
                <div style={{ textAlign: 'center', color: 'var(--text-tertiary)', fontSize: '12px' }}>
                    {t('auth.ssoLoading', 'Loading SSO providers...')}
                </div>
            )}

            {!loading && providers.length > 0 && (
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))', gap: '12px' }}>
                    {providers.map(p => {
                        const meta = ssoMeta[p.provider_type];
                        const label = meta
                            ? t(meta.labelKey, meta.fallback)
                            : p.name || p.provider_type;
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
                                {meta?.icon ? (
                                    <img src={meta.icon} alt="" aria-hidden="true" width={18} height={18} />
                                ) : (
                                    <span style={{ width: 18, height: 18, borderRadius: 4, background: 'var(--bg-tertiary)', display: 'inline-flex', alignItems: 'center', justifyContent: 'center', fontSize: 10 }}>
                                        {label.slice(0, 1).toUpperCase()}
                                    </span>
                                )}
                                {label}
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

            {showDivider && <LoginOptionDivider t={t} />}
        </div>
    );
}

interface OAuthLoginOptionsProps {
    providers: any[];
    loading: boolean;
    error: string;
    onStart: (providerType: string) => void | Promise<void>;
    showDivider: boolean;
    t: TFunction;
}

export function OAuthLoginOptions({ providers, loading, error, onStart, showDivider, t }: OAuthLoginOptionsProps) {
    return (
        <div style={{ marginBottom: '24px' }}>
            {loading && (
                <div style={{ textAlign: 'center', color: 'var(--text-tertiary)', fontSize: '12px' }}>
                    {t('auth.oauthLoading', 'Loading social login providers...')}
                </div>
            )}

            {!loading && providers.length > 0 && (
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))', gap: '12px' }}>
                    {providers.map(p => {
                        const meta = ssoMeta[p.provider_type];
                        const label = meta
                            ? t(meta.labelKey, meta.fallback)
                            : p.name || p.provider_type;
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
                                {meta?.icon ? (
                                    <img
                                        src={meta.icon}
                                        width={18}
                                        height={18}
                                        alt=""
                                        aria-hidden="true"
                                    />
                                ) : (
                                    <span style={{ width: 18, height: 18, borderRadius: 4, background: 'var(--bg-tertiary)', display: 'inline-flex', alignItems: 'center', justifyContent: 'center', fontSize: 10 }}>
                                        {label.slice(0, 1).toUpperCase()}
                                    </span>
                                )}
                                {t('auth.continueWithProvider', 'Continue with {{provider}}', { provider: label })}
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

            {!loading && providers.length > 0 && showDivider && <LoginOptionDivider t={t} />}
        </div>
    );
}

function LoginOptionDivider({ t }: { t: TFunction }) {
    return (
        <div style={{
            display: 'flex', alignItems: 'center', gap: '12px',
            margin: '20px 0', color: 'var(--text-tertiary)', fontSize: '11px'
        }}>
            <div style={{ flex: 1, height: '1px', background: 'var(--border-subtle)' }} />
            {t('auth.or', 'or')}
            <div style={{ flex: 1, height: '1px', background: 'var(--border-subtle)' }} />
        </div>
    );
}
