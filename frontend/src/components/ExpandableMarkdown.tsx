import { useEffect, useId, useRef, useState, type CSSProperties } from 'react';
import { IconChevronDown } from '@tabler/icons-react';
import { useTranslation } from 'react-i18next';

import MarkdownRenderer from './MarkdownRenderer';
import Button from './ui/Button';
import './ExpandableMarkdown.css';

type Props = {
    content: string;
    agentId?: string;
    previewLines?: number;
};

export default function ExpandableMarkdown({ content, agentId, previewLines = 3 }: Props) {
    const { t } = useTranslation();
    const id = useId();
    const viewportRef = useRef<HTMLDivElement>(null);
    const contentRef = useRef<HTMLDivElement>(null);
    const [expanded, setExpanded] = useState(false);
    const [overflowing, setOverflowing] = useState(false);

    useEffect(() => setExpanded(false), [content]);

    useEffect(() => {
        const viewport = viewportRef.current;
        const body = contentRef.current;
        if (!viewport || !body) return;
        const measure = () => {
            const lineHeight = Number.parseFloat(getComputedStyle(viewport).lineHeight);
            setOverflowing(body.getBoundingClientRect().height > lineHeight * previewLines + 1);
        };
        const observer = new ResizeObserver(measure);
        observer.observe(viewport);
        observer.observe(body);
        measure();
        return () => observer.disconnect();
    }, [content, previewLines]);

    return (
        <div
            className="expandable-markdown"
            style={{ '--markdown-preview-lines': previewLines } as CSSProperties}
        >
            <div
                id={id}
                ref={viewportRef}
                className="expandable-markdown__viewport"
                data-expanded={expanded}
            >
                <div ref={contentRef}>
                    <MarkdownRenderer content={content} agentId={agentId} />
                </div>
            </div>
            {overflowing && (
                <Button
                    type="button"
                    variant="ghost"
                    className="expandable-markdown__toggle"
                    aria-expanded={expanded}
                    aria-controls={id}
                    onClick={() => setExpanded((value) => !value)}
                >
                    {t(expanded ? 'markdown.collapseDescription' : 'markdown.expandDescription')}
                    <IconChevronDown size={14} aria-hidden="true" />
                </Button>
            )}
        </div>
    );
}
