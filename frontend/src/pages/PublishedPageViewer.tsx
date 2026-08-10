import { useEffect, useMemo, useState } from 'react';
import { IconFileAlert, IconLoader2 } from '@tabler/icons-react';
import { useParams } from 'react-router-dom';
import {
    formatPlatformWatermarkText,
    installPlatformWatermark,
    type PlatformWatermarkIdentity,
} from '../utils/platformWatermark';
import './PublishedPageViewer.css';

type ViewerContext = {
    title: string;
    access_mode: 'authenticated' | 'restricted';
    watermark_identity: PlatformWatermarkIdentity;
};

function retryThroughPublishedUrl(shortId: string) {
    window.location.replace(`/p/${encodeURIComponent(shortId)}${window.location.search}${window.location.hash}`);
}

export default function PublishedPageViewer() {
    const { shortId = '' } = useParams();
    const [context, setContext] = useState<ViewerContext | null>(null);
    const [html, setHtml] = useState<string | null>(null);
    const [error, setError] = useState('');

    useEffect(() => {
        if (!shortId) {
            setError('页面地址不完整');
            return;
        }
        const controller = new AbortController();
        const load = async () => {
            try {
                const contextResponse = await fetch(
                    `/api/pages/${encodeURIComponent(shortId)}/viewer-context`,
                    { credentials: 'same-origin', cache: 'no-store', signal: controller.signal },
                );
                if (contextResponse.status === 401 || contextResponse.status === 403) {
                    retryThroughPublishedUrl(shortId);
                    return;
                }
                if (!contextResponse.ok) throw new Error('暂时无法打开这个页面');
                const viewerContext = await contextResponse.json() as ViewerContext;
                setContext(viewerContext);
                document.title = viewerContext.title;

                const contentResponse = await fetch(
                    `/api/pages/${encodeURIComponent(shortId)}/content`,
                    { credentials: 'same-origin', cache: 'no-store', signal: controller.signal },
                );
                if (contentResponse.status === 401 || contentResponse.status === 403) {
                    retryThroughPublishedUrl(shortId);
                    return;
                }
                if (!contentResponse.ok) throw new Error('页面内容暂时不可用');
                setHtml(await contentResponse.text());
            } catch (loadError) {
                if ((loadError as Error).name !== 'AbortError') {
                    setError((loadError as Error).message || '暂时无法打开这个页面');
                }
            }
        };
        void load();
        return () => controller.abort();
    }, [shortId]);

    const watermarkText = useMemo(
        () => formatPlatformWatermarkText(context?.watermark_identity),
        [context?.watermark_identity],
    );

    useEffect(() => {
        if (!watermarkText) return;
        try {
            return installPlatformWatermark(watermarkText);
        } catch (watermarkError) {
            console.warn('Unable to render the published-page watermark', watermarkError);
        }
    }, [watermarkText]);

    const sandbox = useMemo(() => {
        const capabilities = ['allow-scripts', 'allow-forms', 'allow-popups', 'allow-modals', 'allow-downloads'];
        if (html?.includes('/sdk/clawith.js')) capabilities.push('allow-top-navigation');
        return capabilities.join(' ');
    }, [html]);

    if (error) {
        return (
            <main className="published-page-viewer-state" role="alert">
                <IconFileAlert size={30} aria-hidden="true" />
                <h1>页面暂时无法打开</h1>
                <p>{error}</p>
                <button type="button" onClick={() => window.location.reload()}>重新加载</button>
            </main>
        );
    }

    if (html === null) {
        return (
            <main className="published-page-viewer-state" aria-live="polite">
                <IconLoader2 className="published-page-viewer-spinner" size={26} aria-hidden="true" />
                <p>正在安全加载页面…</p>
            </main>
        );
    }

    return (
        <main className="published-page-viewer">
            <iframe
                className="published-page-viewer-frame"
                title={context?.title || '发布页面'}
                sandbox={sandbox}
                srcDoc={html}
            />
        </main>
    );
}
