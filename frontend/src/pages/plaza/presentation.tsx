export const Icons = {
    post: (
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="M13 2H3a1 1 0 00-1 1v8a1 1 0 001 1h3l2 2 2-2h3a1 1 0 001-1V3a1 1 0 00-1-1z" />
        </svg>
    ),
    comment: (
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="M2 4a2 2 0 012-2h8a2 2 0 012 2v5a2 2 0 01-2 2H8l-3 3V11H4a2 2 0 01-2-2V4z" />
        </svg>
    ),
    heart: (
        <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="M8 13.7C8 13.7 1.5 9.5 1.5 5.5C1.5 3.5 3 2 5 2C6.2 2 7.3 2.6 8 3.5C8.7 2.6 9.8 2 11 2C13 2 14.5 3.5 14.5 5.5C14.5 9.5 8 13.7 8 13.7Z" />
        </svg>
    ),
    heartFilled: (
        <svg width="15" height="15" viewBox="0 0 16 16" fill="currentColor" stroke="currentColor" strokeWidth="1" strokeLinecap="round" strokeLinejoin="round">
            <path d="M8 13.7C8 13.7 1.5 9.5 1.5 5.5C1.5 3.5 3 2 5 2C6.2 2 7.3 2.6 8 3.5C8.7 2.6 9.8 2 11 2C13 2 14.5 3.5 14.5 5.5C14.5 9.5 8 13.7 8 13.7Z" />
        </svg>
    ),
    fire: (
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="M8.5 1.5C8.5 1.5 12.5 5 12.5 9a4.5 4.5 0 01-9 0c0-2 1-3.5 2-4.5 0 0 .5 2 2 2.5C8 7 8.5 1.5 8.5 1.5z" />
        </svg>
    ),
    trophy: (
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="M5 14h6M8 11v3M4 2h8v3a4 4 0 01-8 0V2z" />
            <path d="M4 3H2.5a1 1 0 00-1 1v1a2 2 0 002 2H4M12 3h1.5a1 1 0 011 1v1a2 2 0 01-2 2H12" />
        </svg>
    ),
    hash: (
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="M3 6h10M3 10h10M6.5 2.5l-1 11M10.5 2.5l-1 11" />
        </svg>
    ),
    info: (
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="8" cy="8" r="6" />
            <path d="M8 7v4M8 5.5v0" />
        </svg>
    ),
    send: (
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="M14.5 1.5l-6 13-2.5-5.5L.5 6.5l14-5z" />
            <path d="M14.5 1.5L6 9" />
        </svg>
    ),
    bot: (
        <svg width="14" height="14" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round">
            <rect x="3" y="5" width="12" height="10" rx="2" />
            <circle cx="7" cy="10" r="1" fill="currentColor" stroke="none" />
            <circle cx="11" cy="10" r="1" fill="currentColor" stroke="none" />
            <path d="M9 2v3M6 2h6" />
        </svg>
    ),
    dot: (
        <svg width="6" height="6" viewBox="0 0 6 6">
            <circle cx="3" cy="3" r="3" fill="currentColor" />
        </svg>
    ),
    trash: (
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="M3 4h10M6 4V3a1 1 0 011-1h2a1 1 0 011 1v1M13 4v9a2 2 0 01-2 2H5a2 2 0 01-2-2V4" />
        </svg>
    ),
};

const linkifyContent = (text: string) => {
    const parts = text.split(/(https?:\/\/[^\s<>"'()\uff0c\u3002\uff01\uff1f\u3001\uff1b\uff1a]+|#[\w\u4e00-\u9fff]+|@\S+)/g);
    if (parts.length <= 1) return text;
    return parts.map((part, i) => {
        if (i % 2 === 1) {
            if (part.startsWith('#')) {
                return (
                    <span key={i} style={{ color: 'var(--accent-primary)', fontWeight: 500 }}>{part}</span>
                );
            }
            if (part.startsWith('@')) {
                return (
                    <span key={i} style={{ color: 'var(--accent-primary)', fontWeight: 600, cursor: 'default' }}>{part}</span>
                );
            }
            return (
                <a key={i} href={part} target="_blank" rel="noopener noreferrer"
                    style={{ color: 'var(--accent-primary)', textDecoration: 'none', wordBreak: 'break-all' }}
                    onMouseOver={e => (e.currentTarget.style.textDecoration = 'underline')}
                    onMouseOut={e => (e.currentTarget.style.textDecoration = 'none')}
                >{part.length > 60 ? part.substring(0, 57) + '...' : part}</a>
            );
        }
        return part;
    });
};

export const renderContent = (text: string) => {
    const elements: any[] = [];
    const lines = text.split('\n');
    lines.forEach((line, li) => {
        const parts = line.split(/(\*\*[^*]+\*\*|`[^`]+`)/g);
        parts.forEach((part, pi) => {
            if (part.startsWith('**') && part.endsWith('**')) {
                elements.push(<strong key={`${li}-${pi}`}>{part.slice(2, -2)}</strong>);
            } else if (part.startsWith('`') && part.endsWith('`')) {
                elements.push(
                    <code key={`${li}-${pi}`} style={{
                        background: 'var(--bg-tertiary)', padding: '1px 5px',
                        borderRadius: 'var(--radius-sm)', fontSize: 'var(--text-xs)',
                        fontFamily: 'var(--font-mono)',
                    }}>{part.slice(1, -1)}</code>
                );
            } else {
                const linked = linkifyContent(part);
                if (Array.isArray(linked)) {
                    elements.push(...linked.map((el, ei) =>
                        typeof el === 'string' ? <span key={`${li}-${pi}-${ei}`}>{el}</span> : el
                    ));
                } else {
                    elements.push(<span key={`${li}-${pi}`}>{linked}</span>);
                }
            }
        });
        if (li < lines.length - 1) elements.push(<br key={`br-${li}`} />);
    });
    return elements;
};

export function Avatar({ name, isAgent, size = 32 }: { name: string; isAgent: boolean; size?: number }) {
    return (
        <div style={{
            width: size, height: size, borderRadius: 'var(--radius-md)',
            background: 'var(--bg-tertiary)', border: '1px solid var(--border-subtle)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            color: 'var(--text-tertiary)', flexShrink: 0,
            fontSize: isAgent ? `${size * 0.45}px` : `${size * 0.4}px`,
            fontWeight: 600,
        }}>
            {isAgent ? Icons.bot : name[0]?.toUpperCase()}
        </div>
    );
}

export function ActionBtn({ icon, label, active, onClick }: {
    icon: React.ReactNode; label: string | number; active?: boolean; onClick?: () => void;
}) {
    return (
        <button
            onClick={onClick}
            style={{
                background: 'none', border: 'none', cursor: 'pointer',
                fontSize: 'var(--text-xs)', color: active ? 'var(--error)' : 'var(--text-tertiary)',
                display: 'flex', alignItems: 'center', gap: '4px',
                padding: '4px 8px', borderRadius: 'var(--radius-sm)',
                transition: 'all var(--transition-fast)',
            }}
            onMouseOver={e => { e.currentTarget.style.background = 'var(--bg-hover)'; e.currentTarget.style.color = active ? 'var(--error)' : 'var(--text-secondary)'; }}
            onMouseOut={e => { e.currentTarget.style.background = 'none'; e.currentTarget.style.color = active ? 'var(--error)' : 'var(--text-tertiary)'; }}
        >
            <span style={{ display: 'flex' }}>{icon}</span> {label}
        </button>
    );
}

export function SidebarSection({ icon, title, children }: { icon: React.ReactNode; title: string; children: React.ReactNode }) {
    return (
        <div style={{
            border: '1px solid var(--border-subtle)',
            borderRadius: 'var(--radius-lg)', overflow: 'hidden',
        }}>
            <div style={{
                padding: '10px 14px', borderBottom: '1px solid var(--border-subtle)',
                display: 'flex', alignItems: 'center', gap: '6px',
                fontSize: 'var(--text-xs)', fontWeight: 500,
                color: 'var(--text-secondary)',
            }}>
                <span style={{ display: 'flex', opacity: 0.6 }}>{icon}</span>
                {title}
            </div>
            <div style={{ padding: '10px 14px' }}>
                {children}
            </div>
        </div>
    );
}

export const styles = `
    .delete-btn { opacity: 0.6; color: var(--text-muted); background: none; border: none; cursor: pointer; font-size: 12px; padding: 4px 8px; border-radius: var(--radius-sm); display: flex; align-items: center; }
    .delete-btn:hover { opacity: 1; color: #ef4444; background: var(--bg-hover); }
`;
