import { useTranslation } from 'react-i18next';
import { IconSettings } from '@tabler/icons-react';

import { useDialog } from '../../../../components/Dialog/DialogProvider';
import LlmModelSelect from '../../../../components/LlmModelSelect';
import { useAuthStore } from '../../../../stores';
import { CATEGORY_CONFIG_SCHEMAS } from './config';

export function ToolConfigModal({
    agentId,
    scope,
    configTool,
    configCategory,
    configData,
    setConfigData,
    configJson,
    setConfigJson,
    configSaving,
    configInitialSnapshot,
    configGlobalData,
    focusedField,
    setFocusedField,
    showAdvancedToolConfig,
    setShowAdvancedToolConfig,
    saveConfig,
    setConfigTool,
    setConfigCategory,
    loadTools,
}: {
    agentId: string;
    scope: 'agent' | 'project';
    configTool: any | null;
    configCategory: string | null;
    configData: Record<string, any>;
    setConfigData: React.Dispatch<React.SetStateAction<Record<string, any>>>;
    configJson: string;
    setConfigJson: React.Dispatch<React.SetStateAction<string>>;
    configSaving: boolean;
    configInitialSnapshot: string;
    configGlobalData: Record<string, any>;
    focusedField: string | null;
    setFocusedField: React.Dispatch<React.SetStateAction<string | null>>;
    showAdvancedToolConfig: boolean;
    setShowAdvancedToolConfig: React.Dispatch<React.SetStateAction<boolean>>;
    saveConfig: () => void | Promise<void>;
    setConfigTool: React.Dispatch<React.SetStateAction<any | null>>;
    setConfigCategory: React.Dispatch<React.SetStateAction<string | null>>;
    loadTools: () => void | Promise<void>;
}) {
    const { t } = useTranslation();
    const dialog = useDialog();

    if (!configTool && !configCategory) return null;

    const target = configTool || CATEGORY_CONFIG_SCHEMAS[configCategory!];
    const fields = configTool ? (configTool.config_schema?.fields || []) : (target.fields || []);
    const title = configTool ? configTool.display_name : t(target.title);
    const isCat = !!configCategory;
    const visibleFields = fields.filter((field: any) => {
        if (!field.depends_on) return true;
        return Object.entries(field.depends_on).every(([depKey, depVals]: [string, any]) =>
            (depVals as string[]).includes(configData[depKey] ?? '')
        );
    });
    const primaryFields = visibleFields.filter((field: any) => !field.advanced);
    const advancedFields = visibleFields.filter((field: any) => field.advanced);
    const currentSnapshot = fields.length > 0 ? JSON.stringify(configData) : configJson;
    const dirty = currentSnapshot !== configInitialSnapshot;
    const missingRequiredModel = visibleFields.some((field: any) => (
        field.type === 'llm_model_picker'
        && field.required
        && !(configData[field.key] ?? field.default)
    ));

    return (
        <div style={{ position: 'fixed', top: 0, left: 0, right: 0, bottom: 0, background: 'rgba(0,0,0,0.55)', zIndex: 2000, display: 'flex', alignItems: 'center', justifyContent: 'center' }}
            onClick={() => { setConfigTool(null); setConfigCategory(null); }}>
            <div onClick={e => e.stopPropagation()} style={{ background: 'var(--bg-primary)', borderRadius: '12px', padding: '24px', width: '480px', maxWidth: '95vw', maxHeight: '80vh', overflow: 'auto', boxShadow: '0 20px 60px rgba(0,0,0,0.4)' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
                    <div>
                        <h3 style={{ margin: 0, display: 'flex', alignItems: 'center', gap: '8px' }}><IconSettings size={20} stroke={1.8} /> {title}</h3>
                        <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>{isCat ? t('agent.tools.sharedCategoryConfig') : t('agent.sessionViewer.digitalEmployee', 'Digital Employee')}</div>
                    </div>
                    <button
                        aria-label={t('agent.tools.close')}
                        title={t('agent.tools.close')}
                        onClick={() => { setConfigTool(null); setConfigCategory(null); }}
                        style={{ background: 'none', border: 'none', fontSize: '18px', cursor: 'pointer', color: 'var(--text-secondary)' }}
                    >×</button>
                </div>

                {fields.length > 0 ? (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
                        {primaryFields
                            .map((field: any) => {
                                const userFromStore = useAuthStore.getState().user;
                                const currentUserRole = userFromStore?.role;
                                const isReadOnly = field.read_only_for_roles?.includes(currentUserRole);
                                return (
                                    <div key={field.key}>
                                        <label style={{ display: 'block', fontSize: '12px', fontWeight: 500, marginBottom: '4px' }}>
                                            {t(field.label)}
                                            {isReadOnly && <span style={{ fontWeight: 400, color: 'var(--text-tertiary)', marginLeft: '4px' }}>{t('agent.tools.adminOnly')}</span>}
                                            {(() => {
                                                const globalVal = configTool?.global_config?.[field.key] ?? configGlobalData?.[field.key];
                                                if (!globalVal) return null;
                                                return (
                                                    <span style={{ fontWeight: 400, color: 'var(--accent-primary)', marginLeft: '4px', fontSize: '11px' }}>
                                                        {t('agent.tools.company', { val: `${String(globalVal).slice(0, 20)}${String(globalVal).length > 20 ? '\u2026' : ''}` })}
                                                    </span>
                                                );
                                            })()}
                                        </label>
                                        {field.type === 'checkbox' ? (
                                            <label style={{ position: 'relative', display: 'inline-block', width: '40px', height: '22px', cursor: isReadOnly ? 'not-allowed' : 'pointer' }}>
                                                <input
                                                    type="checkbox"
                                                    checked={configData[field.key] ?? field.default ?? false}
                                                    disabled={isReadOnly}
                                                    onChange={e => setConfigData(p => ({ ...p, [field.key]: e.target.checked }))}
                                                    style={{ opacity: 0, width: 0, height: 0 }}
                                                />
                                                <span style={{
                                                    position: 'absolute', inset: 0,
                                                    background: (configData[field.key] ?? field.default) ? 'var(--accent-primary)' : 'var(--bg-tertiary)',
                                                    borderRadius: '11px', transition: 'background 0.2s', opacity: isReadOnly ? 0.6 : 1,
                                                }}>
                                                    <span style={{
                                                        position: 'absolute', left: (configData[field.key] ?? field.default) ? '20px' : '2px', top: '2px',
                                                        width: '18px', height: '18px', background: '#fff',
                                                        borderRadius: '50%', transition: 'left 0.2s',
                                                    }} />
                                                </span>
                                            </label>
                                        ) : field.type === 'password' ? (
                                            <>
                                            {(() => {
                                                const globalVal = configTool?.global_config?.[field.key] ?? configGlobalData?.[field.key];
                                                const isUsingGlobal = globalVal && !configData[field.key];

                                                if (isUsingGlobal && focusedField !== field.key) {
                                                    return (
                                                        <div
                                                            className="form-input"
                                                            style={{ display: 'flex', alignItems: 'center', gap: '8px', cursor: 'text', background: 'var(--bg-tertiary)', borderColor: 'var(--border)', overflow: 'hidden' }}
                                                            onClick={() => setFocusedField(field.key)}
                                                        >
                                                            <span style={{ flex: 1, color: 'var(--text-tertiary)', fontSize: '13px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{t('agent.tools.usingCompanyKey', 'Using company key ({{val}})', { val: globalVal })}</span>
                                                            <span style={{ fontSize: '12px', color: 'var(--accent-primary)', flexShrink: 0, cursor: 'pointer' }}>{t('common.edit', 'Edit')}</span>
                                                        </div>
                                                    );
                                                }

                                                return (
                                                    <input type="password" autoComplete="new-password" className="form-input"
                                                        autoFocus={focusedField === field.key}
                                                        value={configData[field.key] ?? ''}
                                                        placeholder={globalVal ? t('agent.tools.usingCompanyKey', 'Using company key ({{val}})', { val: globalVal }) : ((field.placeholder ? t(field.placeholder) : t('admin.leaveBlankDefault', 'Leave blank to use global default')))}
                                                        onBlur={(e) => {
                                                            if (!e.target.value) setFocusedField(null);
                                                        }}
                                                        onChange={e => setConfigData(p => ({ ...p, [field.key]: e.target.value }))} />
                                                );
                                            })()}
                                            {field.key === 'auth_code' && (() => {
                                                const providerField = configTool?.config_schema?.fields?.find((f: any) => f.key === 'email_provider');
                                                const selectedProvider = configData['email_provider'] || providerField?.default || '';
                                                const providerOption = providerField?.options?.find((o: any) => o.value === selectedProvider);
                                                if (!providerOption?.help_text) return null;
                                                return (
                                                    <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px', lineHeight: '1.5' }}>
                                                        {providerOption.help_text}
                                                        {providerOption.help_url && (
                                                            <> &middot; <a href={providerOption.help_url} target="_blank" rel="noopener noreferrer" style={{ color: 'var(--accent-primary)', textDecoration: 'none' }}>{t('agent.tools.setupGuide')}</a></>
                                                        )}
                                                    </div>
                                                );
                                            })()}
                                            </>
                                        ) : field.type === 'llm_model_picker' ? (
                                            <LlmModelSelect
                                                value={configData[field.key] ?? field.default ?? ''}
                                                onChange={value => setConfigData(p => ({ ...p, [field.key]: value }))}
                                                purpose={field.purpose}
                                                placeholder={field.purpose ? t("enterprise.llm.inheritMediaDefault") : undefined}
                                                supportsVision={field.filter?.supports_vision === true}
                                                disabled={isReadOnly}
                                                required={field.required}
                                            />
                                        ) : field.type === 'select' ? (
                                            <select className="form-input" value={configData[field.key] ?? field.default ?? ''}
                                                onChange={e => setConfigData(p => ({ ...p, [field.key]: e.target.value }))}>
                                                {(field.options || []).map((o: any) => <option key={o.value} value={o.value}>{t(o.label)}</option>)}
                                            </select>
                                        ) : field.type === 'number' ? (
                                            <input type="number" className="form-input" value={configData[field.key] ?? field.default ?? ''} placeholder={field.placeholder || ''} min={field.min} max={field.max} onChange={e => setConfigData(p => ({ ...p, [field.key]: e.target.value ? Number(e.target.value) : '' }))} />
                                        ) : field.type === 'textarea' ? (
                                            <textarea
                                                className="form-input"
                                                value={configData[field.key] ?? ''}
                                                placeholder={(field.placeholder ? t(field.placeholder) : t('admin.leaveBlankDefault', 'Leave blank to use global default'))}
                                                rows={Math.max(3, Math.min(10, String(configData[field.key] ?? field.default ?? field.placeholder ?? '').split('\n').length))}
                                                style={{ minHeight: '88px', fontFamily: 'var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace)', resize: 'vertical' }}
                                                onChange={e => setConfigData(p => ({ ...p, [field.key]: e.target.value }))}
                                            />
                                        ) : (
                                            <>
                                            {(() => {
                                                const globalVal = configTool?.global_config?.[field.key] ?? configGlobalData?.[field.key];
                                                const isUsingGlobal = globalVal && !configData[field.key];

                                                if (isUsingGlobal && focusedField !== field.key) {
                                                    return (
                                                        <div
                                                            className="form-input"
                                                            style={{ display: 'flex', alignItems: 'center', gap: '8px', cursor: 'text', background: 'var(--bg-tertiary)', borderColor: 'var(--border)', overflow: 'hidden' }}
                                                            onClick={() => setFocusedField(field.key)}
                                                        >
                                                            <span style={{ flex: 1, color: 'var(--text-tertiary)', fontSize: '13px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{t('agent.tools.usingCompanyConfig', 'Using company config ({{val}})', { val: globalVal })}</span>
                                                            <span style={{ fontSize: '12px', color: 'var(--accent-primary)', flexShrink: 0, cursor: 'pointer' }}>{t('common.edit', 'Edit')}</span>
                                                        </div>
                                                    );
                                                }

                                                return (
                                                    <input type="text" className="form-input"
                                                        autoFocus={focusedField === field.key}
                                                        value={configData[field.key] ?? ''}
                                                        placeholder={globalVal ? t('agent.tools.usingCompanyConfig', 'Using company config ({{val}})', { val: globalVal }) : ((field.placeholder ? t(field.placeholder) : t('admin.leaveBlankDefault', 'Leave blank to use global default')))}
                                                        onBlur={(e) => {
                                                            if (!e.target.value) setFocusedField(null);
                                                        }}
                                                        onChange={e => setConfigData(p => ({ ...p, [field.key]: e.target.value }))} />
                                                );
                                            })()}
                                            </>
                                        )}
                                        {(field.help_text || field.help_url) && (
                                            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px', lineHeight: '1.5' }}>
                                                {field.help_text ? t(field.help_text) : ''}
                                                {field.help_url && (
                                                    <>
                                                        {field.help_text ? ' · ' : ''}
                                                        <a href={field.help_url} download target="_blank" rel="noopener noreferrer" style={{ color: 'var(--accent-primary)', textDecoration: 'none' }}>
                                                            {t(field.help_link_label || 'common.download')}
                                                        </a>
                                                    </>
                                                )}
                                            </div>
                                        )}
                                    </div>
                                );
                            })}
                        {advancedFields.length > 0 && (
                            <div style={{ borderTop: '1px solid var(--border-subtle)', paddingTop: '10px', marginTop: '2px' }}>
                                <button
                                    type="button"
                                    className="btn btn-ghost"
                                    onClick={() => setShowAdvancedToolConfig(v => !v)}
                                    style={{ padding: 0, minWidth: 'auto', fontSize: '12px', color: 'var(--text-secondary)' }}
                                >
                                    {showAdvancedToolConfig ? t('agent.tools.hideAdvancedSettings') : t('agent.tools.advancedSettings')}
                                </button>
                                {showAdvancedToolConfig && (
                                    <div style={{ display: 'flex', flexDirection: 'column', gap: '12px', marginTop: '12px' }}>
                                        {advancedFields.map((field: any) => {
                                            const userFromStore = useAuthStore.getState().user;
                                            const currentUserRole = userFromStore?.role;
                                            const isReadOnly = field.read_only_for_roles?.includes(currentUserRole);
                                            return (
                                                <div key={field.key}>
                                                    <label style={{ display: 'block', fontSize: '12px', fontWeight: 500, marginBottom: '4px' }}>
                                                        {t(field.label)}
                                                        {isReadOnly && <span style={{ fontWeight: 400, color: 'var(--text-tertiary)', marginLeft: '4px' }}>{t('agent.tools.adminOnly')}</span>}
                                                    </label>
                                                    {field.type === 'checkbox' ? (
                                                        <label style={{ position: 'relative', display: 'inline-block', width: '40px', height: '22px', cursor: isReadOnly ? 'not-allowed' : 'pointer' }}>
                                                            <input
                                                                type="checkbox"
                                                                checked={configData[field.key] ?? field.default ?? false}
                                                                disabled={isReadOnly}
                                                                onChange={e => setConfigData(p => ({ ...p, [field.key]: e.target.checked }))}
                                                                style={{ opacity: 0, width: 0, height: 0 }}
                                                            />
                                                            <span style={{ position: 'absolute', inset: 0, background: (configData[field.key] ?? field.default) ? 'var(--accent-primary)' : 'var(--bg-tertiary)', borderRadius: '11px', transition: 'background 0.2s', opacity: isReadOnly ? 0.6 : 1 }}>
                                                                <span style={{ position: 'absolute', left: (configData[field.key] ?? field.default) ? '20px' : '2px', top: '2px', width: '18px', height: '18px', background: '#fff', borderRadius: '50%', transition: 'left 0.2s' }} />
                                                            </span>
                                                        </label>
                                                    ) : field.type === 'llm_model_picker' ? (
                                                        <LlmModelSelect
                                                            value={configData[field.key] ?? field.default ?? ''}
                                                            onChange={value => setConfigData(p => ({ ...p, [field.key]: value }))}
                                                            purpose={field.purpose}
                                                placeholder={field.purpose ? t("enterprise.llm.inheritMediaDefault") : undefined}
                                                supportsVision={field.filter?.supports_vision === true}
                                                            disabled={isReadOnly}
                                                            required={field.required}
                                                        />
                                                    ) : field.type === 'select' ? (
                                                        <select className="form-input" value={configData[field.key] ?? field.default ?? ''} disabled={isReadOnly}
                                                            onChange={e => setConfigData(p => ({ ...p, [field.key]: e.target.value }))}>
                                                            {(field.options || []).map((o: any) => <option key={o.value} value={o.value}>{t(o.label)}</option>)}
                                                        </select>
                                                    ) : field.type === 'number' ? (
                                                        <input type="number" className="form-input" value={configData[field.key] ?? field.default ?? ''} disabled={isReadOnly} placeholder={field.placeholder || ''} min={field.min} max={field.max} onChange={e => setConfigData(p => ({ ...p, [field.key]: e.target.value ? Number(e.target.value) : '' }))} />
                                                    ) : field.type === 'textarea' ? (
                                                        <textarea
                                                            className="form-input"
                                                            value={configData[field.key] ?? field.default ?? ''}
                                                            disabled={isReadOnly}
                                                            placeholder={(field.placeholder ? t(field.placeholder) : t('admin.leaveBlankDefault', 'Leave blank to use global default'))}
                                                            rows={Math.max(3, Math.min(10, String(configData[field.key] ?? field.default ?? field.placeholder ?? '').split('\n').length))}
                                                            style={{ minHeight: '88px', fontFamily: 'var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace)', resize: 'vertical' }}
                                                            onChange={e => setConfigData(p => ({ ...p, [field.key]: e.target.value }))}
                                                        />
                                                    ) : (
                                                        <input type={field.type === 'password' ? 'password' : 'text'} autoComplete={field.type === 'password' ? 'new-password' : undefined} className="form-input"
                                                            value={configData[field.key] ?? field.default ?? ''}
                                                            disabled={isReadOnly}
                                                            placeholder={(field.placeholder ? t(field.placeholder) : t('admin.leaveBlankDefault', 'Leave blank to use global default'))}
                                                            onChange={e => setConfigData(p => ({ ...p, [field.key]: e.target.value }))} />
                                                    )}
                                                </div>
                                            );
                                        })}
                                    </div>
                                )}
                            </div>
                        )}
                        {configTool?.category === 'email' && (
                            <div style={{ borderTop: '1px solid var(--border-subtle)', paddingTop: '12px', display: 'flex', flexDirection: 'column', gap: '8px' }}>
                                <button
                                    className="btn btn-secondary"
                                    style={{ alignSelf: 'flex-start' }}
                                    onClick={async () => {
                                        const btn = document.getElementById('email-test-btn');
                                        const status = document.getElementById('email-test-status');
                                        if (btn) btn.textContent = t('agent.tools.testing');
                                        if (btn) (btn as HTMLButtonElement).disabled = true;
                                        try {
                                            const token = localStorage.getItem('token');
                                            const res = await fetch('/api/tools/test-email', {
                                                method: 'POST',
                                                headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
                                                body: JSON.stringify({ config: configData }),
                                            });
                                            const data = await res.json();
                                            if (status) {
                                                status.textContent = data.ok
                                                    ? `${data.imap}\n${data.smtp}`
                                                    : scope === 'project' ? t('common.error.testFailed') : `${data.imap || ''}\n${data.smtp || ''}\n${data.error || ''}`;
                                                status.style.color = data.ok ? 'var(--success)' : 'var(--error)';
                                            }
                                        } catch (e: any) {
                                            if (status) { status.textContent = scope === 'project' ? t('common.error.testFailed') : t('agent.tools.testError', { message: e.message }); status.style.color = 'var(--error)'; }
                                        } finally {
                                            if (btn) { btn.textContent = t('agent.tools.testConnection'); (btn as HTMLButtonElement).disabled = false; }
                                        }
                                    }}
                                    id="email-test-btn"
                                >{t('agent.tools.testConnection')}</button>
                                <div id="email-test-status" style={{ fontSize: '11px', whiteSpace: 'pre-line', minHeight: '16px' }}></div>
                            </div>
                        )}
                    </div>
                ) : (
                    <div>
                        <label style={{ display: 'block', fontSize: '12px', fontWeight: 500, marginBottom: '4px' }}>{t('agent.tools.configJson')}</label>
                        <textarea
                            className="form-input"
                            value={configJson}
                            onChange={e => setConfigJson(e.target.value)}
                            style={{ fontFamily: 'var(--font-mono)', fontSize: '12px', minHeight: '120px', resize: 'vertical' }}
                            placeholder='{}'
                        />
                        <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px' }}>
                            {t('agent.tools.globalDefault')} <code style={{ fontSize: '10px' }}>{JSON.stringify(configTool?.global_config || {}).slice(0, 80)}</code>
                        </div>
                    </div>
                )}

                <div style={{ display: 'flex', gap: '8px', marginTop: '16px', justifyContent: 'flex-end' }}>
                    {configTool && configTool.agent_config && Object.keys(configTool.agent_config || {}).length > 0 && (
                        <button className="btn btn-ghost" style={{ color: 'var(--error)', marginRight: 'auto' }} onClick={async () => {
                            const token = localStorage.getItem('token');
                            await fetch(`/api/tools/agents/${agentId}/tool-config/${configTool.id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` }, body: JSON.stringify({ config: {} }) });
                            setConfigTool(null); loadTools();
                        }}>{t('agent.tools.resetToGlobal')}</button>
                    )}
                    {isCat && (
                        <button
                            className="btn btn-secondary"
                            style={{ marginRight: 'auto' }}
                            onClick={async () => {
                                const btn = document.getElementById('cat-test-btn');
                                if (btn) btn.textContent = t('agent.tools.testing');
                                try {
                                    const token = localStorage.getItem('token');
                                    const res = await fetch(`/api/tools/agents/${agentId}/category-config/${configCategory}/test`, {
                                        method: 'POST',
                                        headers: { Authorization: `Bearer ${token}` }
                                    });
                                    const data = await res.json();
                                    if (data.ok) {
                                        await dialog.alert(data.message || t('common.error.testSuccess'), { type: 'success', title: t('common.model.connectivityTest') });
                                    } else {
                                        await dialog.alert(t('common.error.testFailed'), { type: 'error', title: t('common.model.connectivityTest'), details: scope === 'project' ? undefined : (typeof data.error === 'string' ? data.error : JSON.stringify(data, null, 2)) });
                                    }
                                } catch (e: any) { await dialog.alert(t('common.error.testFailed'), { type: 'error', title: t('common.model.connectivityTest'), details: scope === 'project' ? undefined : String(e?.message || e) }); }
                                finally { if (btn) btn.textContent = t('agent.tools.testConnection'); }
                            }}
                            id="cat-test-btn"
                        >{t('agent.tools.testConnection')}</button>
                    )}
                    <button className="btn btn-secondary" onClick={() => { setConfigTool(null); setConfigCategory(null); }}>{t('common.cancel')}</button>
                    <button className="btn btn-primary" onClick={saveConfig} disabled={configSaving || !dirty || missingRequiredModel}>{configSaving ? t('common.saving', 'Saving…') : t('common.save', 'Save')}</button>
                </div>
            </div>
        </div>
    );
}
