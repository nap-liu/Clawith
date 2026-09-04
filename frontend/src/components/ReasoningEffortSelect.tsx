import { useEffect, type CSSProperties } from 'react';
import { useTranslation } from 'react-i18next';

import SelectDropdown from './SelectDropdown';
import './ReasoningEffortSelect.css';

export type ReasoningEffort = 'none' | 'minimal' | 'low' | 'medium' | 'high' | 'xhigh' | 'max';
export type ReasoningEffortValue = ReasoningEffort | '';

const EFFORTS: ReasoningEffort[] = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'];

type Props = {
    value: ReasoningEffortValue;
    onChange: (value: ReasoningEffortValue) => void;
    supportedEfforts?: readonly string[];
    inheritLabel?: string;
    compact?: boolean;
    disabled?: boolean;
    style?: CSSProperties;
};

export default function ReasoningEffortSelect({
    value,
    onChange,
    supportedEfforts,
    inheritLabel,
    compact = false,
    disabled = false,
    style,
}: Props) {
    const { t } = useTranslation();
    const supported = supportedEfforts ? new Set(supportedEfforts) : null;
    useEffect(() => {
        if (value && supportedEfforts && !supportedEfforts.includes(value)) {
            onChange('');
        }
    }, [onChange, supportedEfforts, value]);
    const options = [
        {
            value: '' as const,
            label: inheritLabel || t('reasoning.inherit'),
            title: t('reasoning.inheritDescription'),
        },
        ...EFFORTS.filter((effort) => !supported || supported.has(effort)).map((effort) => ({
            value: effort,
            label: t(`reasoning.levels.${effort}`),
            title: t(`reasoning.descriptions.${effort}`),
        })),
    ];

    return (
        <SelectDropdown
            value={value}
            options={options}
            onChange={onChange}
            ariaLabel={t('reasoning.label')}
            disabled={disabled}
            className={compact ? 'reasoning-effort-select--compact' : ''}
            style={style}
        />
    );
}
