import assert from 'node:assert/strict';
import { createServer } from 'vite';

const storage = new Map([['token', 'local-token']]);
globalThis.localStorage = {
    getItem: (key) => storage.get(key) ?? null,
    removeItem: (key) => storage.delete(key),
    setItem: (key, value) => storage.set(key, value),
};

let finishRequest;
const requests = [];
globalThis.fetch = (url, options) => {
    requests.push({ url, options });
    return new Promise((resolve) => {
        finishRequest = resolve;
    });
};

const server = await createServer({
    appType: 'custom',
    optimizeDeps: { noDiscovery: true },
    server: { middlewareMode: true },
});

try {
    const { useAuthStore } = await server.ssrLoadModule('/src/stores/index.ts');
    useAuthStore.setState({ token: 'local-token', user: { id: 'user-1' } });

    const logoutRequest = useAuthStore.getState().logout();
    assert.equal(requests.length, 1);
    assert.equal(requests[0].url, '/api/auth/logout');
    assert.deepEqual(requests[0].options, {
        method: 'POST',
        headers: { Authorization: 'Bearer local-token' },
        credentials: 'same-origin',
        keepalive: true,
    });
    assert.equal(storage.has('token'), false);
    assert.equal(useAuthStore.getState().token, null);
    assert.equal(useAuthStore.getState().user, null);

    finishRequest({ ok: true });
    await logoutRequest;
    assert.equal(storage.has('token'), false);
    assert.equal(useAuthStore.getState().token, null);
    assert.equal(useAuthStore.getState().user, null);

    storage.set('token', 'retry-token');
    useAuthStore.setState({ token: 'retry-token', user: { id: 'user-2' } });
    globalThis.fetch = async () => {
        throw new Error('offline');
    };
    await useAuthStore.getState().logout();
    assert.equal(storage.has('token'), false);
    assert.equal(useAuthStore.getState().token, null);
    assert.equal(useAuthStore.getState().user, null);

    const redirects = [];
    globalThis.window = {
        location: {
            href: 'http://test/agents/agent-1',
            pathname: '/agents/agent-1',
            replace: (url) => redirects.push(url),
        },
    };
    storage.set('token', 'expired-token');
    storage.set('user', '{"id":"user-3"}');
    useAuthStore.setState({ token: 'expired-token', user: { id: 'user-3' } });
    const unauthorizedRequests = [];
    globalThis.fetch = async (url, options) => {
        unauthorizedRequests.push({ url, options });
        if (url === '/api/protected') {
            return { ok: false, status: 401 };
        }
        return { ok: true };
    };
    const { request } = await server.ssrLoadModule('/src/services/api/core.ts');
    await assert.rejects(request('/protected'), /Session expired/);
    assert.equal(storage.has('token'), false);
    assert.equal(storage.has('user'), false);
    assert.equal(unauthorizedRequests[0].url, '/api/protected');
    assert.equal(unauthorizedRequests[1].url, '/api/auth/logout');
    assert.equal(
        unauthorizedRequests[1].options.headers.Authorization,
        'Bearer expired-token',
    );
    assert.deepEqual(redirects, [
        '/login?return_to=http%3A%2F%2Ftest%2Fagents%2Fagent-1',
    ]);

    const { buildLoginUrl } = await server.ssrLoadModule('/src/utils/loginReturn.ts');
    const reportUrl = 'https://reports.example/p/report-1?filter=weekly#chart';
    const accessQuery = new URLSearchParams({
        short_id: 'report-1', tenant_id: 'report-tenant', auto_login: '1',
        sso: 'oauth2', return_to: reportUrl,
    });
    window.location.href = `http://test/published-page-access?${accessQuery}`;
    window.location.pathname = '/published-page-access';
    const missingCredentialsLogin = buildLoginUrl(window.location.href);
    storage.set('token', 'expired-report-token');
    await assert.rejects(request('/protected'), /Session expired/);
    assert.equal(redirects.at(-1), missingCredentialsLogin);
    const login = new URL(redirects.at(-1), 'http://test');
    assert.equal(login.pathname, '/login');
    assert.equal(login.searchParams.get('return_to'), reportUrl);
    assert.equal(login.searchParams.get('tenant_id'), 'report-tenant');
    assert.equal(login.searchParams.get('auto_login'), '1');
    assert.equal(login.searchParams.get('sso'), 'oauth2');

    window.location.href = login.href;
    window.location.pathname = '/login';
    await assert.rejects(request('/protected'), /Session expired/);
    assert.equal(redirects.at(-1), missingCredentialsLogin);

    console.log('auth logout tests passed');
} finally {
    await server.close();
}
