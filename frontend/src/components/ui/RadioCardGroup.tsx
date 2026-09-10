import { useId, type ReactNode } from 'react';
import './RadioCardGroup.css';

export type RadioCardOption<T extends string> = {
    value: T;
    label: string;
    description?: string;
    icon?: ReactNode;
};

export default function RadioCardGroup<T extends string>({
    label, value, options, onChange, disabled = false, busy = false, appearance = 'cards',
}: {
    label: string;
    value: T;
    options: readonly RadioCardOption<T>[];
    onChange: (value: T) => void;
    disabled?: boolean;
    busy?: boolean;
    appearance?: 'cards' | 'segmented';
}) {
    const id = useId();
    return (
        <div className={`ui-radio-cards ui-radio-cards--${appearance}${busy ? ' is-busy' : ''}`}
            role="radiogroup" aria-label={label} aria-busy={busy}>
            {options.map(option => (
                <label key={option.value}
                    className={`ui-radio-card${value === option.value ? ' is-selected' : ''}${disabled ? ' is-disabled' : ''}`}>
                    <input type="radio" name={id} value={option.value}
                        checked={value === option.value} disabled={disabled}
                        aria-disabled={disabled || busy}
                        aria-label={option.label}
                        aria-describedby={option.description ? `${id}-${option.value}` : undefined}
                        onChange={() => { if (!busy) onChange(option.value); }} />
                    <span className="ui-radio-card__content">
                        <span className="ui-radio-card__label">
                            {option.icon && <span aria-hidden="true">{option.icon}</span>}
                            {option.label}
                        </span>
                        {option.description && <span className="ui-radio-card__description"
                            id={`${id}-${option.value}`}>{option.description}</span>}
                    </span>
                </label>
            ))}
        </div>
    );
}
