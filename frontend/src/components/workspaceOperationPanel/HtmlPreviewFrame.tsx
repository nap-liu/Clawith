import { useEffect, useRef, useState } from 'react';

export default function HtmlPreviewFrame({
    content,
    title,
    src,
}: {
    content: string;
    title: string;
    src?: string;
    suspendAutoFit?: boolean;
}) {
    const containerRef = useRef<HTMLDivElement>(null);
    const frameRef = useRef<HTMLIFrameElement>(null);
    const debounceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const [renderContent, setRenderContent] = useState(content);
    const observersRef = useRef<{ frame?: ResizeObserver; document?: ResizeObserver; mutation?: MutationObserver } | null>(null);
    const fitRafRef = useRef<number | null>(null);

    const fitFixedWidthContent = () => {
        const frame = frameRef.current;
        const doc = frame?.contentDocument;
        const root = doc?.documentElement;
        const body = doc?.body;
        if (!frame || !doc || !root || !body) return;

        root.style.zoom = '1';
        const viewportWidth = Math.max(frame.clientWidth, 1);
        const contentWidth = Math.max(
            root.scrollWidth,
            body.scrollWidth,
            root.offsetWidth,
            body.offsetWidth,
            viewportWidth,
        );
        const nextZoom = Math.min(1, viewportWidth / contentWidth);
        root.style.zoom = nextZoom < 0.995 ? String(nextZoom) : '1';
    };

    const requestFitFixedWidthContent = () => {
        if (fitRafRef.current != null) cancelAnimationFrame(fitRafRef.current);
        fitRafRef.current = requestAnimationFrame(() => {
            fitRafRef.current = null;
            fitFixedWidthContent();
        });
    };

    const bindFrameFitObservers = () => {
        const frame = frameRef.current;
        const doc = frame?.contentDocument;
        if (!frame || !doc?.documentElement || !doc.body) return;

        observersRef.current?.frame?.disconnect();
        observersRef.current?.document?.disconnect();
        observersRef.current?.mutation?.disconnect();
        observersRef.current = {};

        if (typeof ResizeObserver !== 'undefined') {
            const frameResize = new ResizeObserver(() => requestFitFixedWidthContent());
            frameResize.observe(frame);
            if (containerRef.current) frameResize.observe(containerRef.current);

            const documentResize = new ResizeObserver(() => requestFitFixedWidthContent());
            documentResize.observe(doc.documentElement);
            documentResize.observe(doc.body);

            observersRef.current.frame = frameResize;
            observersRef.current.document = documentResize;
        }

        if (typeof MutationObserver !== 'undefined') {
            const mutation = new MutationObserver(() => requestFitFixedWidthContent());
            mutation.observe(doc.documentElement, {
                subtree: true,
                childList: true,
                characterData: true,
                attributes: true,
            });
            observersRef.current.mutation = mutation;
        }

        requestFitFixedWidthContent();
    };

    useEffect(() => {
        if (src) return;
        if (!content) {
            setRenderContent(content);
            return;
        }
        if (!renderContent) {
            setRenderContent(content);
            return;
        }
        if (debounceTimerRef.current) clearTimeout(debounceTimerRef.current);
        debounceTimerRef.current = setTimeout(() => {
            setRenderContent(content);
            debounceTimerRef.current = null;
        }, 180);
        return () => {
            if (debounceTimerRef.current) clearTimeout(debounceTimerRef.current);
        };
    }, [content, renderContent, src]);

    useEffect(() => () => {
        if (debounceTimerRef.current) clearTimeout(debounceTimerRef.current);
        observersRef.current?.frame?.disconnect();
        observersRef.current?.document?.disconnect();
        observersRef.current?.mutation?.disconnect();
        if (fitRafRef.current != null) cancelAnimationFrame(fitRafRef.current);
    }, []);

    return (
        <div className="workspace-op-html-fit" ref={containerRef}>
            <iframe
                ref={frameRef}
                sandbox="allow-same-origin allow-scripts allow-forms allow-modals allow-popups allow-downloads allow-pointer-lock allow-top-navigation-by-user-activation"
                src={src}
                srcDoc={src ? undefined : renderContent}
                title={title}
                onLoad={() => {
                    requestAnimationFrame(() => {
                        bindFrameFitObservers();
                        requestFitFixedWidthContent();
                    });
                }}
            />
        </div>
    );
}
