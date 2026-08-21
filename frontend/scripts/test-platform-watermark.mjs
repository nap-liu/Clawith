import assert from 'node:assert/strict';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { loadTypeScriptModule } from './load-typescript-module.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const {
    calculatePlatformWatermarkTileLayout,
    formatPlatformWatermarkText,
    createPlatformWatermarkSvgDataUrl,
    installPlatformWatermark,
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

class FakeStyle {
    constructor() { this.cssText = ''; }
    setProperty(name, value, priority = '') {
        const declaration = `${name}:${value}${priority ? `!${priority}` : ''}`;
        this.cssText = `${this.cssText};${declaration}`;
    }
}

class FakeElement {
    constructor(tagName) {
        this.tagName = tagName.toUpperCase();
        this.style = new FakeStyle();
        this.children = [];
        this.parentNode = null;
        this.attributeMap = new Map();
        this.testShadowRoot = null;
    }
    get attributes() {
        return [...this.attributeMap].map(([name, value]) => ({ name, value }));
    }
    setAttribute(name, value) { this.attributeMap.set(name, String(value)); }
    getAttribute(name) { return this.attributeMap.get(name) ?? null; }
    hasAttribute(name) { return this.attributeMap.has(name); }
    removeAttribute(name) { this.attributeMap.delete(name); }
    appendChild(child) {
        child.parentNode?.removeChild(child);
        this.children.push(child);
        child.parentNode = this;
        return child;
    }
    removeChild(child) {
        this.children = this.children.filter(candidate => candidate !== child);
        child.parentNode = null;
        return child;
    }
    contains(candidate) {
        return candidate === this || this.children.some(child => child.contains(candidate));
    }
    attachShadow() {
        this.testShadowRoot = new FakeElement('shadow-root');
        return this.testShadowRoot;
    }
}

const fakeBody = new FakeElement('body');
const fakeDocumentElement = new FakeElement('html');
const fakeDocument = {
    body: fakeBody,
    documentElement: fakeDocumentElement,
    createElement(tagName) {
        if (tagName === 'canvas') {
            const canvas = new FakeElement(tagName);
            canvas.getContext = () => ({
                font: '',
                measureText: () => ({ width: 140 }),
            });
            return canvas;
        }
        return new FakeElement(tagName);
    },
};
const fakeObservers = [];
class FakeMutationObserver {
    constructor(callback) {
        this.callback = callback;
        this.disconnected = false;
        fakeObservers.push(this);
    }
    observe() {}
    disconnect() { this.disconnected = true; }
    flush() { if (!this.disconnected) this.callback([]); }
}
const fakeWindow = {
    innerWidth: 1280,
    devicePixelRatio: 2,
    MutationObserver: FakeMutationObserver,
    addEventListener() {},
    removeEventListener() {},
    requestAnimationFrame(callback) { callback(); return 1; },
    cancelAnimationFrame() {},
};

const uninstall = installPlatformWatermark('Behavior Test 1234', {
    targetDocument: fakeDocument,
    targetWindow: fakeWindow,
});
const host = fakeBody.children[0];
const layer = host.testShadowRoot.children[0];
const canonicalHostStyle = host.style.cssText;
const canonicalLayerStyle = layer.style.cssText;
host.style.cssText = 'opacity:0!important;z-index:-1!important;width:0!important';
host.setAttribute('hidden', '');
host.setAttribute('id', 'published-page-watermark-host');
layer.style.cssText = 'display:none!important';
layer.setAttribute('class', 'hidden');
fakeBody.removeChild(host);
host.testShadowRoot.removeChild(layer);
fakeObservers.forEach(observer => observer.flush());

assert.equal(host.parentNode, fakeBody);
assert.equal(host.style.cssText, canonicalHostStyle);
assert.equal(host.hasAttribute('hidden'), false);
assert.equal(host.hasAttribute('id'), false);
assert.equal(layer.parentNode, host.testShadowRoot);
assert.equal(layer.style.cssText, canonicalLayerStyle);
assert.equal(layer.hasAttribute('class'), false);
uninstall();
assert.equal(fakeBody.children.length, 0);

console.log('platform watermark tests passed');
