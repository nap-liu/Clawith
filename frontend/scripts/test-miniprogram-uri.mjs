import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { loadTypeScriptModule } from './load-typescript-module.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const { parseMiniProgramUri } = loadTypeScriptModule(
    resolve(__dirname, '../src/utils/miniProgramUri.ts'),
);

const parsed = parseMiniProgramUri('miniprogram://navigate-to/pages/order/detail?id=123&from=h5');
assert.equal(parsed?.action, 'navigate-to');
assert.equal(parsed?.route, '/pages/order/detail?id=123&from=h5');

const uppercase = parseMiniProgramUri('MINIPROGRAM://NAVIGATE-TO/pages/%E8%AE%A2%E5%8D%95?id=%E4%B8%AD%E6%96%87');
assert.equal(uppercase?.action, 'navigate-to');
assert.equal(uppercase?.route, '/pages/%E8%AE%A2%E5%8D%95?id=%E4%B8%AD%E6%96%87');

for (const invalid of [
    'https://navigate-to/pages/order',
    'miniprogram:navigate-to/pages/order',
    'miniprogram://navigate-to',
    'miniprogram://navigate-to/',
    'miniprogram://unknown/pages/order',
    'miniprogram://user@navigate-to/pages/order',
    'miniprogram://navigate-to:80/pages/order',
    'miniprogram://navigate-to/pages/order#detail',
    'miniprogram://navigate-to/pages//order',
    'miniprogram://navigate-to/pages/../admin',
    'miniprogram://navigate-to/pages/%2e%2e/admin',
    'miniprogram://navigate-to/pages/%2Fadmin',
    'miniprogram://navigate-to/pages/%5cadmin',
    'miniprogram://navigate-to/pages/%00admin',
    'miniprogram://navigate-to/pages/order?name=中文',
    'miniprogram://navigate-to/pages/order?bad=%xy',
]) {
    assert.equal(parseMiniProgramUri(invalid), null, invalid);
}

console.log('mini-program URI tests passed');
