import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import QRCode from 'qrcode';
import { channelApi } from '../services/api';
import { CreateChannelCard } from './channel-config/CreateChannelCard';
import { EditChannelCard } from './channel-config/EditChannelCard';
import { fetchAuth } from './channel-config/fetchAuth';
import { CHANNEL_REGISTRY } from './channel-config/registry';
import type { ChannelConfigProps, ChannelDef } from './channel-config/types';

export default function ChannelConfig({ mode, agentId, canManage = true, values, onChange }: ChannelConfigProps) {
    const { t } = useTranslation();
    const queryClient = useQueryClient();

    const [feishuPermMode, setFeishuPermMode] = useState<'basic' | 'full'>('basic');
    const [openChannels, setOpenChannels] = useState<Record<string, boolean>>({});
    const [editingChannels, setEditingChannels] = useState<Record<string, boolean>>({});
    const [forms, setForms] = useState<Record<string, Record<string, string>>>({});
    const [connectionModes, setConnectionModes] = useState<Record<string, string>>({
        feishu: 'websocket',
        wecom: 'websocket',
        dingtalk: 'websocket',
        discord: 'gateway',
    });
    const [showPwds, setShowPwds] = useState<Record<string, boolean>>({});
    const [atlassianTesting, setAtlassianTesting] = useState(false);
    const [atlassianTestResult, setAtlassianTestResult] = useState<{ ok: boolean; message?: string; tool_count?: number; error?: string } | null>(null);
    const [actionFeedback, setActionFeedback] = useState<{ type: 'success' | 'error'; text: string } | null>(null);
    const [wechatQr, setWechatQr] = useState<{ qrcode: string; qrcode_img_content: string } | null>(null);
    const [wechatQrImageSrc, setWechatQrImageSrc] = useState('');
    const [wechatQrStatus, setWechatQrStatus] = useState('');
    const [wechatLoadingQr, setWechatLoadingQr] = useState(false);

    const toggleChannel = (id: string) => setOpenChannels(prev => ({ ...prev, [id]: !prev[id] }));
    const setEditing = (id: string, val: boolean) => setEditingChannels(prev => ({ ...prev, [id]: val }));
    const setFormField = (channelId: string, key: string, val: string) =>
        setForms(prev => ({ ...prev, [channelId]: { ...prev[channelId], [key]: val } }));
    const getForm = (channelId: string) => forms[channelId] || {};
    const togglePwd = (fieldId: string) => setShowPwds(prev => ({ ...prev, [fieldId]: !prev[fieldId] }));

    const enabled = mode === 'edit' && !!agentId;

    const { data: feishuConfig } = useQuery({
        queryKey: ['channel', agentId],
        queryFn: () => channelApi.get(agentId!),
        enabled,
    });
    const { data: feishuWebhook } = useQuery({
        queryKey: ['webhook-url', agentId],
        queryFn: () => channelApi.webhookUrl(agentId!),
        enabled,
    });
    const { data: slackConfig } = useQuery({
        queryKey: ['slack-channel', agentId],
        queryFn: () => fetchAuth<any>(`/agents/${agentId}/slack-channel`).catch(() => null),
        enabled,
    });
    const { data: slackWebhook } = useQuery({
        queryKey: ['slack-webhook-url', agentId],
        queryFn: () => fetchAuth<any>(`/agents/${agentId}/slack-channel/webhook-url`),
        enabled,
    });
    const { data: discordConfig } = useQuery({
        queryKey: ['discord-channel', agentId],
        queryFn: () => fetchAuth<any>(`/agents/${agentId}/discord-channel`).catch(() => null),
        enabled,
    });
    const { data: discordWebhook } = useQuery({
        queryKey: ['discord-webhook-url', agentId],
        queryFn: () => fetchAuth<any>(`/agents/${agentId}/discord-channel/webhook-url`),
        enabled,
    });
    const { data: teamsConfig } = useQuery({
        queryKey: ['teams-channel', agentId],
        queryFn: () => fetchAuth<any>(`/agents/${agentId}/teams-channel`).catch(() => null),
        enabled,
    });
    const { data: teamsWebhook } = useQuery({
        queryKey: ['teams-webhook-url', agentId],
        queryFn: () => fetchAuth<any>(`/agents/${agentId}/teams-channel/webhook-url`).catch(() => null),
        enabled,
    });
    const { data: dingtalkConfig } = useQuery({
        queryKey: ['dingtalk-channel', agentId],
        queryFn: () => fetchAuth<any>(`/agents/${agentId}/dingtalk-channel`).catch(() => null),
        enabled,
    });
    const { data: wechatConfig } = useQuery({
        queryKey: ['wechat-channel', agentId],
        queryFn: () => fetchAuth<any>(`/agents/${agentId}/wechat-channel`).catch(() => null),
        enabled,
    });
    const { data: wecomConfig } = useQuery({
        queryKey: ['wecom-channel', agentId],
        queryFn: () => fetchAuth<any>(`/agents/${agentId}/wecom-channel`).catch(() => null),
        enabled,
    });
    const { data: wecomWebhook } = useQuery({
        queryKey: ['wecom-webhook-url', agentId],
        queryFn: () => fetchAuth<any>(`/agents/${agentId}/wecom-channel/webhook-url`),
        enabled,
    });
    const { data: atlassianConfig } = useQuery({
        queryKey: ['atlassian-channel', agentId],
        queryFn: () => fetchAuth<any>(`/agents/${agentId}/atlassian-channel`).catch(() => null),
        enabled,
    });

    const getConfig = (id: string): any => {
        switch (id) {
            case 'feishu': return feishuConfig;
            case 'slack': return slackConfig;
            case 'discord': return discordConfig;
            case 'teams': return teamsConfig;
            case 'dingtalk': return dingtalkConfig;
            case 'wechat': return wechatConfig;
            case 'wecom': return wecomConfig;
            case 'atlassian': return atlassianConfig;
            default: return null;
        }
    };

    const getWebhook = (id: string): any => {
        switch (id) {
            case 'feishu': return feishuWebhook;
            case 'slack': return slackWebhook;
            case 'discord': return discordWebhook;
            case 'teams': return teamsWebhook;
            case 'wecom': return wecomWebhook;
            default: return null;
        }
    };

    const saveMutation = useMutation({
        mutationFn: ({ ch, data }: { ch: ChannelDef; data: any }) => {
            if (ch.useChannelApi) {
                return channelApi.create(agentId!, data);
            }
            return fetchAuth(`/agents/${agentId}/${ch.apiSlug}`, { method: 'POST', body: JSON.stringify(data) });
        },
        onSuccess: (_data, { ch }) => {
            const keys = ch.useChannelApi
                ? [['channel', agentId]]
                : [[`${ch.apiSlug}`, agentId], [`${ch.id}-webhook-url`, agentId]];
            keys.forEach(queryKey => queryClient.invalidateQueries({ queryKey }));
            setForms(prev => ({ ...prev, [ch.id]: {} }));
            setEditing(ch.id, false);
            setActionFeedback({
                type: 'success',
                text: t('agent.settings.channel.saveSuccess', 'Channel configuration saved.'),
            });
        },
        onError: (error: Error) => {
            setActionFeedback({
                type: 'error',
                text: error.message || t('agent.settings.channel.saveFailed', 'Failed to save channel configuration.'),
            });
        },
    });

    const deleteMutation = useMutation({
        mutationFn: ({ ch }: { ch: ChannelDef }) => {
            if (ch.useChannelApi) {
                return channelApi.delete(agentId!);
            }
            return fetchAuth(`/agents/${agentId}/${ch.apiSlug}`, { method: 'DELETE' });
        },
        onSuccess: (_data, { ch }) => {
            const keys = ch.useChannelApi
                ? [['channel', agentId]]
                : [[`${ch.apiSlug}`, agentId]];
            keys.forEach(queryKey => queryClient.invalidateQueries({ queryKey }));
            if (ch.id === 'atlassian') {
                setAtlassianTestResult(null);
            }
            setEditing(ch.id, false);
            setActionFeedback({
                type: 'success',
                text: t('agent.settings.channel.disconnectSuccess', 'Channel disconnected.'),
            });
        },
        onError: (error: Error) => {
            setActionFeedback({
                type: 'error',
                text: error.message || t('agent.settings.channel.disconnectFailed', 'Failed to disconnect channel.'),
            });
        },
    });

    const testAtlassian = async () => {
        setAtlassianTesting(true);
        setAtlassianTestResult(null);
        try {
            const result = await fetchAuth<any>(`/agents/${agentId}/atlassian-channel/test`, { method: 'POST' });
            setAtlassianTestResult(result);
        } catch (error: any) {
            setAtlassianTestResult({ ok: false, error: String(error) });
        }
        setAtlassianTesting(false);
    };

    useEffect(() => {
        if (mode !== 'edit' || !agentId || !wechatQr?.qrcode) return;
        let cancelled = false;
        let timer: number | null = null;

        const poll = async () => {
            try {
                const result = await fetchAuth<any>(`/agents/${agentId}/wechat-channel/qrcode-status?qrcode=${encodeURIComponent(wechatQr.qrcode)}`);
                if (cancelled) return;
                setWechatQrStatus(result.status || '');
                if (result.status === 'confirmed') {
                    setWechatQr(null);
                    setEditing('wechat', false);
                    queryClient.invalidateQueries({ queryKey: ['wechat-channel', agentId] });
                    return;
                }
            } catch {
                // Keep the polling loop silent; users can refresh the QR code manually.
            }
            if (!cancelled) {
                timer = window.setTimeout(poll, 3000);
            }
        };

        void poll();

        return () => {
            cancelled = true;
            if (timer !== null) {
                window.clearTimeout(timer);
            }
        };
    }, [mode, agentId, wechatQr?.qrcode, queryClient]);

    useEffect(() => {
        const raw = wechatQr?.qrcode_img_content?.trim() || '';
        let disposed = false;
        let objectUrl = '';
        if (!raw) {
            setWechatQrImageSrc('');
            return;
        }
        if (raw.startsWith('http://') || raw.startsWith('https://')) {
            const token = localStorage.getItem('token');
            fetch(`/api/agents/${agentId}/wechat-channel/qrcode-image?url=${encodeURIComponent(raw)}`, {
                headers: token ? { Authorization: `Bearer ${token}` } : {},
            }).then(async resp => {
                if (!resp.ok) {
                    throw new Error(`HTTP ${resp.status}`);
                }
                const blob = await resp.blob();
                const contentType = (blob.type || resp.headers.get('content-type') || '').toLowerCase();
                if (contentType.startsWith('image/')) {
                    return { kind: 'image' as const, blob };
                }
                return { kind: 'qr' as const, text: raw };
            }).then(result => {
                if (disposed) return;
                if (result.kind === 'image') {
                    objectUrl = URL.createObjectURL(result.blob);
                    setWechatQrImageSrc(objectUrl);
                    return;
                }
                return QRCode.toDataURL(result.text, {
                    width: 220,
                    margin: 1,
                    color: {
                        dark: '#111111',
                        light: '#FFFFFF',
                    },
                }).then((dataUrl: string) => {
                    if (!disposed) {
                        setWechatQrImageSrc(dataUrl);
                    }
                });
            }).catch(() => {
                if (!disposed) {
                    setWechatQrImageSrc('');
                }
            });
            return () => {
                disposed = true;
                if (objectUrl) {
                    URL.revokeObjectURL(objectUrl);
                }
            };
        }
        if (raw.startsWith('data:image/')) {
            setWechatQrImageSrc(raw);
            return;
        }

        QRCode.toDataURL(raw, {
            width: 220,
            margin: 1,
            color: {
                dark: '#111111',
                light: '#FFFFFF',
            },
        }).then((dataUrl: string) => {
            if (!disposed) {
                setWechatQrImageSrc(dataUrl);
            }
        }).catch(() => {
            if (!disposed) {
                setWechatQrImageSrc('');
            }
        });

        return () => {
            disposed = true;
        };
    }, [agentId, wechatQr?.qrcode_img_content]);

    const createWechatQr = async () => {
        if (!agentId) return;
        setWechatLoadingQr(true);
        setWechatQrStatus('');
        setWechatQrImageSrc('');
        try {
            const qr = await fetchAuth<any>(`/agents/${agentId}/wechat-channel/qrcode`, {
                method: 'POST',
                body: JSON.stringify({}),
            });
            setWechatQr(qr);
            setWechatQrStatus('wait');
        } catch (error: any) {
            setActionFeedback({
                type: 'error',
                text: error.message || 'Failed to generate WeChat QR code.',
            });
        } finally {
            setWechatLoadingQr(false);
        }
    };

    const buildPayload = (ch: ChannelDef, form: Record<string, string>) => {
        if (ch.id === 'feishu') {
            return {
                channel_type: 'feishu',
                app_id: form.app_id,
                app_secret: form.app_secret,
                encrypt_key: form.encrypt_key || undefined,
                extra_config: { connection_mode: connectionModes.feishu || 'websocket' },
            };
        }
        if (ch.id === 'wecom') {
            const connMode = connectionModes.wecom || 'websocket';
            if (connMode === 'websocket') {
                return { connection_mode: 'websocket', bot_id: form.bot_id, bot_secret: form.bot_secret };
            }
            return { ...form, connection_mode: 'webhook' };
        }
        if (ch.id === 'discord') {
            const connMode = connectionModes.discord || 'gateway';
            if (connMode === 'websocket') {
                return { bot_token: form.bot_token, connection_mode: 'gateway' };
            }
            return { ...form, connection_mode: 'webhook' };
        }
        if (ch.id === 'dingtalk') {
            return {
                ...form,
                extra_config: {
                    connection_mode: connectionModes.dingtalk || 'websocket',
                    agent_id: form.agent_id || '',
                },
            };
        }
        return form;
    };

    if (mode === 'create') {
        return (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                {CHANNEL_REGISTRY.filter(ch => !ch.editOnly).map(ch => (
                    <CreateChannelCard
                        key={ch.id}
                        ch={ch}
                        connectionModes={connectionModes}
                        feishuPermMode={feishuPermMode}
                        onChange={onChange}
                        openChannels={openChannels}
                        setConnectionModes={setConnectionModes}
                        setFeishuPermMode={setFeishuPermMode}
                        showPwds={showPwds}
                        t={t as any}
                        toggleChannel={toggleChannel}
                        togglePwd={togglePwd}
                        values={values}
                    />
                ))}
                {CHANNEL_REGISTRY.filter(ch => ch.editOnly).map(ch => (
                    <div
                        key={ch.id}
                        style={{
                            display: 'flex', alignItems: 'center', gap: '12px', padding: '14px',
                            background: 'var(--bg-elevated)', border: '1px solid var(--border-default)',
                            borderRadius: '8px', opacity: 0.7,
                        }}
                    >
                        {ch.icon}
                        <div style={{ flex: 1 }}>
                            <div style={{ fontWeight: 500, fontSize: '13px' }}>{t(ch.nameKey, ch.nameFallback)}</div>
                            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>{ch.desc}</div>
                        </div>
                        <span style={{ fontSize: '10px', padding: '2px 8px', borderRadius: '10px', background: 'var(--bg-secondary)', color: 'var(--text-tertiary)', fontWeight: 500 }}>Configure in Settings</span>
                    </div>
                ))}
            </div>
        );
    }

    return (
        <div className="card" style={{ marginBottom: '12px' }}>
            <h4 style={{ marginBottom: '12px' }}>{t('agent.settings.channel.title')}</h4>
            <p style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '8px' }}>{t('agent.settings.channel.title')}</p>
            {actionFeedback && (
                <div
                    style={{
                        padding: '10px 14px',
                        borderRadius: '8px',
                        marginBottom: '12px',
                        background: actionFeedback.type === 'success' ? 'rgba(16,185,129,0.12)' : 'rgba(239,68,68,0.12)',
                        border: `1px solid ${actionFeedback.type === 'success' ? 'rgba(16,185,129,0.25)' : 'rgba(239,68,68,0.25)'}`,
                        fontSize: '12px',
                        color: actionFeedback.type === 'success' ? 'rgb(5,150,105)' : 'rgb(220,38,38)',
                    }}
                >
                    {actionFeedback.text}
                </div>
            )}
            {CHANNEL_REGISTRY.map(ch => (
                <EditChannelCard
                    key={ch.id}
                    agentId={agentId}
                    atlassianTestResult={atlassianTestResult}
                    atlassianTesting={atlassianTesting}
                    buildPayload={buildPayload}
                    canManage={canManage}
                    ch={ch}
                    config={getConfig(ch.id)}
                    connectionModes={connectionModes}
                    createWechatQr={createWechatQr}
                    deleteChannel={() => deleteMutation.mutate({ ch })}
                    editingChannels={editingChannels}
                    feishuPermMode={feishuPermMode}
                    form={getForm(ch.id)}
                    openChannels={openChannels}
                    saveChannel={payload => saveMutation.mutate({ ch, data: payload })}
                    savePending={saveMutation.isPending}
                    setConnectionModes={setConnectionModes}
                    setEditing={setEditing}
                    setFeishuPermMode={setFeishuPermMode}
                    setForm={prefill => setForms(prev => ({ ...prev, [ch.id]: prefill }))}
                    setFormField={setFormField}
                    setWechatQr={setWechatQr}
                    setWechatQrStatus={setWechatQrStatus}
                    showPwds={showPwds}
                    t={t as any}
                    testAtlassian={testAtlassian}
                    toggleChannel={toggleChannel}
                    togglePwd={togglePwd}
                    wechatLoadingQr={wechatLoadingQr}
                    wechatQr={wechatQr}
                    wechatQrImageSrc={wechatQrImageSrc}
                    wechatQrStatus={wechatQrStatus}
                    webhook={getWebhook(ch.id)}
                />
            ))}
        </div>
    );
}
