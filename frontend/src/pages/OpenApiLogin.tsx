import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useAuthStore } from '../stores';
import { safeLoginReturnTo } from '../utils/loginReturn';
import type { User } from '../types';

type LoginResult = { access_token: string; user: User; redirect_uri: string };
const exchanges = new Map<string, Promise<LoginResult>>();

// A single-use link must also work with React's development effect remounts.
function exchangeOnce(code: string) {
    const existing = exchanges.get(code);
    if (existing) return existing;
    const pending = fetch('/api/openapi/v1/auth/link-exchange', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        cache: 'no-store',
        referrerPolicy: 'no-referrer',
        body: JSON.stringify({ code }),
    }).then(async (response) => {
        if (!response.ok) throw Object.assign(new Error('LOGIN_FAILED'), { status: response.status });
        return response.json() as Promise<LoginResult>;
    });
    exchanges.set(code, pending);
    void pending.finally(() => {
        window.setTimeout(() => exchanges.delete(code), 10_000);
    }).catch(() => undefined);
    return pending;
}

/** Generic signed-link login. The server owns the destination and its policy. */
export default function OpenApiLogin() {
    const { t } = useTranslation();
    const setAuth = useAuthStore((state) => state.setAuth);
    const [code] = useState(() => new URLSearchParams(window.location.search).get('code') || '');
    const [errorKey, setErrorKey] = useState('');

    useEffect(() => {
        let active = true;
        // Neither browser history nor a subsequent navigation retains the login code.
        window.history.replaceState({}, '', window.location.pathname);
        if (!code || code.length > 8192) {
            setErrorKey('openapiLogin.expired');
            return;
        }
        void exchangeOnce(code).then((result) => {
            if (!active) return;
            const destination = safeLoginReturnTo(result.redirect_uri);
            if (!result.access_token || !result.user?.id || !destination) {
                setErrorKey('openapiLogin.failed');
                return;
            }
            setAuth(result.user, result.access_token);
            window.location.replace(destination);
        }).catch((error: unknown) => {
            if (!active) return;
            const status = (error as { status?: number }).status;
            setErrorKey(status === 400 || status === 401 || status === 410
                ? 'openapiLogin.expired' : 'openapiLogin.failed');
        });
        return () => { active = false; };
    }, [code, setAuth]);

    return (
        <main style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', minHeight: '100dvh', padding: 24, textAlign: 'center', gap: 16 }}>
            {errorKey ? (
                <div role="alert">
                    <h3>{t('oauth.loginFailed')}</h3>
                    <p>{t(errorKey)}</p>
                    <p>{t('openapiLogin.returnToSource')}</p>
                </div>
            ) : (
                <div role="status" aria-live="polite">
                    <div className="login-spinner" style={{ width: 40, height: 40, margin: '0 auto 20px' }} aria-hidden="true" />
                    <p>{t('oauth.completingSignIn')}</p>
                </div>
            )}
        </main>
    );
}
