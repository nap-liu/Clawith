/**
 * Keep cross-domain return URLs without allowing executable URL schemes.
 * Relative paths and arbitrary HTTP(S) origins are intentionally supported.
 */
export function safeLoginReturnTo(value: string | null | undefined): string {
    const target = (value || '').trim();
    if (!target) return '';

    try {
        const parsed = new URL(target, window.location.origin);
        return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? target : '';
    } catch {
        return '';
    }
}

/** Only an explicit URL flag may start SSO without a user click. */
export function isAutomaticLoginRequested(value: string | null | undefined): boolean {
    const flag = (value || '').trim().toLowerCase();
    return flag === '1' || flag === 'true';
}

/** A tenant explicitly carried by the inbound login URL must beat domain discovery. */
export function resolveLoginTenantId(
    requestedTenantId: string | null | undefined,
    domainTenantId: string | null | undefined,
): string {
    return (requestedTenantId || '').trim() || (domainTenantId || '').trim();
}

/** Preserve the login context through both missing credentials and API 401s. */
export function buildLoginUrl(href: string): string {
    const current = new URL(href);
    if (current.pathname === '/login') return `/login${current.search}`;

    const login = new URLSearchParams({ return_to: href });
    if (current.pathname === '/published-page-access') {
        const params = current.searchParams;
        const shortId = params.get('short_id');
        const returnTo = safeLoginReturnTo(params.get('return_to'))
            || (shortId ? `/p/${encodeURIComponent(shortId)}` : '/');
        login.set('return_to', returnTo);
        for (const key of ['tenant_id', 'auto_login', 'sso']) {
            const value = params.get(key);
            if (value) login.set(key, value);
        }
    }
    return `/login?${login}`;
}
