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
