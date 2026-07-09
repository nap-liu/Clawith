import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const css = readFileSync(resolve(__dirname, '../src/pages/h5/H5AgentChat.css'), 'utf8');
const tsx = readFileSync(resolve(__dirname, '../src/pages/h5/H5AgentChat.tsx'), 'utf8');

function blockFor(selector) {
    const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const match = css.match(new RegExp(`${escaped}\\s*\\{([\\s\\S]*?)\\n\\}`));
    return match?.[1] || '';
}

const coreThemeTokens = [
    '--bg-primary',
    '--bg-secondary',
    '--text-primary',
    '--text-secondary',
    '--text-tertiary',
    '--border-subtle',
    '--accent-primary',
    '--error',
];

for (const selector of ['.h5-chat', '.h5-chat--dark']) {
    const block = blockFor(selector);
    for (const token of coreThemeTokens) {
        assert.equal(
            block.includes(`${token}:`),
            false,
            `${selector} must not redefine global PC theme token ${token}`,
        );
    }
}

const h5PrivatePalette = [
    '#f4f5f7',
    '#0f141c',
    '#151c27',
    '#1a7f64',
    '#2ba982',
    '#218866',
    '#eef7f3',
    '#d9efe7',
    '#71d7b5',
];

for (const color of h5PrivatePalette) {
    assert.equal(
        css.toLowerCase().includes(color),
        false,
        `H5 CSS must use shared theme tokens instead of private palette color ${color}`,
    );
}

assert.match(
    tsx,
    /document\.documentElement\.setAttribute\(['"]data-theme['"],\s*theme\)/,
    'H5 theme URL param must apply to the global data-theme token source',
);

console.log('h5 theme token tests passed');
