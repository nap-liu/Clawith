import type { Dispatch, SetStateAction } from 'react';
import LinearCopyButton from '../LinearCopyButton';
import { renderField, renderGuide } from './shared';
import type { ChannelDef } from './types';

type Translator = (key: string, defaultValue?: string) => string;

interface EditChannelCardProps {
    agentId?: string;
    atlassianTestResult: { ok: boolean; message?: string; tool_count?: number; error?: string } | null;
    atlassianTesting: boolean;
    buildPayload: (ch: ChannelDef, form: Record<string, string>) => Record<string, any>;
    canManage: boolean;
    ch: ChannelDef;
    config: any;
    connectionModes: Record<string, string>;
    createWechatQr: () => Promise<void>;
    deleteChannel: () => void;
    editingChannels: Record<string, boolean>;
    feishuPermMode: 'basic' | 'full';
    form: Record<string, string>;
    openChannels: Record<string, boolean>;
    saveChannel: (payload: Record<string, any>) => void;
    savePending: boolean;
    setConnectionModes: Dispatch<SetStateAction<Record<string, string>>>;
    setEditing: (id: string, val: boolean) => void;
    setFeishuPermMode: (mode: 'basic' | 'full') => void;
    setForm: (prefill: Record<string, string>) => void;
    setFormField: (channelId: string, key: string, val: string) => void;
    setWechatQr: Dispatch<SetStateAction<{ qrcode: string; qrcode_img_content: string } | null>>;
    setWechatQrStatus: Dispatch<SetStateAction<string>>;
    showPwds: Record<string, boolean>;
    t: Translator;
    testAtlassian: () => Promise<void>;
    toggleChannel: (id: string) => void;
    togglePwd: (fieldId: string) => void;
    wechatLoadingQr: boolean;
    wechatQr: { qrcode: string; qrcode_img_content: string } | null;
    wechatQrImageSrc: string;
    wechatQrStatus: string;
    webhook: any;
}

export function EditChannelCard({
    agentId,
    atlassianTestResult,
    atlassianTesting,
    buildPayload,
    canManage,
    ch,
    config,
    connectionModes,
    createWechatQr,
    deleteChannel,
    editingChannels,
    feishuPermMode,
    form,
    openChannels,
    saveChannel,
    savePending,
    setConnectionModes,
    setEditing,
    setFeishuPermMode,
    setForm,
    setFormField,
    setWechatQr,
    setWechatQrStatus,
    showPwds,
    t,
    testAtlassian,
    toggleChannel,
    togglePwd,
    wechatLoadingQr,
    wechatQr,
    wechatQrImageSrc,
    wechatQrStatus,
    webhook,
}: EditChannelCardProps) {
    const isOpen = openChannels[ch.id] || false;
    const isEditing = editingChannels[ch.id] || false;
    const isConfigured = ch.id === 'feishu' ? config?.is_configured : config?.is_configured;
    const connMode = connectionModes[ch.id] || 'websocket';
    const isWs = ch.connectionMode && connMode === 'websocket';
    const configConnMode = config?.extra_config?.connection_mode;

    let subtitle = ch.desc;
    if (ch.connectionMode && config) {
        subtitle = configConnMode === 'websocket' ? 'WebSocket Mode' : ch.desc;
    }

    const webhookUrl = webhook?.webhook_url || `${window.location.origin}/api/channel/${ch.id === 'feishu' ? 'feishu' : ch.apiSlug?.replace('-channel', '')}/${agentId}/webhook`;
    const activeFields = (ch.connectionMode && isWs && ch.wsFields) ? ch.wsFields : ch.fields;
    const formFields = ch.id === 'feishu' && connMode === 'webhook'
        ? ch.fields
        : ch.id === 'feishu'
            ? ch.fields.filter(f => f.key !== 'encrypt_key')
            : activeFields;
    const allRequired = formFields.filter(f => f.required).every(f => form[f.key]);

    return (
        <div key={ch.id} style={{ border: '1px solid var(--border-subtle)', borderRadius: '8px', overflow: 'hidden', marginBottom: '12px' }}>
            <div
                onClick={() => toggleChannel(ch.id)}
                style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '14px 16px', cursor: 'pointer', transition: 'background 0.15s' }}
                onMouseEnter={e => (e.currentTarget.style.background = 'var(--bg-hover)')}
                onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}
            >
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                    {ch.icon}
                    <div>
                        <div style={{ fontWeight: 600, fontSize: '14px' }}>{t(ch.nameKey, ch.nameFallback)}</div>
                        <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>{subtitle}</div>
                    </div>
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                    {config && <span className={`badge ${isConfigured ? 'badge-success' : 'badge-warning'}`}>{isConfigured ? t('agent.settings.channel.configured') : t('agent.settings.channel.notConfigured')}</span>}
                    <span style={{ fontSize: '12px', color: 'var(--text-tertiary)', transition: 'transform 0.2s', transform: isOpen ? 'rotate(180deg)' : 'rotate(0deg)' }}>&#9660;</span>
                </div>
            </div>

            {isOpen && (
                <div style={{ padding: '0 16px 16px', borderTop: '1px solid var(--border-subtle)' }}>
                    {!canManage ? (
                        <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', fontStyle: 'italic', padding: '12px', background: 'var(--bg-secondary)', borderRadius: '6px' }}>
                            Only the creator or admin can configure communication channels.
                        </div>
                    ) : isConfigured && !isEditing ? (
                        <div>
                            {ch.id === 'feishu' && configConnMode === 'websocket' && (
                                <div style={{ background: 'var(--bg-secondary)', borderRadius: '6px', padding: '10px', fontSize: '12px', marginBottom: '12px' }}>
                                    <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '6px' }}>
                                        <span style={{ width: '6px', height: '6px', borderRadius: '50%', background: '#00D6B9', display: 'inline-block' }}></span>
                                        <span style={{ color: 'var(--text-secondary)' }}>Connected via WebSocket (No callback URL needed)</span>
                                    </div>
                                    <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>App ID: <code>{config.app_id}</code></div>
                                </div>
                            )}
                            {ch.id === 'feishu' && configConnMode !== 'websocket' && (
                                <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '8px' }}>
                                    <div style={{ marginBottom: '4px' }}>Mode: <strong>Webhook</strong></div>
                                    <div>App ID: <code>{config.app_id}</code></div>
                                </div>
                            )}
                            {ch.id === 'wecom' && configConnMode === 'websocket' && (
                                <div style={{ background: 'var(--bg-secondary)', borderRadius: '6px', padding: '10px', fontSize: '12px', marginBottom: '12px' }}>
                                    <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '6px' }}>
                                        <span
                                            style={{
                                                width: '6px',
                                                height: '6px',
                                                borderRadius: '50%',
                                                background: config.is_connected ? '#07C160' : '#F59E0B',
                                                display: 'inline-block',
                                            }}
                                        ></span>
                                        <span style={{ color: 'var(--text-secondary)' }}>
                                            {config.is_connected
                                                ? t('agent.settings.channel.websocketConnected', 'Connected via WebSocket (No callback URL needed)')
                                                : t('agent.settings.channel.websocketDisconnected', 'Configured for WebSocket, but currently disconnected')}
                                        </span>
                                    </div>
                                    {!config.is_connected && (
                                        <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
                                            {t('agent.settings.channel.websocketDisconnectedHint', 'Reconnect by saving the WeCom WebSocket configuration again.')}
                                        </div>
                                    )}
                                </div>
                            )}
                            {ch.webhookLabel && !(ch.connectionMode && configConnMode === 'websocket') && ch.id !== 'dingtalk' && ch.id !== 'atlassian' && (
                                <div style={{ background: 'var(--bg-secondary)', borderRadius: '6px', padding: '10px', fontSize: '12px', fontFamily: 'var(--font-mono)', marginBottom: '12px' }}>
                                    <div style={{ color: 'var(--text-tertiary)', marginBottom: '6px' }}>{ch.webhookLabel}</div>
                                    <div style={{ lineHeight: 1.6, wordBreak: 'break-all' }}>
                                        <span style={{ color: 'var(--accent-primary)' }}>{webhookUrl}</span>
                                        <LinearCopyButton
                                            textToCopy={webhookUrl}
                                            label="Copy"
                                            iconOnly={true}
                                            className=""
                                            style={{ marginLeft: '6px', padding: '1px 4px', cursor: 'pointer', borderRadius: '3px', border: '1px solid var(--border-color)', background: 'var(--bg-primary)', color: 'var(--text-secondary)', verticalAlign: 'middle' }}
                                        />
                                    </div>
                                </div>
                            )}
                            {ch.id === 'discord' && configConnMode !== 'gateway' && (
                                <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginBottom: '8px' }}>Use <code>/ask message:&lt;your question&gt;</code> to talk to this agent</div>
                            )}
                            {ch.id === 'discord' && configConnMode === 'gateway' && (
                                <div style={{ background: 'var(--bg-secondary)', borderRadius: '6px', padding: '10px', fontSize: '12px', marginBottom: '12px' }}>
                                    <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '6px' }}>
                                        <span style={{ width: '6px', height: '6px', borderRadius: '50%', background: '#5865F2', display: 'inline-block' }}></span>
                                        <span style={{ color: 'var(--text-secondary)' }}>Connected via Gateway (No public URL needed)</span>
                                    </div>
                                    <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>@mention the bot or send a DM to interact</div>
                                </div>
                            )}
                            {ch.id === 'dingtalk' && configConnMode === 'websocket' && (
                                <div style={{ background: 'var(--bg-secondary)', borderRadius: '6px', padding: '10px', fontSize: '12px', marginBottom: '12px' }}>
                                    <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '6px' }}>
                                        <span style={{ width: '6px', height: '6px', borderRadius: '50%', background: '#007FFF', display: 'inline-block' }}></span>
                                        <span style={{ color: 'var(--text-secondary)' }}>Connected via Stream (No callback URL needed)</span>
                                    </div>
                                    <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>App Key: <code>{config.app_id}</code></div>
                                </div>
                            )}
                            {ch.id === 'dingtalk' && configConnMode !== 'websocket' && (
                                <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '8px' }}>
                                    <div style={{ marginBottom: '4px' }}>Mode: <strong>Webhook</strong></div>
                                    <div>App Key: <code>{config.app_id}</code></div>
                                </div>
                            )}
                            {ch.id === 'atlassian' && (
                                <div style={{ background: 'var(--bg-secondary)', borderRadius: '6px', padding: '10px', fontSize: '12px', marginBottom: '12px' }}>
                                    <div style={{ color: 'var(--text-tertiary)', marginBottom: '4px' }}>Status</div>
                                    <div style={{ color: 'var(--text-primary)', fontWeight: 500 }}>API Key configured — Jira / Confluence / Compass tools available</div>
                                    {config.cloud_id && <div style={{ color: 'var(--text-tertiary)', marginTop: '4px', fontSize: '11px' }}>Cloud ID: <code>{config.cloud_id}</code></div>}
                                </div>
                            )}
                            {ch.id === 'wechat' && (
                                <div style={{ background: 'var(--bg-secondary)', borderRadius: '6px', padding: '10px', fontSize: '12px', marginBottom: '12px' }}>
                                    <div style={{ color: 'var(--text-tertiary)', marginBottom: '4px' }}>Status</div>
                                    <div style={{ color: 'var(--text-primary)', fontWeight: 500 }}>
                                        {config.extra_config?.session_expired
                                            ? 'Session expired, reconnect required'
                                            : config.is_connected
                                                ? 'WeChat iLink connected'
                                                : 'Configured, waiting for long polling'}
                                    </div>
                                    {config.app_id && <div style={{ color: 'var(--text-tertiary)', marginTop: '4px', fontSize: '11px' }}>Bot ID: <code>{config.app_id}</code></div>}
                                    {config.extra_config?.ilink_user_id && <div style={{ color: 'var(--text-tertiary)', marginTop: '4px', fontSize: '11px' }}>Linked User: <code>{config.extra_config.ilink_user_id}</code></div>}
                                </div>
                            )}
                            {ch.id === 'atlassian' && atlassianTestResult && (
                                <div style={{ padding: '8px 12px', borderRadius: '6px', fontSize: '12px', marginBottom: '10px', background: atlassianTestResult.ok ? 'rgba(16,185,129,0.08)' : 'rgba(239,68,68,0.08)', border: `1px solid ${atlassianTestResult.ok ? 'rgba(16,185,129,0.25)' : 'rgba(239,68,68,0.25)'}`, color: atlassianTestResult.ok ? 'rgb(5,150,105)' : 'rgb(220,38,38)' }}>
                                    {atlassianTestResult.ok
                                        ? `${atlassianTestResult.message || `Connected — ${atlassianTestResult.tool_count} tools available`}`
                                        : `${atlassianTestResult.error}`}
                                </div>
                            )}
                            {renderGuide({ ch, guide: ch.guide, isWs: !!(ch.connectionMode && configConnMode === 'websocket'), feishuPermMode, setFeishuPermMode, t })}
                            <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
                                {ch.hasTestConnection && ch.id === 'atlassian' && (
                                    <button className="btn btn-secondary" style={{ fontSize: '12px', padding: '4px 12px' }} onClick={() => void testAtlassian()} disabled={atlassianTesting}>
                                        {atlassianTesting ? 'Testing...' : 'Test Connection'}
                                    </button>
                                )}
                                {ch.id === 'wechat' && (
                                    <button
                                        className="btn btn-secondary"
                                        style={{ fontSize: '12px', padding: '4px 12px' }}
                                        onClick={() => {
                                            setWechatQr(null);
                                            setWechatQrStatus('');
                                            setEditing(ch.id, true);
                                        }}
                                    >
                                        Reconnect
                                    </button>
                                )}
                                {ch.id !== 'wechat' && (
                                    <button
                                        className="btn btn-secondary"
                                        style={{ fontSize: '12px', padding: '4px 12px' }}
                                        onClick={() => {
                                            const prefill: Record<string, string> = {};
                                            if (ch.id === 'feishu') {
                                                prefill.app_id = config.app_id || '';
                                                prefill.app_secret = config.app_secret || '';
                                                prefill.encrypt_key = config.encrypt_key || '';
                                                setConnectionModes(prev => ({ ...prev, feishu: config.extra_config?.connection_mode || 'websocket' }));
                                            } else if (ch.id === 'wecom') {
                                                const cm = config.extra_config?.connection_mode === 'websocket' ? 'websocket' : 'webhook';
                                                setConnectionModes(prev => ({ ...prev, wecom: cm }));
                                                if (cm === 'websocket') {
                                                    prefill.bot_id = config.extra_config?.bot_id || '';
                                                    prefill.bot_secret = config.extra_config?.bot_secret || '';
                                                } else {
                                                    prefill.corp_id = config.app_id || '';
                                                    prefill.wecom_agent_id = config.extra_config?.wecom_agent_id || '';
                                                    prefill.secret = config.app_secret || '';
                                                    prefill.token = config.verification_token || '';
                                                    prefill.encoding_aes_key = config.encrypt_key || '';
                                                }
                                            } else if (ch.id === 'slack') {
                                                prefill.bot_token = config.app_secret || '';
                                                prefill.signing_secret = config.encrypt_key || '';
                                            } else if (ch.id === 'discord') {
                                                const cm = config.extra_config?.connection_mode === 'gateway' ? 'websocket' : 'webhook';
                                                setConnectionModes(prev => ({ ...prev, discord: cm }));
                                                if (cm === 'websocket') {
                                                    prefill.bot_token = config.app_secret || '';
                                                } else {
                                                    prefill.application_id = config.app_id || '';
                                                    prefill.bot_token = config.app_secret || '';
                                                    prefill.public_key = config.encrypt_key || '';
                                                }
                                            } else if (ch.id === 'teams') {
                                                prefill.app_id = config.app_id || '';
                                                prefill.app_secret = config.app_secret || '';
                                                prefill.tenant_id = config.extra_config?.tenant_id || '';
                                            } else if (ch.id === 'dingtalk') {
                                                prefill.app_key = config.app_id || '';
                                                prefill.app_secret = config.app_secret || '';
                                                prefill.agent_id = config.extra_config?.agent_id || '';
                                                setConnectionModes(prev => ({ ...prev, dingtalk: config.extra_config?.connection_mode || 'websocket' }));
                                            } else if (ch.id === 'atlassian') {
                                                prefill.api_key = '';
                                                prefill.cloud_id = config.cloud_id || '';
                                            }
                                            setForm(prefill);
                                            setEditing(ch.id, true);
                                        }}
                                    >
                                        Edit
                                    </button>
                                )}
                                <button className="btn btn-danger" style={{ fontSize: '12px', padding: '4px 12px' }} onClick={deleteChannel}>Disconnect</button>
                            </div>
                        </div>
                    ) : (
                        <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                            {ch.id === 'wechat' ? (
                                <>
                                    {renderGuide({ ch, guide: ch.guide, isWs: false, feishuPermMode, setFeishuPermMode, t })}
                                    <div style={{ background: 'var(--bg-secondary)', borderRadius: '8px', padding: '12px', fontSize: '12px', color: 'var(--text-secondary)', lineHeight: 1.6 }}>
                                        This channel uses QR login and long polling. After scan confirmation, the bot will start polling WeChat iLink automatically.
                                    </div>
                                    {wechatQr?.qrcode_img_content ? (
                                        <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-start', gap: '10px' }}>
                                            {wechatQrImageSrc ? (
                                                <img src={wechatQrImageSrc} alt="WeChat QR" style={{ width: '220px', height: '220px', objectFit: 'contain', background: '#fff', padding: '8px', borderRadius: '8px', border: '1px solid var(--border-subtle)' }} />
                                            ) : (
                                                <div style={{ width: '220px', height: '220px', display: 'flex', alignItems: 'center', justifyContent: 'center', background: '#fff', borderRadius: '8px', border: '1px solid var(--border-subtle)', color: 'var(--text-secondary)', fontSize: '12px', padding: '8px', textAlign: 'center' }}>
                                                    Generating QR image...
                                                </div>
                                            )}
                                            <div style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>
                                                Status: <strong>{wechatQrStatus || 'wait'}</strong>
                                            </div>
                                            <div style={{ display: 'flex', gap: '8px' }}>
                                                <button className="btn btn-secondary" style={{ fontSize: '12px' }} onClick={() => void createWechatQr()} disabled={wechatLoadingQr}>
                                                    {wechatLoadingQr ? 'Refreshing...' : 'Refresh QR'}
                                                </button>
                                                {isEditing && <button className="btn btn-secondary" style={{ fontSize: '12px' }} onClick={() => setEditing(ch.id, false)}>Cancel</button>}
                                            </div>
                                        </div>
                                    ) : (
                                        <div style={{ display: 'flex', gap: '8px', marginTop: '4px' }}>
                                            <button className="btn btn-primary" style={{ fontSize: '12px', alignSelf: 'flex-start' }} onClick={() => void createWechatQr()} disabled={wechatLoadingQr}>
                                                {wechatLoadingQr ? 'Generating...' : 'Generate QR Code'}
                                            </button>
                                            {isEditing && <button className="btn btn-secondary" style={{ fontSize: '12px' }} onClick={() => setEditing(ch.id, false)}>Cancel</button>}
                                        </div>
                                    )}
                                </>
                            ) : (
                                <>
                                    {ch.connectionMode && (
                                        <div style={{ marginBottom: '8px' }}>
                                            <label style={{ fontSize: '12px', fontWeight: 500, display: 'block', marginBottom: '8px' }}>{t('wizard.step5.connectionMode')}</label>
                                            <div style={{ display: 'flex', gap: '16px', marginBottom: '8px' }}>
                                                <label style={{ fontSize: '12px', display: 'flex', alignItems: 'center', gap: '6px', cursor: 'pointer' }}>
                                                    <input type="radio" name={`${ch.id}_connection_mode`} value="websocket" checked={connMode === 'websocket'} onChange={() => setConnectionModes(prev => ({ ...prev, [ch.id]: 'websocket' }))} />
                                                    {t('wizard.step5.modeWebsocket')}
                                                </label>
                                                <label style={{ fontSize: '12px', display: 'flex', alignItems: 'center', gap: '6px', cursor: 'pointer' }}>
                                                    <input type="radio" name={`${ch.id}_connection_mode`} value="webhook" checked={connMode === 'webhook'} onChange={() => setConnectionModes(prev => ({ ...prev, [ch.id]: 'webhook' }))} />
                                                    {t('wizard.step5.modeWebhook')}
                                                </label>
                                            </div>
                                        </div>
                                    )}

                                    {renderGuide({ ch, guide: ch.guide, isWs: !!isWs, feishuPermMode, setFeishuPermMode, t })}

                                    {formFields.map(field => renderField({
                                        field,
                                        channelId: ch.id,
                                        fieldValue: form[field.key] || '',
                                        mode: 'edit',
                                        onFieldChange: val => setFormField(ch.id, field.key, val),
                                        showPwds,
                                        t,
                                        togglePwd,
                                    }))}

                                    {ch.id === 'atlassian' && (
                                        <>
                                            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '-4px' }}>
                                                Service account key starts with <code>ATSTT</code>. Personal API token: base64-encode <code>email:token</code> and prefix with <code>Basic </code>
                                            </div>
                                            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>Required for multi-site setups. Find it at <code>your-site.atlassian.net/_edge/tenant_info</code></div>
                                        </>
                                    )}

                                    <div style={{ display: 'flex', gap: '8px', marginTop: '4px' }}>
                                        <button
                                            className="btn btn-primary"
                                            style={{ fontSize: '12px', alignSelf: 'flex-start' }}
                                            onClick={() => saveChannel(buildPayload(ch, form))}
                                            disabled={!allRequired || savePending}
                                        >
                                            {savePending ? t('common.loading') : (isEditing ? 'Save Changes' : t('agent.settings.channel.saveChannel'))}
                                        </button>
                                        {isEditing && <button className="btn btn-secondary" style={{ fontSize: '12px' }} onClick={() => setEditing(ch.id, false)}>Cancel</button>}
                                    </div>
                                </>
                            )}
                        </div>
                    )}
                </div>
            )}
        </div>
    );
}
