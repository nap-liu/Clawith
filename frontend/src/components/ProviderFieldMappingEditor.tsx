import { useId } from 'react';

import Button from './ui/Button';
import TextInput from './ui/TextInput';

export type ProviderFieldMappingDefinition = {
    key: string;
    label: string;
    defaultPath: string;
};

export type ProviderDiscoveredField = {
    path: string;
    sample_value: string | number | boolean | null;
};

function preferredSampleValue(value: unknown): string {
    if (Array.isArray(value)) {
        const samples = value
            .slice(0, 2)
            .map(preferredSampleValue)
            .filter(Boolean);
        return `${samples.join('、')}${value.length > 2 ? ` +${value.length - 2}` : ''}`;
    }
    if (value && typeof value === 'object') {
        const record = value as Record<string, unknown>;
        for (const key of ['display', 'name', 'value', 'email', 'phone']) {
            const preferred = preferredSampleValue(record[key]);
            if (preferred) return preferred;
        }
        return JSON.stringify(value);
    }
    return value == null ? '' : String(value);
}

function fieldSampleLabel(value: ProviderDiscoveredField['sample_value'] | undefined): {
    compact: string;
    full: string;
} {
    const raw = value == null ? '' : String(value).replace(/\s+/g, ' ').trim();
    let display = raw;
    if (raw.startsWith('{') || raw.startsWith('[')) {
        try {
            display = preferredSampleValue(JSON.parse(raw));
        } catch {
            display = raw;
        }
    }
    const compact = display.length > 48 ? `${display.slice(0, 45)}...` : display;
    return { compact, full: raw };
}

export default function ProviderFieldMappingEditor({
    title,
    hint,
    fields,
    value,
    onChange,
    discoveredFields,
    onDiscover,
    discovering,
    discoverLabel,
    discoveringLabel,
    discoveryError,
    discoveryTarget,
    onDiscoveryTargetChange,
    discoveryTargetPlaceholder,
    discoveryTargetLabel,
}: {
    title: string;
    hint: string;
    fields: ProviderFieldMappingDefinition[];
    value: Record<string, string>;
    onChange: (value: Record<string, string>) => void;
    discoveredFields: ProviderDiscoveredField[];
    onDiscover: () => void;
    discovering: boolean;
    discoverLabel: string;
    discoveringLabel: string;
    discoveryError?: string;
    discoveryTarget?: string;
    onDiscoveryTargetChange?: (value: string) => void;
    discoveryTargetPlaceholder?: string;
    discoveryTargetLabel?: string;
}) {
    const optionListId = useId();
    return (
        <div className="form-group" style={{ gridColumn: '1 / -1', marginTop: '4px' }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '12px' }}>
                <label className="form-label">{title}</label>
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px', minWidth: 0 }}>
                    {onDiscoveryTargetChange && (
                        <TextInput
                            value={discoveryTarget || ''}
                            onChange={(event) => onDiscoveryTargetChange(event.target.value)}
                            placeholder={discoveryTargetPlaceholder}
                            aria-label={discoveryTargetLabel}
                            style={{ width: '240px' }}
                        />
                    )}
                    <Button
                        type="button"
                        variant="secondary"
                        className="btn-sm"
                        onClick={onDiscover}
                        disabled={discovering}
                    >
                        {discovering ? discoveringLabel : discoverLabel}
                    </Button>
                </div>
            </div>
            <div style={{ fontSize: '10px', color: 'var(--text-tertiary)', marginBottom: '10px' }}>
                {hint}
            </div>
            {discoveryError && (
                <div style={{ color: 'var(--error)', fontSize: '11px', marginBottom: '10px' }}>
                    {discoveryError}
                </div>
            )}
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px' }}>
                {fields.map((field) => {
                    const selectedPath = value[field.key] ?? field.defaultPath;
                    const sampleByPath = new Map(
                        discoveredFields.map((item) => [item.path, item.sample_value]),
                    );
                    const paths = Array.from(new Set([
                        selectedPath,
                        field.defaultPath,
                        ...discoveredFields.map((item) => item.path),
                    ])).filter(Boolean);
                    return (
                        <div key={field.key}>
                            <label className="form-label">{field.label}</label>
                            <TextInput
                                value={selectedPath}
                                onChange={(event) => onChange({
                                    ...value,
                                    [field.key]: event.target.value,
                                })}
                                list={discoveredFields.length ? `${optionListId}-${field.key}` : undefined}
                                placeholder={field.defaultPath}
                                autoComplete="off"
                                aria-label={`${title} - ${field.label}`}
                            />
                            <datalist id={`${optionListId}-${field.key}`}>
                                {paths.map((path) => {
                                    const sampleValue = sampleByPath.get(path);
                                    const sample = fieldSampleLabel(sampleValue);
                                    const label = sample.compact ? `${sample.compact} (${path})` : path;
                                    return <option key={path} value={path} label={label} />;
                                })}
                            </datalist>
                        </div>
                    );
                })}
            </div>
        </div>
    );
}
