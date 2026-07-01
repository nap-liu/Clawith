import React, { useState } from 'react';
import { agentApi } from '../services/api';
import { MarkdownRenderer } from './MarkdownRenderer';

// A confirmation card is just the rendering of a suspended `request_confirmation`
// tool_call: its content is the tool_call args, its state is the tool_call status/result.
// Clicking a (dynamic) button fills the tool result via resolve, which resumes the agent.

interface CardButton {
    text?: string;
    value?: string;
    color?: string; // blue | red | gray (clamped server-side for DingTalk; CSS-mapped here)
}

interface CardArgs {
    title?: string;
    summary?: string;
    action?: { tool?: string; args?: Record<string, any> } | null;
    risk_level?: string;
    buttons?: CardButton[] | null;
}

interface Props {
    agentId: string;
    callId: string;
    args: CardArgs;
    resolved: boolean;
    result?: string;
    t: (k: string, opts?: any) => string;
}

const DEFAULT_BUTTONS: CardButton[] = [
    { text: '取消', value: 'cancel', color: 'gray' },
    { text: '确认', value: 'confirm', color: 'blue' },
];

// Concrete one-line preview of the action the agent intends to run (mirrors the backend
// _action_preview so web and DingTalk show the same thing).
function actionPreview(action?: CardArgs['action']): string {
    if (!action || !action.tool) return '';
    const a = action.args;
    if (a && typeof a === 'object' && Object.keys(a).length) {
        const parts = Object.entries(a).map(
            ([k, v]) => `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`,
        );
        const preview = `${action.tool}(${parts.join(', ')})`;
        return preview.length <= 300 ? preview : preview.slice(0, 300) + '…';
    }
    return action.tool;
}

const BTN_CLASS: Record<string, string> = {
    blue: 'btn btn-primary',
    red: 'btn btn-danger',
    gray: 'btn btn-ghost',
};

const ConfirmationCard: React.FC<Props> = ({ agentId, callId, args, resolved, result, t }) => {
    const [busy, setBusy] = useState(false);
    const [localResolved, setLocalResolved] = useState(false);
    const [localResult, setLocalResult] = useState<string | undefined>(undefined);

    const isResolved = resolved || localResolved;
    const danger = args.risk_level === 'high';
    const buttons = (Array.isArray(args.buttons) && args.buttons.length ? args.buttons : DEFAULT_BUTTONS)
        .filter(b => b && (b.text || b.value));
    const preview = actionPreview(args.action);

    const click = async (b: CardButton) => {
        if (!callId || busy || isResolved) return;
        const value = b.value || b.text || '';
        const label = b.text || b.value || '';
        setBusy(true);
        try {
            const r = await agentApi.resolveConfirmation(agentId, callId, value, label);
            // result is null when the card was already resolved elsewhere (stale click) → 已过期.
            setLocalResult(r && r.result ? `已收到:你点了「${label}」` : '卡片已过期');
            setLocalResolved(true);
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
                {args.title || t('agent.chat.confirmCardTitle', 'Confirmation required')}
            </div>
            <div style={{ color: 'var(--text-secondary)', fontSize: 13 }}>
                <MarkdownRenderer content={args.summary || ''} />
            </div>
            {preview ? (
                <div style={{ marginTop: 8, fontSize: 12, color: 'var(--text-tertiary)' }}>
                    {t('agent.chat.confirmWillRun', 'Will run')}: <code>{preview}</code>
                </div>
            ) : null}
            <div style={{ marginTop: 12, display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                {!isResolved ? (
                    buttons.map((b, idx) => (
                        <button
                            key={idx}
                            type="button"
                            className={BTN_CLASS[b.color || 'blue'] || BTN_CLASS.blue}
                            disabled={busy}
                            onClick={() => click(b)}
                        >
                            {b.text || b.value}
                        </button>
                    ))
                ) : (
                    <span style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>
                        {localResult || result || t('agent.chat.confirmResolved', 'Resolved')}
                    </span>
                )}
            </div>
        </div>
    );
};

export default ConfirmationCard;
