import LinearCopyButton from '../LinearCopyButton';
import {
    EyeClosed,
    EyeOpen,
    FEISHU_PERM_BASIC_DISPLAY,
    FEISHU_PERM_BASIC_JSON,
    FEISHU_PERM_FULL_DISPLAY,
    FEISHU_PERM_FULL_JSON,
} from './registry';
import type { ChannelDef, ChannelField, GuideConfig } from './types';

type Translator = (key: string, defaultValue?: string) => string;

interface RenderGuideProps {
    ch: ChannelDef;
    guide: GuideConfig;
    isWs: boolean;
    feishuPermMode: 'basic' | 'full';
    setFeishuPermMode: (mode: 'basic' | 'full') => void;
    t: Translator;
}

export function renderGuide({
    ch,
    guide,
    isWs,
    feishuPermMode,
    setFeishuPermMode,
    t,
}: RenderGuideProps) {
    const prefix = isWs && ch.wsGuide ? `${ch.wsGuide.prefix}.ws_step` : `${guide.prefix}.step`;
    const stepCount = isWs && ch.wsGuide ? ch.wsGuide.steps : guide.steps;
    const noteKey = isWs && ch.wsGuide ? `${ch.wsGuide.prefix}.ws_note` : (guide.noteKey || `${guide.prefix}.note`);

    return (
        <details style={{ marginBottom: '8px', fontSize: '12px', color: 'var(--text-secondary)' }}>
            <summary style={{ cursor: 'pointer', fontWeight: 500, color: 'var(--text-primary)', userSelect: 'none', listStyle: 'none', display: 'flex', alignItems: 'center', gap: '6px' }}>
                <span style={{ fontSize: '10px' }}>&#9654;</span> {t('channelGuide.setupGuide')}
            </summary>
            <ol style={{ paddingLeft: '16px', margin: '8px 0', lineHeight: 1.9 }}>
                {Array.from({ length: stepCount }, (_, i) => (
                    <li key={i}>{t(`${prefix}${i + 1}`)}</li>
                ))}
            </ol>
            {ch.showPermJson && (
                <div style={{ margin: '8px 0', borderRadius: '6px', border: '1px solid var(--border-color)', overflow: 'hidden' }}>
                    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '6px 10px', background: 'var(--bg-secondary)', borderBottom: '1px solid var(--border-color)' }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '2px', background: 'var(--bg-primary)', borderRadius: '5px', padding: '2px', border: '1px solid var(--border-color)' }}>
                            {(['basic', 'full'] as const).map(mode => (
                                <button
                                    key={mode}
                                    type="button"
                                    onClick={e => {
                                        e.preventDefault();
                                        setFeishuPermMode(mode);
                                    }}
                                    style={{
                                        padding: '3px 10px',
                                        fontSize: '10px',
                                        borderRadius: '4px',
                                        cursor: 'pointer',
                                        border: 'none',
                                        transition: 'all 0.15s ease',
                                        background: feishuPermMode === mode ? 'var(--accent-primary, #5e6ad2)' : 'transparent',
                                        color: feishuPermMode === mode ? '#fff' : 'var(--text-tertiary)',
                                        fontWeight: 500,
                                    }}
                                >
                                    {t(`channelGuide.feishuPerm${mode === 'basic' ? 'Basic' : 'Full'}`)}
                                </button>
                            ))}
                        </div>
                        <LinearCopyButton
                            textToCopy={feishuPermMode === 'basic' ? FEISHU_PERM_BASIC_JSON : FEISHU_PERM_FULL_JSON}
                            label={t('channelGuide.feishuPermCopy')}
                            copiedLabel={t('channelGuide.feishuPermCopied')}
                            className=""
                            style={{ fontSize: '10px', padding: '1px 7px', cursor: 'pointer', borderRadius: '3px', border: '1px solid var(--border-color)', background: 'var(--bg-primary)', color: 'var(--text-secondary)' }}
                        />
                    </div>
                    <div style={{ padding: '4px 10px', fontSize: '10px', color: 'var(--text-tertiary)', background: 'var(--bg-secondary)', borderBottom: '1px solid var(--border-color)' }}>
                        {feishuPermMode === 'basic' ? t('channelGuide.feishuPermBasicDesc') : t('channelGuide.feishuPermFullDesc')}
                    </div>
                    <pre style={{ margin: 0, padding: '6px 10px', fontSize: '10px', fontFamily: 'var(--font-mono)', lineHeight: 1.5, background: 'var(--bg-primary)', color: 'var(--text-secondary)', overflowX: 'auto', userSelect: 'all', maxHeight: '200px', overflowY: 'auto' }}>{feishuPermMode === 'basic' ? FEISHU_PERM_BASIC_DISPLAY : FEISHU_PERM_FULL_DISPLAY}</pre>
                </div>
            )}
            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', background: 'var(--bg-secondary)', padding: '6px 10px', borderRadius: '6px' }}>
                {t(noteKey)}
            </div>
        </details>
    );
}

interface RenderFieldProps {
    channelId: string;
    field: ChannelField;
    fieldValue: string;
    mode: 'create' | 'edit';
    onFieldChange: (val: string) => void;
    showPwds: Record<string, boolean>;
    t: Translator;
    togglePwd: (fieldId: string) => void;
}

export function renderField({
    channelId,
    field,
    fieldValue,
    mode,
    onFieldChange,
    showPwds,
    t,
    togglePwd,
}: RenderFieldProps) {
    const fieldId = `${channelId}_${field.key}`;
    const isSecret = field.type === 'password';
    const labelText = field.label.startsWith('channelGuide.') ? t(field.label) : field.label;
    const placeholderText = field.placeholder?.startsWith('channelGuide.') ? t(field.placeholder) : field.placeholder;

    return (
        <div key={field.key}>
            <label style={{ fontSize: '12px', fontWeight: 500, display: 'block', marginBottom: '4px' }}>
                {labelText} {field.required && '*'}
                {!field.required && <span style={{ fontWeight: 400, color: 'var(--text-tertiary)' }}> (Optional)</span>}
            </label>
            <div style={{ position: 'relative' }}>
                <input
                    className={mode === 'edit' ? 'input' : 'form-input'}
                    type={isSecret && !showPwds[fieldId] ? 'password' : 'text'}
                    value={fieldValue}
                    onChange={e => onFieldChange(e.target.value)}
                    placeholder={placeholderText || ''}
                    style={mode === 'edit' ? { fontSize: '12px', paddingRight: isSecret ? '36px' : undefined, width: '100%' } : undefined}
                />
                {isSecret && (
                    <button
                        type="button"
                        onClick={() => togglePwd(fieldId)}
                        style={{ position: 'absolute', right: '8px', top: '50%', transform: 'translateY(-50%)', background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-tertiary)', padding: '2px', display: 'flex', alignItems: 'center' }}
                    >
                        {showPwds[fieldId] ? EyeClosed : EyeOpen}
                    </button>
                )}
            </div>
            {channelId === 'teams' && field.key === 'tenant_id' && (
                <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px' }}>{t('channelGuide.teams.tenantIdHint')}</div>
            )}
        </div>
    );
}
