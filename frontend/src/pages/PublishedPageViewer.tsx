import { useEffect, useMemo, useRef, useState } from 'react';
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
    access_mode: 'public' | 'authenticated' | 'restricted';
    watermark_identity: PlatformWatermarkIdentity | null;
    watermark_text: string | null;
};

function retryThroughPublishedUrl(shortId: string) {
    window.location.replace(`/p/${encodeURIComponent(shortId)}${window.location.search}${window.location.hash}`);
}

function showUnavailablePage() {
    window.location.replace('/published-page-unavailable');
}

function handleViewerStatus(shortId: string, status: number): boolean {
    if (status === 401 || status === 403) {
        retryThroughPublishedUrl(shortId);
        return true;
    }
    if (status === 404 || status === 410) {
        showUnavailablePage();
        return true;
    }
    return false;
}

export default function PublishedPageViewer() {
    const { shortId = '' } = useParams();
    const frameRef = useRef<HTMLIFrameElement>(null);
    const [context, setContext] = useState<ViewerContext | null>(null);
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
                if (handleViewerStatus(shortId, contextResponse.status)) return;
                if (!contextResponse.ok) throw new Error('暂时无法打开这个页面');
                const viewerContext = await contextResponse.json() as ViewerContext;
                setContext(viewerContext);
                document.title = viewerContext.title;
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
        () => context?.watermark_text || formatPlatformWatermarkText(context?.watermark_identity),
        [context?.watermark_identity, context?.watermark_text],
    );

    const iframeSrc = useMemo(() => {
        const params = new URLSearchParams(window.location.search);
        params.set('__report_embed', '1');
        return `/p/${encodeURIComponent(shortId)}?${params.toString()}${window.location.hash}`;
    }, [shortId]);

    useEffect(() => {
        const handleSdkAuthStart = (event: MessageEvent) => {
            if (
                event.source !== frameRef.current?.contentWindow
                || event.data?.type !== 'published-page:sdk-auth-start'
                || !shortId
            ) return;
            const returnTo = `${window.location.origin}/p/${encodeURIComponent(shortId)}`;
            window.location.assign(`/api/sdk/auth/start?return_to=${encodeURIComponent(returnTo)}`);
        };
        window.addEventListener('message', handleSdkAuthStart);
        return () => window.removeEventListener('message', handleSdkAuthStart);
    }, [shortId]);

    useEffect(() => {
        const url = new URL(window.location.href);
        if (!url.searchParams.has('code') && !url.searchParams.has('state')) return;
        url.searchParams.delete('code');
        url.searchParams.delete('state');
        window.history.replaceState(window.history.state, '', `${url.pathname}${url.search}${url.hash}`);
    }, []);

    useEffect(() => {
        if (!watermarkText) return;
        try {
            return installPlatformWatermark(watermarkText);
        } catch (watermarkError) {
            console.warn('Unable to render the published-page watermark', watermarkError);
        }
    }, [watermarkText]);

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

    if (context === null) {
        return (
            <main className="published-page-viewer-state" aria-live="polite">
                <IconLoader2 className="published-page-viewer-spinner" size={26} aria-hidden="true" />
                <p>正在加载页面…</p>
            </main>
        );
    }

    return (
        <main className="published-page-viewer">
            <iframe
                ref={frameRef}
                className="published-page-viewer-frame"
                title={context.title || '发布页面'}
                src={iframeSrc}
            />
        </main>
    );
}
