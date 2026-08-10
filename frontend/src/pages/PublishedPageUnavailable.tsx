import { useEffect } from 'react';
import { IconFileOff } from '@tabler/icons-react';

export default function PublishedPageUnavailable() {
    useEffect(() => {
        document.title = '页面已失效';
    }, []);

    return (
        <main style={shell}>
            <div style={iconFrame}><IconFileOff size={30} stroke={1.7} aria-hidden="true" /></div>
            <h1 style={{ margin: '18px 0 6px', fontSize: 22, fontWeight: 650 }}>页面已失效</h1>
            <p style={{ margin: 0, color: 'var(--text-tertiary)', fontSize: 14, lineHeight: 1.7 }}>
                该页面已被删除或源文件已不存在，请联系发布者重新发布。
            </p>
            <a className="btn btn-secondary" href="/" style={{ marginTop: 22 }}>返回首页</a>
        </main>
    );
}

const shell: React.CSSProperties = {
    minHeight: '100vh',
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'center',
    justifyContent: 'center',
    padding: 24,
    textAlign: 'center',
    color: 'var(--text-primary)',
    background: 'var(--bg-primary)',
};

const iconFrame: React.CSSProperties = {
    width: 58,
    height: 58,
    display: 'grid',
    placeItems: 'center',
    border: '1px solid var(--border-default)',
    borderRadius: 16,
    color: 'var(--text-secondary)',
    background: 'var(--bg-secondary)',
};
