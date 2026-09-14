import { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { IconAlertTriangle, IconLock } from '@tabler/icons-react';
import { fetchJson } from '../services/api';
import { buildLoginUrl, safeLoginReturnTo } from '../utils/loginReturn';

type AccessCheck = {
    allowed: boolean;
    title: string;
    access_mode: 'public' | 'authenticated' | 'restricted';
    request_pending: boolean;
};

export default function PublishedPageAccess() {
    const [params] = useSearchParams();
    const shortId = params.get('short_id') || '';
    const returnTo = safeLoginReturnTo(params.get('return_to')) || (shortId ? `/p/${shortId}` : '/');
    const loginUrl = buildLoginUrl(window.location.href);
    const [access, setAccess] = useState<AccessCheck | null>(null);
    const [error, setError] = useState('');
    const [requesting, setRequesting] = useState(false);

    useEffect(() => {
        const token = localStorage.getItem('token');
        if (!token) {
            window.location.replace(loginUrl);
            return;
        }
        fetchJson<AccessCheck>('/pages/session', {
            method: 'POST', body: JSON.stringify({ short_id: shortId }),
        }).then(result => {
            if (result.allowed) window.location.replace(returnTo);
            else setAccess(result);
        }).catch((e: any) => {
            if (e.status === 404 || e.status === 410) {
                window.location.replace('/published-page-unavailable');
                return;
            }
            setError(e.message || '暂时无法验证访问权限');
        });
    }, [loginUrl, returnTo, shortId]);

    const requestAccess = async () => {
        setRequesting(true); setError('');
        try {
            await fetchJson(`/pages/${shortId}/request-access`, { method: 'POST' });
            setAccess(current => current ? { ...current, request_pending: true } : current);
        } catch (e: any) { setError(e.message || '申请失败'); }
        finally { setRequesting(false); }
    };

    if (!access && !error) return <main style={shell}><IconLock size={28} /><p>正在验证访问权限…</p></main>;
    return <main style={shell}>
        <IconAlertTriangle size={32} color="var(--warning, #d97706)" />
        <h1 style={{ fontSize: 21, margin: '14px 0 4px' }}>无权访问此页面</h1>
        <p style={{ color: 'var(--text-tertiary)', fontSize: 14, lineHeight: 1.6 }}>{access?.title || '该页面'} 仅对指定人员开放。</p>
        {access?.access_mode === 'restricted' && <button className="btn btn-primary" style={{ marginTop: 20 }} disabled={requesting || access.request_pending} onClick={requestAccess}>
            {access.request_pending ? '已申请，请等待发布者处理' : requesting ? '正在申请…' : '申请访问权限'}
        </button>}
        {error && <p style={{ color: 'var(--error)', fontSize: 13, marginTop: 16 }}>{error}</p>}
    </main>;
}

const shell: React.CSSProperties = {
    minHeight: '100vh', display: 'flex', flexDirection: 'column', alignItems: 'center',
    justifyContent: 'center', textAlign: 'center', padding: 24, background: 'var(--bg-primary)', color: 'var(--text-primary)',
};
