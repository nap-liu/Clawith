import { useState } from 'react';
import { useTranslation } from 'react-i18next';

import Button from '../../../components/ui/Button';
import TextInput from '../../../components/ui/TextInput';
import { mcpServersApi } from '../../../services/mcpServers';
import type { MCPServerImportPayload } from '../../../types/mcpServer';

type ParsedServer = MCPServerImportPayload & { sourceKey: string };

type Props = {
    selectedTenantId?: string | null;
    onCancel: () => void;
    onImported: () => void | Promise<void>;
    dialog: { alert: (message: string, options?: Record<string, unknown>) => Promise<unknown> };
    toast: { success: (message: string) => void };
};

const objectRecord = (value: unknown): Record<string, string> => {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
    return Object.fromEntries(
        Object.entries(value as Record<string, unknown>).map(([key, item]) => [key, String(item)]),
    );
};

const parseInput = (raw: string, tenantId?: string | null): ParsedServer[] => {
    const value = raw.trim();
    if (!value) return [];
    if (/^https?:\/\//i.test(value)) {
        return [{
            sourceKey: 'url',
            display_name: '',
            tenant_id: tenantId || undefined,
            transport: 'http',
            base_url_template: value,
        }];
    }

    const parsed = JSON.parse(value);
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        throw new Error('invalid');
    }
    const directConfig = typeof parsed.url === 'string' || typeof parsed.command === 'string';
    const source = directConfig ? { server: parsed } : (parsed.mcpServers || parsed);
    if (!source || typeof source !== 'object' || Array.isArray(source)) {
        throw new Error('invalid');
    }

    return Object.entries(source).map(([name, unknownConfig]) => {
        const config = unknownConfig as Record<string, unknown>;
        if (!config || typeof config !== 'object' || Array.isArray(config)) {
            throw new Error('invalid');
        }
        const command = typeof config.command === 'string' ? config.command.trim() : '';
        const url = typeof config.url === 'string'
            ? config.url.trim()
            : typeof config.uri === 'string' ? config.uri.trim() : '';
        if (!command && !url) throw new Error('invalid');
        return {
            sourceKey: name,
            display_name: directConfig ? '' : name,
            tenant_id: tenantId || undefined,
            transport: command && !url ? 'stdio' as const : 'http' as const,
            base_url_template: url,
            headers_template: objectRecord(config.headers),
            command_template: command || undefined,
            args_template: Array.isArray(config.args) ? config.args.map(String) : [],
            env_template: objectRecord(config.env),
        };
    });
};

export default function MCPImportPanel({
    selectedTenantId,
    onCancel,
    onImported,
    dialog,
    toast,
}: Props) {
    const { t } = useTranslation();
    const [rawInput, setRawInput] = useState('');
    const [servers, setServers] = useState<ParsedServer[]>([]);
    const [apiKey, setApiKey] = useState('');
    const [parseError, setParseError] = useState(false);
    const [importing, setImporting] = useState(false);

    const updateInput = (value: string) => {
        setRawInput(value);
        try {
            setServers(parseInput(value, selectedTenantId));
            setParseError(false);
        } catch {
            setServers([]);
            setParseError(Boolean(value.trim()));
        }
    };

    const updateName = (index: number, displayName: string) => {
        setServers((current) => current.map((server, itemIndex) => (
            itemIndex === index ? { ...server, display_name: displayName } : server
        )));
    };

    const canImport = servers.length > 0
        && servers.every((server) => server.display_name.trim())
        && !parseError
        && !importing;

    const importServers = async () => {
        if (!canImport) return;
        setImporting(true);
        let importedGroups = 0;
        let importedTools = 0;
        const errors: string[] = [];
        const failedServers: ParsedServer[] = [];
        for (const server of servers) {
            try {
                const { sourceKey: _sourceKey, ...payload } = server;
                const result = await mcpServersApi.import({
                    ...payload,
                    display_name: payload.display_name.trim(),
                    credential_template: payload.transport === 'http' && apiKey ? apiKey : undefined,
                });
                importedGroups += 1;
                importedTools += result.created;
            } catch (error: any) {
                errors.push(`${server.display_name}: ${String(error?.message || error)}`);
                failedServers.push(server);
            }
        }
        setImporting(false);
        if (importedGroups > 0) await onImported();
        if (errors.length > 0) {
            setServers(failedServers);
            await dialog.alert(
                t('enterprise.tools.mcpImportPartial', { imported: importedGroups, total: servers.length }),
                {
                    type: 'warning',
                    title: t('enterprise.tools.mcpImportFailed'),
                    details: errors.join('\n'),
                },
            );
            return;
        }
        toast.success(t('enterprise.tools.mcpImportSuccess', {
            groups: importedGroups,
            tools: importedTools,
        }));
        onCancel();
    };

    return (
        <div className="card" style={{ padding: 16, marginBottom: 16 }}>
            <h4 style={{ margin: '0 0 4px' }}>{t('enterprise.tools.mcpImportTitle')}</h4>
            <p style={{ margin: '0 0 14px', color: 'var(--text-secondary)', fontSize: 12 }}>
                {t('enterprise.tools.mcpImportHint')}
            </p>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                <div>
                    <label className="form-label">{t('enterprise.tools.mcpConfig')}</label>
                    <textarea
                        className="form-input"
                        value={rawInput}
                        onChange={(event) => updateInput(event.target.value)}
                        placeholder={t('enterprise.tools.mcpConfigPlaceholder')}
                        style={{ minHeight: 132, fontFamily: 'var(--font-mono)', fontSize: 12, resize: 'vertical' }}
                    />
                    {parseError && (
                        <div style={{ marginTop: 5, color: 'var(--danger)', fontSize: 11 }}>
                            {t('enterprise.tools.mcpConfigInvalid')}
                        </div>
                    )}
                </div>

                {servers.map((server, index) => (
                    <div key={`${server.sourceKey}-${index}`} style={{
                        display: 'grid', gridTemplateColumns: 'minmax(180px, 1fr) 2fr', gap: 12,
                        padding: 12, border: '1px solid var(--border-subtle)', borderRadius: 8,
                        background: 'var(--bg-secondary)',
                    }}>
                        <div>
                            <label className="form-label">{t('enterprise.tools.mcpGroupName')}</label>
                            <TextInput
                                value={server.display_name}
                                onChange={(event) => updateName(index, event.target.value)}
                                placeholder={t('enterprise.tools.mcpGroupNamePlaceholder')}
                                maxLength={200}
                            />
                        </div>
                        <div style={{ minWidth: 0, alignSelf: 'end', paddingBottom: 8 }}>
                            <span className="badge badge-info">{server.transport === 'stdio' ? 'stdio' : 'HTTP'}</span>
                            <span style={{ marginLeft: 8, color: 'var(--text-tertiary)', fontSize: 11 }}>
                                {server.transport === 'stdio'
                                    ? [server.command_template, ...(server.args_template || [])].filter(Boolean).join(' ')
                                    : server.base_url_template}
                            </span>
                        </div>
                    </div>
                ))}

                {servers.some((server) => server.transport === 'http') && (
                    <div>
                        <label className="form-label">{t('enterprise.tools.mcpApiKey')}</label>
                        <TextInput
                            type="password"
                            value={apiKey}
                            onChange={(event) => setApiKey(event.target.value)}
                            placeholder={t('enterprise.tools.mcpApiKeyPlaceholder')}
                            autoComplete="new-password"
                        />
                        <div style={{ marginTop: 4, color: 'var(--text-tertiary)', fontSize: 11 }}>
                            {t('enterprise.tools.mcpApiKeyHint')}
                        </div>
                    </div>
                )}

                <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
                    <Button variant="secondary" onClick={onCancel} disabled={importing}>
                        {t('common.cancel')}
                    </Button>
                    <Button variant="primary" onClick={importServers} disabled={!canImport}>
                        {importing
                            ? t('enterprise.tools.mcpImporting')
                            : t('enterprise.tools.mcpImportAction', { count: servers.length || 1 })}
                    </Button>
                </div>
            </div>
        </div>
    );
}
