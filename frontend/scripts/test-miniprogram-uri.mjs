import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { loadTypeScriptModule } from './load-typescript-module.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const { parseMiniProgramUri } = loadTypeScriptModule(
    resolve(__dirname, '../src/utils/miniProgramUri.ts'),
);

const contract = JSON.parse(readFileSync(
    resolve(__dirname, './fixtures/mini_program_uri.json'),
    'utf8',
));

for (const expected of contract.valid) {
    const parsed = parseMiniProgramUri(expected.value);
    assert.equal(parsed?.action, expected.action, expected.value);
    assert.equal(parsed?.route, expected.route, expected.value);
}

for (const invalid of contract.invalid) {
    assert.equal(parseMiniProgramUri(invalid), null, invalid);
}

console.log('mini-program URI tests passed');
