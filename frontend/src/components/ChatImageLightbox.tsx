import { useEffect, useMemo } from 'react';
import Lightbox from 'yet-another-react-lightbox';
import Zoom from 'yet-another-react-lightbox/plugins/zoom';
import Fullscreen from 'yet-another-react-lightbox/plugins/fullscreen';
import Download from 'yet-another-react-lightbox/plugins/download';
import Counter from 'yet-another-react-lightbox/plugins/counter';
import 'yet-another-react-lightbox/styles.css';
import 'yet-another-react-lightbox/plugins/counter.css';
import type { ChatPreviewImage } from '../utils/chatAttachments';
import './ChatImageLightbox.css';

type Props = {
    open: boolean;
    images: ChatPreviewImage[];
    index: number;
    mode: 'desktop' | 'mobile';
    allowDownload?: boolean;
    protectImages?: boolean;
    onClose: () => void;
    onIndexChange?: (index: number) => void;
};

export default function ChatImageLightbox({
    open,
    images,
    index,
    mode,
    allowDownload,
    protectImages = false,
    onClose,
    onIndexChange,
}: Props) {
    const isMobile = mode === 'mobile';
    const downloadEnabled = allowDownload ?? !isMobile;
    const slides = useMemo(() => images.map((image) => ({
        src: image.src,
        alt: image.alt || image.filename || 'image',
        ...(downloadEnabled ? {
            download: {
                url: image.downloadUrl || image.src,
                filename: image.filename || image.alt || 'image',
            },
        } : {}),
    })), [downloadEnabled, images]);

    useEffect(() => {
        if (!open || !protectImages) return undefined;

        const preventProtectedImageAction = (event: Event) => {
            const target = event.target;
            if (
                target instanceof Element
                && target.closest('.chat-image-lightbox--protected img')
            ) {
                event.preventDefault();
            }
        };

        document.addEventListener('contextmenu', preventProtectedImageAction, true);
        document.addEventListener('dragstart', preventProtectedImageAction, true);
        return () => {
            document.removeEventListener('contextmenu', preventProtectedImageAction, true);
            document.removeEventListener('dragstart', preventProtectedImageAction, true);
        };
    }, [open, protectImages]);

    const toolbarButtons = (
        isMobile
            ? [...(downloadEnabled ? ['download'] : []), 'close']
            : ['zoom', 'fullscreen', ...(downloadEnabled ? ['download'] : []), 'close']
    ) as any;
    const plugins = [
        Zoom,
        ...(!isMobile ? [Fullscreen] : []),
        ...(downloadEnabled ? [Download] : []),
        Counter,
    ];

    return (
        <Lightbox
            open={open && slides.length > 0}
            close={onClose}
            slides={slides}
            index={Math.min(Math.max(index, 0), Math.max(0, slides.length - 1))}
            plugins={plugins}
            className={`chat-image-lightbox chat-image-lightbox--${mode}${protectImages ? ' chat-image-lightbox--protected' : ''}`}
            toolbar={{
                buttons: toolbarButtons,
            }}
            counter={{ separator: ' / ' }}
            zoom={{
                pinchZoomV4: true,
                scrollToZoom: !isMobile,
                doubleClickMaxStops: 2,
                maxZoomPixelRatio: 4,
            }}
            carousel={{
                finite: slides.length <= 1,
                preload: 2,
            }}
            controller={{ closeOnBackdropClick: true }}
            on={{ view: ({ index: nextIndex }) => onIndexChange?.(nextIndex) }}
            labels={{
                Close: '关闭',
                Previous: '上一张',
                Next: '下一张',
                Download: '下载',
                'Zoom in': '放大',
                'Zoom out': '缩小',
                'Enter Fullscreen': '全屏',
                'Exit Fullscreen': '退出全屏',
            }}
        />
    );
}
