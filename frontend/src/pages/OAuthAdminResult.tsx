import { useEffect } from 'react';
import { useSearchParams } from 'react-router-dom';

export default function OAuthAdminResult() {
    const [params] = useSearchParams();
    const provider = params.get('provider') || '';
    const success = params.get('status') === 'success';

    useEffect(() => {
        if (!success || provider !== 'google_workspace') return;
        window.opener?.postMessage({ type: 'google-workspace-sync-authorized' }, window.location.origin);
        window.close();
    }, [provider, success]);

    return (
        <main style={{ minHeight: '100vh', display: 'grid', placeItems: 'center', padding: 24 }}>
            <section style={{ textAlign: 'center', maxWidth: 420 }}>
                <h1 style={{ fontSize: 20 }}>{success ? '授权成功' : '授权失败'}</h1>
                <p style={{ color: 'var(--text-secondary)', fontSize: 14 }}>
                    {success ? '授权已完成，可以关闭此窗口。' : '未能完成授权，请关闭窗口后重试。'}
                </p>
            </section>
        </main>
    );
}
