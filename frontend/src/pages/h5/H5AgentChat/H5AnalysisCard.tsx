import { IconChevronDown } from '@tabler/icons-react';
import type { H5AnalysisItem } from '../chatTimeline';
import { h5AnalysisTitle, h5ToolTitle, stringifyDetail } from './model';

export default function H5AnalysisCard({
    items,
    expanded,
    onToggle,
}: {
    items: H5AnalysisItem[];
    expanded: boolean;
    onToggle: () => void;
}) {
    const title = h5AnalysisTitle(items);
    const running = items.some((item) => item.type === 'tool' && item.status === 'running');

    return (
        <article className="h5-chat__message h5-chat__message--analysis">
            <div className={`h5-chat__analysis-card${expanded ? ' h5-chat__analysis-card--open' : ''}${running ? ' h5-chat__analysis-card--running' : ''}`}>
                <button type="button" className="h5-chat__analysis-header" onClick={onToggle}>
                    <span className="h5-chat__analysis-signal" aria-hidden="true"><span /><span /><span /></span>
                    <span className="h5-chat__analysis-title">{title}</span>
                    <IconChevronDown size={15} className="h5-chat__analysis-chevron" />
                </button>
                {expanded ? (
                    <div className="h5-chat__analysis-body">
                        {items.map((item, index) => {
                            if (item.type === 'thinking') {
                                const preview = item.content.length > 420
                                    ? `${item.content.slice(0, 420).trimEnd()}...`
                                    : item.content;
                                return (
                                    <div key={index} className="h5-chat__analysis-row">
                                        <span className="h5-chat__analysis-node">思</span>
                                        <div className="h5-chat__analysis-content">
                                            <div className="h5-chat__analysis-thinking">{preview}</div>
                                            {preview.length < item.content.length ? (
                                                <details className="h5-chat__analysis-detail">
                                                    <summary>展开全部</summary>
                                                    <pre>{item.content}</pre>
                                                </details>
                                            ) : null}
                                        </div>
                                    </div>
                                );
                            }

                            const args = stringifyDetail(item.args);
                            const result = stringifyDetail(item.result);
                            const hasDetail = !!args || !!result;
                            return (
                                <div key={index} className={`h5-chat__analysis-row${item.status === 'running' ? ' h5-chat__analysis-row--running' : ''}`}>
                                    <span className="h5-chat__analysis-node">{item.status === 'running' ? '...' : '✓'}</span>
                                    <div className="h5-chat__analysis-content">
                                        <div className="h5-chat__analysis-tool-title">{h5ToolTitle(item)}</div>
                                        <div className="h5-chat__analysis-tool-meta">
                                            {item.status === 'running' ? '执行中' : '已完成'} · {item.name}
                                        </div>
                                        {hasDetail ? (
                                            <details className="h5-chat__analysis-detail">
                                                <summary>查看详情</summary>
                                                {args ? <pre>{args}</pre> : null}
                                                {result ? <pre>{result}</pre> : null}
                                            </details>
                                        ) : null}
                                    </div>
                                </div>
                            );
                        })}
                    </div>
                ) : null}
            </div>
        </article>
    );
}
