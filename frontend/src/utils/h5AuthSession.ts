const H5_AUTH_CODE_STORAGE_KEY = 'clawith:h5:last-auth-code';
const RETURN_AND_REENTER = '请返回重新进入';

type AuthCodeStorage = Pick<Storage, 'getItem' | 'setItem'>;

export const H5_LOGIN_MESSAGES = {
    expired: `登录已失效，${RETURN_AND_REENTER}`,
    linkExpired: `登录链接已失效，${RETURN_AND_REENTER}`,
    incomplete: `登录信息不完整，${RETURN_AND_REENTER}`,
    unavailable: `登录服务暂时不可用，${RETURN_AND_REENTER}`,
    failed: `登录失败，${RETURN_AND_REENTER}`,
} as const;

export function readLastH5AuthCode(storage: AuthCodeStorage = localStorage) {
    try {
        return storage.getItem(H5_AUTH_CODE_STORAGE_KEY) || '';
    } catch {
        return '';
    }
}

export function rememberH5AuthCode(
    code: string,
    storage: AuthCodeStorage = localStorage,
) {
    try {
        storage.setItem(H5_AUTH_CODE_STORAGE_KEY, code);
        return true;
    } catch {
        return false;
    }
}

export function isRememberedH5AuthCode(
    code: string,
    storage: AuthCodeStorage = localStorage,
) {
    return readLastH5AuthCode(storage) === code;
}

export function formatH5LoginError(error: unknown) {
    const status = Number((error as { status?: unknown } | null)?.status);
    const rawMessage = (error as { message?: unknown } | null)?.message;
    const message = typeof rawMessage === 'string' ? rawMessage : '';

    if (status === 401) return H5_LOGIN_MESSAGES.expired;
    if (status === 400) return H5_LOGIN_MESSAGES.linkExpired;
    if (status >= 500) return H5_LOGIN_MESSAGES.unavailable;
    if (error instanceof TypeError || /failed to fetch|network error/i.test(message)) {
        return H5_LOGIN_MESSAGES.unavailable;
    }
    return H5_LOGIN_MESSAGES.failed;
}
