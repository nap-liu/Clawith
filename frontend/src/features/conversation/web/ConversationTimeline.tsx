import React, { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useVirtualizer } from '@tanstack/react-virtual';
import {
    IconAlertTriangle,
    IconBrain,
    IconBrowser,
    IconChevronDown,
    IconClock,
    IconFileText,
    IconMessageCircle,
    IconSearch,
    IconTerminal2,
    IconTools,
} from '@tabler/icons-react';

import ChatAttachmentIcon from '../../../components/ChatAttachmentIcon';
import ChatMediaCard from '../../../components/ChatMediaCard';
import ChatToolCallRenderer from '../../../components/ChatToolCallRenderer';
import type { SubagentRunCardData } from '../../../components/SubagentRunCard';
import MarkdownRenderer from '../../../components/MarkdownRenderer';
import { copyToClipboard } from '../../../utils/clipboard';
import {
    buildPreviewImage,
    extractChatImageDataMarkers,
    splitAttachmentFileNames,
    stripChatImageDataMarkers,
    type ChatMessageAttachment,
    type ChatPreviewImage,
} from '../../../utils/chatAttachments';
import {
    buildConversationEntries,
    type ConversationAnalysisItem,
    type ConversationMessage,
} from '../core/chatTimeline';

export type ConversationMessageView = {
    isLeft: boolean;
    senderLabel?: string;
    avatarText?: string;
    forceSenderLabel?: boolean;
    hideAvatar?: boolean;
};

export type ConversationTimelineProps = {
    agentId: string;
    agentName: string;
    messages: ConversationMessage[];
    mode?: 'h5' | 'pc';
    viewOf: (message: ConversationMessage) => ConversationMessageView;
    isRunning?: boolean;
    unavailableAttachmentKeys?: ReadonlySet<string>;
    onAttachmentDownload?: (path: string, displayName: string) => void | Promise<void>;
    onAttachmentUnavailable?: (key: string) => void;
    onPreviewImages?: (images: ChatPreviewImage[], index: number) => void;
    onToolResolved?: (message: ConversationMessage, result: string) => void;
    onOpenSubagentSession?: (data: SubagentRunCardData) => void;
    scrollerRef?: React.RefObject<HTMLElement | null>;
    provenance?: {
        source?: string;
        status?: string;
        scheduled_at?: string;
        finished_at?: string;
        last_error?: string | null;
    } | null;
};

type AnalysisToolMeta = {
    title: string;
    label: string;
    target?: string;
    kind: 'command' | 'file' | 'search' | 'browser' | 'message' | 'agent' | 'mcp';
};

function firstString(...values: any[]): string | undefined {
    return values.find((value) => typeof value === 'string' && value.trim())?.trim();
}

function basename(path?: string): string {
    if (!path) return '';
    const clean = String(path).split('?')[0].replace(/\\/g, '/');
    return clean.split('/').filter(Boolean).pop() || clean;
}

function titleCaseToolName(name: string): string {
    return (name || 'tool')
        .replace(/^mcp[_:-]/i, '')
        .replace(/[_-]+/g, ' ')
        .replace(/\s+/g, ' ')
        .trim()
        .replace(/\b\w/g, (char) => char.toUpperCase());
}

function getToolMeta(item: Extract<ConversationAnalysisItem, { type: 'tool' }>): AnalysisToolMeta {
    const name = item.name || 'tool';
    const args = item.args && typeof item.args === 'object' && !Array.isArray(item.args) ? item.args : {};
    const path = firstString(args.output_path, args.path, args.file_path, args.filename, args.name);
    const url = firstString(args.url, args.link, args.uri);
    const query = firstString(args.query, args.q, args.keyword, args.search);
    const recipient = firstString(args.to, args.recipient, args.user, args.channel, args.agent_name);
    const lower = name.toLowerCase();
    if (lower.includes('write_file') || lower.includes('create_file')) return { title: path ? `Created ${basename(path)}` : 'Created a file', label: 'Workspace', target: path, kind: 'file' };
    if (lower.includes('edit_file') || lower.includes('update_file')) return { title: path ? `Updated ${basename(path)}` : 'Updated a file', label: 'Workspace', target: path, kind: 'file' };
    if (lower.includes('move_file')) return { title: path ? `Moved ${basename(path)}` : 'Moved a file', label: 'Workspace', target: path, kind: 'file' };
    if (lower.includes('delete_file')) return { title: path ? `Deleted ${basename(path)}` : 'Deleted a file', label: 'Workspace', target: path, kind: 'file' };
    if (lower.startsWith('convert_')) return { title: path ? `Converted ${basename(path)}` : titleCaseToolName(name), label: 'Workspace', target: path, kind: 'file' };
    if (lower.includes('read_webpage') || lower.includes('browser') || lower.includes('webpage')) return { title: url ? `Read ${url.replace(/^https?:\/\//, '').split('/')[0]}` : titleCaseToolName(name), label: 'Browser', target: url, kind: 'browser' };
    if (lower.includes('search')) return { title: query ? `Searched ${query}` : titleCaseToolName(name), label: 'Search', target: query, kind: 'search' };
    if (lower.includes('send_') || lower.includes('message')) return { title: recipient ? `Sent message to ${recipient}` : titleCaseToolName(name), label: 'Message', target: recipient, kind: 'message' };
    if (lower.includes('agent')) return { title: titleCaseToolName(name), label: 'Agent', target: recipient, kind: 'agent' };
    if (lower.includes('mcp') || lower.includes(':')) return { title: titleCaseToolName(name), label: 'MCP', target: path || url || query, kind: 'mcp' };
    return { title: titleCaseToolName(name), label: 'Tool', target: path || url || query || recipient, kind: 'command' };
}

function getToolIcon(kind: AnalysisToolMeta['kind']) {
    if (kind === 'file') return IconFileText;
    if (kind === 'search') return IconSearch;
    if (kind === 'browser') return IconBrowser;
    if (kind === 'message') return IconMessageCircle;
    if (kind === 'agent') return IconBrain;
    if (kind === 'mcp') return IconTools;
    return IconTerminal2;
}

function describeAnalysis(items: ConversationAnalysisItem[], t: (key: string, options?: any) => string) {
    const tools = items.filter((item): item is Extract<ConversationAnalysisItem, { type: 'tool' }> => item.type === 'tool');
    if (!tools.length) return t('agent.chat.thoughtProcess');
    const counts = { created: 0, updated: 0, deleted: 0, commands: 0, agents: 0 };
    for (const tool of tools) {
        const name = tool.name.toLowerCase();
        if (name.includes('write_file') || name.includes('create_file')) counts.created += 1;
        else if (name.includes('edit_file') || name.includes('update_file') || name.includes('move_file') || name.startsWith('convert_')) counts.updated += 1;
        else if (name.includes('delete_file')) counts.deleted += 1;
        else if (name === 'send_message_to_agent' || name === 'send_file_to_agent') counts.agents += 1;
        else counts.commands += 1;
    }
    const parts: string[] = [];
    if (counts.created) parts.push(t('agent.chat.createdFiles', { count: counts.created }));
    if (counts.updated) parts.push(t('agent.chat.updatedFiles', { count: counts.updated }));
    if (counts.deleted) parts.push(t('agent.chat.deletedFiles', { count: counts.deleted }));
    if (counts.commands) parts.push(t('agent.chat.ranCommands', { count: counts.commands }));
    if (counts.agents) parts.push(t('agent.chat.ranAgents', { count: counts.agents }));
    return parts.join(', ') || t('agent.chat.ranCommands', { count: tools.length });
}

function AnalysisCard({
    items,
    running,
    expanded,
    onToggle,
}: {
    items: ConversationAnalysisItem[];
    running: boolean;
    expanded: boolean;
    onToggle: () => void;
}) {
    const { t } = useTranslation();
    const runningTool = [...items].reverse().find((item) => item.type === 'tool' && item.status === 'running');
    const title = runningTool?.type === 'tool' ? getToolMeta(runningTool).title : describeAnalysis(items, t);
    return (
        <div className={`analysis-trace${expanded ? ' analysis-trace--open' : ''}${running ? ' analysis-trace--running' : ''}`}>
            <div className="analysis-trace-shell">
                <button className="analysis-trace-header" onClick={onToggle}>
                    <span className="analysis-trace-signal" aria-hidden="true"><span /><span /><span /></span>
                    <span className="analysis-trace-title">{title}</span>
                    <IconChevronDown className="analysis-trace-chevron" size={15} stroke={1.8} />
                </button>
                {expanded && (
                    <div className="analysis-trace-body">
                        {items.map((item, index) => {
                            const last = index === items.length - 1;
                            if (item.type === 'thinking') {
                                return (
                                    <div key={`${item.type}-${index}`} className="analysis-trace-row">
                                        <div className="analysis-trace-node-wrap"><div className="analysis-trace-node analysis-trace-node--thought"><IconClock size={18} stroke={1.65} /></div>{!last && <div className="analysis-trace-rail" />}</div>
                                        <div className="analysis-trace-row-content" style={{ paddingBottom: last ? 0 : 18, fontSize: 13, lineHeight: 1.5, whiteSpace: 'pre-wrap' }}>{item.content}</div>
                                    </div>
                                );
                            }
                            const meta = getToolMeta(item);
                            const ToolIcon = getToolIcon(meta.kind);
                            const args = item.args && Object.keys(item.args).length ? JSON.stringify(item.args, null, 2) : '';
                            return (
                                <div key={`${item.name}-${index}`} className={`analysis-trace-row${item.status === 'running' ? ' analysis-trace-row--running' : ''}`}>
                                    <div className="analysis-trace-node-wrap"><div className={`analysis-trace-node analysis-trace-node--tool analysis-tool-icon${item.status === 'running' ? ' analysis-tool-icon--running' : ''}`}><ToolIcon size={18} stroke={1.65} /></div>{!last && <div className="analysis-trace-rail" />}</div>
                                    <div className="analysis-trace-row-content" style={{ paddingBottom: last ? 0 : 18 }}>
                                        <div style={{ color: 'var(--text-secondary)', fontSize: 13 }}>{meta.title}{item.status === 'running' ? ` · ${t('common.loading')}` : ''}</div>
                                        <div style={{ display: 'flex', gap: 6, marginTop: 8, flexWrap: 'wrap' }}><span className="conversation-tool-chip">{meta.label}</span>{meta.target && <span className="conversation-tool-chip conversation-tool-chip--target">{meta.target}</span>}</div>
                                        {(args || item.result) && <details className="conversation-tool-details"><summary>{t('agent.chat.viewDetails')}</summary>{args && <pre>{args}</pre>}{item.result && <pre>{item.result}</pre>}</details>}
                                    </div>
                                </div>
                            );
                        })}
                        {running && <div className="analysis-trace-row"><div className="analysis-trace-node-wrap"><div className="analysis-trace-node analysis-trace-node--pending"><IconClock size={18} stroke={1.65} /></div></div><div style={{ color: 'var(--text-tertiary)', fontSize: 13 }}>{t('agent.chat.inProgress')}</div></div>}
                    </div>
                )}
            </div>
        </div>
    );
}

function CopyMessageButton({ text }: { text: string }) {
    const [copied, setCopied] = useState(false);
    return <button className="conversation-copy-button" title="Copy" onClick={() => void copyToClipboard(text).then((ok) => { if (!ok) return; setCopied(true); window.setTimeout(() => setCopied(false), 1500); })}>{copied ? '✓' : '⧉'}</button>;
}

function MessageItem({ agentId, msg, view, unavailable, onDownload, onUnavailable, onPreview }: {
    agentId: string;
    msg: ConversationMessage;
    view: ConversationMessageView;
    unavailable: ReadonlySet<string>;
    onDownload?: ConversationTimelineProps['onAttachmentDownload'];
    onUnavailable?: ConversationTimelineProps['onAttachmentUnavailable'];
    onPreview?: ConversationTimelineProps['onPreviewImages'];
}) {
    const { t, i18n } = useTranslation();
    const previews: ChatPreviewImage[] = msg.previewImages?.length ? msg.previewImages : (msg.imageUrl ? [buildPreviewImage(msg.imageUrl, msg.fileName)] : []);
    const inlinePreviews = previews.length ? [] : extractChatImageDataMarkers(msg.content || '');
    const content = stripChatImageDataMarkers(msg.display_content ?? msg.content ?? '');
    const attachments = Array.isArray(msg.attachments) ? msg.attachments : [];
    const media = attachments.filter((item) => item.kind === 'audio' || item.kind === 'video');
    const previewNames = new Set(previews.map((image) => image.filename).filter(Boolean));
    const files: Array<{ name: string; path?: string; kind?: ChatMessageAttachment['kind']; mimeType?: string }> = attachments.length
        ? attachments.filter((item) => !['image', 'audio', 'video'].includes(item.kind)).map((item) => ({ name: item.display_name, path: item.path, kind: item.kind, mimeType: item.mime_type }))
        : splitAttachmentFileNames(msg.fileName).filter((name) => !previewNames.has(name)).map((name) => ({ name }));
    const sender = msg.sender_name || view.senderLabel;
    const avatar = view.avatarText || sender?.[0] || (view.isLeft ? 'A' : 'U');
    const showSender = !!sender && (view.forceSenderLabel || !!msg.sender_name);
    const timestamp = msg.timestamp || msg.created_at || undefined;
    const formattedTime = timestamp ? new Date(timestamp).toLocaleString(i18n.language?.startsWith('zh') ? 'zh-CN' : 'en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '';
    const renderPreviews = (images: ChatPreviewImage[]) => images.map((image, index) => {
        const key = image.path || image.src;
        if (unavailable.has(key)) return <div key={`${key}-${index}`} className="chat-msg-file-chip"><IconAlertTriangle size={14} /><span>{image.filename || '图片'} · 当前不可访问</span></div>;
        return <button key={`${key}-${index}`} className="chat-msg-image-preview" onClick={() => onPreview?.(images, index)} title={t('common.preview', 'Preview')}><img src={image.src} alt={image.alt || image.filename || 'image'} loading="lazy" onError={() => onUnavailable?.(key)} /></button>;
    });
    return (
        <div className={`chat-msg-row${view.isLeft ? '' : ' chat-msg-row--user'}`}>
            <div className={`chat-msg-avatar${view.isLeft ? '' : ' chat-msg-avatar--user'}`} style={view.hideAvatar ? { visibility: 'hidden' } : undefined}>{avatar}</div>
            <div className="chat-msg-col">
                <div className="chat-msg-content-line">
                    <div className={`chat-msg-bubble${view.isLeft ? '' : ' chat-msg-bubble--user'}${msg._streaming && !msg.content && !msg.thinking ? ' chat-msg-bubble--thinking' : ''}`}>
                        {showSender && <div className="chat-msg-sender">{sender}</div>}
                        {(previews.length > 0 || inlinePreviews.length > 0) && <div className="conversation-image-list">{renderPreviews(previews.length ? previews : inlinePreviews)}</div>}
                        {media.length > 0 && <div className="conversation-media-list">{media.map((attachment, index) => <ChatMediaCard key={`${attachment.path}-${index}`} agentId={agentId} messageId={msg.id} attachment={attachment} onDownload={() => void onDownload?.(attachment.path, attachment.display_name)} onUnavailable={() => onUnavailable?.(attachment.path)} />)}</div>}
                        {files.length > 0 && <div className="conversation-file-list">{files.map((file, index) => <button key={`${file.path || file.name}-${index}`} className="chat-msg-file-chip" disabled={!file.path || unavailable.has(file.path)} onClick={() => file.path && void onDownload?.(file.path, file.name)}><ChatAttachmentIcon name={file.name} kind={file.kind as ChatMessageAttachment['kind']} mimeType={file.mimeType} size={16} /><span>{file.name}</span></button>)}</div>}
                        {msg._streaming && !msg.content && !msg.thinking ? <div className="thinking-indicator"><div className="thinking-dots"><span /><span /><span /></div><span>{t('agent.chat.thinking', 'Thinking...')}</span></div> : <MarkdownRenderer content={content} />}
                    </div>
                </div>
                {formattedTime && <div className="chat-msg-timestamp">{formattedTime}{content && <CopyMessageButton text={content} />}</div>}
            </div>
        </div>
    );
}

export default function ConversationTimeline({
    agentId,
    agentName,
    messages,
    mode = 'pc',
    viewOf,
    isRunning = false,
    unavailableAttachmentKeys = new Set<string>(),
    onAttachmentDownload,
    onAttachmentUnavailable,
    onPreviewImages,
    onToolResolved,
    onOpenSubagentSession,
    scrollerRef,
    provenance,
}: ConversationTimelineProps) {
    const { t, i18n } = useTranslation();
    const [expandedAnalysis, setExpandedAnalysis] = useState<Record<string, boolean>>({});
    const entries = useMemo(() => buildConversationEntries(messages), [messages]);
    const provenanceTime = provenance?.finished_at || provenance?.scheduled_at;
    const status = provenance?.status || '';
    const statusText = status === 'completed'
        ? (i18n.language?.startsWith('zh') ? '已完成' : 'Completed')
        : status === 'failed'
            ? (i18n.language?.startsWith('zh') ? '失败' : 'Failed')
            : status === 'processing'
                ? (i18n.language?.startsWith('zh') ? '执行中' : 'Running')
                : status === 'pending'
                    ? (i18n.language?.startsWith('zh') ? '等待中' : 'Pending')
                    : status;
    const analysisOwners = new Map<string, Extract<(typeof entries)[number], { type: 'message' }>>();
    let nextAssistant: Extract<(typeof entries)[number], { type: 'message' }> | undefined;
    for (let index = entries.length - 1; index >= 0; index -= 1) {
        const entry = entries[index];
        if (entry.type === 'message' && entry.msg.role === 'assistant') nextAssistant = entry;
        else if (entry.type === 'analysis_group' && nextAssistant) analysisOwners.set(entry.key, nextAssistant);
    }
    const virtualizeEntries = Boolean(scrollerRef && entries.length > 40);
    const rowVirtualizer = useVirtualizer({
        count: virtualizeEntries ? entries.length : 0,
        getScrollElement: () => scrollerRef?.current ?? null,
        estimateSize: (index) => {
            const entry = entries[index];
            if (!entry) return 88;
            if (entry.type === 'analysis_group') return 70;
            if (entry.type === 'special_render') return 140;
            return entry.msg.content?.length > 600 ? 180 : 88;
        },
        getItemKey: (index) => entries[index]?.key ?? `conversation-entry-${index}`,
        overscan: 8,
        enabled: virtualizeEntries,
    });
    const renderEntry = (entry: (typeof entries)[number], index: number) => {
        if (entry.type === 'analysis_group') {
            const owner = analysisOwners.get(entry.key);
            const ownerView = owner?.type === 'message' ? viewOf(owner.msg) : { isLeft: true, avatarText: agentName[0] };
            const running = entry.running || (isRunning && index === entries.length - 1);
            return <div className={`chat-msg-row chat-msg-row--analysis${ownerView.isLeft ? '' : ' chat-msg-row--user'}`}><div className="chat-msg-avatar">{ownerView.avatarText || agentName[0] || 'A'}</div><AnalysisCard items={entry.items} running={running} expanded={!!expandedAnalysis[entry.key]} onToggle={() => setExpandedAnalysis((current) => ({ ...current, [entry.key]: !current[entry.key] }))} /></div>;
        }
        if (entry.type === 'special_render') {
            return <div className={`chat-msg-row chat-msg-row--special-render chat-msg-row--${entry.renderType}`}><div className="chat-msg-avatar">{agentName[0] || 'A'}</div><ChatToolCallRenderer agentId={agentId} message={entry.msg} t={t} mode={mode} onPreviewImages={onPreviewImages} onOpenSubagentSession={onOpenSubagentSession} onResolved={(result) => onToolResolved?.(entry.msg, result)} /></div>;
        }
        const previous = entries[index - 1];
        const view = viewOf(entry.msg);
        return <MessageItem agentId={agentId} msg={entry.msg} view={{ ...view, hideAvatar: view.hideAvatar || (entry.msg.role === 'assistant' && previous?.type === 'analysis_group') }} unavailable={unavailableAttachmentKeys} onDownload={onAttachmentDownload} onUnavailable={onAttachmentUnavailable} onPreview={onPreviewImages} />;
    };
    return <div className="conversation-timeline">
        {provenance && (
            <div className={`conversation-provenance conversation-provenance--${status || 'unknown'}`}>
                <span className="conversation-provenance-source">{provenance.source || 'trigger'}</span>
                {statusText && <span className="conversation-provenance-status">{statusText}</span>}
                {provenanceTime && <span className="conversation-provenance-time">{new Date(provenanceTime).toLocaleString()}</span>}
                {provenance.last_error && <span className="conversation-provenance-error">{provenance.last_error}</span>}
            </div>
        )}
        {virtualizeEntries ? (
            <div className="conversation-timeline__virtual-space" style={{ height: `${rowVirtualizer.getTotalSize()}px` }}>
                {rowVirtualizer.getVirtualItems().map((virtualItem) => {
                    const entry = entries[virtualItem.index];
                    if (!entry) return null;
                    return (
                        <div
                            key={virtualItem.key}
                            ref={rowVirtualizer.measureElement}
                            data-index={virtualItem.index}
                            data-conversation-entry-key={entry.key}
                            className="conversation-timeline__virtual-row"
                            style={{ transform: `translateY(${virtualItem.start}px)` }}
                        >
                            {renderEntry(entry, virtualItem.index)}
                        </div>
                    );
                })}
            </div>
        ) : entries.map((entry, index) => (
            <div key={entry.key} data-conversation-entry-key={entry.key}>
                {renderEntry(entry, index)}
            </div>
        ))}
    </div>;
}
