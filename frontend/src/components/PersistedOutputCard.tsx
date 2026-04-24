import React, { useCallback, useState } from 'react';
import { Paperclip } from 'lucide-react';
import { fileApi } from '../services/api';
import type { PersistedOutput } from '../utils/persistedOutput';
import { persistedOutputBasename } from '../utils/persistedOutput';

interface Props {
    agentId: string;
    parsed: PersistedOutput;
}

/**
 * Renders a tool output that was materialized to the agent workspace by the
 * backend (see `<persisted-output>` envelope in utils/persistedOutput.ts).
 *
 * Visual contract:
 *   - Collapsed: just a header row + a 120-char single-line preview summary.
 *   - Expanded:  preview block + a "read full output" CTA that fetches the
 *                materialized file via `fileApi.read` and renders it below.
 *
 * The fetched full content is cached inside component state so repeated
 * toggles do not refetch. Failures surface inline with a retry button.
 */
const PersistedOutputCard: React.FC<Props> = ({ agentId, parsed }) => {
    const [expanded, setExpanded] = useState(false);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [fullContent, setFullContent] = useState<string | null>(null);

    const basename = persistedOutputBasename(parsed.filePath);

    const loadFull = useCallback(async () => {
        if (loading) return;
        setLoading(true);
        setError(null);
        try {
            const res = await fileApi.read(agentId, parsed.filePath);
            setFullContent(typeof res?.content === 'string' ? res.content : JSON.stringify(res?.content ?? ''));
        } catch (e: any) {
            setError(e?.message || String(e) || 'unknown error');
        } finally {
            setLoading(false);
        }
    }, [agentId, parsed.filePath, loading]);

    const summaryLine = parsed.preview.replace(/\s+/g, ' ').slice(0, 120);
    const summaryHasMore = parsed.preview.length > 120;

    return (
        <div
            style={{
                borderRadius: '6px',
                background: 'var(--bg-secondary)',
                border: '1px solid var(--border-subtle)',
                overflow: 'hidden',
                fontFamily: 'monospace',
                fontSize: '11px',
            }}
        >
            {/* Header */}
            <div
                style={{
                    padding: '6px 10px',
                    display: 'flex',
                    alignItems: 'center',
                    gap: '8px',
                    cursor: 'pointer',
                    userSelect: 'none',
                }}
                onClick={() => setExpanded(v => !v)}
            >
                <Paperclip size={11} style={{ flexShrink: 0, color: 'var(--text-tertiary)' }} />
                <span
                    style={{
                        fontWeight: 600,
                        fontSize: '10px',
                        color: 'var(--text-primary)',
                        padding: '1px 6px',
                        borderRadius: '3px',
                        background: 'var(--bg-tertiary, rgba(0,0,0,0.06))',
                        flexShrink: 0,
                    }}
                >
                    Tool output · {parsed.sizeLabel}
                </span>
                <span
                    style={{
                        color: 'var(--text-tertiary)',
                        fontSize: '10px',
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                        flex: 1,
                        minWidth: 0,
                    }}
                    title={parsed.filePath}
                >
                    {basename}
                </span>
                <button
                    type="button"
                    onClick={(e) => {
                        e.stopPropagation();
                        setExpanded(v => !v);
                    }}
                    style={{
                        border: 'none',
                        background: 'transparent',
                        color: 'var(--text-tertiary)',
                        fontSize: '10px',
                        cursor: 'pointer',
                        padding: '2px 6px',
                        flexShrink: 0,
                    }}
                >
                    {expanded ? '收起' : '展开'}
                </button>
            </div>

            {/* Collapsed: one-line preview summary */}
            {!expanded && (
                <div
                    style={{
                        padding: '0 10px 6px 10px',
                        color: 'var(--text-tertiary)',
                        fontSize: '10px',
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                    }}
                >
                    {summaryLine}{summaryHasMore ? '…' : ''}
                </div>
            )}

            {/* Expanded */}
            {expanded && (
                <div style={{ borderTop: '1px solid var(--border-subtle)' }}>
                    {/* Preview block */}
                    <pre
                        style={{
                            margin: 0,
                            padding: '8px 10px',
                            fontFamily: 'monospace',
                            fontSize: '10px',
                            lineHeight: 1.5,
                            color: 'var(--text-secondary)',
                            whiteSpace: 'pre-wrap',
                            wordBreak: 'break-word',
                            maxHeight: '240px',
                            overflow: 'auto',
                        }}
                    >
                        {parsed.preview}
                    </pre>

                    {/* CTA row */}
                    <div
                        style={{
                            padding: '6px 10px',
                            borderTop: '1px dashed var(--border-subtle)',
                            display: 'flex',
                            alignItems: 'center',
                            gap: '8px',
                        }}
                    >
                        {fullContent === null && !error && (
                            <button
                                type="button"
                                onClick={loadFull}
                                disabled={loading}
                                className="btn btn-ghost"
                                style={{
                                    fontSize: '10px',
                                    padding: '3px 10px',
                                    opacity: loading ? 0.5 : 1,
                                    cursor: loading ? 'wait' : 'pointer',
                                }}
                            >
                                {loading ? '加载中...' : '查看完整输出 (从 workspace 读取)'}
                            </button>
                        )}
                        {error && (
                            <>
                                <span style={{ color: 'var(--accent-danger, #d44)', fontSize: '10px' }}>
                                    读取失败: {error}
                                </span>
                                <button
                                    type="button"
                                    onClick={loadFull}
                                    disabled={loading}
                                    className="btn btn-ghost"
                                    style={{
                                        fontSize: '10px',
                                        padding: '3px 10px',
                                        opacity: loading ? 0.5 : 1,
                                        cursor: loading ? 'wait' : 'pointer',
                                    }}
                                >
                                    {loading ? '重试中...' : '重试'}
                                </button>
                            </>
                        )}
                        {fullContent !== null && !error && (
                            <span style={{ color: 'var(--text-tertiary)', fontSize: '10px' }}>
                                完整输出 · {fullContent.length.toLocaleString()} chars
                            </span>
                        )}
                    </div>

                    {/* Full content */}
                    {fullContent !== null && (
                        <pre
                            style={{
                                margin: 0,
                                padding: '8px 10px',
                                borderTop: '1px solid var(--border-subtle)',
                                background: 'var(--bg-primary)',
                                fontFamily: 'monospace',
                                fontSize: '10px',
                                lineHeight: 1.5,
                                color: 'var(--text-primary)',
                                whiteSpace: 'pre-wrap',
                                wordBreak: 'break-word',
                                maxHeight: '480px',
                                overflow: 'auto',
                            }}
                        >
                            {fullContent}
                        </pre>
                    )}
                </div>
            )}
        </div>
    );
};

export default PersistedOutputCard;
