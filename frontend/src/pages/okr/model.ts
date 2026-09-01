export interface OKRSettings {
    enabled: boolean;
    first_enabled_at?: string | null;
    daily_report_enabled: boolean;
    daily_report_time: string;
    daily_report_skip_non_workdays?: boolean;
    weekly_report_enabled: boolean;
    weekly_report_day: number;
    period_frequency: string;
    period_length_days?: number;
    period_frequency_locked?: boolean;
}

export interface KeyResult {
    id: string;
    objective_id: string;
    title: string;
    target_value: number;
    current_value: number;
    unit?: string;
    focus_ref?: string;
    status: string;
    last_updated_at: string;
    created_at: string;
}

export interface Objective {
    id: string;
    title: string;
    description?: string;
    user_id?: string;
    agent_id?: string;
    owner_name?: string;
    period_start: string;
    period_end: string;
    status: string;
    created_at: string;
    key_results: KeyResult[];
}

export interface Period {
    start: string;
    end: string;
    label: string;
    is_current: boolean;
}

export interface LegacyWorkReport {
    id: string;
    tenant_id: string;
    okr_agent_id: string;
    report_type: string;
    period_label: string;
    content: string;
    created_at: string;
}

export interface CompanyReport {
    id: string;
    report_type: 'daily' | 'weekly' | 'monthly';
    period_start: string;
    period_end: string;
    period_label: string;
    content: string;
    submitted_count: number;
    missing_count: number;
    needs_refresh: boolean;
    generated_at: string;
    updated_at: string;
}

export interface MemberDailyReportItem {
    id: string;
    user_id?: string;
    agent_id?: string;
    display_name: string;
    avatar_url?: string | null;
    group_label: string;
    report_date: string;
    content: string;
    status: string;
    submitted_at?: string | null;
    updated_at?: string | null;
}

export interface MemberWithoutOKR {
    user_id?: string | null;
    agent_id?: string | null;
    display_name: string;
    avatar_url: string;
    channel?: string | null;
    source_label?: string | null;
}

export interface ChannelWarning {
    channel_type: string;
    channel_display: string;
    affected_members: string[];
    count: number;
}

export interface MembersWithoutOKRData {
    period_start: string;
    period_end: string;
    company_okr_exists: boolean;
    okr_agent_id: string | null;
    members_without_okr: MemberWithoutOKR[];
    tracked_user_ids: string[];
    tracked_agent_ids: string[];
    total: number;
    last_outreach_error?: {
        message: string;
        timestamp: string;
        is_read: boolean;
    } | null;
    channel_warnings?: ChannelWarning[];
}

export const STATUS_COLOR: Record<string, string> = {
    on_track: '#22c55e',
    at_risk: '#f59e0b',
    behind: '#ef4444',
    completed: '#6366f1',
};

export const STATUS_LABELS: Record<string, { zh: string; en: string }> = {
    on_track: { zh: '按计划', en: 'On Track' },
    at_risk: { zh: '有风险', en: 'At Risk' },
    behind: { zh: '落后', en: 'Behind' },
    completed: { zh: '已完成', en: 'Completed' },
};

export function progressPercent(kr: KeyResult): number {
    if (!kr.target_value) return 0;
    return Math.min(100, Math.round((kr.current_value / kr.target_value) * 100));
}

export function objectiveProgress(obj: Objective): number {
    if (!obj.key_results.length) return 0;
    const avg = obj.key_results.reduce((s, kr) => s + progressPercent(kr), 0) / obj.key_results.length;
    return Math.round(avg);
}

export function deriveStatus(pct: number, explicit?: string): string {
    if (explicit && explicit !== 'auto') return explicit;
    if (pct >= 100) return 'completed';
    if (pct >= 70) return 'on_track';
    if (pct >= 40) return 'at_risk';
    return 'behind';
}
