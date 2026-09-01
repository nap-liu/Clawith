import type { Dispatch, SetStateAction } from 'react';
import type { ChannelDef } from './types';
import { renderField, renderGuide } from './shared';

type Translator = (key: string, defaultValue?: string) => string;

interface CreateChannelCardProps {
    ch: ChannelDef;
    connectionModes: Record<string, string>;
    feishuPermMode: 'basic' | 'full';
    onChange?: (values: Record<string, string>) => void;
    openChannels: Record<string, boolean>;
    setConnectionModes: Dispatch<SetStateAction<Record<string, string>>>;
    setFeishuPermMode: (mode: 'basic' | 'full') => void;
    showPwds: Record<string, boolean>;
    t: Translator;
    toggleChannel: (id: string) => void;
    togglePwd: (fieldId: string) => void;
    values?: Record<string, string>;
}

export function CreateChannelCard({
    ch,
    connectionModes,
    feishuPermMode,
    onChange,
    openChannels,
    setConnectionModes,
    setFeishuPermMode,
    showPwds,
    t,
    toggleChannel,
    togglePwd,
    values,
}: CreateChannelCardProps) {
    const isOpen = openChannels[ch.id] || false;
    const connMode = ch.connectionMode ? (connectionModes[ch.id] || 'websocket') : null;
    const isWs = connMode === 'websocket';
    const activeFields = (ch.connectionMode && isWs && ch.wsFields) ? ch.wsFields : ch.fields;
    const formFields = ch.id === 'feishu' && isWs
        ? ch.fields.filter(f => f.key !== 'encrypt_key')
        : activeFields;
    const hasValues = formFields.some(f => f.required && values?.[`${ch.id}_${f.key}`]);

    let subtitle = ch.desc;
    if (ch.connectionMode && hasValues) {
        subtitle = isWs ? 'WebSocket Mode' : 'Webhook Mode';
    }

    return (
        <div key={ch.id} style={{ border: '1px solid var(--border-default)', borderRadius: '8px', overflow: 'hidden', marginBottom: '8px' }}>
            <div
                onClick={() => toggleChannel(ch.id)}
                style={{
                    display: 'flex', alignItems: 'center', gap: '12px', padding: '14px',
                    cursor: 'pointer', background: isOpen ? 'var(--accent-subtle)' : 'var(--bg-elevated)',
                    borderBottom: isOpen ? '1px solid var(--border-default)' : 'none',
                }}
            >
                {ch.icon}
                <div style={{ flex: 1 }}>
                    <div style={{ fontWeight: 500, fontSize: '13px' }}>{t(ch.nameKey, ch.nameFallback)}</div>
                    <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>{subtitle}</div>
                </div>
                {hasValues && <span style={{ fontSize: '10px', padding: '2px 8px', borderRadius: '10px', background: 'rgba(16,185,129,0.15)', color: 'rgb(16,185,129)', fontWeight: 500 }}>{t('agent.settings.channel.configured', 'Configured')}</span>}
                <span style={{ fontSize: '12px', color: 'var(--text-tertiary)', transition: 'transform 0.2s', transform: isOpen ? 'rotate(180deg)' : 'rotate(0deg)' }}>&#9660;</span>
            </div>
            {isOpen && (
                <div style={{ padding: '16px' }}>
                    {ch.connectionMode && (
                        <div style={{ marginBottom: '16px', display: 'flex', alignItems: 'center', gap: '8px' }}>
                            <label style={{ fontSize: '12px', fontWeight: 500, width: '120px' }}>{t('agent.settings.channel.mode', 'Connection Mode')}</label>
                            <label style={{ display: 'flex', alignItems: 'center', gap: '4px', fontSize: '13px', cursor: 'pointer' }}>
                                <input type="radio" checked={isWs} onChange={() => setConnectionModes(p => ({ ...p, [ch.id]: 'websocket' }))} />
                                {t('agent.settings.channel.modeWs', 'WebSocket (Recommended)')}
                            </label>
                            <label style={{ display: 'flex', alignItems: 'center', gap: '4px', fontSize: '13px', cursor: 'pointer', marginLeft: '12px' }}>
                                <input type="radio" checked={!isWs} onChange={() => setConnectionModes(p => ({ ...p, [ch.id]: 'webhook' }))} />
                                {t('agent.settings.channel.modeWebhook', 'Webhook')}
                            </label>
                        </div>
                    )}

                    {renderGuide({ ch, guide: ch.guide, isWs: !!isWs, feishuPermMode, setFeishuPermMode, t })}

                    {formFields.map(field => (
                        <div className="form-group" key={field.key}>
                            {renderField({
                                field,
                                channelId: ch.id,
                                fieldValue: values?.[`${ch.id}_${field.key}`] || '',
                                mode: 'create',
                                onFieldChange: val => {
                                    const newValues = { ...values, [`${ch.id}_${field.key}`]: val };
                                    if (ch.connectionMode) {
                                        newValues[`${ch.id}_connection_mode`] = connMode || 'websocket';
                                    }
                                    onChange?.(newValues);
                                },
                                showPwds,
                                t,
                                togglePwd,
                            })}
                        </div>
                    ))}
                </div>
            )}
        </div>
    );
}
