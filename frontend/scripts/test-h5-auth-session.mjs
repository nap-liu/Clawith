import assert from 'node:assert/strict';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { loadTypeScriptModule } from './load-typescript-module.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const {
    H5_LOGIN_MESSAGES,
    formatH5LoginError,
    isRememberedH5AuthCode,
    readLastH5AuthCode,
    rememberH5AuthCode,
} = loadTypeScriptModule(resolve(__dirname, '../src/utils/h5AuthSession.ts'));

function createStorage() {
    const values = new Map();
    return {
        getItem(key) {
            return values.get(key) ?? null;
        },
        setItem(key, value) {
            values.set(key, String(value));
        },
    };
}

const storage = createStorage();
assert.equal(readLastH5AuthCode(storage), '');
assert.equal(isRememberedH5AuthCode('once-code', storage), false);
assert.equal(rememberH5AuthCode('once-code', storage), true);
assert.equal(readLastH5AuthCode(storage), 'once-code');
assert.equal(isRememberedH5AuthCode('once-code', storage), true);
assert.equal(isRememberedH5AuthCode('new-code', storage), false);

const blockedStorage = {
    getItem() {
        throw new Error('blocked');
    },
    setItem() {
        throw new Error('blocked');
    },
};
assert.equal(readLastH5AuthCode(blockedStorage), '');
assert.equal(rememberH5AuthCode('once-code', blockedStorage), false);

assert.equal(
    formatH5LoginError(Object.assign(new Error('Invalid or expired token'), { status: 401 })),
    H5_LOGIN_MESSAGES.expired,
);
assert.equal(
    formatH5LoginError(Object.assign(new Error('OAuth provider rejected the authorization code'), { status: 400 })),
    H5_LOGIN_MESSAGES.linkExpired,
);
assert.equal(
    formatH5LoginError(Object.assign(new Error('backend unavailable'), { status: 503 })),
    H5_LOGIN_MESSAGES.unavailable,
);
assert.equal(
    formatH5LoginError(new TypeError('Failed to fetch')),
    H5_LOGIN_MESSAGES.unavailable,
);
assert.equal(
    formatH5LoginError(new Error('unexpected English provider error')),
    H5_LOGIN_MESSAGES.failed,
);

console.log('h5 auth session tests passed');
