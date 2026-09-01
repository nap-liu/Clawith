import type { CSSProperties } from 'react';

type Props = {
    value: string | number;
    onChange: (value: string | number) => void;
    min?: number;
    max?: number;
    fallback: number;
    disabled?: boolean;
    className?: string;
    style?: CSSProperties;
};

export default function BlurValidatedNumberInput({
    value,
    onChange,
    min,
    max,
    fallback,
    disabled,
    className = 'input',
    style,
}: Props) {
    const normalize = (raw: string) => {
        const parsed = Number(raw);
        let next = raw.trim() !== '' && Number.isFinite(parsed) ? Math.round(parsed) : fallback;
        if (min !== undefined) next = Math.max(min, next);
        if (max !== undefined) next = Math.min(max, next);
        onChange(next);
    };

    return (
        <input
            className={className}
            type="number"
            min={min}
            max={max}
            step={1}
            value={value}
            disabled={disabled}
            onChange={(event) => onChange(event.target.value)}
            onBlur={(event) => normalize(event.target.value)}
            style={style}
        />
    );
}
