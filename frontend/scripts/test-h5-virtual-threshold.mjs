import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(resolve(__dirname, '../src/pages/h5/H5AgentChat.tsx'), 'utf8');
const match = source.match(/const\s+VIRTUALIZE_ENTRY_THRESHOLD\s*=\s*(\d+);/);

assert.ok(match, 'H5 virtual scroll threshold constant must be declared');
assert.equal(Number(match[1]), 40, 'H5 should virtualize from 41 conversation entries for low-end devices');

console.log('h5 virtual threshold tests passed');
