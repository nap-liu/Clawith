import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';
import { loadTypeScriptModule } from './load-typescript-module.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const themeModule = loadTypeScriptModule(resolve(__dirname, '../src/utils/themeMode.ts'));
const {
    THEME_META_COLORS,
    applyDocumentTheme,
    installThemeController,
    parseThemeMode,
    readSavedTheme,
    readSystemTheme,
    resolveThemeMode,
    saveTheme,
} = themeModule;

function createEventHub() {
    const listeners = new Map();
    return {
        addEventListener(type, listener) {
            const values = listeners.get(type) || new Set();
            values.add(listener);
            listeners.set(type, values);
        },
        removeEventListener(type, listener) {
            listeners.get(type)?.delete(listener);
        },
        dispatch(type, event = {}) {
            for (const listener of [...(listeners.get(type) || [])]) listener(event);
        },
        count(type) {
            return listeners.get(type)?.size || 0;
        },
    };
}

function createMediaQuery(matches = false, mode = 'modern') {
    const modernListeners = new Set();
    const legacyListeners = new Set();
    const query = {
        matches,
        addEventListener(_type, listener) {
            if (mode === 'modern-throws') throw new Error('modern listener unavailable');
            modernListeners.add(listener);
        },
        removeEventListener(_type, listener) {
            modernListeners.delete(listener);
        },
        addListener(listener) {
            legacyListeners.add(listener);
        },
        removeListener(listener) {
            legacyListeners.delete(listener);
        },
        emit(nextMatches) {
            query.matches = nextMatches;
            for (const listener of [...modernListeners, ...legacyListeners]) {
                listener({ matches: nextMatches });
            }
        },
        modernCount() {
            return modernListeners.size;
        },
        legacyCount() {
            return legacyListeners.size;
        },
    };
    if (mode === 'legacy') {
        query.addEventListener = undefined;
        query.removeEventListener = undefined;
    }
    return query;
}

function createDocument() {
    const hub = createEventHub();
    const attributes = new Map();
    const themeColorAttributes = new Map([['content', '#f8f8f7']]);
    const root = {
        style: {},
        setAttribute(name, value) {
            attributes.set(name, String(value));
        },
        getAttribute(name) {
            return attributes.get(name) ?? null;
        },
    };
    const themeColor = {
        setAttribute(name, value) {
            themeColorAttributes.set(name, String(value));
        },
        getAttribute(name) {
            return themeColorAttributes.get(name) ?? null;
        },
    };
    return {
        documentElement: root,
        visibilityState: 'visible',
        addEventListener: hub.addEventListener,
        removeEventListener: hub.removeEventListener,
        dispatch: hub.dispatch,
        listenerCount: hub.count,
        querySelector(selector) {
            return selector === 'meta[name="theme-color"]' ? themeColor : null;
        },
        themeColor,
    };
}

function createWindow(mediaQuery, existingBridge) {
    const hub = createEventHub();
    return {
        matchMedia() {
            return mediaQuery;
        },
        addEventListener: hub.addEventListener,
        removeEventListener: hub.removeEventListener,
        dispatch: hub.dispatch,
        listenerCount: hub.count,
        ClawithThemeBridge: existingBridge,
    };
}

assert.equal(parseThemeMode('LIGHT'), 'light');
assert.equal(parseThemeMode(' dark '), 'dark');
assert.equal(parseThemeMode('system'), 'system');
assert.equal(parseThemeMode('unexpected'), 'system');
assert.equal(parseThemeMode(null), 'system');
assert.equal(readSavedTheme({ localStorage: { getItem: () => 'dark' } }), 'dark');
assert.equal(readSavedTheme({ localStorage: { getItem: () => 'corrupt' } }), 'light');
assert.equal(readSavedTheme({ localStorage: { getItem: () => { throw new Error('blocked'); } } }), 'light');
{
    const writes = [];
    assert.equal(saveTheme('dark', { localStorage: { setItem: (key, value) => writes.push([key, value]) } }), true);
    assert.deepEqual(writes, [['theme', 'dark']]);
    assert.equal(saveTheme('light', { localStorage: { setItem: () => { throw new Error('blocked'); } } }), false);
}

{
    const doc = createDocument();
    applyDocumentTheme('dark', 'system', doc);
    assert.equal(doc.documentElement.getAttribute('data-theme'), 'dark');
    assert.equal(doc.documentElement.getAttribute('data-theme-mode'), 'system');
    assert.equal(doc.documentElement.style.colorScheme, 'dark');
    assert.equal(doc.themeColor.getAttribute('content'), THEME_META_COLORS.dark);
}

{
    const win = { matchMedia: () => { throw new Error('unsupported'); } };
    assert.equal(readSystemTheme(win), 'light');
    assert.equal(resolveThemeMode('light', win), 'light');
    assert.equal(resolveThemeMode('dark', win), 'dark');
    assert.equal(resolveThemeMode('system', win), 'light');
}

{
    const mediaQuery = createMediaQuery(true);
    const oldSetTheme = () => {};
    const existingBridge = { owner: 'host', setTheme: oldSetTheme };
    const win = createWindow(mediaQuery, existingBridge);
    const doc = createDocument();
    const changes = [];
    const cleanup = installThemeController({
        mode: 'system',
        targetWindow: win,
        targetDocument: doc,
        onThemeChange: (theme) => changes.push(theme),
    });

    assert.equal(changes.at(-1), 'dark');
    assert.equal(mediaQuery.modernCount(), 1);
    assert.equal(mediaQuery.legacyCount(), 0);
    assert.equal(win.listenerCount('pageshow'), 1);
    assert.equal(doc.listenerCount('visibilitychange'), 1);
    assert.equal(win.ClawithThemeBridge, existingBridge);
    assert.notEqual(existingBridge.setTheme, oldSetTheme);

    existingBridge.setTheme('light');
    assert.equal(changes.at(-1), 'light');
    mediaQuery.emit(false);
    assert.equal(changes.at(-1), 'light', 'host override remains authoritative');

    existingBridge.setTheme('system');
    assert.equal(changes.at(-1), 'light');
    mediaQuery.emit(true);
    assert.equal(changes.at(-1), 'dark');

    win.dispatch('pageshow');
    assert.equal(changes.at(-1), 'dark');
    doc.visibilityState = 'hidden';
    const hiddenCount = changes.length;
    doc.dispatch('visibilitychange');
    assert.equal(changes.length, hiddenCount);
    doc.visibilityState = 'visible';
    doc.dispatch('visibilitychange');
    assert.equal(changes.at(-1), 'dark');

    const disposedSetter = existingBridge.setTheme;
    cleanup();
    cleanup();
    assert.equal(mediaQuery.modernCount(), 0);
    assert.equal(win.listenerCount('pageshow'), 0);
    assert.equal(doc.listenerCount('visibilitychange'), 0);
    assert.equal(existingBridge.setTheme, oldSetTheme);
    const disposedCount = changes.length;
    disposedSetter('light');
    mediaQuery.emit(false);
    assert.equal(changes.length, disposedCount, 'disposed callbacks must not emit');
}

{
    const mediaQuery = createMediaQuery(false, 'modern-throws');
    const win = createWindow(mediaQuery);
    const doc = createDocument();
    const cleanup = installThemeController({
        mode: 'system',
        targetWindow: win,
        targetDocument: doc,
    });
    assert.equal(typeof win.ClawithThemeBridge?.setTheme, 'function');
    assert.equal(mediaQuery.modernCount(), 0);
    assert.equal(mediaQuery.legacyCount(), 1);
    cleanup();
    assert.equal(mediaQuery.legacyCount(), 0);
    assert.equal(win.ClawithThemeBridge, undefined);
}

{
    const mediaQuery = createMediaQuery(false);
    mediaQuery.removeEventListener = undefined;
    const win = createWindow(mediaQuery);
    const doc = createDocument();
    const cleanup = installThemeController({
        mode: 'system',
        targetWindow: win,
        targetDocument: doc,
    });
    assert.equal(mediaQuery.modernCount(), 0);
    assert.equal(mediaQuery.legacyCount(), 1, 'incomplete modern shim must use the removable legacy pair');
    cleanup();
    assert.equal(mediaQuery.legacyCount(), 0);
}

{
    const mediaQuery = createMediaQuery(true);
    const win = createWindow(mediaQuery);
    const doc = createDocument();
    const firstCleanup = installThemeController({
        mode: 'system',
        targetWindow: win,
        targetDocument: doc,
    });
    assert.equal(mediaQuery.modernCount(), 1);
    assert.equal(win.listenerCount('pageshow'), 1);
    assert.equal(typeof win.ClawithThemeBridge?.setTheme, 'function');

    firstCleanup();
    assert.equal(mediaQuery.modernCount(), 0);
    assert.equal(win.listenerCount('pageshow'), 0);
    assert.equal(win.ClawithThemeBridge, undefined);

    const secondCleanup = installThemeController({
        mode: 'system',
        targetWindow: win,
        targetDocument: doc,
    });
    assert.equal(mediaQuery.modernCount(), 1, 'StrictMode remount must install exactly one media listener');
    assert.equal(win.listenerCount('pageshow'), 1);
    assert.equal(typeof win.ClawithThemeBridge?.setTheme, 'function');

    secondCleanup();
    assert.equal(mediaQuery.modernCount(), 0);
    assert.equal(win.listenerCount('pageshow'), 0);
    assert.equal(win.ClawithThemeBridge, undefined);
}

{
    let matchMediaCalls = 0;
    const win = createWindow(createMediaQuery(true));
    win.matchMedia = () => {
        matchMediaCalls += 1;
        return createMediaQuery(true);
    };
    const doc = createDocument();
    const cleanup = installThemeController({
        mode: 'light',
        targetWindow: win,
        targetDocument: doc,
    });
    assert.equal(matchMediaCalls, 0);
    assert.equal(win.ClawithThemeBridge, undefined);
    assert.equal(doc.documentElement.getAttribute('data-theme'), 'light');
    cleanup();
}

const indexHtml = readFileSync(resolve(__dirname, '../index.html'), 'utf8');
const bootstrapMatch = indexHtml.match(/<script id="theme-bootstrap">([\s\S]*?)<\/script>/);
assert.ok(bootstrapMatch, 'index.html must include the synchronous theme bootstrap');
const bootstrapScript = bootstrapMatch[1];

function runBootstrap({
    pathname,
    search = '',
    systemDark = false,
    savedTheme = 'light',
    throwStorage = false,
    throwMatchMedia = false,
}) {
    const doc = createDocument();
    let storageReads = 0;
    let matchMediaReads = 0;
    const targetWindow = {
        location: { pathname, search },
        localStorage: {
            getItem() {
                storageReads += 1;
                if (throwStorage) throw new Error('storage blocked');
                return savedTheme;
            },
        },
        matchMedia() {
            matchMediaReads += 1;
            if (throwMatchMedia) throw new Error('media query blocked');
            return { matches: systemDark };
        },
    };
    vm.runInNewContext(bootstrapScript, {
        window: targetWindow,
        document: doc,
        URLSearchParams,
    });
    return { doc, storageReads, matchMediaReads };
}

{
    const result = runBootstrap({
        pathname: '/h5/agents/a/chat',
        systemDark: true,
        throwStorage: true,
    });
    assert.equal(result.doc.documentElement.getAttribute('data-theme'), 'dark');
    assert.equal(result.doc.documentElement.getAttribute('data-theme-mode'), 'system');
    assert.equal(result.doc.documentElement.style.colorScheme, 'dark');
    assert.equal(result.doc.themeColor.getAttribute('content'), THEME_META_COLORS.dark);
    assert.equal(result.storageReads, 0, 'H5 bootstrap must not read the saved PC theme');
}

{
    const result = runBootstrap({
        pathname: '/h5/agents/a/chat',
        search: '?theme=light',
        systemDark: true,
    });
    assert.equal(result.doc.documentElement.getAttribute('data-theme'), 'light');
    assert.equal(result.doc.documentElement.getAttribute('data-theme-mode'), 'light');
    assert.equal(result.doc.documentElement.style.colorScheme, 'light');
    assert.equal(result.doc.themeColor.getAttribute('content'), THEME_META_COLORS.light);
    assert.equal(result.matchMediaReads, 0, 'explicit URL theme must not query system theme');
}

{
    const result = runBootstrap({
        pathname: '/h5/agents/a/chat',
        search: '?theme=invalid',
        throwMatchMedia: true,
    });
    assert.equal(result.doc.documentElement.getAttribute('data-theme'), 'light');
    assert.equal(result.doc.documentElement.getAttribute('data-theme-mode'), 'system');
}

{
    const dark = runBootstrap({ pathname: '/login', savedTheme: 'dark' });
    assert.equal(dark.doc.documentElement.getAttribute('data-theme'), 'dark');
    assert.equal(dark.doc.documentElement.getAttribute('data-theme-mode'), 'dark');

    const blocked = runBootstrap({ pathname: '/login', throwStorage: true });
    assert.equal(blocked.doc.documentElement.getAttribute('data-theme'), 'light');
}

const css = readFileSync(resolve(__dirname, '../src/index.css'), 'utf8').toLowerCase();
assert.match(css, new RegExp(`--bg-primary:\\s*${THEME_META_COLORS.dark}`), 'dark meta color must match --bg-primary');
const lightThemeBlock = css.match(/\[data-theme="light"\]\s*\{([\s\S]*?)\n\}/)?.[1] || '';
assert.match(lightThemeBlock, new RegExp(`--bg-primary:\\s*${THEME_META_COLORS.light}`), 'light meta color must match --bg-primary');

console.log('h5 theme runtime tests passed');
