import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const scriptSource = await readFile(
    resolve(dirname(fileURLToPath(import.meta.url)), '../public/sdk/clawith.js'),
    'utf8',
);

class FakeStyle {
    constructor() { this.cssText = ''; }
    getPropertyValue(name) {
        const match = this.cssText.match(new RegExp(`(?:^|;)${name}:([^;]+)`));
        return match ? match[1].replace(/\s*!important$/, '').trim() : '';
    }
    getPropertyPriority(name) {
        const match = this.cssText.match(new RegExp(`(?:^|;)${name}:([^;]+)`));
        return match && /!important\s*$/.test(match[1]) ? 'important' : '';
    }
}

class FakeElement {
    constructor(tagName, attributes = {}) {
        this.tagName = tagName.toUpperCase();
        this.attributes = new Map(Object.entries(attributes));
        this.children = [];
        this.parentNode = null;
        this.style = new FakeStyle();
    }
    getAttribute(name) { return this.attributes.get(name) ?? null; }
    hasAttribute(name) { return this.attributes.has(name); }
    setAttribute(name, value) { this.attributes.set(name, String(value)); }
    appendChild(child) {
        child.parentNode = this;
        this.children.push(child);
        return child;
    }
    contains(candidate) {
        return candidate === this || this.children.some(child => child.contains(candidate));
    }
}

function response(body, ok = true, status = ok ? 200 : 500) {
    return {
        ok,
        status,
        json: () => Promise.resolve(body),
    };
}

function plain(value) {
    return JSON.parse(JSON.stringify(value));
}

function createRuntime({
    href = 'http://test/p/sdk-report',
    fetchImpl = () => Promise.resolve(response({ ok: true })),
    scriptAttributes = {},
    embedded = false,
    parentCanPost = true,
} = {}) {
    const currentUrl = new URL(href);
    const assigned = [];
    const posted = [];
    const replaced = [];
    const documentElement = new FakeElement('html');
    const currentScript = new FakeElement('script', scriptAttributes);
    const location = {
        get href() { return currentUrl.href; },
        get pathname() { return currentUrl.pathname; },
        get search() { return currentUrl.search; },
        get hash() { return currentUrl.hash; },
        assign(value) { assigned.push(value); },
    };
    const document = {
        currentScript,
        documentElement,
        title: 'SDK Report',
        querySelector: () => null,
        createElement: tagName => new FakeElement(tagName),
    };
    const runtime = {
        console,
        document,
        fetch: fetchImpl,
        history: { replaceState: (_state, _title, value) => replaced.push(value) },
        location,
        URL,
        URLSearchParams,
        Promise,
        encodeURIComponent,
        MutationObserver: undefined,
    };
    runtime.window = runtime;
    runtime.parent = embedded
        ? (parentCanPost ? { postMessage: (message, target) => posted.push({ message, target }) } : {})
        : runtime;
    vm.runInContext(scriptSource, vm.createContext(runtime), { filename: 'clawith.js' });
    return { runtime, assigned, posted, replaced, documentElement };
}

{
    const { runtime, assigned, posted } = createRuntime({ embedded: true });
    runtime.Clawith.ready();
    assert.equal(assigned.length, 0);
    assert.deepEqual(plain(posted), [{
        message: { type: 'published-page:sdk-auth-start' },
        target: '*',
    }]);
}

{
    const { runtime, assigned } = createRuntime({ embedded: true, parentCanPost: false });
    runtime.Clawith.ready();
    assert.equal(assigned.length, 1);
    assert.equal(
        assigned[0],
        '/api/sdk/auth/start?return_to=http%3A%2F%2Ftest%2Fp%2Fsdk-report',
    );
}

{
    const calls = [];
    const fetchImpl = async (url, options) => {
        calls.push({ url, options });
        if (url === '/api/sdk/auth/exchange') {
            return response({ userId: 'u-1', userName: 'SDK User', mobile: '13800004321' });
        }
        if (url === '/api/webhooks/t/hook-1') return response({ accepted: true });
        throw new Error(`unexpected fetch ${url}`);
    };
    const { runtime, replaced } = createRuntime({
        href: 'http://test/p/sdk-report?code=CODE&state=STATE&keep=1#part',
        fetchImpl,
        scriptAttributes: { 'data-hook': 'hook-1' },
    });
    const readyUser = await runtime.Clawith.ready();
    assert.deepEqual(plain(readyUser), { userId: 'u-1', userName: 'SDK User', mobile: '13800004321' });
    assert.equal(runtime.Clawith.user, readyUser);
    let callbackUser = null;
    await runtime.Clawith.onReady(user => { callbackUser = user; });
    assert.equal(callbackUser, readyUser);
    assert.deepEqual(replaced, ['/p/sdk-report?keep=1#part']);

    const hookResult = await runtime.Clawith.triggerHook(runtime.Clawith.hook, {
        answer: 42,
        report: { title: 'Custom title' },
    });
    assert.deepEqual(plain(hookResult), { accepted: true });
    const hookCall = calls.find(call => call.url === '/api/webhooks/t/hook-1');
    assert.deepEqual(JSON.parse(hookCall.options.body), {
        answer: 42,
        report: { short_id: 'sdk-report', title: 'Custom title' },
    });
}

{
    let attempts = 0;
    const { runtime, replaced } = createRuntime({
        href: 'http://test/p/sdk-retry?code=CODE&state=STATE',
        fetchImpl: async () => {
            attempts += 1;
            return attempts === 1
                ? response({ error: 'temporary' }, false, 502)
                : response({ userId: 'u-2', userName: 'Retry User', mobile: '1000' });
        },
    });
    await assert.rejects(runtime.Clawith.ready(), /exchange failed 502/);
    assert.deepEqual(plain(await runtime.Clawith.ready()), {
        userId: 'u-2', userName: 'Retry User', mobile: '1000',
    });
    assert.equal(attempts, 2);
    assert.deepEqual(replaced, ['/p/sdk-retry']);
}

{
    const { runtime, documentElement } = createRuntime({
        href: 'http://test/p/sdk-watermark?code=CODE&state=STATE',
        fetchImpl: async () => response({
            userId: 'u-3', userName: 'Watermark User', mobile: '13800001234',
        }),
        scriptAttributes: { 'data-watermark': '' },
    });
    await runtime.Clawith.ready();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(documentElement.children.length, 1);
    const watermark = documentElement.children[0];
    assert.equal(watermark.getAttribute('data-clawith-wm'), '1');
    assert.match(watermark.style.cssText, /Watermark%20User%201234/);
}

console.log('published-page SDK runtime tests passed');
