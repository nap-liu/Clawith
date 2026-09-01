import assert from 'node:assert/strict';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { loadCssEntry } from './load-css-entry.mjs';
import { loadLocalSourceGraph } from './load-local-source-graph.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const css = loadCssEntry(resolve(__dirname, '../src/pages/h5/H5AgentChat.css'));
const tsx = loadLocalSourceGraph(resolve(__dirname, '../src/pages/h5/H5AgentChat.tsx'));

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
    /installThemeController\(\{\s*mode:\s*themeMode,/,
    'H5 theme mode must install the shared document theme controller',
);
assert.match(
    tsx,
    /h5-chat--\$\{resolvedTheme\}/,
    'H5 root class must use the resolved light/dark theme',
);
assert.match(
    tsx,
    /data-theme-mode=\{themeMode\}/,
    'H5 root must expose its light/dark/system preference for diagnostics',
);

console.log('h5 theme token tests passed');
