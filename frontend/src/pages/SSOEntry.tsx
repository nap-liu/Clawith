import { useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { IconAlertTriangle } from '@tabler/icons-react';
import { useAuthStore } from '../stores';
import { fetchJson } from '../services/api';
import { safeLoginReturnTo } from '../utils/loginReturn';

export default function SSOEntry() {
    const { t } = useTranslation();
    const [searchParams] = useSearchParams();
    const navigate = useNavigate();
    const setAuth = useAuthStore((s) => s.setAuth);
    const sid = searchParams.get('sid');
    const complete = searchParams.get('complete') === '1';
    const returnTo = safeLoginReturnTo(searchParams.get('return_to'));
    const callbackError = searchParams.get('error') || '';
    const [error, setError] = useState('');
    const [providers, setProviders] = useState<any[]>([]);
    const [loading, setLoading] = useState(true);
    // Initialize polling=true when complete=1 to avoid briefly showing
    // "No SSO providers configured." before the first poll completes.
    const [polling, setPolling] = useState(complete);

    useEffect(() => {
        if (callbackError) {
            const messages: Record<string, string> = {
                browser_mismatch: '登录请求与当前浏览器不匹配，请从登录页重新发起。',
                provider_unavailable: '登录方式当前不可用，请返回登录页重试。',
                invalid_state: '登录状态无效或已过期，请重新登录。',
                invalid_session: '登录会话无效或已过期，请重新登录。',
                authentication_failed: '身份认证失败，请重试。',
                session_update_failed: '登录结果保存失败，请重试。',
            };
            setError(messages[callbackError] || t('sso.sessionExpired'));
            setLoading(false);
            return;
        }
        if (!sid) {
            setError(t('sso.missingSessionId'));
            setLoading(false);
            return;
        }

        // 1. Mark as scanned
        fetchJson(`/sso/session/${sid}/scan`, { method: 'PUT' }).catch(() => {});

        // 2. Load SSO configs (skip auto-redirect on completion step)
        if (!complete) {
            fetchJson<any[]>(`/sso/config?sid=${sid}`)
                .then(data => {
                    setProviders(data);
                    setLoading(false);
                    
                    // 3. Detect UA and Auto-redirect if possible
                    const ua = navigator.userAgent.toLowerCase();
                    let targetProvider = '';
                    
                    if (ua.includes('lark') || ua.includes('feishu')) {
                        targetProvider = 'feishu';
                    } else if (ua.includes('dingtalk')) {
                        targetProvider = 'dingtalk';
                    } else if (ua.includes('wxwork')) {
                        targetProvider = 'wecom';
                    }

                    if (targetProvider) {
                        const p = data.find(it => it.provider_type === targetProvider);
                        if (p && p.url) {
                            window.location.href = p.url;
                        }
                    }
                })
                .catch(() => {
                    setError(t('sso.failedToLoadConfig'));
                    setLoading(false);
                });
        } else {
            setLoading(false);
        }
    }, [sid, complete, callbackError, t]);

    useEffect(() => {
        if (!sid || callbackError) return;
        let cancelled = false;
        let timer: number | undefined;

        const poll = async () => {
            if (cancelled) return;
            try {
                setPolling(true);
                const res = await fetchJson<any>(`/sso/session/${sid}/status`);
                if (res?.access_token && res?.user) {
                    setAuth(res.user, res.access_token);
                    if (res.user && !res.user.tenant_id) {
                        navigate('/setup-company');
                    } else if (returnTo) {
                        window.location.replace(returnTo);
                    } else {
                        navigate('/');
                    }
                    return;
                }
                if (res?.status === 'expired') {
                    setError(t('sso.sessionExpired'));
                    return;
                }
                if (res?.error_msg) {
                    setError(res.error_msg);
                    return;
                }
            } catch {
                // ignore transient errors
            } finally {
                setPolling(false);
            }
            timer = window.setTimeout(poll, 1500);
        };

        poll();

        return () => {
            cancelled = true;
            if (timer) window.clearTimeout(timer);
        };
    }, [sid, setAuth, navigate, returnTo, callbackError]);

    if (loading) {
        return (
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', height: '100vh', padding: '20px', textAlign: 'center' }}>
                <div className="login-spinner" style={{ width: '40px', height: '40px', marginBottom: '20px' }}></div>
                <p>{t('sso.redirectingToLogin')}</p>
            </div>
        );
    }

    if (error) {
        return (
            <div style={{ padding: '40px', textAlign: 'center' }}>
                <h3 style={{ color: 'var(--error)', display: 'inline-flex', alignItems: 'center', gap: '6px' }}><IconAlertTriangle size={18} stroke={1.8} /> {t('sso.error')}</h3>
                <p>{error}</p>
            </div>
        );
    }

    // When complete=1, only show a completion spinner — no provider selection UI
    if (complete) {
        return (
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', height: '100vh', padding: '20px', textAlign: 'center' }}>
                <div className="login-spinner" style={{ width: '40px', height: '40px', marginBottom: '20px' }}></div>
                <p>{t('sso.completingLogin')}</p>
            </div>
        );
    }

    return (
        <div style={{ padding: '40px', textAlign: 'center' }}>
            <h2>{t('sso.loginTitle')}</h2>
            <p style={{ color: 'var(--text-secondary)', marginBottom: '30px' }}>{t('sso.selectLoginMethod')}</p>
            
            <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
                {providers.map(p => (
                    <button 
                        key={p.provider_type} 
                        className="btn btn-primary" 
                        style={{ padding: '12px', fontSize: '16px' }}
                        onClick={() => window.location.href = p.url}
                    >
                        {t('sso.loginWith', { provider: p.name })}
                    </button>
                ))}
                
                {providers.length === 0 && (
                    <p style={{ color: 'var(--text-tertiary)' }}>
                        {polling ? t('sso.completingLogin') : t('sso.noProviders')}
                    </p>
                )}
            </div>
        </div>
    );
}
