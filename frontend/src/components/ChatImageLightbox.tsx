import { useMemo } from 'react';
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
    onClose: () => void;
    onIndexChange?: (index: number) => void;
};

export default function ChatImageLightbox({
    open,
    images,
    index,
    mode,
    onClose,
    onIndexChange,
}: Props) {
    const slides = useMemo(() => images.map((image) => ({
        src: image.src,
        alt: image.alt || image.filename || 'image',
        download: {
            url: image.downloadUrl || image.src,
            filename: image.filename || image.alt || 'image',
        },
    })), [images]);

    const isMobile = mode === 'mobile';
    const toolbarButtons = (isMobile ? ['close'] : ['zoom', 'fullscreen', 'download', 'close']) as any;

    return (
        <Lightbox
            open={open && slides.length > 0}
            close={onClose}
            slides={slides}
            index={Math.min(Math.max(index, 0), Math.max(0, slides.length - 1))}
            plugins={isMobile ? [Zoom, Counter] : [Zoom, Fullscreen, Download, Counter]}
            className={`chat-image-lightbox chat-image-lightbox--${mode}`}
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
