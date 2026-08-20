import { useEffect, useState } from 'react';
import {
    IconArrowUpRight,
    IconBinaryTree,
    IconCheck,
    IconClock,
    IconLoader2,
    IconPlayerStop,
    IconX,
} from '@tabler/icons-react';

import { chatSessionApi } from '../services/api';

export type SubagentRunCardData = {
    subagentId?: string;
    sessionId?: string;
    executionAgentId?: string;
    status: string;
    mode?: string;
    task?: string;
    model?: string;
    fork?: boolean;
};

type Translate = (key: string, options?: any) => string;

function parseObject(value: unknown): Record<string, any> {
    if (value && typeof value === 'object' && !Array.isArray(value)) return value as Record<string, any>;
    if (typeof value !== 'string' || !value.trim()) return {};
    try {
        const parsed = JSON.parse(value);
        return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
    } catch {
        return {};
    }
}

export function parseSubagentRunCardData(message: any, payload: Record<string, any>): SubagentRunCardData {
    const args = parseObject(message?.toolArgs ?? payload.args);
    const result = parseObject(message?.toolResult ?? payload.result);
    const toolStatus = String(message?.toolStatus || payload.status || '').toLowerCase();
    const fallbackStatus = result.subagent_id || result.session_id
        ? 'completed'
        : (['done', 'failed', 'error'].includes(toolStatus) ? 'failed' : 'running');
    const status = String(
        result.status
        || fallbackStatus,
    ).toLowerCase();
    return {
        subagentId: result.subagent_id ? String(result.subagent_id) : undefined,
        sessionId: result.session_id ? String(result.session_id) : (result.subagent_id ? String(result.subagent_id) : undefined),
        executionAgentId: result.execution_agent_id ? String(result.execution_agent_id) : undefined,
        status,
        mode: result.mode || args.mode ? String(result.mode || args.mode) : undefined,
        task: args.task ? String(args.task) : undefined,
        model: args.model ? String(args.model) : undefined,
        fork: args.fork === true,
    };
}

function statusMeta(status: string) {
    if (['completed', 'done', 'success'].includes(status)) {
        return { key: 'completed', icon: IconCheck, className: 'completed' };
    }
    if (['failed', 'error'].includes(status)) {
        return { key: 'failed', icon: IconX, className: 'failed' };
    }
    if (['cancelled', 'canceled', 'stopped'].includes(status)) {
        return { key: status === 'stopped' ? 'stopped' : 'cancelled', icon: IconPlayerStop, className: 'cancelled' };
    }
    if (['queued', 'pending'].includes(status)) {
        return { key: 'queued', icon: IconClock, className: 'queued' };
    }
    if (['running', 'processing'].includes(status)) {
        return { key: 'running', icon: IconLoader2, className: 'running' };
    }
    return { key: 'unknown', icon: IconClock, className: 'unknown' };
}

export default function SubagentRunCard({
    agentId,
    mode = 'pc',
    data,
    t,
    onOpenSession,
}: {
    agentId: string;
    mode?: 'h5' | 'pc';
    data: SubagentRunCardData;
    t: Translate;
    onOpenSession?: (data: SubagentRunCardData) => void;
}) {
    const [liveData, setLiveData] = useState(data);

    useEffect(() => {
        setLiveData(data);
        if (!agentId || !data.sessionId) return;
        let cancelled = false;
        let timer: number | undefined;

        const refresh = async () => {
            try {
                const detail = await chatSessionApi.get(agentId, data.sessionId!);
                if (cancelled) return;
                const runtime = detail?.runtime;
                const next = {
                    ...data,
                    status: String(runtime?.status || data.status).toLowerCase(),
                    executionAgentId: String(runtime?.execution_agent_id || detail?.agent_id || data.executionAgentId || ''),
                    mode: runtime?.mode || data.mode,
                    model: runtime?.model || data.model,
                };
                setLiveData(next);
                if (['queued', 'pending', 'running', 'processing'].includes(next.status)) {
                    timer = window.setTimeout(refresh, 3000);
                }
            } catch {
                if (!cancelled && ['queued', 'pending', 'running', 'processing'].includes(data.status)) {
                    timer = window.setTimeout(refresh, 5000);
                }
            }
        };

        void refresh();
        return () => {
            cancelled = true;
            if (timer !== undefined) window.clearTimeout(timer);
        };
    }, [agentId, data.executionAgentId, data.mode, data.model, data.sessionId, data.status, data.task, data.fork]);

    const status = statusMeta(liveData.status);
    const StatusIcon = status.icon;
    const canOpen = Boolean(liveData.sessionId && onOpenSession);
    const shortId = liveData.sessionId ? liveData.sessionId.slice(0, 8) : '';
    const modeKey = liveData.mode === 'sync' ? 'sync' : 'async';

    return (
        <button
            type="button"
            className={`subagent-run-card subagent-run-card--${status.className} subagent-run-card--${mode}`}
            onClick={() => canOpen && onOpenSession?.(liveData)}
            disabled={!canOpen}
            aria-label={canOpen ? t('agent.subagentRun.viewSession') : t('agent.subagentRun.sessionPending')}
        >
            <span className="subagent-run-card__rail" aria-hidden="true" />
            <span className="subagent-run-card__body">
                <span className="subagent-run-card__header">
                    <span className="subagent-run-card__identity">
                        <span className="subagent-run-card__icon"><IconBinaryTree size={18} stroke={1.8} /></span>
                        <span>{t('agent.subagentRun.title')}</span>
                    </span>
                    <span className={`subagent-run-card__status subagent-run-card__status--${status.className}`}>
                        <StatusIcon className={status.className === 'running' ? 'subagent-run-card__spin' : ''} size={14} stroke={2} />
                        {t(`agent.subagentRun.status.${status.key}`)}
                    </span>
                </span>
                <span className="subagent-run-card__task">{liveData.task || t('agent.subagentRun.taskFallback')}</span>
                <span className="subagent-run-card__meta">
                    {liveData.mode && <span>{t(`agent.subagentRun.mode.${modeKey}`)}</span>}
                    {liveData.model && <span>{liveData.model}</span>}
                    {liveData.fork && <span>{t('agent.subagentRun.forked')}</span>}
                    {shortId && <span className="subagent-run-card__session">#{shortId}</span>}
                </span>
                <span className="subagent-run-card__action">
                    {canOpen ? t('agent.subagentRun.viewSession') : t('agent.subagentRun.sessionPending')}
                    {canOpen && <IconArrowUpRight size={15} stroke={1.8} />}
                </span>
            </span>
        </button>
    );
}
