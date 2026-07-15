import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useParams, useSearchParams } from 'react-router-dom';
import { useVirtualizer } from '@tanstack/react-virtual';
import {
    IconAlertTriangle,
    IconArrowLeft,
    IconChevronDown,
    IconHistory,
    IconLoader2,
    IconMicrophone,
    IconPaperclip,
    IconPlayerStopFilled,
    IconPlus,
    IconRefresh,
    IconSend,
    IconX,
} from '@tabler/icons-react';
import ChatImageLightbox from '../../components/ChatImageLightbox';
import ChatFileDeliveryCard from '../../components/ChatFileDeliveryCard';
import ConfirmationCard from '../../components/ConfirmationCard';
import MarkdownRenderer from '../../components/MarkdownRenderer';
import { useToast } from '../../components/Toast/ToastProvider';
import { useAuthStore } from '../../stores';
import { agentApi, authApi, chatSessionApi, enterpriseApi, tenantApi, uploadFileWithProgress } from '../../services/api';
import type { Agent, TokenResponse } from '../../types';
import {
    buildChatAttachmentPayload,
    buildPreviewImage,
    buildPreviewImagesFromAttachments,
    extractChatImageDataMarkers,
    isPreviewableImageName,
    modelSupportsVision,
    resolveEffectiveChatModelId,
    splitAttachmentFileNames,
    stripChatImageDataMarkers,
    type ChatAttachedFile,
    type ChatPreviewImage,
    type ChatModelOption,
} from '../../utils/chatAttachments';
import {
    applyAssistantStreamMessage,
    buildH5ConversationEntries,
    getH5ScrollAnchor,
    isConfirmationToolCall,
    mapHistoryMessage,
    mergeHistoryMessages,
    toolCallMessageFromEvent,
    upsertToolCallMessage,
    type H5AnalysisItem,
    type H5ChatMessage,
} from './chatTimeline';
import { parseH5Theme } from './h5Params';
import { parseChatSessionId, writeChatSessionIdToHref } from '../../utils/chatUrlParams';
import { copyToClipboard } from '../../utils/clipboard';
import { isWechatMiniProgramWebView, resolveExternalHttpLink } from '../../utils/h5LinkPolicy';
import { insertSpeechTranscript, useSpeechInput } from '../../hooks/useSpeechInput';
import './H5AgentChat.css';

type AuthStatus = 'checking' | 'exchanging' | 'ready' | 'error';
type ConnectionStatus = 'idle' | 'connecting' | 'connected' | 'disconnected';
type H5UploadDraft = { id: string; name: string; percent: number; previewUrl?: string; sizeBytes: number };
type H5SessionSummary = {
    id: string;
    title: string;
    source_channel: string;
    created_at: string;
    last_message_at?: string | null;
    message_count: number;
    unread_count: number;
    is_primary: boolean;
};

const VIRTUALIZE_ENTRY_THRESHOLD = 40;

const OAUTH_TRANSIENT_PARAMS = [
    'code',
    'state',
    'error',
    'error_description',
    'scope',
    'authuser',
    'prompt',
    'session_state',
];

type CodeExchangePayload = Parameters<typeof authApi.exchangeCode>[0];

const codeExchangePromises = new Map<string, Promise<TokenResponse>>();
const consumedExchangeCodes = new Set<string>();

const makeId = () => {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
        return crypto.randomUUID();
    }
    return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
};

export function buildCodeExchangeRedirectUri(href: string) {
    const url = new URL(href);
    for (const key of OAUTH_TRANSIENT_PARAMS) url.searchParams.delete(key);
    return url.toString();
}

function cleanedOAuthAddress(href: string) {
    const url = new URL(href);
    for (const key of OAUTH_TRANSIENT_PARAMS) url.searchParams.delete(key);
    const search = url.searchParams.toString();
    return `${url.pathname}${search ? `?${search}` : ''}${url.hash}`;
}

function exchangeCodeOnce(key: string, payload: CodeExchangePayload) {
    const existing = codeExchangePromises.get(key);
    if (existing) return existing;
    const promise = authApi.exchangeCode(payload).finally(() => {
        codeExchangePromises.delete(key);
    });
    codeExchangePromises.set(key, promise);
    return promise;
}

function titleCaseToolName(name: string) {
    return (name || 'tool')
        .replace(/^mcp[_:-]/i, '')
        .replace(/[_-]+/g, ' ')
        .replace(/\s+/g, ' ')
        .trim()
        .replace(/\b\w/g, ch => ch.toUpperCase());
}

function firstString(...values: any[]) {
    for (const value of values) {
        if (typeof value === 'string' && value.trim()) return value.trim();
    }
    return '';
}

function h5ToolTitle(item: Extract<H5AnalysisItem, { type: 'tool' }>) {
    const args = item.args && typeof item.args === 'object' && !Array.isArray(item.args) ? item.args : {};
    const path = firstString(args.output_path, args.path, args.file_path, args.filename, args.name);
    const url = firstString(args.url, args.link, args.uri);
    const query = firstString(args.query, args.q, args.keyword, args.search);
    const recipient = firstString(args.to, args.recipient, args.user, args.channel, args.agent_name);
    const target = path || url || query || recipient;
    const base = titleCaseToolName(item.name);
    return target ? `${base}: ${target}` : base;
}

function h5AnalysisTitle(items: H5AnalysisItem[]) {
    const toolItems = items.filter((item) => item.type === 'tool') as Extract<H5AnalysisItem, { type: 'tool' }>[];
    const runningTool = [...toolItems].reverse().find((item) => item.status === 'running');
    if (runningTool) return `正在使用：${h5ToolTitle(runningTool)}`;
    if (toolItems.length > 0) return `已执行 ${toolItems.length} 个工具`;
    return '思考过程';
}

function stringifyDetail(value: any) {
    if (value == null || value === '') return '';
    if (typeof value === 'string') return value;
    try {
        return JSON.stringify(value, null, 2);
    } catch {
        return String(value);
    }
}

function h5T(key: string, opts?: any) {
    if (key === 'agent.chat.confirmCardTitle') return '需要确认';
    if (key === 'agent.chat.confirmWillRun') return '将执行';
    if (key === 'agent.chat.confirmResolved') return '已处理';
    if (key === 'common.loading') return '处理中';
    if (typeof opts === 'string') return opts;
    if (opts?.defaultValue) return opts.defaultValue;
    return key;
}

function formatFileSize(bytes: number) {
    if (!Number.isFinite(bytes) || bytes <= 0) return '';
    if (bytes < 1024) return `${bytes}B`;
    if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)}KB`;
    return `${(bytes / 1024 / 1024).toFixed(1)}MB`;
}

function stripAttachmentDisplayPrefix(content: string) {
    return (content || '').replace(/^(?:\[Attachment: [^\]]+\]\s*)+/, '').trim();
}

function buildAgentFileImageUrl(agentId: string, token: string | null | undefined, fileName: string) {
    return `/api/agents/${agentId}/files/download?path=workspace/uploads/${encodeURIComponent(fileName)}${token ? `&token=${encodeURIComponent(token)}` : ''}`;
}

function resolveAgentAvatarUrl(avatarUrl: string | null | undefined, token: string | null | undefined) {
    if (!avatarUrl) return '';
    if (!avatarUrl.startsWith('/api') || !token) return avatarUrl;
    return `${avatarUrl}${avatarUrl.includes('?') ? '&' : '?'}token=${encodeURIComponent(token)}`;
}

function normalizeH5SessionSummary(row: any): H5SessionSummary | null {
    if (!row || row.id == null) return null;
    return {
        id: String(row.id),
        title: String(row.title || row.group_name || '未命名会话'),
        source_channel: String(row.source_channel || 'web'),
        created_at: String(row.created_at || ''),
        last_message_at: row.last_message_at ? String(row.last_message_at) : null,
        message_count: Number(row.message_count || 0),
        unread_count: Number(row.unread_count || 0),
        is_primary: Boolean(row.is_primary),
    };
}

function h5SessionChannelLabel(channel: string) {
    const labels: Record<string, string> = {
        web: 'Web',
        wechat_miniprogram: '微信小程序',
        feishu: '飞书',
        dingtalk: '钉钉',
        wecom: '企业微信',
        slack: 'Slack',
        discord: 'Discord',
    };
    return labels[channel] || channel || '未知渠道';
}

function formatH5SessionTime(value?: string | null) {
    if (!value) return '';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '';
    const locale = typeof navigator !== 'undefined' ? navigator.language : 'zh-CN';
    return new Intl.DateTimeFormat(locale, {
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
    }).format(date);
}

function H5AnalysisCard({
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

function estimateConversationEntrySize(entry: ReturnType<typeof buildH5ConversationEntries>[number], expanded: boolean) {
    if (entry.type === 'analysis_group') {
        return expanded ? Math.min(360, 52 + entry.items.length * 74) : 52;
    }
    if (entry.type === 'file_delivery') return entry.delivery.message ? 142 : 86;
    const msg = entry.msg;
    if (msg.role === 'tool_call') return 190;
    const text = `${msg.content || ''}${msg.thinking || ''}`;
    return Math.min(360, Math.max(54, 38 + Math.ceil(text.length / 18) * 24));
}

export default function H5AgentChat() {
    const { agentId } = useParams<{ agentId: string }>();
    const [searchParams] = useSearchParams();
    const toast = useToast();
    const searchString = searchParams.toString();
    const channel = useMemo(() => {
        const params = new URLSearchParams(searchString);
        return params.get('channel') || 'wechat_miniprogram';
    }, [searchString]);
    const provider = useMemo(() => new URLSearchParams(searchString).get('provider') || '', [searchString]);
    const code = useMemo(() => new URLSearchParams(searchString).get('code') || '', [searchString]);
    const oauthState = useMemo(() => new URLSearchParams(searchString).get('state'), [searchString]);
    const theme = useMemo(() => parseH5Theme(new URLSearchParams(searchString).get('theme')), [searchString]);
    const initialSessionId = useMemo(() => parseChatSessionId(new URLSearchParams(searchString).get('session_id')), [searchString]);
    const isWechatMiniProgram = useMemo(() => isWechatMiniProgramWebView(), []);

    const token = useAuthStore((s) => s.token);
    const setAuth = useAuthStore((s) => s.setAuth);

    const [authStatus, setAuthStatus] = useState<AuthStatus>('checking');
    const [authError, setAuthError] = useState('');
    const [agent, setAgent] = useState<Agent | null>(null);
    const [agentError, setAgentError] = useState('');
    const [messages, setMessages] = useState<H5ChatMessage[]>([]);
    const [input, setInput] = useState('');
    const [sessionId, setSessionId] = useState<string | null>(initialSessionId);
    const [sessionsPanelOpen, setSessionsPanelOpen] = useState(false);
    const [sessions, setSessions] = useState<H5SessionSummary[]>([]);
    const [sessionsLoading, setSessionsLoading] = useState(false);
    const [sessionsError, setSessionsError] = useState('');
    const [llmModels, setLlmModels] = useState<ChatModelOption[]>([]);
    const [tenantDefaultModelId, setTenantDefaultModelId] = useState<string | null>(null);
    const [connectionStatus, setConnectionStatus] = useState<ConnectionStatus>('idle');
    const [isWaiting, setIsWaiting] = useState(false);
    const [isStreaming, setIsStreaming] = useState(false);
    const [isStopping, setIsStopping] = useState(false);
    const [isStartingNew, setIsStartingNew] = useState(false);
    const [isSwitchingSession, setIsSwitchingSession] = useState(false);
    const [analysisExpanded, setAnalysisExpanded] = useState<Record<string, boolean>>({});
    const [uploadDrafts, setUploadDrafts] = useState<H5UploadDraft[]>([]);
    const [attachedFiles, setAttachedFiles] = useState<ChatAttachedFile[]>([]);
    const [uploadError, setUploadError] = useState('');
    const [imagePreview, setImagePreview] = useState<{ images: ChatPreviewImage[]; index: number } | null>(null);

    const wsRef = useRef<WebSocket | null>(null);
    const sessionIdRef = useRef<string | null>(initialSessionId);
    const reconnectTimerRef = useRef<number | null>(null);
    const reconnectAttemptRef = useRef(0);
    const manualCloseRef = useRef(false);
    const unmountedRef = useRef(false);
    const messagesEndRef = useRef<HTMLDivElement | null>(null);
    const messagesScrollerRef = useRef<HTMLElement | null>(null);
    const textareaRef = useRef<HTMLTextAreaElement | null>(null);
    const fileInputRef = useRef<HTMLInputElement | null>(null);
    const uploadAbortRef = useRef<Map<string, () => void>>(new Map());
    const skipNextConnectedHistoryRef = useRef<string | null>(null);
    const speechInputSnapshotRef = useRef({ value: '', selectionStart: 0, selectionEnd: 0 });
    const speechSelectionCapturedRef = useRef(false);
    const inputSelectionRef = useRef({ start: 0, end: 0, hasPosition: false });

    const speechTextAtCursor = useCallback((text: string) => {
        const snapshot = speechInputSnapshotRef.current;
        return insertSpeechTranscript(snapshot.value, text, snapshot.selectionStart, snapshot.selectionEnd);
    }, []);

    const restoreInputCaret = useCallback((position: number) => {
        inputSelectionRef.current = { start: position, end: position, hasPosition: true };
        window.requestAnimationFrame(() => {
            window.requestAnimationFrame(() => {
                textareaRef.current?.setSelectionRange(position, position);
            });
        });
    }, []);

    const handleSpeechInterim = useCallback((text: string) => {
        setInput(speechTextAtCursor(text).value);
    }, [speechTextAtCursor]);
    const handleSpeechFinal = useCallback((text: string) => {
        const next = speechTextAtCursor(text);
        setInput(next.value);
        restoreInputCaret(next.caret);
    }, [restoreInputCaret, speechTextAtCursor]);
    const handleSpeechCancel = useCallback(() => {
        const snapshot = speechInputSnapshotRef.current;
        setInput(snapshot.value);
        restoreInputCaret(snapshot.selectionStart);
    }, [restoreInputCaret]);
    const speech = useSpeechInput({
        onInterim: handleSpeechInterim,
        onFinal: handleSpeechFinal,
        onCancel: handleSpeechCancel,
    });
    const startSpeech = speech.start;

    const captureSpeechInsertionPoint = useCallback(() => {
        const selection = inputSelectionRef.current;
        const field = textareaRef.current;
        const selectionStart = selection.hasPosition && field ? field.selectionStart : input.length;
        const selectionEnd = selection.hasPosition && field ? field.selectionEnd : input.length;
        speechInputSnapshotRef.current = { value: input, selectionStart, selectionEnd };
        speechSelectionCapturedRef.current = true;
    }, [input]);

    const startSpeechInput = useCallback(() => {
        if (!speechSelectionCapturedRef.current) captureSpeechInsertionPoint();
        speechSelectionCapturedRef.current = false;
        void startSpeech();
    }, [captureSpeechInsertionPoint, startSpeech]);

    const handleInputSelect = (event: React.SyntheticEvent<HTMLTextAreaElement>) => {
        const field = event.currentTarget;
        inputSelectionRef.current = {
            start: field.selectionStart,
            end: field.selectionEnd,
            hasPosition: true,
        };
    };

    useEffect(() => {
        document.body.classList.add('h5-chat-active');
        return () => document.body.classList.remove('h5-chat-active');
    }, []);

    useEffect(() => {
        const previousTheme = document.documentElement.getAttribute('data-theme');
        document.documentElement.setAttribute('data-theme', theme);
        document.body.dataset.h5Theme = theme;
        return () => {
            if (previousTheme) {
                document.documentElement.setAttribute('data-theme', previousTheme);
            } else {
                document.documentElement.removeAttribute('data-theme');
            }
            delete document.body.dataset.h5Theme;
        };
    }, [theme]);

    useEffect(() => {
        const textarea = textareaRef.current;
        if (!textarea) return;
        textarea.style.height = '0px';
        textarea.style.height = `${Math.min(textarea.scrollHeight, 132)}px`;
    }, [input]);

    useEffect(() => {
        unmountedRef.current = false;
        return () => {
            unmountedRef.current = true;
            if (reconnectTimerRef.current) window.clearTimeout(reconnectTimerRef.current);
            uploadAbortRef.current.forEach((abort) => abort());
            uploadAbortRef.current.clear();
            manualCloseRef.current = true;
            wsRef.current?.close();
        };
    }, []);

    useEffect(() => {
        if (!initialSessionId || sessionIdRef.current) return;
        sessionIdRef.current = initialSessionId;
        setSessionId(initialSessionId);
    }, [initialSessionId]);

    useEffect(() => {
        if (!agentId) {
            setAuthStatus('error');
            setAuthError('缺少 Agent 参数');
            return;
        }

        const existingToken = useAuthStore.getState().token;
        if (code) {
            if (!provider) {
                setAuthStatus('error');
                setAuthError('缺少 OAuth provider 参数');
                return;
            }

            const lockKey = `${provider}:${code}`;
            if (consumedExchangeCodes.has(lockKey)) {
                setAuthStatus(existingToken ? 'ready' : 'error');
                if (!existingToken) setAuthError('登录凭证已失效');
                return;
            }
            setAuthStatus('exchanging');
            setAuthError('');

            let active = true;
            exchangeCodeOnce(lockKey, {
                provider,
                code,
                state: oauthState,
                redirect_uri: buildCodeExchangeRedirectUri(window.location.href),
                purpose: 'h5_agent_chat',
                channel,
                context: { agent_id: agentId },
            }).then((res) => {
                if (!active) return;
                setAuth(res.user, res.access_token);
                consumedExchangeCodes.add(lockKey);
                setAuthStatus('ready');
                window.history.replaceState({}, '', cleanedOAuthAddress(window.location.href));
            }).catch((error: any) => {
                if (!active) return;
                setAuthStatus('error');
                setAuthError(error?.message || '登录失败');
            });

            return () => { active = false; };
        }

        if (existingToken) {
            setAuthStatus('ready');
            setAuthError('');
        } else {
            setAuthStatus('error');
            setAuthError('缺少登录凭证');
        }
    }, [agentId, channel, code, oauthState, provider, setAuth, token]);

    useEffect(() => {
        if (authStatus !== 'ready' || !agentId || !token) return;
        let cancelled = false;
        setAgentError('');
        agentApi.get(agentId)
            .then((data) => {
                if (!cancelled) setAgent(data);
            })
            .catch((error: any) => {
                if (!cancelled) setAgentError(error?.message || '无法加载 Agent');
            });
        return () => { cancelled = true; };
    }, [agentId, authStatus, token]);

    useEffect(() => {
        if (authStatus !== 'ready' || !token) return;
        let cancelled = false;

        enterpriseApi.llmModels()
            .then((models) => {
                if (!cancelled) setLlmModels(models || []);
            })
            .catch(() => {
                if (!cancelled) setLlmModels([]);
            });

        tenantApi.me()
            .then((tenant) => {
                if (!cancelled) setTenantDefaultModelId(tenant?.default_model_id || null);
            })
            .catch(() => {
                if (!cancelled) setTenantDefaultModelId(null);
            });

        return () => { cancelled = true; };
    }, [authStatus, token]);

    const normalizeHistoryMessage = useCallback((row: any): H5ChatMessage | null => {
        const msg = mapHistoryMessage(row, makeId);
        if (!msg || msg.role !== 'user' || !agentId) return msg;
        const fileMatch = msg.content.match(/^\[file:([^\]]+)\]\n?/);
        if (!fileMatch) {
            const markerImages = extractChatImageDataMarkers(msg.content);
            if (markerImages.length === 0) return msg;
            const next: H5ChatMessage = {
                ...msg,
                content: stripChatImageDataMarkers(msg.content),
                previewImages: markerImages,
            };
            if (markerImages.length === 1) next.imageUrl = markerImages[0].src;
            return next;
        }
        const fileName = fileMatch[1];
        const contentWithMarkers = stripAttachmentDisplayPrefix(msg.content.slice(fileMatch[0].length).trim());
        const markerImages = extractChatImageDataMarkers(contentWithMarkers);
        const content = stripChatImageDataMarkers(contentWithMarkers);
        const next: H5ChatMessage = { ...msg, content, fileName };
        const previewImages = splitAttachmentFileNames(fileName)
            .filter(isPreviewableImageName)
            .map((name) => buildPreviewImage(buildAgentFileImageUrl(agentId, token, name), name));
        const images = previewImages.length > 0 ? previewImages : markerImages;
        if (images.length > 0) {
            next.previewImages = images;
            if (images.length === 1) next.imageUrl = images[0].src;
        }
        return next;
    }, [agentId, token]);

    const loadHistory = useCallback(async (nextSessionId: string) => {
        if (!agentId || !token) return false;
        const pages: any[][] = [];
        let fullyLoaded = true;
        try {
            let before = '';
            while (sessionIdRef.current === nextSessionId) {
                const params = new URLSearchParams({ limit: '500' });
                if (before) params.set('before', before);
                const response = await fetch(`/api/agents/${agentId}/sessions/${nextSessionId}/messages?${params}`, {
                    headers: { Authorization: `Bearer ${token}` },
                });
                if (!response.ok) throw new Error(`HTTP ${response.status}`);
                const rows = await response.json();
                if (!Array.isArray(rows) || rows.length === 0) break;
                pages.push(rows);
                const nextBefore = rows[0]?.created_at || '';
                if (!nextBefore || nextBefore === before) break;
                before = nextBefore;
            }
        } catch {
            fullyLoaded = false;
        }
        if (sessionIdRef.current !== nextSessionId) return false;
        const normalized = pages.reverse().flat().map(normalizeHistoryMessage).filter(Boolean) as H5ChatMessage[];
        const seenToolCalls = new Set<string>();
        const history = normalized.reverse().filter((msg) => (
            !msg.toolCallId || (!seenToolCalls.has(msg.toolCallId) && !!seenToolCalls.add(msg.toolCallId))
        )).reverse();
        setMessages((prev) => mergeHistoryMessages(prev, history));
        return fullyLoaded;
    }, [agentId, normalizeHistoryMessage, token]);

    const loadSessions = useCallback(async () => {
        if (!agentId) return;
        setSessionsLoading(true);
        setSessionsError('');
        try {
            const rows = await chatSessionApi.list(agentId, {
                scope: 'mine',
                limit: 50,
                offset: 0,
            });
            const next = rows.map(normalizeH5SessionSummary).filter(Boolean) as H5SessionSummary[];
            setSessions(next);
        } catch (error: any) {
            setSessionsError(error?.message || '无法加载历史会话');
        } finally {
            setSessionsLoading(false);
        }
    }, [agentId]);

    const scheduleReconnect = useCallback(() => {
        if (manualCloseRef.current || unmountedRef.current || !token || !agentId) return;
        if (reconnectTimerRef.current) window.clearTimeout(reconnectTimerRef.current);
        const attempt = reconnectAttemptRef.current;
        reconnectAttemptRef.current = attempt + 1;
        const delay = Math.min(12000, 900 * 2 ** attempt);
        reconnectTimerRef.current = window.setTimeout(() => {
            reconnectTimerRef.current = null;
            openSocketRef.current(sessionIdRef.current);
        }, delay);
    }, [agentId, token]);

    const handleSocketMessage = useCallback((data: any) => {
        if (data.type === 'connected' && data.session_id) {
            const nextSessionId = String(data.session_id);
            sessionIdRef.current = nextSessionId;
            setSessionId(nextSessionId);
            window.history.replaceState({}, '', writeChatSessionIdToHref(window.location.href, nextSessionId));
            if (skipNextConnectedHistoryRef.current === nextSessionId) {
                skipNextConnectedHistoryRef.current = null;
            } else {
                loadHistory(nextSessionId);
            }
            return;
        }

        if (data.type === 'thinking') {
            setIsWaiting(false);
            setIsStreaming(true);
            setMessages((prev) => applyAssistantStreamMessage(prev, {
                type: 'thinking',
                content: data.content || '',
            }, makeId));
            return;
        }

        if (data.type === 'chunk') {
            setIsWaiting(false);
            setIsStreaming(true);
            setMessages((prev) => applyAssistantStreamMessage(prev, {
                type: 'chunk',
                content: data.content || '',
            }, makeId));
            return;
        }

        if (data.type === 'done') {
            setIsWaiting(false);
            setIsStreaming(false);
            setIsStopping(false);
            setMessages((prev) => applyAssistantStreamMessage(prev, {
                type: 'done',
                content: data.content || '',
                now: new Date().toISOString(),
            }, makeId));
            return;
        }

        if (data.type === 'tool_call') {
            setIsWaiting(false);
            setIsStreaming(true);
            const toolMsg = toolCallMessageFromEvent(data, makeId, new Date().toISOString());
            setMessages((prev) => upsertToolCallMessage(prev, toolMsg));
            return;
        }

        if (data.type === 'error' || data.type === 'quota_exceeded') {
            setIsWaiting(false);
            setIsStreaming(false);
            setIsStopping(false);
            setMessages((prev) => [...prev, {
                id: makeId(),
                role: 'system',
                content: data.content || '消息发送失败',
                created_at: new Date().toISOString(),
            }]);
        }
    }, [loadHistory]);

    const openSocket = useCallback((requestedSessionId?: string | null) => {
        if (!agentId || !token) return;

        if (wsRef.current && wsRef.current.readyState !== WebSocket.CLOSED) {
            manualCloseRef.current = true;
            wsRef.current.close();
            manualCloseRef.current = false;
        }

        setConnectionStatus('connecting');
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const params = new URLSearchParams({
            token,
            lang: navigator.language.toLowerCase().startsWith('zh') ? 'zh' : 'en',
            channel,
        });
        const effectiveSessionId = requestedSessionId || sessionIdRef.current;
        if (effectiveSessionId) params.set('session_id', effectiveSessionId);

        const ws = new WebSocket(`${protocol}//${window.location.host}/ws/chat/${agentId}?${params.toString()}`);
        wsRef.current = ws;

        ws.onopen = () => {
            reconnectAttemptRef.current = 0;
            setConnectionStatus('connected');
        };

        ws.onmessage = (event) => {
            try {
                handleSocketMessage(JSON.parse(event.data));
            } catch {
                // Ignore malformed server frames.
            }
        };

        ws.onerror = () => {
            setConnectionStatus('disconnected');
        };

        ws.onclose = () => {
            if (wsRef.current !== ws) return;
            wsRef.current = null;
            setConnectionStatus('disconnected');
            setIsWaiting(false);
            setIsStreaming(false);
            setIsStopping(false);
            scheduleReconnect();
        };
    }, [agentId, channel, handleSocketMessage, scheduleReconnect, token]);

    const openSocketRef = useRef(openSocket);
    useEffect(() => {
        openSocketRef.current = openSocket;
    }, [openSocket]);

    useEffect(() => {
        if (authStatus !== 'ready' || !agent || !token) return;
        openSocket(initialSessionId);
        return () => {
            manualCloseRef.current = true;
            wsRef.current?.close();
            manualCloseRef.current = false;
        };
    }, [agent, authStatus, initialSessionId, openSocket, token]);

    const clearUploadDrafts = useCallback((abortUploads = false) => {
        if (abortUploads) {
            uploadAbortRef.current.forEach((abort) => abort());
            uploadAbortRef.current.clear();
        }
        setUploadDrafts((prev) => {
            prev.forEach((draft) => {
                if (draft.previewUrl) URL.revokeObjectURL(draft.previewUrl);
            });
            return [];
        });
    }, []);

    const openSessionPanel = useCallback(() => {
        setSessionsPanelOpen(true);
        loadSessions();
    }, [loadSessions]);

    const activateSession = useCallback(async (nextSessionId: string) => {
        if (!nextSessionId) return;
        if (isWaiting || isStreaming || isStopping) {
            setSessionsError('当前回复进行中，请先终止后再切换会话');
            return;
        }
        if (nextSessionId === sessionIdRef.current) {
            setSessionsPanelOpen(false);
            return;
        }

        setIsSwitchingSession(true);
        setSessionsPanelOpen(false);
        setIsWaiting(false);
        setIsStreaming(false);
        setIsStopping(false);
        setUploadError('');
        setAttachedFiles([]);
        clearUploadDrafts(true);
        sessionIdRef.current = nextSessionId;
        setSessionId(nextSessionId);
        setMessages([]);
        window.history.replaceState({}, '', writeChatSessionIdToHref(window.location.href, nextSessionId));

        manualCloseRef.current = true;
        wsRef.current?.close();
        if (await loadHistory(nextSessionId)) skipNextConnectedHistoryRef.current = nextSessionId;
        window.setTimeout(() => {
            manualCloseRef.current = false;
            openSocket(nextSessionId);
            setIsSwitchingSession(false);
        }, 0);
    }, [clearUploadDrafts, isStopping, isStreaming, isWaiting, loadHistory, openSocket]);

    const startNewSession = useCallback(async () => {
        if (!agentId || isStartingNew) return;
        setIsStartingNew(true);
        setIsWaiting(false);
        setIsStreaming(false);
        setIsStopping(false);
        setUploadError('');
        setAttachedFiles([]);
        clearUploadDrafts(true);
        try {
            const session = await chatSessionApi.create(agentId, { source_channel: channel });
            const nextSessionId = String(session.id);
            const summary = normalizeH5SessionSummary(session);
            sessionIdRef.current = nextSessionId;
            setSessionId(nextSessionId);
            setMessages([]);
            setSessionsPanelOpen(false);
            window.history.replaceState({}, '', writeChatSessionIdToHref(window.location.href, nextSessionId));
            if (summary) {
                setSessions((prev) => [summary, ...prev.filter((item) => item.id !== summary.id)]);
            }
            manualCloseRef.current = true;
            wsRef.current?.close();
            window.setTimeout(() => {
                manualCloseRef.current = false;
                openSocket(nextSessionId);
            }, 0);
        } catch (error: any) {
            setMessages((prev) => [...prev, {
                id: makeId(),
                role: 'system',
                content: error?.message || '无法开启新会话',
                created_at: new Date().toISOString(),
            }]);
        } finally {
            setIsStartingNew(false);
        }
    }, [agentId, channel, clearUploadDrafts, isStartingNew, openSocket]);

    const stopGeneration = useCallback(() => {
        const ws = wsRef.current;
        if (ws?.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'abort' }));
            setIsStopping(true);
            return;
        }
        setIsWaiting(false);
        setIsStreaming(false);
        setIsStopping(false);
    }, []);

    const removeAttachedFile = useCallback((index: number) => {
        setAttachedFiles((prev) => prev.filter((_, i) => i !== index));
    }, []);

    const cancelUploadDraft = useCallback((draft: H5UploadDraft) => {
        uploadAbortRef.current.get(draft.id)?.();
        uploadAbortRef.current.delete(draft.id);
        if (draft.previewUrl) URL.revokeObjectURL(draft.previewUrl);
        setUploadDrafts((prev) => prev.filter((item) => item.id !== draft.id));
    }, []);

    const handleH5Files = useCallback(async (files: File[]) => {
        if (!agentId || !files.length) return;
        setUploadError('');
        const availableSlots = Math.max(0, 10 - attachedFiles.length - uploadDrafts.length);
        const allowedFiles = files.slice(0, availableSlots);
        if (!allowedFiles.length) {
            setUploadError('最多可附加 10 个文件');
            return;
        }
        if (allowedFiles.length < files.length) {
            setUploadError(`最多可附加 10 个文件，已选择前 ${allowedFiles.length} 个`);
        }

        const baseTime = Date.now();
        const newDrafts = allowedFiles.map((file, index) => ({
            id: `h5-up-${baseTime}-${index}-${file.name}`,
            name: file.name,
            percent: 0,
            previewUrl: file.type.startsWith('image/') ? URL.createObjectURL(file) : undefined,
            sizeBytes: file.size,
        }));
        setUploadDrafts((prev) => [...prev, ...newDrafts]);

        const runOne = async (file: File, draft: H5UploadDraft) => {
            const { promise, abort } = uploadFileWithProgress(
                '/chat/upload',
                file,
                (pct) => {
                    setUploadDrafts((prev) =>
                        prev.map((item) => item.id === draft.id ? { ...item, percent: pct >= 101 ? 100 : pct } : item),
                    );
                },
                { agent_id: agentId },
                600_000,
            );
            uploadAbortRef.current.set(draft.id, abort);
            try {
                const data = await promise;
                const uploadedName = data.saved_filename || data.filename || file.name;
                setAttachedFiles((prev) => [...prev, {
                    name: uploadedName,
                    text: data.extracted_text || '',
                    path: data.workspace_path,
                    imageUrl: data.image_data_url || undefined,
                }].slice(0, 10));
            } catch (error: any) {
                if (error?.message !== 'Upload cancelled') {
                    setUploadError(error?.message || '文件上传失败');
                }
            } finally {
                uploadAbortRef.current.delete(draft.id);
                if (draft.previewUrl) URL.revokeObjectURL(draft.previewUrl);
                setUploadDrafts((prev) => prev.filter((item) => item.id !== draft.id));
            }
        };

        await Promise.all(allowedFiles.map((file, index) => runOne(file, newDrafts[index])));
    }, [agentId, attachedFiles.length, uploadDrafts.length]);

    const handleFileInputChange = useCallback((event: React.ChangeEvent<HTMLInputElement>) => {
        const files = Array.from(event.target.files || []);
        event.target.value = '';
        void handleH5Files(files);
    }, [handleH5Files]);

    const handlePaste = useCallback((event: React.ClipboardEvent<HTMLTextAreaElement>) => {
        const items = event.clipboardData?.items;
        if (!items?.length) return;
        const imageFiles: File[] = [];
        for (let i = 0; i < items.length; i += 1) {
            const item = items[i];
            if (!item.type.startsWith('image/')) continue;
            const blob = item.getAsFile();
            if (!blob) continue;
            const ext = blob.type.split('/')[1] || 'png';
            imageFiles.push(new File([blob], `paste-${Date.now()}-${i}.${ext}`, { type: blob.type }));
        }
        if (!imageFiles.length) return;
        event.preventDefault();
        void handleH5Files(imageFiles);
    }, [handleH5Files]);

    const effectiveModelId = useMemo(() => resolveEffectiveChatModelId({
        preferredModelId: agent?.primary_model_id || null,
        tenantDefaultModelId,
        models: llmModels,
    }), [agent?.primary_model_id, llmModels, tenantDefaultModelId]);

    const effectiveModelSupportsVision = useMemo(
        () => modelSupportsVision(llmModels, effectiveModelId),
        [effectiveModelId, llmModels],
    );

    const sendMessage = useCallback(async () => {
        const content = input.trim();
        if ((!content && attachedFiles.length === 0) || isWaiting || isStreaming || isStopping || isStartingNew || isSwitchingSession || uploadDrafts.length > 0 || speech.isActive) return;

        if (attachedFiles.length === 0 && (content === '/new' || content === '/reset')) {
            setInput('');
            await startNewSession();
            return;
        }

        const ws = wsRef.current;
        if (!ws || ws.readyState !== WebSocket.OPEN) {
            setMessages((prev) => [...prev, {
                id: makeId(),
                role: 'system',
                content: '连接未就绪',
                created_at: new Date().toISOString(),
            }]);
            openSocket(sessionIdRef.current);
            return;
        }

        const payload = buildChatAttachmentPayload({
            input: content,
            attachments: attachedFiles,
            supportsVision: effectiveModelSupportsVision || attachedFiles.some((file) => !!file.imageUrl),
        });
        setMessages((prev) => [...prev, {
            id: makeId(),
            role: 'user',
            content: stripAttachmentDisplayPrefix(payload.userMsg),
            fileName: payload.fileName,
            imageUrl: payload.imageUrl,
            previewImages: payload.previewImages,
            created_at: new Date().toISOString(),
        }]);
        setInput('');
        setAttachedFiles([]);
        setIsWaiting(true);
        setIsStreaming(false);
        ws.send(JSON.stringify({
            content: payload.contentForLLM,
            display_content: payload.userMsg,
            file_name: payload.fileName,
            model_id: effectiveModelId,
        }));
    }, [attachedFiles, effectiveModelId, effectiveModelSupportsVision, input, isStartingNew, isStreaming, isStopping, isSwitchingSession, isWaiting, openSocket, speech.isActive, startNewSession, uploadDrafts.length]);

    const handleInputKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
        if (event.key === 'Enter' && !event.shiftKey) {
            event.preventDefault();
            if (!isWaiting && !isStreaming && !isStopping && uploadDrafts.length === 0 && !speech.isActive) {
                sendMessage();
            }
        }
    };

    const conversationEntries = useMemo(() => buildH5ConversationEntries(messages), [messages]);
    const attachedImagePreviews = useMemo(() => buildPreviewImagesFromAttachments(attachedFiles), [attachedFiles]);
    const scrollAnchor = useMemo(() => getH5ScrollAnchor(conversationEntries, isWaiting), [conversationEntries, isWaiting]);
    const virtualizeMessages = conversationEntries.length > VIRTUALIZE_ENTRY_THRESHOLD;
    const virtualItemCount = conversationEntries.length + (isWaiting ? 1 : 0);
    const rowVirtualizer = useVirtualizer({
        count: virtualizeMessages ? virtualItemCount : 0,
        getScrollElement: () => messagesScrollerRef.current,
        estimateSize: (index) => {
            const entry = conversationEntries[index];
            if (!entry) return 54;
            return estimateConversationEntrySize(entry, entry.type === 'analysis_group' && !!analysisExpanded[entry.key]);
        },
        getItemKey: (index) => {
            if (index >= conversationEntries.length) return 'h5-waiting';
            return conversationEntries[index].key;
        },
        overscan: 8,
        enabled: virtualizeMessages,
    });
    const toggleAnalysis = useCallback((key: string) => {
        setAnalysisExpanded((prev) => ({ ...prev, [key]: !prev[key] }));
    }, []);

    useEffect(() => {
        const total = conversationEntries.length + (isWaiting ? 1 : 0);
        const scrollToBottom = () => {
            if (virtualizeMessages && total > 0) {
                rowVirtualizer.scrollToIndex(total - 1, { align: 'end' });
            } else {
                messagesEndRef.current?.scrollIntoView({ block: 'end' });
            }
        };
        const frame = window.requestAnimationFrame(() => {
            scrollToBottom();
            window.requestAnimationFrame(scrollToBottom);
        });
        return () => window.cancelAnimationFrame(frame);
    }, [connectionStatus, rowVirtualizer, scrollAnchor, virtualizeMessages]);

    const renderWaitingMessage = useCallback(() => (
        <article className="h5-chat__message h5-chat__message--assistant">
            <div className="h5-chat__bubble">
                <div className="h5-chat__typing"><span /><span /><span /></div>
            </div>
        </article>
    ), []);

    const handleMarkdownLinkClick = useCallback((href: string): boolean => {
        if (!isWechatMiniProgram) return false;

        const externalUrl = resolveExternalHttpLink(href);
        if (!externalUrl) return false;

        void copyToClipboard(externalUrl).then((copied) => {
            if (copied) {
                toast.success('链接已复制，请在外部浏览器中打开');
            } else {
                toast.error('链接复制失败，请稍后重试');
            }
        });
        return true;
    }, [isWechatMiniProgram, toast]);

    const renderConversationEntry = useCallback((entry: (typeof conversationEntries)[number]) => {
        if (entry.type === 'analysis_group') {
            return (
                <H5AnalysisCard
                    items={entry.items}
                    expanded={!!analysisExpanded[entry.key]}
                    onToggle={() => toggleAnalysis(entry.key)}
                />
            );
        }

        if (entry.type === 'file_delivery') {
            return (
                <article className="h5-chat__message h5-chat__message--assistant h5-chat__message--file-delivery">
                    <ChatFileDeliveryCard
                        agentId={agentId || ''}
                        delivery={entry.delivery}
                        mode="h5"
                        onPreviewImages={(images, index) => setImagePreview({ images, index })}
                    />
                </article>
            );
        }

        const msg = entry.msg;
        const rawDisplayContent = msg.fileName ? stripAttachmentDisplayPrefix(msg.content) : msg.content;
        const displayContent = stripChatImageDataMarkers(rawDisplayContent);
        const filePreviewImages = msg.previewImages || (msg.imageUrl ? [buildPreviewImage(msg.imageUrl, msg.fileName)] : []);
        const inlinePreviewImages = filePreviewImages.length > 0 ? [] : extractChatImageDataMarkers(rawDisplayContent);
        const previewImages = filePreviewImages.length > 0 ? filePreviewImages : inlinePreviewImages;
        const previewedImageNames = new Set(previewImages.map((image) => image.filename).filter(Boolean));
        const fileChips = splitAttachmentFileNames(msg.fileName)
            .filter((name) => !previewedImageNames.has(name));
        if (msg.role === 'tool_call' && isConfirmationToolCall(msg)) {
            return (
                <article className="h5-chat__message h5-chat__message--assistant h5-chat__message--confirmation">
                    <div className="h5-chat__confirmation-card">
                        <ConfirmationCard
                            agentId={agentId || ''}
                            callId={msg.toolCallId || ''}
                            args={msg.toolArgs || {}}
                            resolved={msg.toolStatus === 'done'}
                            result={msg.toolResult}
                            t={h5T}
                        />
                    </div>
                </article>
            );
        }

        return (
            <article className={`h5-chat__message h5-chat__message--${msg.role}`}>
                <div className="h5-chat__bubble">
                    {previewImages.length > 0 ? (
                        <div className="h5-chat__image-grid">
                            {previewImages.map((image, index) => (
                                <button
                                    key={`${image.src}-${index}`}
                                    type="button"
                                    className="h5-chat__bubble-image-button"
                                    onClick={() => setImagePreview({ images: previewImages, index })}
                                    aria-label="预览图片"
                                >
                                    <img className="h5-chat__bubble-image" src={image.src} alt={image.alt || image.filename || 'image'} loading="lazy" />
                                </button>
                            ))}
                        </div>
                    ) : null}
                    {fileChips.length > 0 ? (
                        <div className="h5-chat__file-chip-list">
                            {fileChips.map((fileName) => (
                                <div key={fileName} className="h5-chat__file-chip">
                                    <IconPaperclip size={14} stroke={1.75} />
                                    <span>{fileName}</span>
                                </div>
                            ))}
                        </div>
                    ) : null}
                    {msg.thinking && !msg.content ? (
                        <div className="h5-chat__thinking">思考中</div>
                    ) : null}
                    {displayContent ? (
                        <MarkdownRenderer
                            className="h5-chat__markdown"
                            content={displayContent}
                            imagePreviewMode="mobile"
                            onLinkClick={handleMarkdownLinkClick}
                        />
                    ) : msg.streaming ? (
                        <div className="h5-chat__typing"><span /><span /><span /></div>
                    ) : null}
                </div>
            </article>
        );
    }, [agentId, analysisExpanded, handleMarkdownLinkClick, toggleAnalysis]);

    const connectionLabel = connectionStatus === 'connected'
        ? '已连接'
        : connectionStatus === 'connecting'
            ? '连接中'
            : '未连接';

    const showBlockingError = authStatus === 'error' || !!agentError;
    const isBusy = authStatus === 'checking' || authStatus === 'exchanging' || (authStatus === 'ready' && !agent && !agentError);
    const generationActive = isWaiting || isStreaming || isStopping;
    const sendDisabled = (!input.trim() && attachedFiles.length === 0)
        || showBlockingError
        || isBusy
        || generationActive
        || speech.isActive
        || isStartingNew
        || isSwitchingSession
        || uploadDrafts.length > 0;
    const uploadDisabled = showBlockingError
        || isBusy
        || generationActive
        || speech.isActive
        || isStartingNew
        || isSwitchingSession
        || uploadDrafts.length > 0
        || attachedFiles.length >= 10;
    const agentAvatarUrl = resolveAgentAvatarUrl(agent?.avatar_url, token);

    return (
        <main className={`h5-chat h5-chat--${theme}`} data-theme={theme}>
            <header className="h5-chat__header">
                <div className="h5-chat__agent">
                    <div className="h5-chat__avatar">
                        {agentAvatarUrl
                            ? <img src={agentAvatarUrl} alt={agent?.name || 'Agent'} />
                            : <span>{(agent?.name || 'A').slice(0, 1).toUpperCase()}</span>}
                    </div>
                    <div className="h5-chat__agent-copy">
                        <div className="h5-chat__agent-name">{agent?.name || 'Agent'}</div>
                        <div className={`h5-chat__status h5-chat__status--${connectionStatus}`}>
                            <span />
                            {connectionLabel}
                        </div>
                    </div>
                </div>
                <div className="h5-chat__header-actions">
                    <button
                        type="button"
                        className="h5-chat__icon-button"
                        onClick={openSessionPanel}
                        disabled={authStatus !== 'ready' || isSwitchingSession || speech.isActive}
                        aria-label="历史会话"
                        title="历史会话"
                    >
                        <IconHistory size={18} />
                    </button>
                    <button
                        type="button"
                        className="h5-chat__icon-button"
                        onClick={startNewSession}
                        disabled={isStartingNew || authStatus !== 'ready' || speech.isActive}
                        aria-label="新会话"
                        title="新会话"
                    >
                        <IconPlus size={19} />
                    </button>
                    <button
                        type="button"
                        className="h5-chat__icon-button"
                        onClick={() => openSocket(sessionIdRef.current)}
                        disabled={authStatus !== 'ready' || speech.isActive}
                        aria-label="重连"
                        title="重连"
                    >
                        <IconRefresh size={18} />
                    </button>
                </div>
            </header>

            {sessionsPanelOpen ? (
                <section className="h5-chat__session-panel" role="dialog" aria-modal="true" aria-label="历史会话">
                    <div className="h5-chat__session-panel-header">
                        <button
                            type="button"
                            className="h5-chat__icon-button"
                            onClick={() => setSessionsPanelOpen(false)}
                            aria-label="返回"
                            title="返回"
                        >
                            <IconArrowLeft size={19} />
                        </button>
                        <div className="h5-chat__session-panel-title">历史会话</div>
                        <button
                            type="button"
                            className="h5-chat__icon-button"
                            onClick={loadSessions}
                            disabled={sessionsLoading}
                            aria-label="刷新历史会话"
                            title="刷新历史会话"
                        >
                            <IconRefresh size={18} className={sessionsLoading ? 'h5-chat__spin' : undefined} />
                        </button>
                    </div>

                    {generationActive ? (
                        <div className="h5-chat__session-warning">当前回复进行中，请先终止后再切换会话</div>
                    ) : null}

                    <div className="h5-chat__session-list">
                        {sessionsLoading && sessions.length === 0 ? (
                            <div className="h5-chat__session-state">
                                <IconLoader2 size={20} className="h5-chat__spin" />
                                <span>加载中</span>
                            </div>
                        ) : sessionsError ? (
                            <div className="h5-chat__session-state h5-chat__session-state--error">
                                <IconAlertTriangle size={20} />
                                <span>{sessionsError}</span>
                            </div>
                        ) : sessions.length === 0 ? (
                            <div className="h5-chat__session-state">暂无历史会话</div>
                        ) : (
                            sessions.map((item) => {
                                const active = item.id === sessionId;
                                const timeLabel = formatH5SessionTime(item.last_message_at || item.created_at);
                                return (
                                    <button
                                        key={item.id}
                                        type="button"
                                        className={`h5-chat__session-item${active ? ' h5-chat__session-item--active' : ''}`}
                                        onClick={() => activateSession(item.id)}
                                        disabled={generationActive || isSwitchingSession}
                                    >
                                        <span className="h5-chat__session-main">
                                            <span className="h5-chat__session-title">{item.title}</span>
                                            <span className="h5-chat__session-meta">
                                                {timeLabel ? <span>{timeLabel}</span> : null}
                                                <span>{item.message_count} 条消息</span>
                                            </span>
                                        </span>
                                        <span className="h5-chat__session-side">
                                            <span className="h5-chat__session-channel">{h5SessionChannelLabel(item.source_channel)}</span>
                                            {item.is_primary ? <span className="h5-chat__session-badge">主会话</span> : null}
                                            {item.unread_count > 0 ? <span className="h5-chat__session-unread">{item.unread_count}</span> : null}
                                        </span>
                                    </button>
                                );
                            })
                        )}
                    </div>
                </section>
            ) : null}

            {showBlockingError ? (
                <section className="h5-chat__state h5-chat__state--error">
                    <IconAlertTriangle size={28} />
                    <div>{authError || agentError}</div>
                </section>
            ) : isBusy ? (
                <section className="h5-chat__state">
                    <IconLoader2 size={28} className="h5-chat__spin" />
                    <div>{authStatus === 'exchanging' ? '正在登录' : '加载中'}</div>
                </section>
            ) : (
                <section
                    ref={messagesScrollerRef}
                    className={`h5-chat__messages${virtualizeMessages ? ' h5-chat__messages--virtual' : ''}`}
                    aria-live="polite"
                >
                    {virtualizeMessages ? (
                        <div
                            className="h5-chat__virtual-spacer"
                            style={{ height: `${rowVirtualizer.getTotalSize()}px` }}
                        >
                            {rowVirtualizer.getVirtualItems().map((virtualItem) => {
                                const entry = conversationEntries[virtualItem.index];
                                return (
                                    <div
                                        key={virtualItem.key}
                                        ref={rowVirtualizer.measureElement}
                                        data-index={virtualItem.index}
                                        className="h5-chat__virtual-row"
                                        style={{ transform: `translateY(${virtualItem.start}px)` }}
                                    >
                                        {entry ? renderConversationEntry(entry) : renderWaitingMessage()}
                                    </div>
                                );
                            })}
                        </div>
                    ) : (
                        <>
                            {conversationEntries.map((entry) => (
                                <div key={entry.key} className="h5-chat__flow-row">
                                    {renderConversationEntry(entry)}
                                </div>
                            ))}
                            {isWaiting ? (
                                <div className="h5-chat__flow-row">
                                    {renderWaitingMessage()}
                                </div>
                            ) : null}
                        </>
                    )}
                    <div ref={messagesEndRef} />
                </section>
            )}

            <form
                className="h5-chat__composer"
                onSubmit={(event) => {
                    event.preventDefault();
                    sendMessage();
                }}
            >
                <input
                    ref={fileInputRef}
                    type="file"
                    multiple
                    className="h5-chat__file-input"
                    onChange={handleFileInputChange}
                />
                {uploadError ? <div className="h5-chat__upload-error">{uploadError}</div> : null}
                {(uploadDrafts.length > 0 || attachedFiles.length > 0) ? (
                    <div className="h5-chat__attachments">
                        {uploadDrafts.map((draft) => (
                            <div key={draft.id} className="h5-chat__file-pill h5-chat__file-pill--uploading">
                                <div className="h5-chat__file-pill-fill" style={{ width: `${draft.percent}%` }} />
                                <div className="h5-chat__file-pill-row">
                                    {draft.previewUrl ? (
                                        <img className="h5-chat__file-thumb" src={draft.previewUrl} alt="" />
                                    ) : (
                                        <span className="h5-chat__file-icon"><IconPaperclip size={14} stroke={1.75} /></span>
                                    )}
                                    <span className="h5-chat__file-name">{draft.name}</span>
                                    <span className="h5-chat__file-size">{formatFileSize(draft.sizeBytes)}</span>
                                    <span className="h5-chat__file-progress">{draft.percent}%</span>
                                    <button
                                        type="button"
                                        className="h5-chat__file-remove"
                                        onClick={() => cancelUploadDraft(draft)}
                                        aria-label="取消上传"
                                    >
                                        <IconX size={14} stroke={1.8} />
                                    </button>
                                </div>
                            </div>
                        ))}
                        {attachedFiles.map((file, index) => {
                            const imageIndex = attachedImagePreviews.findIndex((image) => image.src === file.imageUrl);
                            return (
                                <div key={`${file.name}-${index}`} className="h5-chat__file-pill" title={file.path || file.name}>
                                    <div className="h5-chat__file-pill-row">
                                        {file.imageUrl ? (
                                            <button
                                                type="button"
                                                className="h5-chat__file-thumb-button"
                                                onClick={() => {
                                                    if (imageIndex >= 0) setImagePreview({ images: attachedImagePreviews, index: imageIndex });
                                                }}
                                                aria-label="预览图片"
                                            >
                                                <img className="h5-chat__file-thumb" src={file.imageUrl} alt="" />
                                            </button>
                                        ) : (
                                            <span className="h5-chat__file-icon"><IconPaperclip size={14} stroke={1.75} /></span>
                                        )}
                                        <span className="h5-chat__file-name">{file.name}</span>
                                        <button
                                            type="button"
                                            className="h5-chat__file-remove"
                                            onClick={() => removeAttachedFile(index)}
                                            aria-label="移除附件"
                                        >
                                            <IconX size={14} stroke={1.8} />
                                        </button>
                                    </div>
                                </div>
                            );
                        })}
                    </div>
                ) : null}
                {speech.isActive || speech.error ? (
                    <div className={`h5-chat__speech-status${speech.error ? ' h5-chat__speech-status--error' : ''}`}>
                        <span>
                            {speech.error
                                || (speech.status === 'connecting'
                                    ? '正在连接语音识别…'
                                    : speech.status === 'stopping'
                                        ? '正在整理文字…'
                                        : `正在听${speech.transcript ? `：${speech.transcript}` : '…'}`)}
                        </span>
                    </div>
                ) : null}
                <div className="h5-chat__composer-row">
                    <button
                        type="button"
                        className="h5-chat__attach"
                        onClick={() => fileInputRef.current?.click()}
                        disabled={uploadDisabled}
                        aria-label="上传附件"
                        title="上传附件"
                    >
                        <IconPaperclip size={19} stroke={1.75} />
                    </button>
                    <div className="h5-chat__composer-input">
                        <textarea
                            ref={textareaRef}
                            value={input}
                            onChange={(event) => setInput(event.target.value)}
                            onSelect={handleInputSelect}
                            onKeyDown={handleInputKeyDown}
                            onPaste={handlePaste}
                            placeholder="输入消息"
                            rows={1}
                            onFocus={handleInputSelect}
                            disabled={showBlockingError || isBusy || isStartingNew || speech.isActive}
                        />
                        <button
                            type="button"
                            className={`h5-chat__speech-button${speech.status === 'recording' ? ' h5-chat__speech-button--recording' : ''}`}
                            onPointerDown={speech.status === 'recording' ? undefined : captureSpeechInsertionPoint}
                            onClick={speech.status === 'recording' ? speech.stop : startSpeechInput}
                            disabled={!speech.supported
                                || showBlockingError
                                || isBusy
                                || generationActive
                                || isStartingNew
                                || isSwitchingSession
                                || speech.status === 'connecting'
                                || speech.status === 'stopping'}
                            aria-label={speech.status === 'recording' ? '停止语音输入' : '开始语音输入'}
                            title={!speech.supported ? '当前浏览器不支持实时语音输入' : speech.status === 'recording' ? '停止语音输入' : '语音输入'}
                            aria-pressed={speech.status === 'recording'}
                        >
                            {speech.status === 'connecting' || speech.status === 'stopping'
                                ? <IconLoader2 size={19} className="h5-chat__spin" />
                                : speech.status === 'recording'
                                    ? <IconPlayerStopFilled size={17} />
                                    : <IconMicrophone size={19} stroke={1.8} />}
                        </button>
                    </div>
                    {generationActive ? (
                        <button
                            type="button"
                            className="h5-chat__send h5-chat__send--stop"
                            onClick={stopGeneration}
                            aria-label="停止"
                            title="停止"
                        >
                            <IconPlayerStopFilled size={18} />
                        </button>
                    ) : (
                        <button
                            type="submit"
                            className="h5-chat__send"
                            disabled={sendDisabled}
                            aria-label="发送"
                            title="发送"
                        >
                            {isStartingNew ? <IconLoader2 size={19} className="h5-chat__spin" /> : <IconSend size={19} />}
                        </button>
                    )}
                </div>
            </form>
            <ChatImageLightbox
                open={!!imagePreview}
                images={imagePreview?.images || []}
                index={imagePreview?.index || 0}
                mode="mobile"
                onClose={() => setImagePreview(null)}
                onIndexChange={(index) => setImagePreview((prev) => prev ? { ...prev, index } : prev)}
            />
        </main>
    );
}
