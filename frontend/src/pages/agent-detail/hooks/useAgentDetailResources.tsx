import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { copyToClipboard } from '../../../utils/clipboard';
import { activityApi, agentApi, channelApi, enterpriseApi, fileApi, focusApi, scheduleApi, taskApi, tenantApi, triggerApi } from '../../../services/api';
import { fetchAuth } from '../utils/fetchAuth';
import {
    focusItemFromApi,
    formatTokensParts,
    schedToCron,
} from '../shared';

const settingsFormFromAgent = (agent: any) => ({
    primary_model_id: agent?.primary_model_id || '',
    fallback_model_id: agent?.fallback_model_id || '',
    temperature: (agent?.temperature ?? null) as number | null,
    context_window_size: (agent?.context_window_size ?? 100) as string | number,
    daily_memory_load_days: (agent?.daily_memory_load_days ?? 0) as string | number,
    max_tool_rounds: (agent?.max_tool_rounds ?? 50) as string | number,
    max_tokens_per_day: (agent?.max_tokens_per_day ?? '') as string | number,
    max_tokens_per_month: (agent?.max_tokens_per_month ?? '') as string | number,
    max_triggers: (agent?.max_triggers ?? 20) as string | number,
    min_poll_interval_min: (agent?.min_poll_interval_min ?? 5) as string | number,
    webhook_rate_limit: (agent?.webhook_rate_limit ?? 5) as string | number,
    im_thinking_output_enabled: agent?.im_thinking_output_enabled ?? false,
});

type SettingsForm = ReturnType<typeof settingsFormFromAgent>;

const boundedNumber = (value: string | number, min: number, max: number, fallback: number) => {
    const parsed = Number(value);
    const isBlank = typeof value === 'string' && value.trim() === '';
    return Math.max(min, Math.min(max, !isBlank && Number.isFinite(parsed) ? parsed : fallback));
};

const settingsUpdateFromForm = (form: SettingsForm) => ({
    primary_model_id: form.primary_model_id || null,
    fallback_model_id: form.fallback_model_id || null,
    temperature: form.temperature,
    context_window_size: boundedNumber(form.context_window_size, 10, 500, 100),
    daily_memory_load_days: boundedNumber(form.daily_memory_load_days, 0, 30, 0),
    max_tool_rounds: boundedNumber(form.max_tool_rounds, 5, 200, 50),
    max_tokens_per_day: form.max_tokens_per_day === '' ? null : Number(form.max_tokens_per_day),
    max_tokens_per_month: form.max_tokens_per_month === '' ? null : Number(form.max_tokens_per_month),
    max_triggers: boundedNumber(form.max_triggers, 1, 100, 20),
    min_poll_interval_min: boundedNumber(form.min_poll_interval_min, 1, 60, 5),
    webhook_rate_limit: boundedNumber(form.webhook_rate_limit, 1, 60, 5),
    im_thinking_output_enabled: form.im_thinking_output_enabled,
});

export function useAgentDetailResources({
    id,
    agent,
    activeTab,
    awareDataActive,
    currentUser,
    queryClient,
    toast,
    t,
    i18n,
    location,
    navigate,
    overrideModelId,
    noModelIcon,
    settingsIcon,
}: any) {
    const { data: myTenant } = useQuery({
        queryKey: ['tenant', 'me'],
        queryFn: () => tenantApi.me(),
        staleTime: 5 * 60 * 1000,
        refetchOnMount: 'always',
    });

    const { data: awareTriggers = [], refetch: refetchTriggers } = useQuery({
        queryKey: ['triggers', id],
        queryFn: () => triggerApi.list(id),
        enabled: !!id && awareDataActive,
        refetchInterval: awareDataActive ? 5000 : false,
    });
    const isPlatformAdmin = currentUser?.role === 'platform_admin' || !!currentUser?.is_platform_admin;
    const canReassignExecutionUser = (isPlatformAdmin || currentUser?.role === 'org_admin') && agent?.access_level === 'manage';
    const { data: executionUsers = [] } = useQuery({
        queryKey: ['background-execution-users', currentUser?.tenant_id],
        queryFn: () => enterpriseApi.listMembers(),
        enabled: !!id && awareDataActive && canReassignExecutionUser,
        staleTime: 60_000,
    });
    const [executionUserPickerTarget, setExecutionUserPickerTarget] = useState<{
        resourceType: 'trigger' | 'task' | 'schedule';
        resourceId: string;
        executionUserId: string;
        expectedExecutionUserId: string | null;
    } | null>(null);
    const reassignExecutionUser = useMutation({
        mutationFn: ({ resourceType, resourceId, executionUserId, expectedExecutionUserId }: any) => {
            const update = { execution_user_id: executionUserId, expected_execution_user_id: expectedExecutionUserId };
            if (resourceType === 'trigger') return triggerApi.update(id, resourceId, update);
            if (resourceType === 'task') return taskApi.update(id, resourceId, update);
            return scheduleApi.update(id, resourceId, update);
        },
        onSuccess: (_data, variables) => {
            const queryKey = variables.resourceType === 'trigger'
                ? ['triggers', id]
                : variables.resourceType === 'task'
                    ? ['tasks', id]
                    : ['schedules', id];
            queryClient.invalidateQueries({ queryKey });
            toast.success(t('agent.aware.executionIdentity.updated'));
        },
        onError: (err: any) => {
            toast.error(t('agent.aware.executionIdentity.updateFailed'), { details: String(err?.detail || err?.message || err) });
        },
    });
    const updateBackgroundRuntime = useMutation({
        mutationFn: ({ resourceType, resourceId, update }: any) => {
            if (resourceType === 'trigger') return triggerApi.update(id, resourceId, update);
            if (resourceType === 'task') return taskApi.update(id, resourceId, update);
            return scheduleApi.update(id, resourceId, update);
        },
        onSuccess: (_data, variables) => {
            const key = variables.resourceType === 'trigger' ? 'triggers' : `${variables.resourceType}s`;
            queryClient.invalidateQueries({ queryKey: [key, id] });
            toast.success(i18n.language?.startsWith('zh') ? '运行配置已更新' : 'Runtime settings updated');
        },
        onError: (err: any) => toast.error(
            i18n.language?.startsWith('zh') ? '运行配置更新失败' : 'Runtime settings update failed',
            { details: String(err?.detail || err?.message || err) },
        ),
    });

    const { data: focusRecords = [], refetch: refetchFocusItems } = useQuery({
        queryKey: ['focus', id],
        queryFn: () => focusApi.list(id, true),
        enabled: !!id && awareDataActive,
        refetchInterval: awareDataActive ? 5000 : false,
    });
    const { data: taskHistoryFile } = useQuery({
        queryKey: ['file', id, 'task_history.md'],
        queryFn: () => fileApi.read(id, 'task_history.md').catch(() => null),
        enabled: !!id && awareDataActive,
    });
    const { data: awareSessionRows = [] } = useQuery({
        queryKey: ['reflection-sessions', id],
        queryFn: async () => {
            const tkn = localStorage.getItem('token');
            const res = await fetch(`/api/agents/${id}/sessions?scope=all`, { headers: { Authorization: `Bearer ${tkn}` } });
            if (!res.ok) return [];
            return await res.json();
        },
        enabled: !!id && awareDataActive,
        refetchInterval: awareDataActive ? 10000 : false,
    });
    const { data: triggerExecutions = [] } = useQuery({
        queryKey: ['trigger-executions', id],
        queryFn: () => triggerApi.executions(id, 200),
        enabled: !!id && awareDataActive,
        refetchInterval: awareDataActive ? 10000 : false,
    });
    const reflectionSessions = useMemo(() => {
        const sessions = awareSessionRows as any[];
        const sessionById = new Map(sessions.map((session) => [session.id, session]));
        const linkedConversationIds = new Set<string>();
        const executionRows = (triggerExecutions as any[]).map((execution) => {
            const session = execution.conversation_id ? sessionById.get(execution.conversation_id) : null;
            if (execution.conversation_id) linkedConversationIds.add(execution.conversation_id);
            return {
                ...(session || {}),
                id: session?.id || `execution:${execution.id}`,
                record_id: execution.id,
                conversation_id: execution.conversation_id,
                conversation_missing: !session,
                title: execution.trigger_name,
                created_at: execution.scheduled_at,
                last_message_at: execution.finished_at || execution.scheduled_at,
                message_count: session?.message_count || 0,
                source_channel: session?.source_channel || execution.source,
                execution,
            };
        });
        const legacyRows = sessions
            .filter((session) => session.source_channel === 'trigger' && !linkedConversationIds.has(session.id))
            .map((session) => ({ ...session, record_id: `legacy:${session.id}`, conversation_id: session.id, execution: null }));
        return [...executionRows, ...legacyRows].sort((a, b) => new Date(b.created_at || 0).getTime() - new Date(a.created_at || 0).getTime());
    }, [awareSessionRows, triggerExecutions]);

    const [expandedFocusIds, setExpandedFocusIds] = useState<Set<string>>(() => new Set());
    const [expandedReflection, setExpandedReflection] = useState<string | null>(null);
    const [reflectionMessages, setReflectionMessages] = useState<Record<string, any[]>>({});
    const [showAllFocus, setShowAllFocus] = useState(false);
    const [showCompletedFocus, setShowCompletedFocus] = useState(false);
    const [showAllReflections, setShowAllReflections] = useState(false);
    const [showAllSideActive, setShowAllSideActive] = useState(false);
    const [showAllSideSystem, setShowAllSideSystem] = useState(false);
    const [showAllSideCompleted, setShowAllSideCompleted] = useState(false);
    const [awareView, setAwareView] = useState<'list' | 'calendar'>('list');
    const [awareCalendarMode, setAwareCalendarMode] = useState<'day' | 'week' | 'month'>('week');
    const [awareCalendarDate, setAwareCalendarDate] = useState<Date>(() => new Date());
    const [reflectionPage, setReflectionPage] = useState(0);
    const toggleExpandedFocus = (focusId: string) => {
        setExpandedFocusIds((prev) => {
            const next = new Set(prev);
            if (next.has(focusId)) next.delete(focusId);
            else next.add(focusId);
            return next;
        });
    };
    const loadReflectionMessages = async (sessionId: string) => {
        if (!id || reflectionMessages[sessionId]) return;
        try {
            const tkn = localStorage.getItem('token');
            const res = await fetch(`/api/agents/${id}/sessions/${sessionId}/messages`, { headers: { Authorization: `Bearer ${tkn}` } });
            if (res.ok) {
                const data = await res.json();
                setReflectionMessages((prev) => ({ ...prev, [sessionId]: data }));
            }
        } catch {
        }
    };

    const { data: soulContent } = useQuery({
        queryKey: ['file', id, 'soul.md'],
        queryFn: () => fileApi.read(id, 'soul.md'),
        enabled: !!id && activeTab === 'mind',
    });
    const { data: memoryFiles = [] } = useQuery({
        queryKey: ['files', id, 'memory'],
        queryFn: () => fileApi.list(id, 'memory'),
        enabled: !!id && activeTab === 'mind',
    });
    const [expandedMemory, setExpandedMemory] = useState<string | null>(null);
    const { data: memoryFileContent } = useQuery({
        queryKey: ['file', id, expandedMemory],
        queryFn: () => fileApi.read(id, expandedMemory!),
        enabled: !!id && !!expandedMemory,
    });
    const { data: skillFiles = [] } = useQuery({
        queryKey: ['files', id, 'skills'],
        queryFn: () => fileApi.list(id, 'skills'),
        enabled: !!id && activeTab === 'skills',
    });
    const [workspacePath, setWorkspacePath] = useState('workspace');
    const { data: workspaceFiles = [] } = useQuery({
        queryKey: ['files', id, workspacePath],
        queryFn: () => fileApi.list(id, workspacePath),
        enabled: !!id && activeTab === 'workspace',
    });
    const { data: activityLogs = [] } = useQuery({
        queryKey: ['activity', id],
        queryFn: () => activityApi.list(id, 100),
        enabled: !!id && (activeTab === 'activityLog' || activeTab === 'status'),
        refetchInterval: activeTab === 'activityLog' ? 10000 : false,
    });

    const [showExpiryModal, setShowExpiryModal] = useState(false);
    const [expiryValue, setExpiryValue] = useState('');
    const [expiryQuickHours, setExpiryQuickHours] = useState<number | null>(null);
    const [expirySaving, setExpirySaving] = useState(false);
    const openExpiryModal = () => {
        const cur = agent?.expires_at;
        setExpiryValue(cur ? new Date(cur).toISOString().slice(0, 16) : '');
        setExpiryQuickHours(null);
        setShowExpiryModal(true);
    };
    const addHours = (h: number) => {
        const base = agent?.expires_at ? new Date(agent.expires_at) : new Date();
        const next = new Date(base.getTime() + h * 3600_000);
        setExpiryValue(next.toISOString().slice(0, 16));
        setExpiryQuickHours(h);
    };
    const saveExpiry = async (permanent = false) => {
        setExpirySaving(true);
        try {
            const token = localStorage.getItem('token');
            const body = permanent ? { expires_at: null } : { expires_at: expiryValue ? new Date(expiryValue).toISOString() : null };
            await fetch(`/api/agents/${id}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
                body: JSON.stringify(body),
            });
            queryClient.invalidateQueries({ queryKey: ['agent', id] });
            setShowExpiryModal(false);
        } catch (e: any) {
            toast.error('保存失败', { details: String(e?.message || e) });
        }
        setExpirySaving(false);
    };

    const [settingsForm, setSettingsForm] = useState(() => settingsFormFromAgent(agent));
    const [settingsSaving, setSettingsSaving] = useState(false);
    const [settingsSaved, setSettingsSaved] = useState(false);
    const [settingsError, setSettingsError] = useState('');
    const settingsInitRef = useRef(false);
    useEffect(() => {
        if (agent && !settingsInitRef.current) {
            setSettingsForm(settingsFormFromAgent(agent));
            settingsInitRef.current = true;
        }
    }, [agent]);
    const [wmDraft, setWmDraft] = useState('');
    const [wmSaved, setWmSaved] = useState(false);
    useEffect(() => { setWmDraft(agent?.welcome_message || ''); }, [agent?.welcome_message]);
    const settingsUpdate = settingsUpdateFromForm(settingsForm);
    const persistedSettings = settingsUpdateFromForm(settingsFormFromAgent(agent));
    const hasSettingsChanges = Object.entries(settingsUpdate).some(
        ([field, value]) => value !== persistedSettings[field as keyof typeof persistedSettings],
    );
    const handleSaveSettings = async () => {
        setSettingsSaving(true);
        setSettingsError('');
        try {
            const result: any = await agentApi.update(id, settingsUpdate as any);
            queryClient.setQueryData(['agent', id], (current: any) => ({ ...current, ...result }));
            setSettingsForm(settingsFormFromAgent(result));
            settingsInitRef.current = true;
            void queryClient.invalidateQueries({ queryKey: ['agent', id] });
            const clamped = result?._clamped_fields;
            if (clamped && clamped.length > 0) {
                const isCh = i18n.language?.startsWith('zh');
                const fieldNames: Record<string, string> = isCh
                    ? { min_poll_interval_min: 'Poll 最短间隔', webhook_rate_limit: 'Webhook 频率限制', heartbeat_interval_minutes: '心跳间隔' }
                    : { min_poll_interval_min: 'Min Poll Interval', webhook_rate_limit: 'Webhook Rate Limit', heartbeat_interval_minutes: 'Heartbeat Interval' };
                const msgs = clamped.map((c: any) => {
                    const name = fieldNames[c.field] || c.field;
                    return isCh ? `${name}: ${c.requested} -> ${c.applied} (公司策略限制)` : `${name}: ${c.requested} -> ${c.applied} (company policy)`;
                });
                setSettingsError((isCh ? 'Some values were adjusted:\n' : 'Some values were adjusted:\n') + msgs.join('\n'));
                setTimeout(() => setSettingsError(''), 5000);
            }
            setSettingsSaved(true);
            setTimeout(() => setSettingsSaved(false), 2000);
        } catch (e: any) {
            setSettingsError(e?.message || 'Failed to save');
        } finally {
            setSettingsSaving(false);
        }
    };
    const handleSaveWelcomeMessage = async () => {
        try {
            await agentApi.update(id, { welcome_message: wmDraft } as any);
            queryClient.invalidateQueries({ queryKey: ['agent', id] });
            setWmSaved(true);
            setTimeout(() => setWmSaved(false), 2000);
        } catch {
        }
    };
    const prevIdRef = useRef(id);
    useEffect(() => {
        if (id && id !== prevIdRef.current) {
            prevIdRef.current = id;
            settingsInitRef.current = false;
            setSettingsSaved(false);
            setSettingsError('');
            setWmDraft('');
            setWmSaved(false);
            queryClient.invalidateQueries({ queryKey: ['agent', id] });
            if (location.pathname.endsWith('/settings')) window.history.replaceState(null, '', `#${activeTab}`);
        }
    }, [activeTab, id, location.pathname, queryClient]);

    const [expandedLogId, setExpandedLogId] = useState<string | null>(null);
    const [logFilter, setLogFilter] = useState<string>('user');
    const { data: backgroundTasks = [] } = useQuery({
        queryKey: ['tasks', id],
        queryFn: () => taskApi.list(id),
        enabled: !!id && awareDataActive,
        staleTime: 15_000,
    });
    const { data: schedules = [] } = useQuery({
        queryKey: ['schedules', id],
        queryFn: () => scheduleApi.list(id),
        enabled: !!id && awareDataActive,
        staleTime: 15_000,
    });
    const [showScheduleForm, setShowScheduleForm] = useState(false);
    const schedDefaults = { freq: 'daily', interval: 1, time: '09:00', weekdays: [1, 2, 3, 4, 5] };
    const [schedForm, setSchedForm] = useState({ name: '', instruction: '', schedule: JSON.stringify(schedDefaults), due_date: '' });
    const createScheduleMut = useMutation({
        mutationFn: () => {
            let sched: any;
            try { sched = JSON.parse(schedForm.schedule); } catch { sched = schedDefaults; }
            return scheduleApi.create(id, { name: schedForm.name, instruction: schedForm.instruction, cron_expr: schedToCron(sched) });
        },
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: ['schedules', id] });
            setShowScheduleForm(false);
            setSchedForm({ name: '', instruction: '', schedule: JSON.stringify(schedDefaults), due_date: '' });
        },
        onError: (err: any) => {
            const msg = err?.detail || err?.message || String(err);
            toast.error('创建计划任务失败', { details: String(msg) });
        },
    });
    const toggleScheduleMut = useMutation({
        mutationFn: ({ sid, enabled }: { sid: string; enabled: boolean }) => scheduleApi.update(id, sid, { is_enabled: enabled }),
        onSuccess: () => queryClient.invalidateQueries({ queryKey: ['schedules', id] }),
    });
    const deleteScheduleMut = useMutation({
        mutationFn: (sid: string) => scheduleApi.delete(id, sid),
        onSuccess: () => queryClient.invalidateQueries({ queryKey: ['schedules', id] }),
    });
    const [uploadToast, setUploadToast] = useState<{ message: string; type: 'success' | 'error' } | null>(null);
    const showToast = (message: string, type: 'success' | 'error' = 'success') => {
        setUploadToast({ message, type });
        setTimeout(() => setUploadToast(null), 3000);
    };
    const triggerScheduleMut = useMutation({
        mutationFn: async (sid: string) => scheduleApi.trigger(id, sid),
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: ['schedules', id] });
            showToast('Schedule triggered — executing in background', 'success');
        },
        onError: (err: any) => {
            const msg = err?.response?.data?.detail || err?.message || 'Failed to trigger schedule';
            showToast(msg, 'error');
        },
    });

    const { data: metrics } = useQuery({
        queryKey: ['metrics', id],
        queryFn: () => agentApi.metrics(id).catch(() => null),
        enabled: !!id && activeTab === 'status',
        retry: false,
    });
    const { data: channelConfig } = useQuery({
        queryKey: ['channel', id],
        queryFn: () => channelApi.get(id),
        enabled: !!id && activeTab === 'settings',
    });
    const { data: webhookData } = useQuery({
        queryKey: ['webhook-url', id],
        queryFn: () => channelApi.webhookUrl(id),
        enabled: !!id && activeTab === 'settings',
    });
    const { data: llmModels = [], isLoading: llmModelsLoading } = useQuery({
        queryKey: ['llm-models'],
        queryFn: () => enterpriseApi.llmModels(),
        enabled: activeTab === 'settings' || activeTab === 'status' || activeTab === 'chat' || awareDataActive,
        refetchOnMount: 'always',
    });
    useEffect(() => {
        if (activeTab !== 'chat') return;
        queryClient.refetchQueries({ queryKey: ['llm-models'] });
        queryClient.refetchQueries({ queryKey: ['tenant', 'me'] });
    }, [activeTab, location.pathname, queryClient]);
    const enabledLlmModels = useMemo(() => (llmModels as any[]).filter((m: any) => m.enabled), [llmModels]);
    const effectiveChatModelId = overrideModelId || agent?.primary_model_id || myTenant?.default_model_id || enabledLlmModels[0]?.id || null;
    const enabledModelCount = enabledLlmModels.length;
    const effectiveModelReady = !!effectiveChatModelId && enabledLlmModels.some((m: any) => m.id === effectiveChatModelId);
    const { data: permData } = useQuery({
        queryKey: ['agent-permissions', id],
        queryFn: () => fetchAuth<any>(`/agents/${id}/permissions`),
        enabled: !!id && activeTab === 'settings',
    });

    const [soulEditing, setSoulEditing] = useState(false);
    const [soulDraft, setSoulDraft] = useState('');
    const saveSoul = useMutation({
        mutationFn: () => fileApi.write(id, 'soul.md', soulDraft),
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: ['file', id, 'soul.md'] });
            setSoulEditing(false);
        },
    });
    const CopyBtn = ({ url }: { url: string }) => (
        <button
            title="Copy"
            style={{ display: 'inline-flex', alignItems: 'center', justifyContent: 'center', marginLeft: '6px', padding: '1px 4px', cursor: 'pointer', borderRadius: '3px', border: '1px solid var(--border-color)', background: 'var(--bg-primary)', color: 'var(--text-secondary)', verticalAlign: 'middle', lineHeight: 1 }}
            onClick={() => copyToClipboard(url).then(() => { })}
        >
            <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                <rect x="4" y="4" width="9" height="11" rx="1.5" />
                <path d="M3 11H2a1 1 0 01-1-1V2a1 1 0 011-1h8a1 1 0 011 1v1" />
            </svg>
        </button>
    );

    const [viewingFile, setViewingFile] = useState<string | null>(null);
    const [fileEditing, setFileEditing] = useState(false);
    const [fileDraft, setFileDraft] = useState('');
    const [promptModal, setPromptModal] = useState<{ title: string; placeholder: string; action: string } | null>(null);
    const [deleteConfirm, setDeleteConfirm] = useState<{ path: string; name: string; isDir: boolean } | null>(null);
    const [editingRole, setEditingRole] = useState(false);
    const [roleInput, setRoleInput] = useState('');
    const [editingName, setEditingName] = useState(false);
    const [nameInput, setNameInput] = useState('');
    const [infoCardOpen, setInfoCardOpen] = useState(false);
    const infoCardCloseTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
    const clearCardCloseTimer = () => {
        if (infoCardCloseTimer.current) {
            clearTimeout(infoCardCloseTimer.current);
            infoCardCloseTimer.current = null;
        }
    };
    const scheduleCardClose = () => {
        clearCardCloseTimer();
        infoCardCloseTimer.current = setTimeout(() => setInfoCardOpen(false), 180);
    };
    const { data: fileContent } = useQuery({
        queryKey: ['file-content', id, viewingFile],
        queryFn: () => fileApi.read(id, viewingFile!),
        enabled: !!viewingFile,
    });

    const [showTaskForm, setShowTaskForm] = useState(false);
    const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
    const [taskForm, setTaskForm] = useState({ title: '', description: '', priority: 'medium', type: 'todo' as 'todo' | 'supervision', supervision_target_user_id: '', supervision_target_agent_id: '', supervision_channel: '', remind_schedule: '', due_date: '' });
    const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
    const { data: taskLogs = [] } = useQuery({
        queryKey: ['task-logs', id, selectedTaskId],
        queryFn: () => taskApi.getLogs(id, selectedTaskId!),
        enabled: !!id && !!selectedTaskId,
        refetchInterval: selectedTaskId ? 3000 : false,
    });
    const expandedScheduleId = selectedTaskId?.startsWith('sched-') ? selectedTaskId.slice(6) : null;
    const { data: scheduleHistoryData } = useQuery({
        queryKey: ['schedule-history', id, expandedScheduleId],
        queryFn: () => scheduleApi.history(id, expandedScheduleId!),
        enabled: !!id && !!expandedScheduleId,
    });
    const createTask = useMutation({
        mutationFn: (data: any) => {
            const cleaned = { ...data };
            if (!cleaned.due_date) delete cleaned.due_date;
            return taskApi.create(id, cleaned);
        },
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: ['tasks', id] });
            setShowTaskForm(false);
            setTaskForm({ title: '', description: '', priority: 'medium', type: 'todo', supervision_target_user_id: '', supervision_target_agent_id: '', supervision_channel: '', remind_schedule: '', due_date: '' });
        },
    });

    const computeStatusKey = () => {
        if (agent.status === 'error') return 'error';
        if (agent.status === 'creating') return 'creating';
        if (agent.status === 'stopped') return 'stopped';
        if (agent.agent_type === 'openclaw' && agent.status === 'running' && agent.openclaw_last_seen) {
            const elapsed = Date.now() - new Date(agent.openclaw_last_seen).getTime();
            if (elapsed > 60 * 60 * 1000) return 'disconnected';
        }
        return agent.status === 'running' ? 'running' : 'idle';
    };
    const statusKey = computeStatusKey();
    const canManage = agent.access_level === 'manage';
    const formatAgentDate = (d?: string | null) => {
        if (!d) return '—';
        try { return new Date(d).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }); } catch { return d; }
    };
    const primaryModel = (llmModels as any[]).find((m: any) => m.id === agent.primary_model_id);
    const showNoModelState = !llmModelsLoading && agent.agent_type !== 'openclaw' && (enabledModelCount === 0 || !effectiveModelReady);
    const canConfigureModels = currentUser?.role === 'platform_admin' || currentUser?.role === 'org_admin' || !!currentUser?.is_platform_admin;
    const renderNoModelGuide = (variant: 'empty' | 'floating' = 'empty') => (
        <div className={`chat-no-model-state${variant === 'floating' ? ' chat-no-model-state--floating' : ''}`}>
            <div className="chat-no-model-state__icon">{noModelIcon}</div>
            <div className="chat-no-model-state__title">{t('agent.chat.noModelTitle', 'No company model configured')}</div>
            <div className="chat-no-model-state__text">
                {canConfigureModels
                    ? t('agent.chat.noModelAdmin', 'Configure a company model before chatting with this assistant.')
                    : t('agent.chat.noModelMember', 'This company has not configured a model yet. Please contact an administrator.')}
            </div>
            {canConfigureModels && (
                <button className="btn btn-primary" onClick={() => navigate('/enterprise#llm')}>
                    {settingsIcon}
                    {t('agent.chat.goModelSettings', 'Go to model management')}
                </button>
            )}
        </div>
    );
    const modelLabel = primaryModel ? (primaryModel.label || primaryModel.model) : '—';
    const modelProvider = primaryModel ? primaryModel.provider : '—';
    const todayParts = formatTokensParts(agent.tokens_used_today || 0);
    const monthParts = formatTokensParts(agent.tokens_used_month || 0);
    const totalParts = formatTokensParts(agent.tokens_used_total || 0);
    const cacheReadToday = agent.cache_read_tokens_today || metrics?.tokens?.cache_read_today || 0;
    const cacheReadMonth = agent.cache_read_tokens_month || metrics?.tokens?.cache_read_month || 0;
    const cacheReadTotal = agent.cache_read_tokens_total || metrics?.tokens?.cache_read_total || 0;
    const cacheHitRateToday = (agent.tokens_used_today || 0) > 0 ? Math.round((cacheReadToday / (agent.tokens_used_today || 1)) * 100) : 0;
    const cacheHitRateMonth = (agent.tokens_used_month || 0) > 0 ? Math.round((cacheReadMonth / (agent.tokens_used_month || 1)) * 100) : 0;
    const cacheHitRateTotal = (agent.tokens_used_total || 0) > 0 ? Math.round((cacheReadTotal / (agent.tokens_used_total || 1)) * 100) : 0;
    const expiryLabel = agent.is_expired
        ? t('agent.settings.expiry.expired')
        : agent.expires_at
            ? new Date(agent.expires_at).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
            : t('agent.settings.expiry.neverExpires');

    return {
        myTenant,
        awareTriggers,
        refetchTriggers,
        executionUsers,
        canReassignExecutionUser,
        executionUserPickerTarget,
        setExecutionUserPickerTarget,
        reassignExecutionUser,
        updateBackgroundRuntime,
        focusRecords,
        refetchFocusItems,
        taskHistoryFile,
        reflectionSessions,
        expandedFocusIds,
        setExpandedFocusIds,
        expandedReflection,
        setExpandedReflection,
        reflectionMessages,
        setReflectionMessages,
        showAllFocus,
        setShowAllFocus,
        showCompletedFocus,
        setShowCompletedFocus,
        showAllReflections,
        setShowAllReflections,
        showAllSideActive,
        setShowAllSideActive,
        showAllSideSystem,
        setShowAllSideSystem,
        showAllSideCompleted,
        setShowAllSideCompleted,
        awareView,
        setAwareView,
        awareCalendarMode,
        setAwareCalendarMode,
        awareCalendarDate,
        setAwareCalendarDate,
        reflectionPage,
        setReflectionPage,
        toggleExpandedFocus,
        loadReflectionMessages,
        soulContent,
        memoryFiles,
        expandedMemory,
        setExpandedMemory,
        memoryFileContent,
        skillFiles,
        workspacePath,
        setWorkspacePath,
        workspaceFiles,
        activityLogs,
        showExpiryModal,
        setShowExpiryModal,
        expiryValue,
        setExpiryValue,
        expiryQuickHours,
        setExpiryQuickHours,
        expirySaving,
        openExpiryModal,
        addHours,
        saveExpiry,
        settingsForm,
        setSettingsForm,
        settingsSaving,
        settingsSaved,
        settingsError,
        hasSettingsChanges,
        handleSaveSettings,
        wmDraft,
        setWmDraft,
        wmSaved,
        handleSaveWelcomeMessage,
        expandedLogId,
        setExpandedLogId,
        logFilter,
        setLogFilter,
        backgroundTasks,
        schedules,
        showScheduleForm,
        setShowScheduleForm,
        schedDefaults,
        schedForm,
        setSchedForm,
        createScheduleMut,
        toggleScheduleMut,
        deleteScheduleMut,
        triggerScheduleMut,
        metrics,
        channelConfig,
        webhookData,
        llmModels,
        llmModelsLoading,
        enabledLlmModels,
        effectiveChatModelId,
        enabledModelCount,
        effectiveModelReady,
        permData,
        soulEditing,
        setSoulEditing,
        soulDraft,
        setSoulDraft,
        saveSoul,
        CopyBtn,
        viewingFile,
        setViewingFile,
        fileEditing,
        setFileEditing,
        fileDraft,
        setFileDraft,
        promptModal,
        setPromptModal,
        deleteConfirm,
        setDeleteConfirm,
        uploadToast,
        setUploadToast,
        showToast,
        editingRole,
        setEditingRole,
        roleInput,
        setRoleInput,
        editingName,
        setEditingName,
        nameInput,
        setNameInput,
        infoCardOpen,
        setInfoCardOpen,
        clearCardCloseTimer,
        scheduleCardClose,
        fileContent,
        showTaskForm,
        setShowTaskForm,
        showDeleteConfirm,
        setShowDeleteConfirm,
        taskForm,
        setTaskForm,
        selectedTaskId,
        setSelectedTaskId,
        taskLogs,
        expandedScheduleId,
        scheduleHistoryData,
        createTask,
        statusKey,
        canManage,
        formatAgentDate,
        primaryModel,
        showNoModelState,
        canConfigureModels,
        renderNoModelGuide,
        modelLabel,
        modelProvider,
        todayParts,
        monthParts,
        totalParts,
        cacheReadToday,
        cacheReadMonth,
        cacheReadTotal,
        cacheHitRateToday,
        cacheHitRateMonth,
        cacheHitRateTotal,
        expiryLabel,
    };
}
