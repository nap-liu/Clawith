import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(resolve(__dirname, '../index.html'), 'utf8');
const css = readFileSync(resolve(__dirname, '../src/pages/h5/H5AgentChat.css'), 'utf8');

function blockFor(selector) {
    const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const match = css.match(new RegExp(`${escaped}\\s*\\{([\\s\\S]*?)\\n\\}`));
    return match?.[1] || '';
}

const rootBlock = blockFor('.h5-chat');
const messagesBlock = blockFor('.h5-chat__messages');
const composerBlock = blockFor('.h5-chat__composer');

assert.match(
    html,
    /<meta\s+name=["']viewport["'][^>]*content=["'][^"']*viewport-fit=cover[^"']*["']/,
    'H5 mobile viewport must enable viewport-fit=cover so iOS WebView exposes safe-area insets',
);

for (const edge of ['top', 'bottom', 'left', 'right']) {
    assert.match(
        css,
        new RegExp(`--h5-safe-area-${edge}:\\s*constant\\(safe-area-inset-${edge}\\);`),
        `H5 CSS must keep constant() fallback for safe-area ${edge}`,
    );
    assert.match(
        css,
        new RegExp(`--h5-safe-area-${edge}:\\s*env\\(safe-area-inset-${edge}\\);`),
        `H5 CSS must read env() safe-area ${edge}`,
    );
}

assert.match(
    css,
    /@supports\s*\(padding-top:\s*constant\(safe-area-inset-top\)\)/,
    'H5 constant() safe-area fallback must be gated by @supports',
);

assert.match(
    css,
    /@supports\s*\(padding-top:\s*env\(safe-area-inset-top\)\)/,
    'H5 env() safe-area support must be gated by @supports',
);

assert.match(
    composerBlock,
    /padding:\s*var\(--h5-composer-padding-top\)\s+var\(--h5-composer-padding-right\)\s+var\(--h5-composer-safe-bottom\)\s+var\(--h5-composer-padding-left\);/,
    'H5 composer padding must use safe-area-aware bottom and horizontal padding variables',
);

assert.match(
    messagesBlock,
    /scroll-padding-bottom:\s*calc\(18px \+ var\(--h5-safe-area-bottom\)\);/,
    'H5 message scroller must reserve safe-area bottom space when scrolling to the end',
);

console.log('h5 safe-area tests passed');
