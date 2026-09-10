import React, { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import { agentApi } from '../services/api';
import LinearCopyButton from '../components/LinearCopyButton';
import AccessPermissionsPanel from './agent-detail/components/AccessPermissionsPanel';
function fetchAuth<T>(url: string, options?: RequestInit): Promise<T> {
    const token = localStorage.getItem('token');
    return fetch(`/api${url}`, {
        ...options,
        headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    }).then(r => r.json());
}

interface OpenClawSettingsProps {
    agent: any;
    agentId: string;
    canManage: boolean;
}

export default function OpenClawSettings({ agent, agentId, canManage }: OpenClawSettingsProps) {
    const { t, i18n } = useTranslation();
    const queryClient = useQueryClient();
    const navigate = useNavigate();
    const isChinese = i18n.language?.startsWith('zh');

    // ─── API Key state ──────────────────────────────────
    const [apiKey, setApiKey] = useState<string | null>(null);
    const [regenerating, setRegenerating] = useState(false);
    const [showConfirm, setShowConfirm] = useState(false);
    // ─── Delete state ───────────────────────────────────
    const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
    const [deleting, setDeleting] = useState(false);

    const hasKey = agent?.has_api_key || false;

    const handleRegenerate = async (autoCopy = false) => {
        if (!canManage) return;
        setRegenerating(true);
        try {
            const result = await fetchAuth<{ api_key: string }>(`/agents/${agentId}/api-key`, { method: 'POST' });
            setApiKey(result.api_key);
            setShowConfirm(false);
            // Refresh agent data so has_api_key updates
            queryClient.invalidateQueries({ queryKey: ['agent', agentId] });
            if (autoCopy) {
                try {
                    await navigator.clipboard.writeText(result.api_key);
                } catch (err) {
                    console.error('Failed to auto-copy to clipboard:', err);
                }
            }
        } catch (e) {
            console.error('Failed to regenerate API key', e);
        } finally {
            setRegenerating(false);
        }
    };

    const handleDelete = async () => {
        if (!canManage) return;
        setDeleting(true);
        try {
            await agentApi.delete(agentId);
            queryClient.invalidateQueries({ queryKey: ['agents'] });
            navigate('/');
        } catch (e) {
            console.error('Failed to delete agent', e);
            setDeleting(false);
        }
    };

    // ─── Permissions state ──────────────────────────────
    const { data: permData } = useQuery({
        queryKey: ['agent-permissions', agentId],
        queryFn: () => fetchAuth<any>(`/agents/${agentId}/permissions`),
        enabled: !!agentId,
    });

    const canEditPermissions = canManage && (permData?.is_owner ?? false);

    return (
        <div>
            <h3 style={{ marginBottom: '16px' }}>{t('agent.settings.title')}</h3>

            {/* ── API Key Management ── */}
            <div className="card" style={{ marginBottom: '12px' }}>
                <h4 style={{ marginBottom: '4px' }}>
                    API Key
                </h4>
                <p style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '12px' }}>
                    {isChinese
                        ? 'OpenClaw 通过此 Key 连接平台。重新生成后旧 Key 将立即失效。'
                        : 'OpenClaw uses this key to connect to the platform. Regenerating will immediately invalidate the old key.'}
                </p>

                {/* API Key Display Logic */}
                {(() => {
                    const activeKey = apiKey || (agent?.api_key_hash?.startsWith('oc-') ? agent.api_key_hash : null);
                    const isLegacyHash = hasKey && !activeKey;

                    if (activeKey) {
                        return (
                            <div style={{
                                display: 'flex', alignItems: 'center', gap: '8px',
                                padding: '10px 14px', background: 'rgba(99,102,241,0.06)',
                                borderRadius: '8px', border: '1px solid var(--accent-primary)',
                            }}>
                                <code style={{
                                    flex: 1, fontSize: '13px', fontFamily: 'monospace',
                                    wordBreak: 'break-all', color: 'var(--text-primary)',
                                }}>
                                    {activeKey}
                                </code>
                                <LinearCopyButton
                                    className="btn btn-secondary"
                                    textToCopy={activeKey}
                                    label="Copy"
                                    copiedLabel="Copied"
                                    style={{ padding: '4px 12px', fontSize: '12px', whiteSpace: 'nowrap', minWidth: '70px', height: 'fit-content' }}
                                />
                                {canManage && <button
                                    className="btn btn-secondary"
                                    onClick={() => setShowConfirm(true)}
                                    style={{ padding: '4px 12px', fontSize: '12px', whiteSpace: 'nowrap' }}
                                >
                                    {isChinese ? '重新生成' : 'Regenerate'}
                                </button>}
                            </div>
                        );
                    }

                    return (
                        <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                            <div style={{
                                flex: 1, padding: '8px 14px', borderRadius: '8px',
                                background: 'var(--bg-elevated)', border: '1px solid var(--border-subtle)',
                                fontFamily: 'monospace', fontSize: '13px', color: 'var(--text-secondary)',
                                letterSpacing: '0.5px',
                            }}>
                                {isLegacyHash
                                    ? (isChinese ? '旧版密钥（已加密隐藏），请重新生成以查看明文' : 'Legacy key (encrypted), please regenerate to view')
                                    : (isChinese ? '未生成' : 'Not generated')}
                            </div>
                            {canManage && <button
                                className="btn btn-secondary"
                                onClick={() => setShowConfirm(true)}
                                style={{ padding: '6px 16px', fontSize: '12px', whiteSpace: 'nowrap' }}
                            >
                                {isLegacyHash
                                    ? (isChinese ? '重新生成' : 'Regenerate')
                                    : (isChinese ? '生成' : 'Generate')}
                            </button>}
                        </div>
                    );
                })()}

                {/* Confirmation dialog */}
                {showConfirm && (
                    <div style={{
                        marginTop: '12px', padding: '14px', borderRadius: '8px',
                        background: hasKey ? 'rgba(255,80,80,0.06)' : 'rgba(99,102,241,0.04)',
                        border: hasKey ? '1px solid rgba(255,80,80,0.2)' : '1px solid var(--border-subtle)',
                    }}>
                        <div style={{ fontSize: '13px', fontWeight: 500, marginBottom: '8px', color: 'var(--text-primary)' }}>
                            {hasKey
                                ? (isChinese ? '确认重新生成 API Key？' : 'Regenerate API Key?')
                                : (isChinese ? '生成 API Key？' : 'Generate API Key?')}
                        </div>
                        <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginBottom: '12px' }}>
                            {hasKey
                                ? (isChinese
                                    ? '当前 Key 将立即失效，所有使用旧 Key 的设备将断开连接。'
                                    : 'The current key will be revoked immediately. All devices using the old key will be disconnected.')
                                : (isChinese
                                    ? '将为此 Agent 生成一个新的 API Key。'
                                    : 'A new API Key will be generated for this agent.')}
                        </div>
                        <div style={{ display: 'flex', gap: '8px', justifyContent: 'flex-end' }}>
                            <button
                                className="btn btn-secondary"
                                onClick={() => setShowConfirm(false)}
                                style={{ padding: '5px 14px', fontSize: '12px' }}
                            >
                                {isChinese ? '取消' : 'Cancel'}
                            </button>
                            <button
                                className="btn btn-primary"
                                onClick={() => handleRegenerate(false)}
                                disabled={!canManage || regenerating}
                                style={{ padding: '5px 14px', fontSize: '12px' }}
                            >
                                {regenerating
                                    ? (isChinese ? '生成中...' : 'Generating...')
                                    : (isChinese ? '确认' : 'Confirm')}
                            </button>
                        </div>
                    </div>
                )}
            </div>

            {/* ── Permissions ── */}
            <AccessPermissionsPanel agentId={agentId} permData={permData}
                canManage={canManage} queryClient={queryClient} />

            {/* ── Danger Zone: Delete Agent ── */}
            {canEditPermissions && (
                <div className="card" style={{
                    marginBottom: '12px',
                    border: '1px solid rgba(255,80,80,0.2)',
                }}>
                    <h4 style={{ marginBottom: '4px', color: 'var(--error)' }}>
                        {isChinese ? '危险操作' : 'Danger Zone'}
                    </h4>
                    <p style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '12px' }}>
                        {isChinese
                            ? '删除后无法恢复，所有聊天记录、活动日志和关联数据都将被永久清除。'
                            : 'This action cannot be undone. All chat history, activity logs, and associated data will be permanently deleted.'}
                    </p>

                    {showDeleteConfirm ? (
                        <div style={{
                            padding: '14px', borderRadius: '8px',
                            background: 'rgba(255,80,80,0.06)', border: '1px solid rgba(255,80,80,0.2)',
                        }}>
                            <div style={{ fontSize: '13px', fontWeight: 500, marginBottom: '8px', color: 'var(--text-primary)' }}>
                                {isChinese
                                    ? `确认删除 Agent "${agent?.name}"？`
                                    : `Delete agent "${agent?.name}"?`}
                            </div>
                            <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginBottom: '12px' }}>
                                {isChinese
                                    ? '此操作不可撤销。'
                                    : 'This action is irreversible.'}
                            </div>
                            <div style={{ display: 'flex', gap: '8px', justifyContent: 'flex-end' }}>
                                <button
                                    className="btn btn-secondary"
                                    onClick={() => setShowDeleteConfirm(false)}
                                    style={{ padding: '5px 14px', fontSize: '12px' }}
                                >
                                    {isChinese ? '取消' : 'Cancel'}
                                </button>
                                <button
                                    className="btn btn-danger"
                                    onClick={handleDelete}
                                    disabled={deleting}
                                    style={{ padding: '5px 14px', fontSize: '12px' }}
                                >
                                    {deleting
                                        ? (isChinese ? '删除中...' : 'Deleting...')
                                        : (isChinese ? '确认删除' : 'Delete')}
                                </button>
                            </div>
                        </div>
                    ) : (
                        <button
                            className="btn btn-danger"
                            onClick={() => setShowDeleteConfirm(true)}
                            style={{ padding: '6px 20px', fontSize: '12px' }}
                        >
                            {isChinese ? '删除此数字员工' : 'Delete this Agent'}
                        </button>
                    )}
                </div>
            )}
        </div>
    );
}
