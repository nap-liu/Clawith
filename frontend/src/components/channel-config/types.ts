import type { ReactNode } from 'react';

export interface ChannelConfigProps {
    mode: 'create' | 'edit';
    agentId?: string;
    canManage?: boolean;
    values?: Record<string, string>;
    onChange?: (values: Record<string, string>) => void;
}

export interface ChannelField {
    key: string;
    label: string;
    placeholder?: string;
    type?: 'text' | 'password';
    required?: boolean;
}

export interface GuideConfig {
    prefix: string;
    steps: number;
    noteKey?: string;
}

export interface ChannelDef {
    id: string;
    icon: ReactNode;
    nameKey: string;
    nameFallback: string;
    desc: string;
    apiSlug?: string;
    useChannelApi?: boolean;
    fields: ChannelField[];
    guide: GuideConfig;
    connectionMode?: boolean;
    wsGuide?: GuideConfig;
    showPermJson?: boolean;
    webhookLabel?: string;
    editOnly?: boolean;
    wsFields?: ChannelField[];
    hasTestConnection?: boolean;
}
