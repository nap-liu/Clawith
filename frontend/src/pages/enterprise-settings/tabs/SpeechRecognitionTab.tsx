import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useToast } from '../../../components/Toast/ToastProvider';
import { fetchJson } from '../utils/fetchJson';

type SpeechConfig = {
    configured: boolean;
    provider: string;
    model: string;
    api_key_masked: string;
    enabled: boolean;
};

type SpeechRecognitionTabProps = {
    selectedTenantId: string;
};

export default function SpeechRecognitionTab({ selectedTenantId }: SpeechRecognitionTabProps) {
    const toast = useToast();
    const queryClient = useQueryClient();
    const querySuffix = selectedTenantId ? `?tenant_id=${encodeURIComponent(selectedTenantId)}` : '';
    const [apiKey, setApiKey] = useState('');
    const [enabled, setEnabled] = useState(true);

    const configQuery = useQuery({
        queryKey: ['speech-recognition-config', selectedTenantId],
        queryFn: () => fetchJson<SpeechConfig>(`/speech-config${querySuffix}`),
    });

    useEffect(() => {
        if (configQuery.data) setEnabled(configQuery.data.configured ? configQuery.data.enabled : true);
    }, [configQuery.data]);

    const saveConfig = useMutation({
        mutationFn: () => fetchJson<SpeechConfig>(`/speech-config${querySuffix}`, {
            method: 'PUT',
            body: JSON.stringify({
                provider: 'aliyun_dashscope',
                model: 'fun-asr-realtime',
                api_key: apiKey || null,
                enabled,
            }),
        }),
        onSuccess: (next) => {
            setApiKey('');
            queryClient.setQueryData(['speech-recognition-config', selectedTenantId], next);
            toast.success('语音识别配置已保存');
        },
        onError: (error: any) => {
            toast.error('语音识别配置保存失败', { details: String(error?.message || error) });
        },
    });

    const testConfig = useMutation({
        mutationFn: () => fetchJson<{ success: boolean }>(`/speech-config/test${querySuffix}`, { method: 'POST' }),
        onSuccess: () => toast.success('阿里云语音识别连接测试成功'),
        onError: (error: any) => {
            toast.error('语音识别连接测试失败', { details: String(error?.message || error) });
        },
    });

    const config = configQuery.data;
    const busy = saveConfig.isPending || testConfig.isPending;

    return (
        <div className="card" style={{ maxWidth: '760px' }}>
            <h3 style={{ marginBottom: '6px' }}>中文语音识别</h3>
            <p style={{ margin: '0 0 20px', color: 'var(--text-tertiary)', fontSize: '13px', lineHeight: 1.65 }}>
                这是独立的语音服务配置，不会读取或复用模型池中的模型及 API Key。当前使用阿里云百炼 Fun-ASR 实时中文识别。
            </p>

            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '14px' }}>
                <div className="form-group">
                    <label className="form-label">服务商</label>
                    <select className="form-input" value="aliyun_dashscope" disabled>
                        <option value="aliyun_dashscope">阿里云百炼（DashScope）</option>
                    </select>
                </div>
                <div className="form-group">
                    <label className="form-label">识别模型</label>
                    <select className="form-input" value="fun-asr-realtime" disabled>
                        <option value="fun-asr-realtime">fun-asr-realtime</option>
                    </select>
                </div>
                <div className="form-group" style={{ gridColumn: '1 / -1' }}>
                    <label className="form-label">语音服务 API Key</label>
                    <input
                        className="form-input"
                        type="password"
                        autoComplete="new-password"
                        value={apiKey}
                        onChange={(event) => setApiKey(event.target.value)}
                        placeholder={config?.configured ? `已保存 ${config.api_key_masked}，留空表示不修改` : '请输入阿里云百炼 API Key'}
                    />
                    <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '5px' }}>
                        API Key 将独立加密保存，不会传给浏览器，也不会与大模型配置共享。
                    </div>
                </div>
                <label style={{ gridColumn: '1 / -1', display: 'flex', alignItems: 'center', gap: '8px', fontSize: '13px' }}>
                    <input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} />
                    启用 H5 中文语音输入
                </label>
            </div>

            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '8px', marginTop: '20px' }}>
                <button
                    className="btn btn-secondary"
                    disabled={!config?.configured || !config.enabled || busy}
                    onClick={() => testConfig.mutate()}
                >
                    {testConfig.isPending ? '测试中…' : '测试连接'}
                </button>
                <button
                    className="btn btn-primary"
                    disabled={busy || (!config?.configured && !apiKey.trim())}
                    onClick={() => saveConfig.mutate()}
                >
                    {saveConfig.isPending ? '保存中…' : '保存配置'}
                </button>
            </div>
        </div>
    );
}
