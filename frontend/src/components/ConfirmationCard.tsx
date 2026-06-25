import React, { useState } from 'react';
import { agentApi } from '../services/api';

interface ConfirmMsg {
    confirmationId?: string;
    title?: string;
    summary?: string;
    actionPreview?: string;
    riskLevel?: string;
    status?: string;
}

interface Props {
    msg: ConfirmMsg;
    agentId: string;
    t: (k: string, opts?: any) => string;
}

const ConfirmationCard: React.FC<Props> = ({ msg, agentId, t }) => {
    const [busy, setBusy] = useState(false);
    const [localStatus, setLocalStatus] = useState(msg.status || 'pending');
    const status = msg.status || localStatus; // WS update takes priority, local optimistic fallback
    const pending = status === 'pending';
    const danger = msg.riskLevel === 'high';

    const act = async (action: 'confirm' | 'cancel') => {
        if (!msg.confirmationId || busy) return;
        setBusy(true);
        try {
            const r = await agentApi.resolveConfirmation(agentId, msg.confirmationId, action);
            setLocalStatus(r?.status || (action === 'confirm' ? 'executed' : 'cancelled'));
        } finally {
            setBusy(false);
        }
    };

    return (
        <div
            style={{
                borderRadius: 12,
                padding: 14,
                maxWidth: 560,
                background: 'var(--bg-secondary)',
                border: `1px solid ${danger ? 'var(--error, #d44)' : 'var(--border-subtle)'}`,
            }}
        >
            <div style={{ fontWeight: 600, color: 'var(--text-primary)', marginBottom: 6 }}>
                {msg.title || t('agent.chat.confirmCardTitle', 'Confirmation required')}
            </div>
            <div style={{ color: 'var(--text-secondary)', whiteSpace: 'pre-wrap', fontSize: 13 }}>
                {msg.summary}
            </div>
            {msg.actionPreview ? (
                <div style={{ marginTop: 8, fontSize: 12, color: 'var(--text-tertiary)' }}>
                    {t('agent.chat.confirmWillRun', 'Will run')}: <code>{msg.actionPreview}</code>
                </div>
            ) : null}
            <div style={{ marginTop: 12, display: 'flex', gap: 8 }}>
                {pending ? (
                    <>
                        <button
                            type="button"
                            className="btn btn-primary"
                            disabled={busy}
                            onClick={() => act('confirm')}
                        >
                            {t('agent.chat.confirmAction', 'Confirm')}
                        </button>
                        <button
                            type="button"
                            className="btn btn-ghost"
                            disabled={busy}
                            onClick={() => act('cancel')}
                        >
                            {t('agent.chat.cancelAction', 'Cancel')}
                        </button>
                    </>
                ) : (
                    <span style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>
                        {status === 'cancelled' || status === 'expired'
                            ? t('agent.chat.confirmCancelled', 'Cancelled')
                            : t('agent.chat.confirmResolved', 'Confirmed')}
                    </span>
                )}
            </div>
        </div>
    );
};

export default ConfirmationCard;
