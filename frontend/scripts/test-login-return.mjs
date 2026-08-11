import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

import { loadTypeScriptModule } from './load-typescript-module.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const sourcePath = resolve(__dirname, '../src/utils/loginReturn.ts');
const { isAutomaticLoginRequested, resolveLoginTenantId, safeLoginReturnTo } = loadTypeScriptModule(sourcePath, {
    window: { location: { origin: 'https://clawith.example' } },
});

assert.equal(safeLoginReturnTo('/p/report?a=1'), '/p/report?a=1');
assert.equal(safeLoginReturnTo('https://external.example/after-login'), 'https://external.example/after-login');
assert.equal(safeLoginReturnTo('//external.example/after-login'), '//external.example/after-login');
assert.equal(safeLoginReturnTo('javascript:globalThis.stolen=true'), '');
assert.equal(safeLoginReturnTo('data:text/html,<script>alert(1)</script>'), '');
assert.equal(safeLoginReturnTo('vbscript:msgbox(1)'), '');

assert.equal(isAutomaticLoginRequested(null), false);
assert.equal(isAutomaticLoginRequested(''), false);
assert.equal(isAutomaticLoginRequested('0'), false);
assert.equal(isAutomaticLoginRequested('false'), false);
assert.equal(isAutomaticLoginRequested('1'), true);
assert.equal(isAutomaticLoginRequested('TRUE'), true);

assert.equal(resolveLoginTenantId('published-page-tenant', 'domain-tenant'), 'published-page-tenant');
assert.equal(resolveLoginTenantId('', 'domain-tenant'), 'domain-tenant');
assert.equal(resolveLoginTenantId(null, undefined), '');

console.log('login return URL safety tests passed');
