import { useState, useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { agentApi } from '../services/api';
import type { Agent } from '../types';
import { Globe, Zap, Coffee, PauseCircle } from 'lucide-react';

/* ────── Avatar Gradient Palette ────── */

const AVATAR_GRADIENTS = [
    'linear-gradient(135deg, #667eea 0%, #764ba2 100%)',
    'linear-gradient(135deg, #f093fb 0%, #f5576c 100%)',
    'linear-gradient(135deg, #4facfe 0%, #00f2fe 100%)',
    'linear-gradient(135deg, #43e97b 0%, #38f9d7 100%)',
    'linear-gradient(135deg, #fa709a 0%, #fee140 100%)',
    'linear-gradient(135deg, #a18cd1 0%, #fbc2eb 100%)',
    'linear-gradient(135deg, #fccb90 0%, #d57eeb 100%)',
    'linear-gradient(135deg, #30cfd0 0%, #330867 100%)',
];

function getAvatarGradient(name: string): string {
    const code = (name || '?').charCodeAt(0);
    return AVATAR_GRADIENTS[code % AVATAR_GRADIENTS.length];
}

/* ────── Inline SVG Icons ────── */

const Icons = {
    search: (
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="7" cy="7" r="4.5" />
            <path d="M10.5 10.5L14 14" />
        </svg>
    ),
    chat: (
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="M2 4a2 2 0 012-2h8a2 2 0 012 2v5a2 2 0 01-2 2H8l-3 3V11H4a2 2 0 01-2-2V4z" />
        </svg>
    ),
    user: (
        <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="8" cy="5" r="3" />
            <path d="M2.5 14a5.5 5.5 0 0111 0" />
        </svg>
    ),
    bot: (
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <rect x="4" y="8" width="16" height="12" rx="3" />
            <circle cx="9" cy="14" r="1.5" fill="currentColor" stroke="none" />
            <circle cx="15" cy="14" r="1.5" fill="currentColor" stroke="none" />
            <path d="M12 3v5M8 3h8" />
        </svg>
    ),
};

/* ────── Status Config ────── */

const statusConfig: Record<string, { color: string; labelZh: string; labelEn: string }> = {
    running: { color: 'var(--status-running)', labelZh: '运行中', labelEn: 'Running' },
    idle: { color: 'var(--status-idle)', labelZh: '空闲', labelEn: 'Idle' },
    stopped: { color: 'var(--status-stopped)', labelZh: '已停止', labelEn: 'Stopped' },
    creating: { color: 'var(--warning)', labelZh: '创建中', labelEn: 'Creating' },
    error: { color: 'var(--status-error)', labelZh: '错误', labelEn: 'Error' },
};

/* ────── Category Tabs ────── */

const CATEGORIES = [
    { key: 'all', icon: <Globe size={14} />, labelZh: '全部', labelEn: 'All' },
    { key: 'running', icon: <Zap size={14} />, labelZh: '运行中', labelEn: 'Running' },
    { key: 'idle', icon: <Coffee size={14} />, labelZh: '空闲', labelEn: 'Idle' },
    { key: 'stopped', icon: <PauseCircle size={14} />, labelZh: '已停止', labelEn: 'Stopped' },
];

const TAG_LABELS_ZH: Record<string, string> = {
    NATIVE: '原生智能体',
    OPENCLAW: '外部应用',
};

/* ────── Tag Colors ────── */

const TAG_COLORS: Record<string, { bg: string; color: string }> = {
    native: { bg: 'rgba(99, 102, 241, 0.15)', color: '#818cf8' },
    openclaw: { bg: 'rgba(16, 185, 129, 0.15)', color: '#34d399' },
};

const DEFAULT_TAG_COLOR = { bg: 'rgba(139, 139, 158, 0.12)', color: 'var(--text-secondary)' };

function getTagColor(tag: string) {
    return TAG_COLORS[tag.toLowerCase()] || DEFAULT_TAG_COLOR;
}

/* ────── Helpers ────── */

const fetchJson = async <T,>(url: string): Promise<T> => {
    const token = localStorage.getItem('token');
    const res = await fetch(url, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
    if (!res.ok) throw new Error('Failed to fetch');
    return res.json();
};

function timeAgo(dateStr: string | undefined, isChinese: boolean): string {
    if (!dateStr) return '-';
    const diff = Date.now() - new Date(dateStr).getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return isChinese ? '刚刚' : 'just now';
    if (mins < 60) return `${mins}m`;
    const hours = Math.floor(mins / 60);
    if (hours < 24) return `${hours}h`;
    return `${Math.floor(hours / 24)}d`;
}

/** Extract #hashtags from a text string */
function extractTags(text: string | undefined | null): string[] {
    if (!text) return [];
    const matches = text.match(/#[\w\u4e00-\u9fff]+/g);
    return matches ? [...new Set(matches.map(t => t.toUpperCase()))] : [];
}

/* ────── Injected Styles ────── */

const styles = `
    .explore-grid {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(440px, 1fr));
        gap: 24px;
        max-width: 1440px;
        margin: 0 auto;
        padding-bottom: 40px;
        width: 100%;
    }
    .explore-card {
        border: 1px solid var(--border-subtle);
        border-radius: var(--radius-lg);
        padding: 20px;
        background: var(--bg-secondary);
        transition: all 0.2s ease;
        cursor: pointer;
        display: flex;
        flex-direction: column;
    }
    .explore-card:hover {
        border-color: var(--border-strong);
        box-shadow: var(--shadow-md);
        transform: translateY(-2px);
    }
    .explore-search {
        width: 100%;
        max-width: 480px;
        padding: 10px 16px 10px 40px;
        font-size: 14px;
        background: var(--bg-secondary);
        border: 1px solid var(--border-default);
        border-radius: var(--radius-full);
        color: var(--text-primary);
        outline: none;
        transition: all 0.2s ease;
        font-family: var(--font-family);
    }
    .explore-search:focus {
        border-color: var(--accent-primary);
        box-shadow: 0 0 0 3px var(--accent-subtle);
    }
    .explore-search::placeholder {
        color: var(--text-tertiary);
    }
    .explore-tab {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        padding: 6px 16px;
        border-radius: var(--radius-full);
        font-size: 13px;
        font-weight: 500;
        border: none;
        cursor: pointer;
        transition: all 0.15s ease;
        white-space: nowrap;
    }
    .explore-tab.active {
        background: var(--accent-primary);
        color: var(--text-inverse);
    }
    .explore-tab:not(.active) {
        background: transparent;
        color: var(--text-secondary);
    }
    .explore-tab:not(.active):hover {
        background: var(--bg-hover);
        color: var(--text-primary);
    }
    .explore-chat-btn {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        padding: 5px 14px;
        border-radius: var(--radius-full);
        font-size: 12px;
        font-weight: 500;
        border: 1px solid var(--border-default);
        background: var(--bg-tertiary);
        color: var(--text-secondary);
        cursor: pointer;
        transition: all 0.15s ease;
        white-space: nowrap;
    }
    .explore-chat-btn:hover {
        background: var(--accent-primary);
        color: var(--text-inverse);
        border-color: var(--accent-primary);
    }
    .explore-tag {
        display: inline-flex;
        align-items: center;
        padding: 2px 8px;
        border-radius: var(--radius-sm);
        font-size: 11px;
        font-weight: 600;
        letter-spacing: 0.3px;
    }
`;

/* ────── Bot Card Component ────── */

function BotCard({ agent, creatorName, isChinese, onCardClick, onChatClick }: {
    agent: Agent;
    creatorName: string;
    isChinese: boolean;
    onCardClick: () => void;
    onChatClick: (e: React.MouseEvent) => void;
}) {
    const status = statusConfig[agent.status] || statusConfig.stopped;
    const firstChar = ((Array.from(agent.name || '?')[0] as string) || '?').toUpperCase();
    const gradient = getAvatarGradient(agent.name);

    // Build tags list
    const tags: string[] = [];
    tags.push(...extractTags(agent.bio));
    // Limit to 3 tags max
    const displayTags = tags.slice(0, 3);

    const description = agent.role_description || agent.bio || '';

    return (
        <div className="explore-card" onClick={onCardClick}>
            {/* Header: Avatar + Name + Author */}
            <div style={{ display: 'flex', gap: '12px', marginBottom: '12px' }}>
                {/* Avatar */}
                <div style={{
                    width: 48, height: 48, borderRadius: 'var(--radius-lg)',
                    background: gradient,
                    display: 'flex', alignItems: 'center', justifyContent: 'center',
                    color: '#fff', fontSize: '20px', fontWeight: 700,
                    flexShrink: 0, boxShadow: '0 2px 8px rgba(0,0,0,0.15)',
                }}>
                    {agent.avatar_url ? (
                        <img
                            src={agent.avatar_url.startsWith('/api') ? `${agent.avatar_url}${agent.avatar_url.includes('?') ? '&' : '?'}token=${localStorage.getItem('token') || ''}` : agent.avatar_url}
                            alt=""
                            style={{ width: '100%', height: '100%', objectFit: 'cover', borderRadius: 'var(--radius-lg)' }}
                        />
                    ) : (
                        firstChar
                    )}
                </div>

                {/* Name + Author */}
                <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{
                        display: 'flex', alignItems: 'center', gap: '8px',
                        marginBottom: '2px',
                    }}>
                        <span style={{
                            fontSize: '15px', fontWeight: 600,
                            color: 'var(--text-primary)',
                            overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                        }}>
                            {agent.name}
                        </span>
                        {/* Status badge */}
                        <span style={{
                            display: 'inline-flex', alignItems: 'center', gap: '4px',
                            fontSize: '11px', fontWeight: 500,
                            color: status.color,
                            flexShrink: 0,
                        }}>
                            <span style={{
                                width: 6, height: 6, borderRadius: '50%',
                                background: status.color,
                                display: 'inline-block',
                            }} />
                            {isChinese ? status.labelZh : status.labelEn}
                        </span>
                    </div>
                    {/* Author line */}
                    <div style={{
                        display: 'flex', alignItems: 'center', gap: '4px',
                        fontSize: '12px', color: 'var(--text-tertiary)',
                    }}>
                        <span style={{ display: 'flex', opacity: 0.7 }}>{Icons.user}</span>
                        <span>{isChinese ? '作者' : 'by'} {creatorName}</span>
                    </div>
                </div>
            </div>

            {/* Description */}
            <div style={{
                fontSize: '13px', lineHeight: '20px',
                color: 'var(--text-secondary)',
                marginBottom: displayTags.length > 0 ? '12px' : '16px',
                overflow: 'hidden',
                display: '-webkit-box',
                WebkitLineClamp: 3,
                WebkitBoxOrient: 'vertical' as const,
                minHeight: '60px',
                height: '60px',
            }}>
                {description || (isChinese ? '暂无描述' : 'No description')}
            </div>

            {/* Tags */}
            {displayTags.length > 0 && (
                <div style={{
                    display: 'flex', gap: '6px', flexWrap: 'wrap',
                    marginBottom: '16px',
                }}>
                    {displayTags.map(tag => {
                        const tc = getTagColor(tag);
                        const displayTag = isChinese && TAG_LABELS_ZH[tag.toUpperCase()] ? TAG_LABELS_ZH[tag.toUpperCase()] : tag;
                        return (
                            <span key={tag} className="explore-tag" style={{
                                background: tc.bg, color: tc.color,
                            }}>
                                #{displayTag}
                            </span>
                        );
                    })}
                </div>
            )}

            {/* Footer: Status + Chat Button */}
            <div style={{
                borderTop: '1px solid var(--border-subtle)',
                paddingTop: '12px',
                marginTop: 'auto',
                display: 'flex', alignItems: 'center', justifyContent: 'space-between',
            }}>
                <div style={{
                    display: 'flex', alignItems: 'center', gap: '6px',
                    fontSize: '12px', color: 'var(--text-tertiary)',
                }}>
                    <span style={{ display: 'flex', opacity: 0.5 }}>{Icons.bot}</span>
                    <span>{timeAgo(agent.last_active_at, isChinese)}</span>
                </div>

                <button className="explore-chat-btn" onClick={onChatClick}>
                    <span style={{ display: 'flex' }}>{Icons.chat}</span>
                    {isChinese ? '对 话' : 'Chat'}
                </button>
            </div>
        </div>
    );
}

/* ────── Main Component ────── */

export default function Explore() {
    const { i18n } = useTranslation();
    const navigate = useNavigate();
    const isChinese = i18n.language?.startsWith('zh');
    const [search, setSearch] = useState('');
    const [category, setCategory] = useState('all');
    const tenantId = localStorage.getItem('current_tenant_id') || '';

    // Fetch agents
    const { data: agents = [], isLoading } = useQuery({
        queryKey: ['agents', tenantId],
        queryFn: () => agentApi.list(tenantId || undefined),
        refetchInterval: 15000,
    });

    // Fetch users for creator names
    const { data: users = [] } = useQuery<any[]>({
        queryKey: ['users-for-explore', tenantId],
        queryFn: () => fetchJson(`/api/org/users${tenantId ? `?tenant_id=${tenantId}` : ''}`),
        refetchInterval: 60000,
    });

    const userMap = useMemo(() => {
        const map = new Map<string, string>();
        users.forEach((u: any) => map.set(u.id, u.display_name || u.username));
        return map;
    }, [users]);

    // Filter agents
    const filtered = useMemo(() => {
        let result = [...agents];

        // Category filter
        if (category !== 'all') {
            result = result.filter(a => a.status === category);
        }

        // Search filter
        if (search.trim()) {
            const q = search.trim().toLowerCase();
            result = result.filter(a =>
                a.name.toLowerCase().includes(q) ||
                (a.role_description || '').toLowerCase().includes(q) ||
                (a.bio || '').toLowerCase().includes(q)
            );
        }

        // Sort: running first, then by last_active_at descending
        result.sort((a, b) => {
            const statusOrder: Record<string, number> = { running: 0, idle: 1, creating: 2, stopped: 3, error: 4 };
            const sa = statusOrder[a.status] ?? 5;
            const sb = statusOrder[b.status] ?? 5;
            if (sa !== sb) return sa - sb;
            const ta = a.last_active_at ? new Date(a.last_active_at).getTime() : 0;
            const tb = b.last_active_at ? new Date(b.last_active_at).getTime() : 0;
            return tb - ta;
        });

        return result;
    }, [agents, category, search]);

    // Category counts
    const counts = useMemo(() => {
        const c: Record<string, number> = { all: agents.length, running: 0, idle: 0, stopped: 0 };
        agents.forEach(a => { if (c[a.status] !== undefined) c[a.status]++; });
        return c;
    }, [agents]);

    return (
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column' }}>
            <style>{styles}</style>

            {/* ─── Hero Section ─── */}
            <div style={{
                textAlign: 'center',
                paddingTop: '8px',
                paddingBottom: '24px',
            }}>
                <h1 style={{
                    fontSize: '28px', fontWeight: 700,
                    color: 'var(--text-primary)',
                    letterSpacing: '-0.03em',
                    marginBottom: '6px',
                }}>
                    {isChinese ? '探索你的 AI 小伙伴' : 'Explore Your AI Agents'}
                </h1>
                <p style={{
                    fontSize: '14px',
                    color: 'var(--text-tertiary)',
                    margin: 0,
                }}>
                    {isChinese
                        ? '发现、对话，与团队中的智能体协作'
                        : 'Discover, chat with, and collaborate with agents in your team'}
                </p>
            </div>

            {/* ─── Search Bar ─── */}
            <div style={{
                display: 'flex', justifyContent: 'center',
                marginBottom: '20px', position: 'relative',
            }}>
                <div style={{ position: 'relative', width: '100%', maxWidth: '480px' }}>
                    <div style={{
                        position: 'absolute', left: '14px', top: '50%',
                        transform: 'translateY(-50%)',
                        color: 'var(--text-tertiary)', display: 'flex',
                        pointerEvents: 'none',
                    }}>
                        {Icons.search}
                    </div>
                    <input
                        className="explore-search"
                        type="text"
                        value={search}
                        onChange={e => setSearch(e.target.value)}
                        placeholder={isChinese ? '搜索机器人、描述或标签...' : 'Search bots, descriptions, or tags...'}
                    />
                </div>
            </div>

            {/* ─── Category Tabs ─── */}
            <div style={{
                display: 'flex', justifyContent: 'center',
                gap: '8px', marginBottom: '28px',
                flexWrap: 'wrap',
            }}>
                {CATEGORIES.map(cat => (
                    <button
                        key={cat.key}
                        className={`explore-tab ${category === cat.key ? 'active' : ''}`}
                        onClick={() => setCategory(cat.key)}
                    >
                        <span style={{ display: 'flex', alignItems: 'center' }}>{cat.icon}</span>
                        {isChinese ? cat.labelZh : cat.labelEn}
                        <span style={{
                            fontSize: '11px',
                            opacity: category === cat.key ? 0.8 : 0.5,
                            marginLeft: '2px',
                        }}>
                            {counts[cat.key] ?? 0}
                        </span>
                    </button>
                ))}
            </div>

            {/* ─── Content ─── */}
            {isLoading ? (
                <div style={{
                    textAlign: 'center', padding: '80px 20px',
                    color: 'var(--text-tertiary)', fontSize: '14px',
                }}>
                    <div style={{
                        width: '32px', height: '32px', margin: '0 auto 12px',
                        border: '2px solid var(--border-subtle)',
                        borderTopColor: 'var(--accent-primary)',
                        borderRadius: '50%',
                        animation: 'spin 0.8s linear infinite',
                    }} />
                    {isChinese ? '加载中...' : 'Loading...'}
                    <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
                </div>
            ) : filtered.length === 0 ? (
                <div style={{
                    textAlign: 'center', padding: '80px 20px',
                    color: 'var(--text-tertiary)',
                }}>
                    <div style={{
                        display: 'flex', justifyContent: 'center',
                        marginBottom: '12px', opacity: 0.3,
                    }}>
                        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1" strokeLinecap="round" strokeLinejoin="round">
                            <rect x="4" y="8" width="16" height="12" rx="3" />
                            <circle cx="9" cy="14" r="1.5" />
                            <circle cx="15" cy="14" r="1.5" />
                            <path d="M12 3v5M8 3h8" />
                        </svg>
                    </div>
                    <div style={{ fontSize: '15px', fontWeight: 500, marginBottom: '4px' }}>
                        {search
                            ? (isChinese ? '没有找到匹配的智能体' : 'No agents match your search')
                            : (isChinese ? '暂无智能体' : 'No agents yet')}
                    </div>
                    <div style={{ fontSize: '13px' }}>
                        {search
                            ? (isChinese ? '试试其他关键词' : 'Try different keywords')
                            : (isChinese ? '创建你的第一个 AI 智能体开始探索' : 'Create your first agent to get started')}
                    </div>
                </div>
            ) : (
                <div className="explore-grid">
                    {filtered.map(agent => (
                        <BotCard
                            key={agent.id}
                            agent={agent}
                            creatorName={userMap.get(agent.creator_id) || (isChinese ? '未知' : 'Unknown')}
                            isChinese={!!isChinese}
                            onCardClick={() => navigate(`/agents/${agent.id}`)}
                            onChatClick={(e) => {
                                e.stopPropagation();
                                navigate(`/agents/${agent.id}#chat`);
                            }}
                        />
                    ))}
                </div>
            )}
        </div>
    );
}
