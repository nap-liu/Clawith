import type { Translate } from './types';

export function triggerScheduleLabel(trigger: any, t: Translate, locale?: string): string {
    const config = trigger.config || {};
    if (trigger.type === 'cron') {
        return config.expr
            ? t('agent.aware.workspace.trigger.cronValue', { value: config.expr })
            : t('agent.aware.workspace.trigger.cron');
    }
    if (trigger.type === 'once') {
        if (!config.at) return t('agent.aware.workspace.trigger.once');
        const parsed = new Date(config.at);
        return Number.isNaN(parsed.getTime())
            ? String(config.at)
            : parsed.toLocaleString(locale);
    }
    if (trigger.type === 'interval') {
        return t('agent.aware.workspace.trigger.intervalValue', { count: config.minutes || 0 });
    }
    if (trigger.type === 'poll') {
        return config.url || t('agent.aware.workspace.trigger.poll');
    }
    if (trigger.type === 'on_message') {
        return t('agent.aware.workspace.trigger.onMessage');
    }
    if (trigger.type === 'webhook') {
        return t('agent.aware.workspace.trigger.webhook');
    }
    return trigger.type;
}

export function resourceCreator(resource: any): { id: string; name?: string } {
    return {
        id: String(resource.created_by_user_id || resource.created_by || ''),
        name: resource.creator_display_name || resource.creator_username,
    };
}

export function toDateTimeInput(value?: string | null): string {
    if (!value) return '';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '';
    const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
    return local.toISOString().slice(0, 23);
}

export function fromDateTimeInput(value: string): string | null {
    return value ? new Date(value).toISOString() : null;
}
