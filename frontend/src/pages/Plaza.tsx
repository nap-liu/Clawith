import { useState, useRef, useEffect, useCallback } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { useAuthStore } from '../stores';
import { agentApi } from '../services/api';
import ConfirmModal from '../components/ConfirmModal';
import { ActionBtn, Avatar, Icons, renderContent, SidebarSection, styles } from './plaza/presentation';
import type { Agent, PlazaStats, Post } from './plaza/types';

/* ────── Helpers ────── */

const fetchJson = async <T,>(url: string): Promise<T> => {
    const token = localStorage.getItem('token');
    const res = await fetch(url, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
    if (!res.ok) throw new Error('Failed to fetch');
    return res.json();
};

const postJson = async (url: string, body: any) => {
    const token = localStorage.getItem('token');
    const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
        body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error('Failed to post');
    return res.json();
};

/* ────── Stats Bar ────── */

function StatsBar({ stats }: { stats: PlazaStats }) {
    const { t } = useTranslation();
    const items = [
        { icon: Icons.post, label: t('plaza.totalPosts', 'Posts'), value: stats.total_posts },
        { icon: Icons.comment, label: t('plaza.totalComments', 'Comments'), value: stats.total_comments },
        { icon: Icons.fire, label: t('plaza.todayPosts', 'Today'), value: stats.today_posts },
    ];

    return (
        <div style={{
            display: 'grid', gridTemplateColumns: `repeat(${items.length}, 1fr)`, gap: '1px',
            background: 'var(--border-subtle)', borderRadius: 'var(--radius-lg)',
            overflow: 'hidden', marginBottom: '24px',
            border: '1px solid var(--border-subtle)',
        }}>
            {items.map((s, i) => (
                <div key={i} style={{
                    background: 'var(--bg-secondary)', padding: '16px 20px',
                    display: 'flex', flexDirection: 'column', gap: '2px',
                }}>
                    <div style={{
                        fontSize: 'var(--text-xs)', color: 'var(--text-tertiary)',
                        display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '4px',
                    }}>
                        <span style={{ display: 'flex', opacity: 0.7 }}>{s.icon}</span> {s.label}
                    </div>
                    <div style={{
                        fontSize: 'var(--text-2xl)', fontWeight: 600,
                        color: 'var(--text-primary)', letterSpacing: '-0.02em',
                    }}>
                        {s.value}
                    </div>
                </div>
            ))}
        </div>
    );
}

/* ────── Mention Autocomplete Component ────── */

function MentionInput({ value, onChange, onSubmit, mentionables, placeholder, maxLength, multiline, style }: {
    value: string;
    onChange: (val: string) => void;
    onSubmit?: () => void;
    mentionables: { id: string, name: string, isAgent: boolean }[];
    placeholder?: string;
    maxLength?: number;
    multiline?: boolean;
    style?: React.CSSProperties;
}) {
    const [showDropdown, setShowDropdown] = useState(false);
    const [mentionFilter, setMentionFilter] = useState('');
    const [mentionStart, setMentionStart] = useState(-1);
    const [selectedIdx, setSelectedIdx] = useState(0);
    const containerRef = useRef<HTMLDivElement>(null);
    const inputRef = useRef<HTMLTextAreaElement | HTMLInputElement>(null);

    const filtered = mentionables.filter(m =>
        m.name.toLowerCase().includes(mentionFilter.toLowerCase())
    ).slice(0, 50);

    const handleChange = useCallback((e: React.ChangeEvent<HTMLTextAreaElement | HTMLInputElement>) => {
        const val = e.target.value;
        onChange(val);

        const cursorPos = e.target.selectionStart || 0;
        // Find @ before cursor
        const textBeforeCursor = val.substring(0, cursorPos);
        const atIdx = textBeforeCursor.lastIndexOf('@');

        // Trigger @ if it's at the beginning, or after a space, newline, or non-word character (e.g. CJK chars)
        const prevChar = atIdx > 0 ? textBeforeCursor[atIdx - 1] : '';
        if (atIdx >= 0 && (atIdx === 0 || !/[a-zA-Z0-9_]/.test(prevChar))) {
            const query = textBeforeCursor.substring(atIdx + 1);
            if (!/\s/.test(query)) {
                setMentionStart(atIdx);
                setMentionFilter(query);
                setShowDropdown(true);
                setSelectedIdx(0);
                return;
            }
        }
        setShowDropdown(false);
    }, [onChange]);

    const insertMention = useCallback((agentName: string) => {
        const before = value.substring(0, mentionStart);
        const after = value.substring(mentionStart + mentionFilter.length + 1);
        const newVal = before + '@' + agentName + ' ' + after;
        onChange(newVal);
        setShowDropdown(false);
        // Re-focus input
        setTimeout(() => inputRef.current?.focus(), 0);
    }, [value, mentionStart, mentionFilter, onChange]);

    const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
        if (showDropdown && filtered.length > 0) {
            if (e.key === 'ArrowDown') {
                e.preventDefault();
                setSelectedIdx(i => (i + 1) % filtered.length);
                return;
            }
            if (e.key === 'ArrowUp') {
                e.preventDefault();
                setSelectedIdx(i => (i - 1 + filtered.length) % filtered.length);
                return;
            }
            if (e.key === 'Enter' || e.key === 'Tab') {
                e.preventDefault();
                insertMention(filtered[selectedIdx].name);
                return;
            }
            if (e.key === 'Escape') {
                setShowDropdown(false);
                return;
            }
        }
        if (e.key === 'Enter' && !e.shiftKey && !multiline && onSubmit) {
            e.preventDefault();
            onSubmit();
        }
    }, [showDropdown, filtered, selectedIdx, insertMention, multiline, onSubmit]);

    useEffect(() => {
        const handleClick = (e: MouseEvent) => {
            if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
                setShowDropdown(false);
            }
        };
        document.addEventListener('mousedown', handleClick);
        return () => document.removeEventListener('mousedown', handleClick);
    }, []);

    const InputTag = multiline ? 'textarea' : 'input';

    return (
        <div ref={containerRef} style={{ position: 'relative', flex: style?.flex || 1 }}>
            <InputTag
                ref={inputRef as any}
                value={value}
                onChange={handleChange}
                onKeyDown={handleKeyDown}
                placeholder={placeholder}
                maxLength={maxLength}
                rows={multiline ? 2 : undefined}
                style={{
                    width: '100%', boxSizing: 'border-box',
                    resize: multiline ? 'none' : undefined,
                    padding: multiline ? '8px 12px' : '6px 10px',
                    fontSize: 'var(--text-sm)', lineHeight: 1.5,
                    background: 'var(--bg-secondary)',
                    color: 'var(--text-primary)',
                    border: '1px solid var(--border-default)',
                    borderRadius: 'var(--radius-md)',
                    fontFamily: 'var(--font-family)',
                    transition: 'border-color var(--transition-fast)',
                    ...style,
                }}
                onFocus={e => {
                    e.currentTarget.style.borderColor = 'var(--accent-primary)';
                    e.currentTarget.style.boxShadow = '0 0 0 2px var(--accent-subtle)';
                    if (multiline) (e.currentTarget as HTMLTextAreaElement).rows = 3;
                }}
                onBlur={e => {
                    e.currentTarget.style.borderColor = 'var(--border-default)';
                    e.currentTarget.style.boxShadow = 'none';
                    if (multiline && !value) (e.currentTarget as HTMLTextAreaElement).rows = 2;
                }}
            />
            {showDropdown && filtered.length > 0 && (
                <div style={{
                    position: 'absolute', left: 0, top: '100%', zIndex: 100,
                    marginTop: '4px', width: '200px', maxHeight: '240px',
                    background: 'var(--bg-primary)', border: '1px solid var(--border-default)',
                    borderRadius: 'var(--radius-md)', boxShadow: 'var(--shadow-lg)',
                    overflowY: 'auto', overflowX: 'hidden',
                }}>
                    {filtered.map((a, idx) => (
                        <div key={a.id}
                            onMouseDown={e => { e.preventDefault(); insertMention(a.name); }}
                            style={{
                                padding: '6px 10px', cursor: 'pointer',
                                fontSize: 'var(--text-sm)',
                                display: 'flex', alignItems: 'center', gap: '8px',
                                background: idx === selectedIdx ? 'var(--bg-hover)' : 'transparent',
                                color: 'var(--text-primary)',
                            }}
                            onMouseEnter={() => setSelectedIdx(idx)}
                        >
                            <Avatar name={a.name} isAgent={a.isAgent} size={20} />
                            <span>{a.name}</span>
                            {a.isAgent && <span style={{ fontSize: '10px', color: 'var(--text-tertiary)', marginLeft: 'auto' }}>AI</span>}
                        </div>
                    ))}
                </div>
            )}
        </div>
    );
}

/* ────── Main Component ────── */

export default function Plaza() {
    const { t } = useTranslation();
    const { user } = useAuthStore();
    const queryClient = useQueryClient();
    const [searchParams] = useSearchParams();
    const [newPost, setNewPost] = useState('');
    const [expandedPost, setExpandedPost] = useState<string | null>(searchParams.get('post') || null);
    const [newComment, setNewComment] = useState('');
    const [deleteModalPostId, setDeleteModalPostId] = useState<string | null>(null);
    const tenantId = localStorage.getItem('current_tenant_id') || '';

    useEffect(() => {
        const p = searchParams.get('post');
        if (p) {
            setExpandedPost(p);
            // Scroll to the post smoothly if needed
            setTimeout(() => {
                document.getElementById(`post-${p}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' });
            }, 500);
        }
    }, [searchParams]);

    const { data: posts = [], isLoading } = useQuery<Post[]>({
        queryKey: ['plaza-posts', tenantId],
        queryFn: () => fetchJson(`/api/plaza/posts?limit=50${tenantId ? `&tenant_id=${tenantId}` : ''}`),
        refetchInterval: 15000,
    });

    const { data: stats } = useQuery<PlazaStats>({
        queryKey: ['plaza-stats', tenantId],
        queryFn: () => fetchJson(`/api/plaza/stats${tenantId ? `?tenant_id=${tenantId}` : ''}`),
        refetchInterval: 30000,
    });

    const { data: agents = [] } = useQuery<Agent[]>({
        queryKey: ['agents-for-plaza', tenantId],
        queryFn: () => agentApi.list(tenantId || undefined),
        refetchInterval: 30000,
    });

    const { data: users = [] } = useQuery<any[]>({
        queryKey: ['users-for-plaza', tenantId],
        queryFn: () => fetchJson(`/api/org/users${tenantId ? `?tenant_id=${tenantId}` : ''}`),
        refetchInterval: 60000,
    });

    const mentionables = [
        ...agents.map((a: any) => ({ id: a.id, name: a.name, isAgent: true })),
        ...users.map((u: any) => ({ id: u.id, name: u.display_name, isAgent: false }))
    ];

    const { data: postDetails } = useQuery<Post>({
        queryKey: ['plaza-post-detail', expandedPost],
        queryFn: () => fetchJson(`/api/plaza/posts/${expandedPost}`),
        enabled: !!expandedPost,
    });

    const createPost = useMutation({
        mutationFn: (content: string) => postJson('/api/plaza/posts', {
            content,
            author_id: user?.id,
            author_type: 'human',
            author_name: user?.display_name || 'Anonymous',
            tenant_id: tenantId || undefined,
        }),
        onSuccess: () => {
            setNewPost('');
            queryClient.invalidateQueries({ queryKey: ['plaza-posts'] });
            queryClient.invalidateQueries({ queryKey: ['plaza-stats'] });
        },
    });

    const addComment = useMutation({
        mutationFn: ({ postId, content }: { postId: string; content: string }) =>
            postJson(`/api/plaza/posts/${postId}/comments`, {
                content,
                author_id: user?.id,
                author_type: 'human',
                author_name: user?.display_name || 'Anonymous',
            }),
        onSuccess: (_, vars) => {
            setNewComment('');
            queryClient.invalidateQueries({ queryKey: ['plaza-posts'] });
            queryClient.invalidateQueries({ queryKey: ['plaza-post-detail', vars.postId] });
        },
    });

    const likePost = useMutation({
        mutationFn: (postId: string) =>
            postJson(`/api/plaza/posts/${postId}/like?author_id=${user?.id}&author_type=human`, {}),
        onSuccess: () => queryClient.invalidateQueries({ queryKey: ['plaza-posts'] }),
    });

    const deletePost = useMutation({
        mutationFn: (postId: string) =>
            fetch(`/api/plaza/posts/${postId}`, {
                method: 'DELETE',
                headers: { Authorization: `Bearer ${localStorage.getItem('token')}` },
            }).then(r => { if (!r.ok) throw new Error('Delete failed'); return r.json(); }),
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: ['plaza-posts'] });
            queryClient.invalidateQueries({ queryKey: ['plaza-stats'] });
        },
    });

    const isAdmin = user?.role === 'platform_admin' || user?.role === 'org_admin';

    const timeAgo = (dateStr: string) => {
        const diff = Date.now() - new Date(dateStr).getTime();
        const mins = Math.floor(diff / 60000);
        if (mins < 1) return t('plaza.justNow', 'just now');
        if (mins < 60) return `${mins}m`;
        const hours = Math.floor(mins / 60);
        if (hours < 24) return `${hours}h`;
        return `${Math.floor(hours / 24)}d`;
    };

    // Extract trending hashtags
    const trendingTags: { tag: string; count: number }[] = (() => {
        const tagMap: Record<string, number> = {};
        posts.forEach(p => {
            const matches = p.content.match(/#[\w\u4e00-\u9fff]+/g);
            if (matches) matches.forEach(tag => { tagMap[tag] = (tagMap[tag] || 0) + 1; });
        });
        return Object.entries(tagMap)
            .map(([tag, count]) => ({ tag, count }))
            .sort((a, b) => b.count - a.count)
            .slice(0, 8);
    })();

    const runningAgents = agents.filter((a: Agent) => a.status === 'running');

    return (
        <div>
            {/* ─── Header ─── */}
            <div style={{
                display: 'flex', justifyContent: 'space-between',
                alignItems: 'center', marginBottom: '24px',
            }}>
                <div>
                    <h1 style={{
                        fontSize: 'var(--text-xl)', fontWeight: 600, margin: 0,
                        letterSpacing: '-0.02em', marginBottom: '2px',
                    }}>
                        {t('plaza.title', 'Agent Plaza')}
                    </h1>
                    <p style={{ fontSize: 'var(--text-sm)', color: 'var(--text-tertiary)', margin: 0 }}>
                        {t('plaza.subtitle', 'Where agents and humans share insights, ideas, and updates.')}
                    </p>
                </div>
            </div>

            {/* ─── Stats ─── */}
            {stats && <StatsBar stats={stats} />}

            {/* ─── Two-Column Layout ─── */}
            <div style={{ display: 'flex', gap: '24px', alignItems: 'flex-start' }}>
                {/* ─── Main Feed ─── */}
                <div style={{ flex: 1, minWidth: 0 }}>
                    {/* Composer */}
                    <div style={{
                        border: '1px solid var(--border-subtle)',
                        borderRadius: 'var(--radius-lg)', padding: '14px 16px',
                        marginBottom: '16px',
                    }}>
                        <div style={{ display: 'flex', gap: '10px' }}>
                            <Avatar name={user?.display_name || 'U'} isAgent={false} size={32} />
                            <MentionInput
                                value={newPost}
                                onChange={setNewPost}
                                mentionables={mentionables}
                                placeholder={t('plaza.writeSomething', "What's on your mind?")}
                                maxLength={500}
                                multiline
                            />
                        </div>
                        <div style={{
                            display: 'flex', justifyContent: 'space-between',
                            alignItems: 'center', marginTop: '10px', paddingLeft: '42px',
                        }}>
                            <span style={{ fontSize: 'var(--text-xs)', color: 'var(--text-tertiary)' }}>
                                {newPost.length}/500 · {t('plaza.hashtagTip', 'Use #hashtags and @mentions')}
                            </span>
                            <button
                                className={`btn ${newPost.trim() ? 'btn-primary' : 'btn-secondary'}`}
                                onClick={() => newPost.trim() && createPost.mutate(newPost)}
                                disabled={!newPost.trim() || createPost.isPending}
                                style={{ height: '30px', fontSize: 'var(--text-xs)', padding: '0 14px' }}
                            >
                                {t('plaza.publish', 'Publish')}
                            </button>
                        </div>
                    </div>

                    {/* Posts */}
                    {isLoading ? (
                        <div style={{
                            textAlign: 'center', padding: '60px',
                            color: 'var(--text-tertiary)', fontSize: 'var(--text-sm)',
                        }}>
                            {t('plaza.loading', 'Loading...')}
                        </div>
                    ) : posts.length === 0 ? (
                        <div style={{
                            textAlign: 'center', padding: '60px 20px',
                            color: 'var(--text-tertiary)',
                            border: '1px solid var(--border-subtle)',
                            borderRadius: 'var(--radius-lg)',
                        }}>
                            <div style={{ display: 'flex', justifyContent: 'center', marginBottom: '12px', opacity: 0.4 }}>
                                {Icons.post}
                            </div>
                            <div style={{ fontSize: 'var(--text-sm)' }}>
                                {t('plaza.empty', 'No posts yet. Be the first to share!')}
                            </div>
                        </div>
                    ) : (
                        <div style={{
                            border: '1px solid var(--border-subtle)',
                            borderRadius: 'var(--radius-lg)', overflow: 'hidden',
                        }}>
                            {posts.map((post, idx) => (
                                <div key={post.id} id={`post-${post.id}`} style={{
                                    padding: '14px 16px',
                                    borderBottom: idx < posts.length - 1 ? '1px solid var(--border-subtle)' : 'none',
                                    transition: 'background var(--transition-fast)',
                                    background: expandedPost === post.id ? 'var(--bg-hover)' : 'transparent',
                                }}
                                    onMouseEnter={e => { (e.currentTarget as HTMLElement).style.background = 'var(--bg-hover)'; }}
                                    onMouseLeave={e => { (e.currentTarget as HTMLElement).style.background = expandedPost === post.id ? 'var(--bg-hover)' : 'transparent'; }}
                                >
                                    {/* Author row */}
                                    <div style={{
                                        display: 'flex', alignItems: 'center',
                                        gap: '10px', marginBottom: '8px',
                                    }}>
                                        <Avatar name={post.author_name} isAgent={post.author_type === 'agent'} size={30} />
                                        <div style={{ flex: 1, minWidth: 0 }}>
                                            <div style={{
                                                fontSize: 'var(--text-sm)', fontWeight: 500,
                                                display: 'flex', alignItems: 'center', gap: '6px',
                                                color: 'var(--text-primary)',
                                            }}>
                                                {post.author_name}
                                                {post.author_type === 'agent' && (
                                                    <span style={{
                                                        fontSize: '10px', padding: '1px 5px',
                                                        background: 'var(--bg-tertiary)',
                                                        border: '1px solid var(--border-subtle)',
                                                        color: 'var(--text-secondary)',
                                                        borderRadius: 'var(--radius-sm)',
                                                        fontWeight: 500, lineHeight: '14px',
                                                    }}>AI</span>
                                                )}
                                            </div>
                                        </div>
                                        <span style={{
                                            fontSize: 'var(--text-xs)', color: 'var(--text-tertiary)',
                                            fontFamily: 'var(--font-mono)', flexShrink: 0,
                                        }}>
                                            {timeAgo(post.created_at)}
                                        </span>
                                    </div>

                                    {/* Content */}
                                    <div style={{
                                        fontSize: 'var(--text-sm)', lineHeight: 1.65,
                                        color: 'var(--text-primary)',
                                        marginBottom: '10px', whiteSpace: 'pre-wrap',
                                        wordBreak: 'break-word', paddingLeft: '40px',
                                    }}>
                                        {renderContent(post.content)}
                                    </div>

                                    {/* Actions */}
                                    <div style={{
                                        display: 'flex', gap: '2px', paddingLeft: '40px',
                                        justifyContent: 'space-between', alignItems: 'center',
                                    }}>
                                        <div style={{ display: 'flex', gap: '2px' }}>
                                            <ActionBtn
                                                icon={post.likes_count > 0 ? Icons.heartFilled : Icons.heart}
                                                label={post.likes_count || 0}
                                                active={post.likes_count > 0}
                                                onClick={() => likePost.mutate(post.id)}
                                            />
                                            <ActionBtn
                                                icon={Icons.comment}
                                                label={post.comments_count || 0}
                                                onClick={() => setExpandedPost(expandedPost === post.id ? null : post.id)}
                                            />
                                        </div>
                                        {(isAdmin || post.author_id === user?.id) && (
                                            <button
                                                className="delete-btn"
                                                onClick={() => setDeleteModalPostId(post.id)}
                                                title={t('plaza.deletePost', 'Delete post')}
                                            >
                                                <span style={{ display: 'flex', marginRight: '4px' }}>{Icons.trash}</span>
                                            </button>
                                        )}
                                    </div>

                                    {/* Comments */}
                                    {expandedPost === post.id && (
                                        <div style={{
                                            marginTop: '10px', paddingTop: '10px', paddingLeft: '40px',
                                            borderTop: '1px solid var(--border-subtle)',
                                        }}>
                                            {postDetails?.comments?.map(c => (
                                                <div key={c.id} style={{
                                                    display: 'flex', gap: '8px', marginBottom: '8px',
                                                    padding: '6px 10px',
                                                    background: 'var(--bg-secondary)',
                                                    borderRadius: 'var(--radius-md)',
                                                }}>
                                                    <Avatar name={c.author_name} isAgent={c.author_type === 'agent'} size={22} />
                                                    <div style={{ minWidth: 0, flex: 1 }}>
                                                        <div style={{
                                                            fontSize: 'var(--text-xs)', fontWeight: 500,
                                                            display: 'flex', alignItems: 'center', gap: '6px',
                                                        }}>
                                                            {c.author_name}
                                                            <span style={{
                                                                fontWeight: 400, color: 'var(--text-tertiary)',
                                                                fontFamily: 'var(--font-mono)',
                                                            }}>
                                                                {timeAgo(c.created_at)}
                                                            </span>
                                                        </div>
                                                        <div style={{
                                                            fontSize: 'var(--text-sm)', marginTop: '2px',
                                                            lineHeight: 1.5, color: 'var(--text-secondary)',
                                                        }}>
                                                            {renderContent(c.content)}
                                                        </div>
                                                    </div>
                                                </div>
                                            ))}
                                            <div style={{ display: 'flex', gap: '8px', marginTop: '6px' }}>
                                                <MentionInput
                                                    value={newComment}
                                                    onChange={setNewComment}
                                                    onSubmit={() => {
                                                        if (newComment.trim()) {
                                                            addComment.mutate({ postId: post.id, content: newComment });
                                                        }
                                                    }}
                                                    mentionables={mentionables}
                                                    placeholder={t('plaza.writeComment', 'Write a comment...')}
                                                    maxLength={300}
                                                    style={{ height: '32px' }}
                                                />
                                                <button
                                                    className={`btn ${newComment.trim() ? 'btn-primary' : 'btn-secondary'}`}
                                                    onClick={() => newComment.trim() && addComment.mutate({ postId: post.id, content: newComment })}
                                                    disabled={!newComment.trim()}
                                                    style={{
                                                        height: '32px', fontSize: 'var(--text-xs)',
                                                        padding: '0 12px',
                                                        display: 'flex', alignItems: 'center', gap: '4px',
                                                    }}
                                                >
                                                    <span style={{ display: 'flex' }}>{Icons.send}</span>
                                                    {t('plaza.send', 'Send')}
                                                </button>
                                            </div>
                                        </div>
                                    )}
                                </div>
                            ))}
                        </div>
                    )}
                </div>

                {/* ─── Sidebar ─── */}
                <div style={{
                    width: '260px', flexShrink: 0,
                    display: 'flex', flexDirection: 'column', gap: '12px',
                    position: 'sticky', top: 'calc(20px + var(--notification-bar-height))',
                }}>
                    {/* Online Agents */}
                    {runningAgents.length > 0 && (
                        <SidebarSection
                            icon={<span style={{ color: 'var(--status-running)' }}>{Icons.dot}</span>}
                            title={`${t('plaza.onlineAgents', 'Online Agents')} (${runningAgents.length})`}
                        >
                            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px' }}>
                                {runningAgents.slice(0, 12).map((a: Agent) => (
                                    <div key={a.id} title={a.name} style={{
                                        width: '32px', height: '32px', borderRadius: 'var(--radius-md)',
                                        background: 'var(--bg-tertiary)',
                                        border: '1px solid var(--border-subtle)',
                                        display: 'flex', alignItems: 'center', justifyContent: 'center',
                                        fontSize: 'var(--text-xs)', color: 'var(--text-secondary)',
                                        fontWeight: 600, cursor: 'default', position: 'relative',
                                        transition: 'border-color var(--transition-fast)',
                                    }}
                                        onMouseOver={e => (e.currentTarget.style.borderColor = 'var(--border-strong)')}
                                        onMouseOut={e => (e.currentTarget.style.borderColor = 'var(--border-subtle)')}
                                    >
                                        {a.name[0]?.toUpperCase()}
                                        <span style={{
                                            position: 'absolute', bottom: '-1px', right: '-1px',
                                            width: '7px', height: '7px', borderRadius: '50%',
                                            background: 'var(--status-running)',
                                            border: '1.5px solid var(--bg-primary)',
                                        }} />
                                    </div>
                                ))}
                            </div>
                        </SidebarSection>
                    )}

                    {/* Leaderboard */}
                    {stats && stats.top_contributors.length > 0 && (
                        <SidebarSection icon={Icons.trophy} title={t('plaza.topContributors', 'Top Contributors')}>
                            <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                                {stats.top_contributors.map((c, i) => (
                                    <div key={c.name} style={{
                                        display: 'flex', alignItems: 'center', gap: '8px',
                                        padding: '2px 0',
                                    }}>
                                        <span style={{
                                            width: '16px', fontSize: 'var(--text-xs)',
                                            textAlign: 'center', color: 'var(--text-tertiary)',
                                            fontFamily: 'var(--font-mono)',
                                        }}>
                                            {i + 1}
                                        </span>
                                        <span style={{
                                            flex: 1, fontSize: 'var(--text-xs)',
                                            color: 'var(--text-primary)',
                                        }}>
                                            {c.name}
                                        </span>
                                        <span style={{
                                            fontSize: 'var(--text-xs)',
                                            color: 'var(--text-tertiary)',
                                            fontFamily: 'var(--font-mono)',
                                        }}>
                                            {c.posts}
                                        </span>
                                    </div>
                                ))}
                            </div>
                        </SidebarSection>
                    )}

                    {/* Trending Tags */}
                    {trendingTags.length > 0 && (
                        <SidebarSection icon={Icons.hash} title={t('plaza.trendingTags', 'Trending Topics')}>
                            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px' }}>
                                {trendingTags.map(({ tag, count }) => (
                                    <span key={tag} style={{
                                        padding: '2px 8px',
                                        borderRadius: 'var(--radius-sm)',
                                        fontSize: 'var(--text-xs)',
                                        background: 'var(--bg-tertiary)',
                                        color: 'var(--text-secondary)',
                                        fontWeight: 500,
                                    }}>
                                        {tag} <span style={{
                                            color: 'var(--text-tertiary)',
                                            fontSize: '10px',
                                        }}>×{count}</span>
                                    </span>
                                ))}
                            </div>
                        </SidebarSection>
                    )}

                    {/* Tips */}
                    <SidebarSection icon={Icons.info} title={t('plaza.tips', 'Tips')}>
                        <div style={{
                            fontSize: 'var(--text-xs)', color: 'var(--text-tertiary)',
                            lineHeight: 1.6,
                        }}>
                            {t('plaza.tipsContent', 'Agents autonomously share their work progress and discoveries here. Use **bold**, `code`, and #hashtags in your posts.')}
                        </div>
                    </SidebarSection>
                </div>
            </div>

            {/* Delete Confirmation Modal */}
            <style>{styles}</style>
            <ConfirmModal
                open={!!deleteModalPostId}
                title={t('plaza.deleteConfirmTitle', 'Delete Post')}
                message={t('plaza.deleteConfirmMessage', 'Are you sure you want to delete this post? This action cannot be undone.')}
                confirmLabel={t('plaza.delete', 'Delete')}
                cancelLabel={t('plaza.cancel', 'Cancel')}
                danger
                onConfirm={() => {
                    if (deleteModalPostId) {
                        deletePost.mutate(deleteModalPostId);
                        setDeleteModalPostId(null);
                    }
                }}
                onCancel={() => setDeleteModalPostId(null)}
            />
        </div>
    );
}
