import { DOCUMENT_THEME_CHANGE_EVENT } from './themeMode';

export type PlatformWatermarkTheme = 'light' | 'dark';

export type PlatformWatermarkIdentity = {
    display_name?: string | null;
    username?: string | null;
    primary_mobile?: string | null;
};

export type PlatformWatermarkTile = {
    dataUrl: string;
    width: number;
    height: number;
};

type PlatformWatermarkInstallOptions = {
    targetDocument?: Document;
    targetWindow?: Window;
};

const WATERMARK_ATTRIBUTE = 'data-platform-watermark';
const WATERMARK_HOST_ID = 'clawith-platform-watermark-host';
const MAX_NAME_CHARACTERS = 24;
const MAX_DEVICE_PIXEL_RATIO = 3;
const MOBILE_VIEWPORT_MAX_WIDTH = 600;
const WATERMARK_ROTATION_DEGREES = -22;
const WATERMARK_FONT_FAMILY = [
    '-apple-system',
    'BlinkMacSystemFont',
    '"Segoe UI"',
    '"PingFang SC"',
    '"Microsoft YaHei"',
    'Arial',
    'sans-serif',
].join(',');

const WATERMARK_HOST_STYLE = [
    'all:initial!important',
    'position:fixed!important',
    'top:0!important',
    'right:0!important',
    'bottom:0!important',
    'left:0!important',
    'display:block!important',
    'pointer-events:none!important',
    'user-select:none!important',
    '-webkit-user-select:none!important',
    'z-index:2147483647!important',
    'overflow:hidden!important',
    'contain:strict!important',
].join(';');

const WATERMARK_LAYER_STYLE = [
    'position:absolute!important',
    'top:0!important',
    'right:0!important',
    'bottom:0!important',
    'left:0!important',
    'display:block!important',
    'pointer-events:none!important',
    'background-color:transparent!important',
    'background-repeat:repeat!important',
    'background-position:0 0!important',
].join(';');

function firstNonEmptyString(...values: Array<string | null | undefined>) {
    for (const value of values) {
        if (typeof value === 'string' && value.trim()) return value.trim();
    }
    return '';
}

function truncateName(name: string) {
    const characters = Array.from(name);
    if (characters.length <= MAX_NAME_CHARACTERS) return name;
    return `${characters.slice(0, MAX_NAME_CHARACTERS).join('')}…`;
}

export function formatPlatformWatermarkText(identity: PlatformWatermarkIdentity | null | undefined) {
    if (!identity) return null;
    const name = truncateName(firstNonEmptyString(identity.display_name, identity.username));
    if (!name) return null;

    const mobileDigits = String(identity.primary_mobile || '').replace(/\D/g, '');
    const mobileTail = mobileDigits.slice(-4);
    return mobileTail ? `${name} ${mobileTail}` : name;
}

export function resolvePlatformWatermarkTheme(
    targetDocument: Pick<Document, 'documentElement'> = document,
): PlatformWatermarkTheme {
    return targetDocument.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
}

export function calculatePlatformWatermarkTileLayout(
    measuredTextWidth: number,
    viewportWidth: number,
    devicePixelRatio: number,
) {
    const isNarrow = viewportWidth > 0 && viewportWidth <= MOBILE_VIEWPORT_MAX_WIDTH;
    const fontSize = isNarrow ? 13 : 14;
    const horizontalPadding = isNarrow ? 54 : 68;
    return {
        fontSize,
        width: Math.max(isNarrow ? 168 : 190, Math.ceil(measuredTextWidth + horizontalPadding)),
        height: isNarrow ? 112 : 128,
        devicePixelRatio: Math.min(MAX_DEVICE_PIXEL_RATIO, Math.max(1, devicePixelRatio || 1)),
    };
}

function escapePlatformWatermarkXml(value: string) {
    return value.replace(/[&<>"']/g, (character) => {
        switch (character) {
            case '&':
                return '&amp;';
            case '<':
                return '&lt;';
            case '>':
                return '&gt;';
            case '"':
                return '&quot;';
            case '\'':
                return '&apos;';
            default:
                return character;
        }
    });
}

export function createPlatformWatermarkSvgDataUrl(
    text: string,
    theme: PlatformWatermarkTheme,
    layout: Pick<ReturnType<typeof calculatePlatformWatermarkTileLayout>, 'fontSize' | 'width' | 'height'>,
) {
    const escapedText = escapePlatformWatermarkXml(text);
    const escapedFontFamily = escapePlatformWatermarkXml(WATERMARK_FONT_FAMILY);
    const fill = theme === 'dark' ? 'rgb(255,255,255)' : 'rgb(0,0,0)';
    const fillOpacity = theme === 'dark' ? '0.10' : '0.09';
    const centerX = layout.width / 2;
    const centerY = layout.height / 2;
    const svg = [
        `<svg xmlns="http://www.w3.org/2000/svg" width="${layout.width}" height="${layout.height}"`,
        ` viewBox="0 0 ${layout.width} ${layout.height}" preserveAspectRatio="xMidYMid meet">`,
        `<text x="${centerX}" y="${centerY}"`,
        ` transform="rotate(${WATERMARK_ROTATION_DEGREES} ${centerX} ${centerY})"`,
        ` font-family="${escapedFontFamily}" font-size="${layout.fontSize}" font-weight="500"`,
        ` fill="${fill}" fill-opacity="${fillOpacity}" text-anchor="middle" dominant-baseline="middle"`,
        ` text-rendering="geometricPrecision">${escapedText}</text></svg>`,
    ].join('');

    return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}

function createPlatformWatermarkCanvasTile(
    text: string,
    theme: PlatformWatermarkTheme,
    layout: ReturnType<typeof calculatePlatformWatermarkTileLayout>,
    targetDocument: Document,
): PlatformWatermarkTile {
    const canvas = targetDocument.createElement('canvas');
    canvas.width = Math.ceil(layout.width * layout.devicePixelRatio);
    canvas.height = Math.ceil(layout.height * layout.devicePixelRatio);
    const drawingContext = canvas.getContext('2d');
    if (!drawingContext) throw new Error('Canvas 2D is unavailable');

    const scaleX = canvas.width / layout.width;
    const scaleY = canvas.height / layout.height;
    drawingContext.setTransform(scaleX, 0, 0, scaleY, 0, 0);
    drawingContext.translate(layout.width / 2, layout.height / 2);
    drawingContext.rotate(WATERMARK_ROTATION_DEGREES * Math.PI / 180);
    drawingContext.font = `500 ${layout.fontSize}px ${WATERMARK_FONT_FAMILY}`;
    drawingContext.fillStyle = theme === 'dark'
        ? 'rgba(255,255,255,0.10)'
        : 'rgba(0,0,0,0.09)';
    drawingContext.textAlign = 'center';
    drawingContext.textBaseline = 'middle';
    drawingContext.fillText(text, 0, 0);

    return {
        dataUrl: canvas.toDataURL('image/png'),
        width: layout.width,
        height: layout.height,
    };
}

export function createPlatformWatermarkTile(
    text: string,
    theme: PlatformWatermarkTheme,
    targetDocument: Document = document,
    targetWindow: Window = window,
): PlatformWatermarkTile {
    const measurementCanvas = targetDocument.createElement('canvas');
    const measurementContext = measurementCanvas.getContext('2d');
    if (!measurementContext) throw new Error('Canvas 2D is unavailable');

    const isNarrow = targetWindow.innerWidth > 0 && targetWindow.innerWidth <= MOBILE_VIEWPORT_MAX_WIDTH;
    const fontSize = isNarrow ? 13 : 14;
    measurementContext.font = `500 ${fontSize}px ${WATERMARK_FONT_FAMILY}`;
    const measuredTextWidth = measurementContext.measureText(text).width;
    const layout = calculatePlatformWatermarkTileLayout(
        measuredTextWidth,
        targetWindow.innerWidth,
        targetWindow.devicePixelRatio,
    );

    try {
        return {
            dataUrl: createPlatformWatermarkSvgDataUrl(text, theme, layout),
            width: layout.width,
            height: layout.height,
        };
    } catch {
        return createPlatformWatermarkCanvasTile(text, theme, layout, targetDocument);
    }
}

export function installPlatformWatermark(
    text: string,
    {
        targetDocument = document,
        targetWindow = window,
    }: PlatformWatermarkInstallOptions = {},
) {
    const body = targetDocument.body;
    const staleHost = targetDocument.getElementById(WATERMARK_HOST_ID);
    staleHost?.parentNode?.removeChild(staleHost);

    const host = targetDocument.createElement('div');
    host.id = WATERMARK_HOST_ID;
    host.setAttribute(WATERMARK_ATTRIBUTE, 'true');
    host.setAttribute('aria-hidden', 'true');
    host.style.cssText = WATERMARK_HOST_STYLE;

    const renderRoot = typeof host.attachShadow === 'function'
        ? host.attachShadow({ mode: 'closed' })
        : host;
    const layer = targetDocument.createElement('div');
    layer.setAttribute('part', 'layer');
    layer.style.cssText = WATERMARK_LAYER_STYLE;
    renderRoot.appendChild(layer);

    let currentTheme: PlatformWatermarkTheme | null = null;
    let currentLayoutKey = '';
    let resizeFrame: number | null = null;

    const getLayoutKey = () => {
        const viewport = targetWindow.innerWidth <= MOBILE_VIEWPORT_MAX_WIDTH ? 'narrow' : 'wide';
        const pixelRatio = Math.min(MAX_DEVICE_PIXEL_RATIO, Math.max(1, targetWindow.devicePixelRatio || 1));
        return `${viewport}:${pixelRatio}`;
    };

    const render = (nextTheme = resolvePlatformWatermarkTheme(targetDocument)) => {
        const nextLayoutKey = getLayoutKey();
        if (nextTheme === currentTheme && nextLayoutKey === currentLayoutKey) return;
        const tile = createPlatformWatermarkTile(
            text,
            nextTheme,
            targetDocument,
            targetWindow,
        );
        layer.style.setProperty('background-image', `url("${tile.dataUrl}")`, 'important');
        layer.style.setProperty('background-size', `${tile.width}px ${tile.height}px`, 'important');
        currentTheme = nextTheme;
        currentLayoutKey = nextLayoutKey;
    };

    render();
    body.appendChild(host);

    const handleThemeChange = (event: Event) => {
        const detail = (event as CustomEvent<{ theme?: PlatformWatermarkTheme }>).detail;
        render(detail?.theme === 'dark' ? 'dark' : 'light');
    };

    const handleResize = () => {
        if (resizeFrame != null) targetWindow.cancelAnimationFrame(resizeFrame);
        resizeFrame = targetWindow.requestAnimationFrame(() => {
            resizeFrame = null;
            render();
        });
    };
    targetWindow.addEventListener(DOCUMENT_THEME_CHANGE_EVENT, handleThemeChange);
    targetWindow.addEventListener('resize', handleResize);

    return () => {
        targetWindow.removeEventListener(DOCUMENT_THEME_CHANGE_EVENT, handleThemeChange);
        targetWindow.removeEventListener('resize', handleResize);
        if (resizeFrame != null) targetWindow.cancelAnimationFrame(resizeFrame);
        if (host.parentNode) host.parentNode.removeChild(host);
    };
}
