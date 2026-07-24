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
const MAX_NAME_CHARACTERS = 24;
const MAX_DEVICE_PIXEL_RATIO = 2;
const MOBILE_VIEWPORT_MAX_WIDTH = 600;
const WATERMARK_FONT_FAMILY = [
    '-apple-system',
    'BlinkMacSystemFont',
    '"Segoe UI"',
    '"PingFang SC"',
    '"Microsoft YaHei"',
    'Arial',
    'sans-serif',
].join(',');

const CRITICAL_STYLE: Readonly<Record<string, string>> = {
    position: 'fixed',
    inset: '0px',
    margin: '0px',
    padding: '0px',
    border: '0px',
    display: 'block',
    visibility: 'visible',
    opacity: '1',
    'pointer-events': 'none',
    'user-select': 'none',
    '-webkit-user-select': 'none',
    'z-index': '2147483647',
    overflow: 'hidden',
    transform: 'none',
    filter: 'none',
    'background-color': 'transparent',
    'background-repeat': 'repeat',
    'background-position': '0px 0px',
};

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

export function createPlatformWatermarkTile(
    text: string,
    theme: PlatformWatermarkTheme,
    targetDocument: Document = document,
    targetWindow: Window = window,
): PlatformWatermarkTile {
    const canvas = targetDocument.createElement('canvas');
    const context = canvas.getContext('2d');
    if (!context) throw new Error('Canvas 2D is unavailable');

    const isNarrow = targetWindow.innerWidth > 0 && targetWindow.innerWidth <= MOBILE_VIEWPORT_MAX_WIDTH;
    const fontSize = isNarrow ? 13 : 14;
    context.font = `500 ${fontSize}px ${WATERMARK_FONT_FAMILY}`;
    const measuredTextWidth = context.measureText(text).width;
    const layout = calculatePlatformWatermarkTileLayout(
        measuredTextWidth,
        targetWindow.innerWidth,
        targetWindow.devicePixelRatio,
    );

    canvas.width = Math.ceil(layout.width * layout.devicePixelRatio);
    canvas.height = Math.ceil(layout.height * layout.devicePixelRatio);
    const drawingContext = canvas.getContext('2d');
    if (!drawingContext) throw new Error('Canvas 2D is unavailable');

    drawingContext.scale(layout.devicePixelRatio, layout.devicePixelRatio);
    drawingContext.translate(layout.width / 2, layout.height / 2);
    drawingContext.rotate(-22 * Math.PI / 180);
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

function setImportantStyle(element: HTMLElement, property: string, value: string) {
    element.style.setProperty(property, value, 'important');
}

function applyCanonicalStyle(element: HTMLElement, tile: PlatformWatermarkTile) {
    element.removeAttribute('class');
    element.setAttribute(WATERMARK_ATTRIBUTE, 'true');
    element.setAttribute('aria-hidden', 'true');
    for (const [property, value] of Object.entries(CRITICAL_STYLE)) {
        setImportantStyle(element, property, value);
    }
    setImportantStyle(element, 'background-image', `url("${tile.dataUrl}")`);
    setImportantStyle(element, 'background-size', `${tile.width}px ${tile.height}px`);
}

function hasStyleDrifted(element: HTMLElement, tile: PlatformWatermarkTile) {
    if (element.getAttribute(WATERMARK_ATTRIBUTE) !== 'true') return true;
    if (element.hasAttribute('class')) return true;
    for (const [property, value] of Object.entries(CRITICAL_STYLE)) {
        if (element.style.getPropertyValue(property) !== value) return true;
        if (element.style.getPropertyPriority(property) !== 'important') return true;
    }
    if (!element.style.getPropertyValue('background-image').includes(tile.dataUrl)) return true;
    if (element.style.getPropertyPriority('background-image') !== 'important') return true;
    if (element.style.getPropertyValue('background-size') !== `${tile.width}px ${tile.height}px`) return true;
    return element.style.getPropertyPriority('background-size') !== 'important';
}

export function installPlatformWatermark(
    text: string,
    {
        targetDocument = document,
        targetWindow = window,
    }: PlatformWatermarkInstallOptions = {},
) {
    const body = targetDocument.body;
    const root = targetDocument.documentElement;
    const element = targetDocument.createElement('div');
    let currentTile: PlatformWatermarkTile;
    let resizeFrame: number | null = null;

    const render = () => {
        currentTile = createPlatformWatermarkTile(
            text,
            resolvePlatformWatermarkTheme(targetDocument),
            targetDocument,
            targetWindow,
        );
        applyCanonicalStyle(element, currentTile);
    };

    render();
    body.querySelectorAll<HTMLElement>(`[${WATERMARK_ATTRIBUTE}]`).forEach((staleElement) => {
        staleElement.remove();
    });
    body.appendChild(element);

    const Observer = (targetWindow as Window & typeof globalThis).MutationObserver;
    const themeObserver = Observer
        ? new Observer(() => render())
        : null;
    themeObserver?.observe(root, {
        attributes: true,
        attributeFilter: ['data-theme'],
    });

    const bodyObserver = Observer
        ? new Observer(() => {
            if (element.parentNode !== body) body.appendChild(element);
        })
        : null;
    bodyObserver?.observe(body, { childList: true });

    const elementObserver = Observer
        ? new Observer(() => {
            if (hasStyleDrifted(element, currentTile)) applyCanonicalStyle(element, currentTile);
        })
        : null;
    elementObserver?.observe(element, {
        attributes: true,
        attributeFilter: ['style', 'class', WATERMARK_ATTRIBUTE],
    });

    const handleResize = () => {
        if (resizeFrame != null) targetWindow.cancelAnimationFrame(resizeFrame);
        resizeFrame = targetWindow.requestAnimationFrame(() => {
            resizeFrame = null;
            render();
        });
    };
    targetWindow.addEventListener('resize', handleResize);

    return () => {
        themeObserver?.disconnect();
        bodyObserver?.disconnect();
        elementObserver?.disconnect();
        targetWindow.removeEventListener('resize', handleResize);
        if (resizeFrame != null) targetWindow.cancelAnimationFrame(resizeFrame);
        element.remove();
    };
}
