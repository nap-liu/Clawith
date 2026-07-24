import assert from 'node:assert/strict';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { loadTypeScriptModule } from './load-typescript-module.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const {
    calculatePlatformWatermarkTileLayout,
    formatPlatformWatermarkText,
    createPlatformWatermarkSvgDataUrl,
    resolvePlatformWatermarkTheme,
} = loadTypeScriptModule(resolve(__dirname, '../src/utils/platformWatermark.ts'));

assert.equal(formatPlatformWatermarkText(null), null);
assert.equal(formatPlatformWatermarkText({}), null);
assert.equal(formatPlatformWatermarkText({
    display_name: ' 张伟 ',
    username: 'zhangwei',
    primary_mobile: '+86 138-0013-8666',
}), '张伟 8666');
assert.equal(formatPlatformWatermarkText({
    display_name: ' ',
    username: ' fallback-user ',
    primary_mobile: '123',
}), 'fallback-user 123');
assert.equal(formatPlatformWatermarkText({
    display_name: 'No Phone',
    primary_mobile: null,
}), 'No Phone');
assert.equal(formatPlatformWatermarkText({
    display_name: '',
    username: '',
    primary_mobile: '13800138666',
}), null);
assert.equal(formatPlatformWatermarkText({
    display_name: '<x&"\'>',
    primary_mobile: '13800138666',
}), '<x&"\'> 8666');
assert.equal(formatPlatformWatermarkText({
    display_name: '一二三四五六七八九十一二三四五六七八九十二二三四五六七八九十',
    primary_mobile: '13800138666',
}), '一二三四五六七八九十一二三四五六七八九十二二三四… 8666');

assert.equal(resolvePlatformWatermarkTheme({
    documentElement: { getAttribute: () => 'dark' },
}), 'dark');
assert.equal(resolvePlatformWatermarkTheme({
    documentElement: { getAttribute: () => 'light' },
}), 'light');
assert.equal(resolvePlatformWatermarkTheme({
    documentElement: { getAttribute: () => null },
}), 'light');

assert.equal(
    JSON.stringify(calculatePlatformWatermarkTileLayout(90.2, 390, 3)),
    JSON.stringify({ fontSize: 13, width: 168, height: 112, devicePixelRatio: 3 }),
);
assert.equal(
    JSON.stringify(calculatePlatformWatermarkTileLayout(220.2, 1440, 0.75)),
    JSON.stringify({ fontSize: 14, width: 289, height: 128, devicePixelRatio: 1 }),
);

const svgDataUrl = createPlatformWatermarkSvgDataUrl(
    '张伟 <x&"\'> 8666',
    'light',
    { fontSize: 14, width: 190, height: 128 },
);
assert.ok(svgDataUrl.startsWith('data:image/svg+xml;charset=utf-8,'));
const decodedSvg = decodeURIComponent(svgDataUrl.split(',', 2)[1]);
assert.ok(decodedSvg.includes('viewBox="0 0 190 128"'));
assert.ok(decodedSvg.includes('transform="rotate(-22 95 64)"'));
assert.ok(decodedSvg.includes('text-rendering="geometricPrecision"'));
assert.ok(decodedSvg.includes('fill="rgb(0,0,0)" fill-opacity="0.09"'));
assert.ok(decodedSvg.includes('张伟 &lt;x&amp;&quot;&apos;&gt; 8666'));
assert.ok(!decodedSvg.includes('张伟 <x&"\'> 8666'));

const darkSvgDataUrl = createPlatformWatermarkSvgDataUrl(
    'Dark User 1234',
    'dark',
    { fontSize: 13, width: 168, height: 112 },
);
const decodedDarkSvg = decodeURIComponent(darkSvgDataUrl.split(',', 2)[1]);
assert.ok(decodedDarkSvg.includes('fill="rgb(255,255,255)" fill-opacity="0.10"'));

console.log('platform watermark tests passed');
