import MarkdownRenderer from '../MarkdownRenderer';
import { fileApi } from '../../services/api';
import type { WorkspaceLiveDraft } from './types';
import HtmlPreviewFrame from './HtmlPreviewFrame';
import { extOf, fileName, parseCsv } from './utils';

export default function PreviewContent({
    activePath,
    agentId,
    content,
    csvRows,
    draft,
    editing,
    ext,
    htmlPreviewSrc,
    isHtml,
    isImage,
    isSideResizing,
    liveDraft,
    preview,
    previewState,
    previewType,
    setDraft,
    shouldRenderLiveDraft,
    xlsxRows,
}: {
    activePath?: string | null;
    agentId: string;
    content: string;
    csvRows: string[][];
    draft: string;
    editing: boolean;
    ext: string;
    htmlPreviewSrc: string;
    isHtml: boolean;
    isImage: boolean;
    isSideResizing: boolean;
    liveDraft?: WorkspaceLiveDraft | null;
    preview: any;
    previewState: 'idle' | 'loading' | 'ready' | 'deleted';
    previewType?: string;
    setDraft: (value: string) => void;
    shouldRenderLiveDraft: boolean;
    xlsxRows: string[][];
}) {
    if (shouldRenderLiveDraft && liveDraft) {
        const draftExt = liveDraft.path ? extOf(liveDraft.path) : ext;
        const draftContent = liveDraft.content || '';
        if (draftExt === '.html' || draftExt === '.htm') {
            return (
                <div className="workspace-op-live">
                    <div className="workspace-op-live-banner">{liveDraft.status === 'drafting' ? 'Drafting HTML...' : 'Writing HTML...'}</div>
                    {draftContent ? (
                        <HtmlPreviewFrame content={draftContent} title={fileName(liveDraft.path || 'draft.html')} suspendAutoFit={isSideResizing} />
                    ) : (
                        <div className="workspace-op-empty">Preparing file content...</div>
                    )}
                </div>
            );
        }
        if (draftExt === '.csv') {
            const rows = parseCsv(draftContent).slice(0, 200);
            const [header, ...bodyRows] = rows;
            return (
                <div className="workspace-op-live">
                    <div className="workspace-op-live-banner">{liveDraft.status === 'drafting' ? 'Drafting CSV...' : 'Writing CSV...'}</div>
                    {rows.length ? (
                        <div className="workspace-op-table-wrap">
                            <table className="workspace-op-table">
                                {!!header?.length && (
                                    <thead>
                                        <tr>{header.map((cell, j) => <th key={j}>{cell}</th>)}</tr>
                                    </thead>
                                )}
                                <tbody>
                                    {bodyRows.map((row, i) => (
                                        <tr key={i}>{row.map((cell, j) => <td key={j}>{cell}</td>)}</tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    ) : (
                        <div className="workspace-op-empty">Preparing file content...</div>
                    )}
                </div>
            );
        }
        return (
            <div className="workspace-op-live">
                <div className="workspace-op-live-banner">{liveDraft.status === 'drafting' ? 'Drafting file...' : 'Writing file...'}</div>
                {draftContent ? <MarkdownRenderer content={draftContent} /> : <div className="workspace-op-empty">Preparing file content...</div>}
            </div>
        );
    }
    if (!activePath) {
        return <div className="workspace-op-empty">No workspace file activity yet.</div>;
    }
    if (previewState === 'loading') {
        return <div className="workspace-op-empty">Loading file preview...</div>;
    }
    if (previewState === 'deleted') {
        return (
            <div className="workspace-op-empty workspace-op-deleted">
                <strong className="workspace-op-deleted-title">This file was deleted.</strong>
                <span className="workspace-op-deleted-path">{activePath}</span>
            </div>
        );
    }
    if (!preview) {
        return <div className="workspace-op-empty">Preview is not available for this file.</div>;
    }
    if (editing) {
        return (
            <textarea
                className="workspace-op-editor"
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                spellCheck={false}
            />
        );
    }
    if (previewType === 'md' || previewType === 'markdown') {
        return <MarkdownRenderer content={content || ''} />;
    }
    if (previewType === 'text') {
        return <pre className="workspace-op-text-preview">{preview.content || preview.text || ''}</pre>;
    }
    if (previewType === 'csv') {
        const rows = csvRows;
        const maxCols = rows.reduce((max, row) => Math.max(max, row.length), 0);
        const [header, ...bodyRows] = rows;
        return (
            <div className="workspace-op-table-wrap">
                <table className="workspace-op-table">
                    {!!header?.length && (
                        <thead>
                            <tr>
                                {Array.from({ length: maxCols }).map((_, j) => <th key={j}>{header[j] || ''}</th>)}
                            </tr>
                        </thead>
                    )}
                    <tbody>
                        {bodyRows.map((row, i) => (
                            <tr key={i}>
                                {Array.from({ length: maxCols }).map((_, j) => <td key={j}>{row[j] || ''}</td>)}
                            </tr>
                        ))}
                    </tbody>
                </table>
            </div>
        );
    }
    if (previewType === 'xlsx') {
        const rows = xlsxRows;
        const maxCols = rows.reduce((max: number, row: string[]) => Math.max(max, row.length), 0);
        const [header, ...bodyRows] = rows;
        return (
            <div className="workspace-op-table-wrap">
                <table className="workspace-op-table">
                    {!!header?.length && (
                        <thead>
                            <tr>
                                {Array.from({ length: maxCols }).map((_, j) => <th key={j}>{header[j] || ''}</th>)}
                            </tr>
                        </thead>
                    )}
                    <tbody>
                        {bodyRows.map((row: string[], i: number) => (
                            <tr key={i}>
                                {Array.from({ length: maxCols }).map((_, j) => <td key={j}>{row[j] || ''}</td>)}
                            </tr>
                        ))}
                    </tbody>
                </table>
            </div>
        );
    }
    if (isHtml) {
        if (isSideResizing) {
            return <div className="workspace-op-empty workspace-op-preview-paused">Release to refresh HTML preview.</div>;
        }
        return <HtmlPreviewFrame content={content || ''} title={fileName(activePath)} src={htmlPreviewSrc || undefined} suspendAutoFit={isSideResizing} />;
    }
    if (isImage) {
        return (
            <div className="workspace-op-image-preview">
                <img
                    src={fileApi.downloadUrl(agentId, activePath, { inline: true })}
                    alt={fileName(activePath)}
                    className="workspace-op-image"
                />
            </div>
        );
    }
    if (previewType === 'pdf') {
        if (isSideResizing) {
            return <div className="workspace-op-empty workspace-op-preview-paused">Release to refresh PDF preview.</div>;
        }
        return <iframe className="workspace-op-pdf" src={fileApi.downloadUrl(agentId, activePath, { inline: true })} title={fileName(activePath)} />;
    }
    if (previewType === 'docx') {
        return <pre className="workspace-op-text-preview">{preview.content || preview.text}</pre>;
    }
    if (previewType === 'pptx') {
        if (!(preview.slides || []).length) {
            return <pre className="workspace-op-text-preview">{preview.content || preview.text || ''}</pre>;
        }
        return (
            <div className="workspace-op-ppt-preview">
                {(preview.slides || []).map((slide: any) => (
                    <section className="workspace-op-slide-card" key={slide.slide}>
                        <div className="workspace-op-slide-label">Slide {slide.slide}</div>
                        <div className="workspace-op-slide-canvas">
                            {(slide.shapes || []).map((shape: any, idx: number) => (
                                <div
                                    key={idx}
                                    className="workspace-op-slide-shape"
                                    style={{
                                        left: `${Math.max(0, Math.min(1, shape.left || 0)) * 100}%`,
                                        top: `${Math.max(0, Math.min(1, shape.top || 0)) * 100}%`,
                                        width: `${Math.max(0.04, Math.min(1, shape.width || 0.5)) * 100}%`,
                                        height: `${Math.max(0.04, Math.min(1, shape.height || 0.15)) * 100}%`,
                                    }}
                                >
                                    {shape.text}
                                </div>
                            ))}
                        </div>
                    </section>
                ))}
            </div>
        );
    }
    return <div className="workspace-op-empty">Preview is not available for this file. Download it instead.</div>;
}
